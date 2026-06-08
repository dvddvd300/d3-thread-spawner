"""Tests for the triage report + conflicts resolution paths."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner import github as gh  # noqa: E402
from d3_thread_spawner.commands import conflicts as conflicts_mod  # noqa: E402
from d3_thread_spawner.commands import triage as triage_mod  # noqa: E402
from d3_thread_spawner.models import AgentSettings, PRStatus  # noqa: E402
from d3_thread_spawner.prompts import build_conflict_resolution_prompt  # noqa: E402


def _pr(number=1, **kw):
    base = dict(number=number, title="t", branch=f"feat-{number}",
                base_branch="main", url=f"https://x/{number}")
    base.update(kw)
    return PRStatus(**base)


class CIStateDerivationTest(unittest.TestCase):
    def test_failure_wins_over_pending_and_success(self):
        rollup = [
            {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "lint", "status": "IN_PROGRESS", "conclusion": ""},
            {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        state, failing = gh._derive_ci_state(rollup)
        self.assertEqual(state, "FAILURE")
        self.assertEqual(failing, ["test"])

    def test_status_context_state_field(self):
        rollup = [{"__typename": "StatusContext", "context": "ci/circle", "state": "ERROR"}]
        state, failing = gh._derive_ci_state(rollup)
        self.assertEqual(state, "FAILURE")
        self.assertEqual(failing, ["ci/circle"])

    def test_pending_when_no_failures(self):
        rollup = [
            {"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "b", "status": "QUEUED", "conclusion": ""},
        ]
        self.assertEqual(gh._derive_ci_state(rollup), ("PENDING", []))

    def test_all_success(self):
        rollup = [{"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertEqual(gh._derive_ci_state(rollup), ("SUCCESS", []))

    def test_no_checks(self):
        self.assertEqual(gh._derive_ci_state([]), ("NONE", []))
        self.assertEqual(gh._derive_ci_state(None), ("NONE", []))


class ParsePRStatusTest(unittest.TestCase):
    def test_parse_full_object(self):
        data = {
            "number": 7, "title": "Add thing", "headRefName": "feature/thing",
            "baseRefName": "main", "url": "https://x/7",
            "author": {"login": "alice"}, "updatedAt": "U7", "isDraft": False,
            "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
            "reviewDecision": "CHANGES_REQUESTED",
            "statusCheckRollup": [{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}],
            "labels": [{"name": "bug"}, {"name": "p1"}],
            "additions": 10, "deletions": 2, "changedFiles": 3,
        }
        s = gh._parse_pr_status(data)
        self.assertEqual((s.number, s.branch, s.base_branch, s.author), (7, "feature/thing", "main", "alice"))
        self.assertTrue(s.conflicting)
        self.assertTrue(s.ci_failing)
        self.assertEqual(s.review_decision, "CHANGES_REQUESTED")
        self.assertEqual(s.labels, ["bug", "p1"])
        self.assertEqual((s.additions, s.deletions, s.changed_files), (10, 2, 3))

    def test_parse_handles_missing_optionals(self):
        s = gh._parse_pr_status({"number": 1, "headRefName": "b", "baseRefName": "main"})
        self.assertEqual(s.mergeable, "UNKNOWN")
        self.assertEqual(s.ci_state, "NONE")
        self.assertEqual(s.author, "")
        self.assertFalse(s.conflicting)


class FetchPRStatusTest(unittest.TestCase):
    def setUp(self):
        self._orig_run = gh.run

    def tearDown(self):
        gh.run = self._orig_run

    def test_list_path_parses_all(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            payload = '[{"number":1,"headRefName":"a","baseRefName":"main","mergeable":"CONFLICTING"},' \
                      '{"number":2,"headRefName":"b","baseRefName":"main","mergeable":"MERGEABLE"}]'
            return SimpleNamespace(stdout=payload)

        gh.run = fake_run
        prs = gh.fetch_prs_status("o/n")
        self.assertEqual([p.number for p in prs], [1, 2])
        self.assertEqual([p.conflicting for p in prs], [True, False])
        self.assertIn("list", captured["cmd"])
        self.assertNotIn("--author", captured["cmd"])

    def test_mine_only_adds_author(self):
        gh.run = lambda cmd, **kw: (self.assertIn("--author", cmd), SimpleNamespace(stdout="[]"))[1]
        gh.fetch_prs_status("o/n", mine_only=True)

    def test_pr_numbers_use_view(self):
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            n = cmd[cmd.index("view") + 1]
            return SimpleNamespace(stdout=f'{{"number":{n},"headRefName":"b{n}","baseRefName":"main"}}')

        gh.run = fake_run
        prs = gh.fetch_prs_status("o/n", pr_numbers=[5, 6])
        self.assertEqual([p.number for p in prs], [5, 6])
        self.assertTrue(all("view" in c for c in seen))


class RefreshUnknownTest(unittest.TestCase):
    def setUp(self):
        self._orig_run = gh.run
        self._orig_sleep = gh.time.sleep
        gh.time.sleep = lambda *_: None

    def tearDown(self):
        gh.run = self._orig_run
        gh.time.sleep = self._orig_sleep

    def test_unknown_gets_refreshed(self):
        prs = [_pr(1, mergeable="UNKNOWN"), _pr(2, mergeable="MERGEABLE")]

        def fake_run(cmd, **kw):
            n = cmd[cmd.index("view") + 1]
            return SimpleNamespace(
                stdout=f'{{"number":{n},"headRefName":"feat-{n}","baseRefName":"main","mergeable":"CONFLICTING"}}')

        gh.run = fake_run
        out = gh.refresh_unknown_mergeable("o/n", prs, attempts=1, delay=0)
        self.assertEqual([p.number for p in out], [1, 2])      # order preserved
        self.assertTrue(out[0].conflicting)                     # refreshed
        self.assertEqual(out[1].mergeable, "MERGEABLE")         # untouched

    def test_refresh_failure_keeps_stale_value(self):
        import subprocess
        prs = [_pr(1, mergeable="UNKNOWN")]

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, "gh", stderr="boom")

        gh.run = boom
        out = gh.refresh_unknown_mergeable("o/n", prs, attempts=2, delay=0)
        self.assertEqual(out[0].mergeable, "UNKNOWN")           # not crashed, kept


class TriageCategoryTest(unittest.TestCase):
    def test_priority_ordering(self):
        cat = triage_mod.triage_category
        self.assertEqual(cat(_pr(is_draft=True, mergeable="CONFLICTING")), "draft")
        self.assertEqual(cat(_pr(mergeable="CONFLICTING", ci_state="FAILURE")), "conflicts")
        self.assertEqual(cat(_pr(mergeable="MERGEABLE", ci_state="FAILURE")), "ci_failing")
        self.assertEqual(cat(_pr(mergeable="MERGEABLE", review_decision="CHANGES_REQUESTED")), "changes_requested")
        self.assertEqual(cat(_pr(mergeable="MERGEABLE", merge_state="BEHIND")), "behind")
        self.assertEqual(cat(_pr(mergeable="MERGEABLE", ci_state="PENDING")), "ci_pending")
        self.assertEqual(cat(_pr(mergeable="MERGEABLE", review_decision="REVIEW_REQUIRED")), "review_required")
        self.assertEqual(cat(_pr(mergeable="MERGEABLE", ci_state="SUCCESS", review_decision="APPROVED")), "ready")
        self.assertEqual(cat(_pr(mergeable="MERGEABLE", ci_state="NONE", review_decision="")), "ready")


class ConflictPromptTest(unittest.TestCase):
    def test_merge_prompt(self):
        p = _pr(58, branch="feature/x", base_branch="develop")
        out = build_conflict_resolution_prompt(p, "merge")
        self.assertIn("git merge --no-edit origin/develop", out)
        self.assertIn("git push origin feature/x", out)
        self.assertNotIn("force-with-lease", out)
        self.assertIn("#58", out)

    def test_rebase_prompt(self):
        p = _pr(58, branch="feature/x", base_branch="develop")
        out = build_conflict_resolution_prompt(p, "rebase")
        self.assertIn("git rebase origin/develop", out)
        self.assertIn("--force-with-lease origin feature/x", out)

    def test_default_is_merge(self):
        self.assertIn("git merge", build_conflict_resolution_prompt(_pr(), "merge"))
        self.assertIn("git merge", build_conflict_resolution_prompt(_pr()))


class BuildConflictItemsTest(unittest.TestCase):
    def test_one_item_per_pr_on_its_branch(self):
        prs = [_pr(58, branch="feature/auth-fix"), _pr(61, branch="bugfix/timeout")]
        items = conflicts_mod.build_conflict_items(prs, AgentSettings(), "merge")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].branch, "feature/auth-fix")
        self.assertFalse(items[0].create_branch)         # check out existing branch
        self.assertIsNone(items[0].worktree_from)
        self.assertIn("58", items[0].name)
        self.assertIn("git merge", items[0].prompt)


class ConflictingOpenTest(unittest.TestCase):
    def test_partitions_by_state(self):
        prs = [_pr(1, mergeable="CONFLICTING", state="OPEN"),
               _pr(2, mergeable="MERGEABLE", state="OPEN"),
               _pr(3, mergeable="CONFLICTING", state="CLOSED")]
        open_c, closed_c = conflicts_mod.conflicting_open(prs)
        self.assertEqual([p.number for p in open_c], [1])
        self.assertEqual([p.number for p in closed_c], [3])


class StrategyResolutionTest(unittest.TestCase):
    def test_flags_and_config_default(self):
        rs = conflicts_mod.resolve_strategy
        s_merge = AgentSettings(conflict_strategy="merge")
        s_rebase = AgentSettings(conflict_strategy="rebase")
        self.assertEqual(rs(SimpleNamespace(rebase=True, merge=False), s_merge), "rebase")
        self.assertEqual(rs(SimpleNamespace(rebase=False, merge=True), s_rebase), "merge")
        self.assertEqual(rs(SimpleNamespace(rebase=False, merge=False), s_rebase), "rebase")  # config default
        self.assertEqual(rs(SimpleNamespace(rebase=False, merge=False), s_merge), "merge")


class CmdConflictsTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(conflicts_mod, k) for k in
                       ("fetch_prs_status", "refresh_unknown_mergeable", "launch_batch")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(conflicts_mod, k, v)

    def test_only_conflicting_prs_launched(self):
        prs = [_pr(1, mergeable="CONFLICTING"), _pr(2, mergeable="MERGEABLE"),
               _pr(3, mergeable="CONFLICTING")]
        conflicts_mod.fetch_prs_status = lambda *a, **k: prs
        conflicts_mod.refresh_unknown_mergeable = lambda repo, p, **k: p
        launched = {}
        conflicts_mod.launch_batch = lambda items, settings: launched.update(
            n=len(items), branches=[i.branch for i in items])

        args = SimpleNamespace(pr_numbers=[], mine=False, merge=False, rebase=False)
        settings = AgentSettings(github_repo="o/n", dry_run=True, conflict_strategy="merge")
        buf = io.StringIO()
        with redirect_stdout(buf):
            conflicts_mod.cmd_conflicts(args, settings)
        self.assertEqual(launched.get("n"), 2)
        self.assertEqual(launched.get("branches"), ["feat-1", "feat-3"])

    def test_closed_conflicting_pr_is_skipped(self):
        prs = [_pr(1, mergeable="CONFLICTING", state="OPEN"),
               _pr(2, mergeable="CONFLICTING", state="CLOSED"),
               _pr(3, mergeable="CONFLICTING", state="MERGED")]
        conflicts_mod.fetch_prs_status = lambda *a, **k: prs
        conflicts_mod.refresh_unknown_mergeable = lambda repo, p, **k: p
        launched = {}
        conflicts_mod.launch_batch = lambda items, settings: launched.update(
            branches=[i.branch for i in items])

        args = SimpleNamespace(pr_numbers=[1, 2, 3], mine=False, merge=False, rebase=False)
        settings = AgentSettings(github_repo="o/n", dry_run=True, conflict_strategy="merge")
        buf = io.StringIO()
        with redirect_stdout(buf):
            conflicts_mod.cmd_conflicts(args, settings)
        out = buf.getvalue()
        self.assertEqual(launched.get("branches"), ["feat-1"])     # only the open one
        self.assertIn("closed/merged", out)
        self.assertIn("#2", out)
        self.assertIn("#3", out)

    def test_no_conflicts_does_not_launch(self):
        conflicts_mod.fetch_prs_status = lambda *a, **k: [_pr(1, mergeable="MERGEABLE")]
        conflicts_mod.refresh_unknown_mergeable = lambda repo, p, **k: p
        called = {"launched": False}
        conflicts_mod.launch_batch = lambda *a, **k: called.update(launched=True)
        args = SimpleNamespace(pr_numbers=[], mine=False, merge=False, rebase=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            conflicts_mod.cmd_conflicts(args, AgentSettings(github_repo="o/n", dry_run=True))
        self.assertFalse(called["launched"])
        self.assertIn("No open conflicting PRs", buf.getvalue())


class CmdTriageTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(triage_mod, k) for k in
                       ("fetch_prs_status", "refresh_unknown_mergeable", "launch_conflict_resolution")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(triage_mod, k, v)

    def _run(self, args, prs):
        triage_mod.fetch_prs_status = lambda *a, **k: prs
        triage_mod.refresh_unknown_mergeable = lambda repo, p, **k: p
        self.launched = []
        triage_mod.launch_conflict_resolution = lambda prs_, s, strat: self.launched.append((len(prs_), strat))
        buf = io.StringIO()
        with redirect_stdout(buf):
            triage_mod.cmd_triage(args, AgentSettings(github_repo="o/n", dry_run=True, conflict_strategy="merge"))
        return buf.getvalue()

    def test_report_groups_and_no_launch_by_default(self):
        prs = [_pr(1, mergeable="CONFLICTING"), _pr(2, mergeable="MERGEABLE", ci_state="FAILURE"),
               _pr(3, mergeable="MERGEABLE", ci_state="SUCCESS", review_decision="APPROVED")]
        args = SimpleNamespace(pr_numbers=[], mine=False, resolve_conflicts=False, merge=False, rebase=False)
        out = self._run(args, prs)
        self.assertIn("CONFLICTS (1)", out)
        self.assertIn("CI FAILING (1)", out)
        self.assertIn("READY TO MERGE (1)", out)
        self.assertEqual(self.launched, [])

    def test_resolve_conflicts_launches_only_conflicting(self):
        prs = [_pr(1, mergeable="CONFLICTING"), _pr(2, mergeable="MERGEABLE")]
        args = SimpleNamespace(pr_numbers=[], mine=False, resolve_conflicts=True, merge=False, rebase=True)
        self._run(args, prs)
        self.assertEqual(self.launched, [(1, "rebase")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
