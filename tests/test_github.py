"""Tests for the optimized PR rate-limit / batching / cache paths in github.py."""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner import cache as cache_mod  # noqa: E402
from d3_thread_spawner import github as gh  # noqa: E402
from d3_thread_spawner.models import AgentSettings  # noqa: E402


def _settings(cache_dir):
    return AgentSettings(github_repo="o/n", cache=True, cache_dir=cache_dir,
                         wait=False, wait_max_seconds=300)


class FallbackRegressionTest(unittest.TestCase):
    """The central bug: a GraphQL rate-limit must fall back to REST, not abort."""

    def setUp(self):
        gh.set_graphql_disabled(False)
        self._orig_g = gh._fetch_pr_threads_graphql
        self._orig_r = gh._fetch_pr_threads_rest

    def tearDown(self):
        gh._fetch_pr_threads_graphql = self._orig_g
        gh._fetch_pr_threads_rest = self._orig_r
        gh.set_graphql_disabled(False)

    def test_graphql_rate_limit_falls_back_to_rest(self):
        def boom(*a, **k):
            raise gh.GitHubRateLimitError("18:30 UTC (~2m) [0/5000 remaining]")

        called = {"rest": False}

        def fake_rest(owner, name, pr_number, *, meta_hint=None):
            called["rest"] = True
            return ({"title": "t", "headRefName": "h", "baseRefName": "main",
                     "url": "u", "updatedAt": "X"},
                    [{"id": "1", "isResolved": False, "isOutdated": False,
                      "path": "f.py", "line": 1,
                      "comments": {"nodes": [{"author": {"login": "coderabbitai"},
                                              "body": "b"}]}}])

        gh._fetch_pr_threads_graphql = boom
        gh._fetch_pr_threads_rest = fake_rest

        meta, threads = gh.fetch_pr_threads("o", "n", 5)
        self.assertTrue(called["rest"], "REST fallback should fire on GraphQL rate-limit")
        self.assertEqual(len(threads), 1)
        self.assertTrue(gh.graphql_disabled(), "latch should be set after fallback")


class CacheRoundTripTest(unittest.TestCase):
    def test_put_get_and_invalidation(self):
        with tempfile.TemporaryDirectory() as d:
            scope = cache_mod.scope_key(False, False, "coderabbitai")
            meta = {"title": "t", "headRefName": "h", "baseRefName": "main",
                    "url": "u", "updatedAt": "2026-06-04T00:00:00Z"}
            raw = [{"id": "1", "isResolved": False, "isOutdated": False,
                    "path": "f.py", "line": 2,
                    "comments": {"nodes": [{"author": {"login": "coderabbitai"},
                                            "body": "x"}]}}]
            cache_mod.put_cached(d, "o/n", 7, scope, meta["updatedAt"], meta, raw, "graphql")

            hit = cache_mod.get_cached(d, "o/n", 7, scope, "2026-06-04T00:00:00Z")
            self.assertIsNotNone(hit)
            gmeta, graw, src = hit
            self.assertEqual(gmeta, meta)
            self.assertEqual(graw, raw)
            self.assertEqual(src, "graphql")

            # Different updatedAt -> miss
            self.assertIsNone(cache_mod.get_cached(d, "o/n", 7, scope, "2026-06-05T00:00:00Z"))
            # Different scope -> miss
            other = cache_mod.scope_key(True, False, "coderabbitai")
            self.assertIsNone(cache_mod.get_cached(d, "o/n", 7, other, "2026-06-04T00:00:00Z"))
            # Unknown updatedAt -> miss (can't validate freshness)
            self.assertIsNone(cache_mod.get_cached(d, "o/n", 7, scope, None))


