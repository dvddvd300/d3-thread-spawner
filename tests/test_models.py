"""Tests for provider-aware T3 model selection."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner.commands.spawn import _load_jsonl  # noqa: E402
from d3_thread_spawner import models as models_mod  # noqa: E402
from d3_thread_spawner.models import AgentSettings  # noqa: E402


class ModelSelectionTest(unittest.TestCase):
    def setUp(self):
        self._provider_cache_files = models_mod.PROVIDER_CACHE_FILES
        models_mod.PROVIDER_CACHE_FILES = {}
        models_mod._cached_provider_options.cache_clear()

    def tearDown(self):
        models_mod.PROVIDER_CACHE_FILES = self._provider_cache_files
        models_mod._cached_provider_options.cache_clear()

    def test_gpt_55_uses_codex_provider_options(self):
        s = AgentSettings(model="gpt5.5", effort="xhigh", service_tier="standard")

        self.assertEqual(s.resolved_model, "gpt-5.5")
        self.assertEqual(s.model_provider, "codex")
        self.assertEqual(s.normalized_service_tier, "default")
        self.assertEqual(
            s.model_selection_options(),
            [
                {"id": "reasoningEffort", "value": "xhigh"},
                {"id": "serviceTier", "value": "default"},
            ],
        )
        self.assertEqual(
            s.model_selection(),
            {
                "instanceId": "codex",
                "provider": "codex",
                "model": "gpt-5.5",
                "options": [
                    {"id": "reasoningEffort", "value": "xhigh"},
                    {"id": "serviceTier", "value": "default"},
                ],
            },
        )

    def test_gpt_model_without_service_tier_capability_omits_it(self):
        s = AgentSettings(model="gpt-5.4-mini", effort="high", service_tier="standard")

        self.assertEqual(s.model_provider, "codex")
        self.assertEqual(
            s.model_selection_options(),
            [{"id": "reasoningEffort", "value": "high"}],
        )

    def test_fast_service_tier_alias_maps_to_priority(self):
        s = AgentSettings(model="gpt-5.5", effort="xhigh", service_tier="fast")

        self.assertEqual(s.normalized_service_tier, "priority")
        self.assertIn(
            {"id": "serviceTier", "value": "priority"},
            s.model_selection_options(),
        )

    def test_claude_opus_48_stays_on_claude_options(self):
        s = AgentSettings(
            model="opus",
            effort="max",
            context_window="1m",
            service_tier="standard",
        )

        self.assertEqual(s.model_provider, "claudeAgent")
        self.assertEqual(s.resolved_model, "claude-opus-4-8")
        options = s.model_selection_options()
        self.assertIn({"id": "effort", "value": "max"}, options)
        self.assertNotIn({"id": "serviceTier", "value": "default"}, options)
        self.assertFalse(any(o["id"] == "reasoningEffort" for o in options))
        self.assertFalse(any(o["id"] == "contextWindow" for o in options))


class JsonlModelOverrideTest(unittest.TestCase):
    def test_jsonl_task_can_override_gpt_service_tier(self):
        settings = AgentSettings(
            model="opus",
            effort="high",
            service_tier="priority",
            cookies_path="/custom/cookies",
        )
        row = {
            "name": "gpt-task",
            "prompt": "Do the task",
            "new_branch": "feature/gpt-task",
            "model": "gpt5.5",
            "effort": "xhigh",
            "service_tier": "standard",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(row) + "\n")
            path = f.name
        try:
            items = _load_jsonl(path, settings)
        finally:
            os.unlink(path)

        self.assertEqual(len(items), 1)
        item_settings = items[0].settings
        self.assertEqual(item_settings.resolved_model, "gpt-5.5")
        self.assertEqual(item_settings.model_provider, "codex")
        self.assertEqual(item_settings.normalized_service_tier, "default")
        self.assertEqual(item_settings.effort, "xhigh")
        self.assertEqual(item_settings.cookies_path, "/custom/cookies")


if __name__ == "__main__":
    unittest.main(verbosity=2)
