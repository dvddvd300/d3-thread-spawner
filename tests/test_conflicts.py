"""Tests for the conflict-resolution shared-branch safety guard.

Rebasing + force-pushing a shared/long-lived branch (dev, main, release/*) rewrites
history every dependent PR and clone relies on. These tests pin the guard that
auto-downgrades --rebase to merge for such branches (and the explicit override).
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner.commands import conflicts as conflicts_mod  # noqa: E402
from d3_thread_spawner import prompts as prompts_mod  # noqa: E402
from d3_thread_spawner.models import AgentSettings, PRStatus  # noqa: E402


def _pr(number=1, branch="feature/x", base_branch="main", **kw):
    base = dict(number=number, title="t", branch=branch,
                base_branch=base_branch, url=f"https://x/{number}")
    base.update(kw)
    return PRStatus(**base)


class IsProtectedBranchTest(unittest.TestCase):
    def setUp(self):
        self.s = AgentSettings()

    def test_exact_shared_names_are_protected(self):
        for b in ("dev", "main", "master", "develop", "DEV", "Main", "trunk", "next"):
            self.assertTrue(self.s.is_protected_branch(b), b)

    def test_first_path_segment_match(self):
        self.assertTrue(self.s.is_protected_branch("release/2.28"))
        self.assertTrue(self.s.is_protected_branch("stage/foo"))
        self.assertTrue(self.s.is_protected_branch("prod/hotfix"))

    def test_feature_branches_not_protected(self):
        for b in ("feature/x", "bugfix/y", "PUUL-123-fix", "hotfix-thing", "renovate/dep"):
            self.assertFalse(self.s.is_protected_branch(b), b)

    def test_empty_branch(self):
        self.assertFalse(self.s.is_protected_branch(""))

    def test_custom_list_overrides_defaults(self):
        s = AgentSettings(conflict_protected_branches=["sandbox"])
        self.assertTrue(s.is_protected_branch("sandbox"))
        self.assertFalse(s.is_protected_branch("dev"))  # not in the custom list


class EffectiveStrategyTest(unittest.TestCase):
    def setUp(self):
        self.s = AgentSettings()

    def test_rebase_on_shared_downgrades_to_merge(self):
        self.assertEqual(
            conflicts_mod.effective_strategy(_pr(branch="dev"), "rebase", self.s), "merge")

    def test_rebase_on_release_branch_downgrades(self):
        self.assertEqual(
            conflicts_mod.effective_strategy(_pr(branch="release/2.28"), "rebase", self.s), "merge")

    def test_rebase_on_feature_stays_rebase(self):
        self.assertEqual(
            conflicts_mod.effective_strategy(_pr(branch="feature/x"), "rebase", self.s), "rebase")

    def test_override_allows_rebase_on_shared(self):
        s = AgentSettings(conflict_rebase_protected=True)
        self.assertEqual(
            conflicts_mod.effective_strategy(_pr(branch="dev"), "rebase", s), "rebase")

    def test_merge_on_shared_stays_merge(self):
        self.assertEqual(
            conflicts_mod.effective_strategy(_pr(branch="dev"), "merge", self.s), "merge")


class BuildConflictItemsTest(unittest.TestCase):
    """build_conflict_items must emit the MERGE prompt for a downgraded PR."""

    def _prompt_for(self, branch, strategy, settings=None):
        settings = settings or AgentSettings()
        buf = io.StringIO()
        with redirect_stdout(buf):  # swallow the downgrade warning log
            items = conflicts_mod.build_conflict_items(
                [_pr(branch=branch)], settings, strategy)
        return items[0].prompt, buf.getvalue()

    def test_dev_rebase_yields_merge_prompt_and_warns(self):
        prompt, out = self._prompt_for("dev", "rebase")
        self.assertIn("merge main into dev", prompt)
        self.assertNotIn("REBASING it onto its base branch", prompt)
        self.assertIn("--force-rebase-protected", out)  # warned about the downgrade

    def test_feature_rebase_yields_rebase_prompt(self):
        prompt, out = self._prompt_for("feature/x", "rebase")
        self.assertIn("REBASING it onto its base branch", prompt)
        self.assertIn("STOP-FIRST", prompt)  # the in-prompt guard is present
        self.assertEqual(out, "")             # no downgrade warning for a feature branch

    def test_override_dev_rebase_yields_rebase_prompt(self):
        prompt, _ = self._prompt_for(
            "dev", "rebase", AgentSettings(conflict_rebase_protected=True))
        self.assertIn("REBASING it onto its base branch", prompt)


class RebaseTemplateGuardTest(unittest.TestCase):
    def test_template_has_stop_first_and_merge_recommendation(self):
        t = prompts_mod.BUILTIN_CONFLICT_REBASE
        self.assertIn("STOP-FIRST", t)
        self.assertIn("MERGE strategy", t)
        self.assertIn("gh pr list --base {pr_branch} --state open", t)


if __name__ == "__main__":
    unittest.main()