class BatchedFetchTest(unittest.TestCase):
    """Skeleton+bodies batching, reviewer-gated selection, and cache integration."""

    def setUp(self):
        gh.set_graphql_disabled(False)
        self._orig = gh.gh_graphql
        self.queries = []
        gh.gh_graphql = self._fake_graphql

    def tearDown(self):
        gh.gh_graphql = self._orig
        gh.set_graphql_disabled(False)

    def _fake_graphql(self, query, *, wait=False, wait_max_seconds=0):
        self.queries.append(query)
        # Skeleton pass: aliases like `pr0: pullRequest(number: 314)`
        sk = re.findall(r"(pr\d+): pullRequest\(number: (\d+)\)", query)
        if sk:
            repo = {}
            for alias, num in sk:
                num = int(num)
                repo[alias] = {
                    "number": num,
                    "title": f"PR {num}",
                    "headRefName": f"branch-{num}",
                    "baseRefName": "main",
                    "url": f"https://x/{num}",
                    "updatedAt": f"U{num}",
                    "reviewThreads": {
                        "totalCount": 2,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {"id": f"T{num}-cr", "isResolved": False, "isOutdated": False,
                             "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}},
                            {"id": f"T{num}-hu", "isResolved": True, "isOutdated": False,
                             "comments": {"nodes": [{"author": {"login": "human"}}]}},
                        ],
                    },
                }
            return {"data": {"repository": repo}}

        # Bodies pass: aliases like `t0: node(id: "T314-cr")`
        bo = re.findall(r'(t\d+): node\(id: "([^"]+)"\)', query)
        if bo:
            out = {}
            for alias, tid in bo:
                out[alias] = {
                    "path": "f.py", "line": 3,
                    "comments": {"nodes": [{"author": {"login": "coderabbitai"},
                                            "body": "please fix"}]},
                }
            return {"data": out}

        raise AssertionError(f"unexpected query: {query[:120]}")

    def test_reviewer_selection_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            settings = _settings(d)
            hints = {314: {"number": 314, "updatedAt": "U314"},
                     313: {"number": 313, "updatedAt": "U313"}}
            infos, skipped = gh.fetch_prs_info(
                "o", "n", [314, 313],
                include_resolved=False, include_outdated=False,
                reviewer="coderabbitai", settings=settings, pr_hints=hints,
            )
            self.assertEqual(skipped, [])
            self.assertEqual(set(infos), {314, 313})
            # Only the coderabbit, unresolved thread survives selection.
            for n in (314, 313):
                self.assertEqual(len(infos[n].threads), 1)
                self.assertEqual(infos[n].threads[0].reviewer, "coderabbitai")

            # Bodies pass should have requested ONLY the coderabbit thread ids.
            body_queries = [q for q in self.queries if "node(id:" in q]
            joined = " ".join(body_queries)
            self.assertIn("T314-cr", joined)
            self.assertNotIn("T314-hu", joined, "resolved/non-reviewer thread must not be body-fetched")

            # Second run with same hints -> served from cache, no new gh_graphql calls.
            before = len(self.queries)
            infos2, _ = gh.fetch_prs_info(
                "o", "n", [314, 313],
                include_resolved=False, include_outdated=False,
                reviewer="coderabbitai", settings=settings, pr_hints=hints,
            )
            self.assertEqual(len(self.queries), before, "cache hit should issue no GraphQL")
            self.assertEqual(set(infos2), {314, 313})

    def test_alias_number_mapping_across_batches(self):
        # 7 PRs > skeleton batch of 5 -> 2 skeleton requests; mapping must stay correct.
        with tempfile.TemporaryDirectory() as d:
            settings = _settings(d)
            nums = [100, 101, 102, 103, 104, 105, 106]
            infos, _ = gh.fetch_prs_info(
                "o", "n", nums,
                include_resolved=False, include_outdated=False,
                reviewer="coderabbitai", settings=settings, pr_hints={},
            )
            self.assertEqual(set(infos), set(nums))
            for n in nums:
                self.assertEqual(infos[n].branch, f"branch-{n}")


class BudgetPreflightTest(unittest.TestCase):
    def setUp(self):
        self._orig = gh._graphql_rate_limit

    def tearDown(self):
        gh._graphql_rate_limit = self._orig

    def test_budget_low_true_false_and_unknown(self):
        gh._graphql_rate_limit = lambda: {"remaining": 10, "limit": 5000, "reset_dt": None}
        self.assertTrue(gh.graphql_budget_low(200))
        gh._graphql_rate_limit = lambda: {"remaining": 4000, "limit": 5000, "reset_dt": None}
        self.assertFalse(gh.graphql_budget_low(200))
        gh._graphql_rate_limit = lambda: None  # unreadable -> don't disable
        self.assertFalse(gh.graphql_budget_low(200))


class ReviewerMatchTest(unittest.TestCase):
    def test_normalization_and_no_false_positives(self):
        self.assertTrue(gh.reviewer_matches("coderabbitai", "coderabbitai"))
        self.assertTrue(gh.reviewer_matches("coderabbitai[bot]", "coderabbitai"))   # [bot] on login
        self.assertTrue(gh.reviewer_matches("coderabbitai", "coderabbitai[bot]"))   # [bot] on query
        self.assertTrue(gh.reviewer_matches("CodeRabbitAI", "coderabbitai"))        # case-insensitive
        self.assertTrue(gh.reviewer_matches("anyone", None))                         # no filter
        self.assertFalse(gh.reviewer_matches("dependabot", "bot"))                   # no substring fp
        self.assertFalse(gh.reviewer_matches("coderabbitai", "coderabbit"))          # partial != exact
        self.assertFalse(gh.reviewer_matches(None, "coderabbitai"))


