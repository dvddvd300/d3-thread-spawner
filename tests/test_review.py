"""Tests for the local-reviewer (`review`) command + its prompt builders."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner.commands import review as review_mod  # noqa: E402
from d3_thread_spawner.models import AgentSettings, PRStatus  # noqa: E402
from d3_thread_spawner.prompts import (  # noqa: E402
    build_pr_local_review_prompt,
    load_review_guide,
)


def _pr(number=1, **kw):
    base = dict(number=number, title="t", branch=f"feat-{number}",
                base_branch="main", url=f"https://x/{number}")
    base.update(kw)
    return PRStatus(**base)


class LoadReviewGuideTest(unittest.TestCase):
    def test_bundled_guide_loads_and_is_sanitized(self):
        guide = load_review_guide()
        self.assertGreater(len(guide), 5000)
        # No project-identifying or secret tokens / domain hints leaked into the
        # bundled guide.
        for token in ("Puul", "puul", "luna", "atlassian", "edf37f98", "PUUL-",
                      "bilingual", "Hot Bug", "week-overview", "Section 2I"):
            self.assertNotIn(token, guide, f"leaked token: {token}")
        # Still a real reviewer guide.
        self.assertIn("N+1", guide)
        self.assertIn("OUTPUT FORMAT", guide)

    def test_missing_custom_path_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            load_review_guide("/no/such/review-prompt.md")
        self.assertIn("not found", str(ctx.exception))

    def test_custom_path_is_used(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("MY CUSTOM REVIEW GUIDE")
            path = f.name
        try:
            self.assertEqual(load_review_guide(path), "MY CUSTOM REVIEW GUIDE")
        finally:
            os.unlink(path)


class BuildLocalReviewPromptTest(unittest.TestCase):
    def test_pr_context_and_base_substitution(self):
        pr = _pr(58, title="Fix auth timeout", branch="feature/auth", base_branch="develop",
                 url="https://github.com/o/n/pull/58")
        out = build_pr_local_review_prompt(pr, "GUIDE-BODY")
        self.assertIn("#58", out)
        self.assertIn("Fix auth timeout", out)
        self.assertIn("feature/auth", out)
        self.assertIn("develop", out)
        self.assertIn("https://github.com/o/n/pull/58", out)
        # Concrete fetch command uses the real branch names.
        self.assertIn("git fetch origin develop feature/auth", out)
        # Read-only contract is explicit.
        self.assertIn("READ-ONLY", out)
        self.assertIn("do NOT", out.replace("Do NOT", "do NOT"))
        # Header scopes the single-PR / recommend-don't-act framing so the
        # bundled multi-PR methodology can't push the reviewer into acting.
        self.assertIn("SINGLE PR", out)
        self.assertIn("RECOMMEND, DON'T ACT", out)
        # Guide is appended verbatim.
        self.assertTrue(out.rstrip().endswith("GUIDE-BODY"))

    def test_guide_braces_are_not_interpolated(self):
        # The methodology contains code samples with { } — they must survive
        # untouched (the builder appends the guide, it does not .format() it).
        pr = _pr(1)
        guide = "example: for (const x of items) { await repo.findOne(x.id) }"
        out = build_pr_local_review_prompt(pr, guide)
        self.assertIn("{ await repo.findOne(x.id) }", out)


class BuildReviewItemsTest(unittest.TestCase):
    def test_one_readonly_item_per_pr_on_its_branch(self):
        prs = [_pr(58, branch="feature/auth-fix"), _pr(61, branch="bugfix/timeout")]
        items = review_mod.build_review_items(prs, AgentSettings(), "GUIDE")
        self.assertEqual([i.name for i in items], ["review-58-auth-fix", "review-61-timeout"])
        self.assertEqual([i.branch for i in items], ["feature/auth-fix", "bugfix/timeout"])
        for i in items:
            self.assertFalse(i.create_branch)      # check out existing branch
            self.assertIsNone(i.worktree_from)
        self.assertIn("#58", items[0].prompt)
        self.assertIn("GUIDE", items[0].prompt)

    def test_name_falls_back_to_number_for_empty_slug(self):
        items = review_mod.build_review_items([_pr(7, branch="---")], AgentSettings(), "G")
        self.assertEqual(items[0].name, "review-7-7")


class CmdReviewTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(review_mod, k) for k in
                       ("fetch_prs_status", "launch_batch")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(review_mod, k, v)

    def _run(self, args, prs=None, settings=None, fetch=None):
        review_mod.fetch_prs_status = fetch or (lambda *a, **k: prs)
        self.launched = []
        review_mod.launch_batch = lambda items, s: self.launched.append([i.name for i in items])
        settings = settings or AgentSettings(github_repo="o/n", dry_run=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            review_mod.cmd_review(args, settings)
        return buf.getvalue()

    def test_no_repo_errors_without_launching(self):
        args = SimpleNamespace(pr_numbers=[58], open=False, mine=False)
        out = self._run(args, prs=[_pr(58)], settings=AgentSettings(github_repo="", dry_run=True))
        self.assertEqual(self.launched, [])
        self.assertIn("repo not detected", out)

    def test_requires_numbers_or_open(self):
        args = SimpleNamespace(pr_numbers=[], open=False, mine=False)
        out = self._run(args, prs=[])
        self.assertEqual(self.launched, [])
        self.assertIn("Specify PR numbers or use --open", out)

    def test_open_launches_one_thread_per_pr(self):
        prs = [_pr(58, branch="feature/auth"), _pr(61, branch="bugfix/x")]
        args = SimpleNamespace(pr_numbers=[], open=True, mine=True)
        self._run(args, prs=prs)
        self.assertEqual(self.launched, [["review-58-auth", "review-61-x"]])

    def test_named_pr_numbers_are_reviewed(self):
        args = SimpleNamespace(pr_numbers=[58], open=False, mine=False)
        self._run(args, prs=[_pr(58, branch="feature/auth")])
        self.assertEqual(self.launched, [["review-58-auth"]])

    def test_closed_pr_warns_but_still_reviews(self):
        prs = [_pr(58, branch="feature/auth", state="MERGED")]
        args = SimpleNamespace(pr_numbers=[58], open=False, mine=False)
        out = self._run(args, prs=prs)
        self.assertIn("closed/merged", out)
        self.assertEqual(self.launched, [["review-58-auth"]])

    def test_no_prs_does_not_launch(self):
        args = SimpleNamespace(pr_numbers=[], open=True, mine=False)
        out = self._run(args, prs=[])
        self.assertEqual(self.launched, [])
        self.assertIn("No PRs to review", out)

    def test_deleted_head_branch_is_skipped(self):
        prs = [_pr(58, branch="feature/auth"), _pr(99, branch="", state="MERGED")]
        args = SimpleNamespace(pr_numbers=[58, 99], open=False, mine=False)
        out = self._run(args, prs=prs)
        self.assertIn("no head branch", out)
        self.assertIn("#99", out)
        self.assertEqual(self.launched, [["review-58-auth"]])   # only the one with a branch

    def test_all_branches_deleted_does_not_launch(self):
        args = SimpleNamespace(pr_numbers=[99], open=False, mine=False)
        out = self._run(args, prs=[_pr(99, branch="", state="MERGED")])
        self.assertEqual(self.launched, [])
        self.assertIn("deleted head branches", out)

    def test_bad_custom_prompt_path_errors_before_fetch(self):
        called = {"fetched": False}

        def fetch(*a, **k):
            called["fetched"] = True
            return [_pr(58)]

        args = SimpleNamespace(pr_numbers=[58], open=False, mine=False)
        settings = AgentSettings(github_repo="o/n", dry_run=True,
                                 review_prompt_file="/no/such/file.md")
        out = self._run(args, settings=settings, fetch=fetch)
        self.assertFalse(called["fetched"])       # failed before the GitHub call
        self.assertEqual(self.launched, [])
        self.assertIn("not found", out)

    def test_rate_limit_is_handled_gracefully(self):
        from d3_thread_spawner.github import GitHubRateLimitError

        def boom(*a, **k):
            raise GitHubRateLimitError("limit hit")

        args = SimpleNamespace(pr_numbers=[], open=True, mine=False)
        out = self._run(args, fetch=boom)
        self.assertEqual(self.launched, [])
        self.assertIn("limit hit", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