class BatchErrorHandlingTest(unittest.TestCase):
    """Non-rate-limit gh failures must degrade to REST, not crash (review blocker)."""

    def setUp(self):
        gh.set_graphql_disabled(False)
        self._sk = gh._batch_skeletons
        self._rest = gh._fetch_pr_threads_rest

    def tearDown(self):
        gh._batch_skeletons = self._sk
        gh._fetch_pr_threads_rest = self._rest
        gh.set_graphql_disabled(False)

    def _rest_ok(self, owner, name, n, *, meta_hint=None):
        return ({"title": "t", "headRefName": "h", "baseRefName": "main",
                 "url": "u", "updatedAt": "X"},
                [{"id": "1", "isResolved": False, "isOutdated": False,
                  "path": "f.py", "line": 1,
                  "comments": {"nodes": [{"author": {"login": "coderabbitai"}, "body": "b"}]}}])

    def test_non_ratelimit_graphql_error_falls_back_to_rest(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            def boom(*a, **k):
                raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 401: Bad credentials")
            gh._batch_skeletons = boom
            gh._fetch_pr_threads_rest = self._rest_ok
            infos, skipped = gh.fetch_prs_info(
                "o", "n", [5], reviewer="coderabbitai", settings=_settings(d), pr_hints={})
            self.assertEqual(skipped, [])
            self.assertIn(5, infos)
            self.assertTrue(gh.graphql_disabled())

    def test_non_ratelimit_rest_error_marks_skipped(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            gh.set_graphql_disabled(True)  # force REST path
            def boom(*a, **k):
                raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 404: Not Found")
            gh._fetch_pr_threads_rest = boom
            infos, skipped = gh.fetch_prs_info(
                "o", "n", [5], reviewer="coderabbitai", settings=_settings(d), pr_hints={})
            self.assertNotIn(5, infos)
            self.assertEqual(skipped, [5], "non-rate-limit REST failure must be surfaced, not dropped")


class PartialResultsTest(unittest.TestCase):
    """pr.cmd_pr must salvage already-fetched PRs and print a resume command."""

    def setUp(self):
        from d3_thread_spawner.commands import pr as prmod
        self.prmod = prmod
        self._saved = {k: getattr(prmod, k) for k in
                       ("fetch_open_prs", "fetch_prs_info", "graphql_budget_low",
                        "set_graphql_disabled", "launch_batch")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(self.prmod, k, v)

    def test_partial_salvage_and_resume_command(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from d3_thread_spawner.models import AgentSettings, PRInfo, ReviewComment, ReviewThread

        prmod = self.prmod
        thread = ReviewThread(
            thread_id="T1", path="a.py", line=1, is_resolved=False, is_outdated=False,
            comments=[ReviewComment(author="coderabbitai", body="fix this")],
        )
        pr1 = PRInfo(number=10, title="t", branch="b", base_branch="main", url="u",
                     threads=[thread])

        prmod.graphql_budget_low = lambda *a, **k: False
        prmod.set_graphql_disabled = lambda *a, **k: None
        prmod.fetch_open_prs = lambda repo, mine_only=False: [
            {"number": 10, "title": "t", "headRefName": "b", "baseRefName": "main",
             "author": {"login": "me"}, "updatedAt": "U10"},
            {"number": 11, "title": "t2", "headRefName": "b2", "baseRefName": "main",
             "author": {"login": "me"}, "updatedAt": "U11"},
        ]
        # PR 10 fetched, PR 11 skipped (rate limit) -> partial salvage.
        prmod.fetch_prs_info = lambda *a, **k: ({10: pr1}, [11])
        launched = {}
        prmod.launch_batch = lambda items, settings: launched.update(n=len(items))

        args = SimpleNamespace(
            pr_numbers=[], open=True, mine=True, reviewer="coderabbitai",
            no_cache=False, include_resolved=False, include_outdated=False, per_thread=False,
        )
        settings = AgentSettings(github_repo="o/n", dry_run=True)

        buf = io.StringIO()
        with redirect_stdout(buf):
            prmod.cmd_pr(args, settings)
        out = buf.getvalue()

        self.assertIn("not fetched", out)
        self.assertIn("#11", out)
        self.assertIn("d3-spawn pr 11", out)      # resume command for skipped PR
        self.assertIn("--reviewer coderabbitai", out)
        self.assertIn("--wait", out)
        self.assertEqual(launched.get("n"), 1, "the one fetched PR should still launch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
