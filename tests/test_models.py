"""Tests for model provider routing and option payloads."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner import models as models_mod  # noqa: E402
from d3_thread_spawner.models import (  # noqa: E402
    CLAUDE_EFFORTS,
    CLAUDE_MODEL_OPTIONS,
    CODEX_EFFORTS,
    CODEX_MODEL_OPTIONS,
    CONTEXT_WINDOWS,
    AgentSettings,
)


class ModelSelectionOptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache_dir = models_mod.T3_CACHE_DIR
        models_mod.T3_CACHE_DIR = self.tmp.name
        models_mod._cached_provider_model_options.cache_clear()

    def tearDown(self):
        models_mod.T3_CACHE_DIR = self.old_cache_dir
        models_mod._cached_provider_model_options.cache_clear()
        self.tmp.cleanup()

    def test_gpt_defaults_to_standard_service_tier_without_fast_mode(self):
        settings = AgentSettings(model="gpt-5.5", effort="high", fast_mode=True)

        self.assertEqual(settings.provider, "codex")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "reasoningEffort", "value": "high"},
            {"id": "serviceTier", "value": "default"},
        ])

    def test_gpt_invalid_effort_normalizes_to_xhigh(self):
        settings = AgentSettings(model="gpt-5.5", effort="mini")

        self.assertEqual(settings.model_selection_options(), [
            {"id": "reasoningEffort", "value": "xhigh"},
            {"id": "serviceTier", "value": "default"},
        ])

    def test_mini_alias_routes_to_codex_and_filters_service_tier(self):
        settings = AgentSettings(model="mini", effort="mini", context_window="1m")

        self.assertEqual(settings.provider, "codex")
        self.assertEqual(settings.resolved_model, "gpt-5.4-mini")
        self.assertEqual(settings.effective_effort(), "xhigh")
        self.assertEqual(settings.effective_context_window(), "200k")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "reasoningEffort", "value": "xhigh"},
        ])

    def test_claude_context_without_model_support_falls_back_to_200k(self):
        settings = AgentSettings(model="haiku", context_window="1m")

        self.assertEqual(settings.effective_context_window(), "200k")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "thinking", "value": True},
        ])

    def test_claude_invalid_effort_uses_highest_supported_effort(self):
        settings = AgentSettings(model="claude-opus-4-5", effort="ultrathink")

        self.assertEqual(settings.effective_effort(), "max")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "effort", "value": "max"},
            {"id": "fastMode", "value": False},
        ])

    def test_claude_unsupported_effort_uses_highest_real_effort(self):
        settings = AgentSettings(model="sonnet", effort="xhigh", context_window="1m")

        self.assertEqual(settings.effective_effort(), "ultrathink")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "effort", "value": "ultrathink"},
            {"id": "contextWindow", "value": "1m"},
        ])

    def test_static_claude_matrix_never_emits_unsupported_options(self):
        for model_id, supported in CLAUDE_MODEL_OPTIONS.items():
            for effort in (*CLAUDE_EFFORTS, "mini"):
                for context_window in (*CONTEXT_WINDOWS, "2m"):
                    with self.subTest(
                        model=model_id,
                        effort=effort,
                        context_window=context_window,
                    ):
                        settings = AgentSettings(
                            model=model_id,
                            effort=effort,
                            context_window=context_window,
                        )
                        emitted = settings.model_selection_options()
                        for option in emitted:
                            option_id = option["id"]
                            self.assertIn(option_id, supported)
                            values = supported[option_id]
                            if values:
                                self.assertIn(option["value"], values)

    def test_static_codex_matrix_never_emits_unsupported_options(self):
        for model_id, supported in CODEX_MODEL_OPTIONS.items():
            for effort in (*CODEX_EFFORTS, "mini", "not-real"):
                with self.subTest(model=model_id, effort=effort):
                    settings = AgentSettings(model=model_id, effort=effort)
                    emitted = settings.model_selection_options()
                    for option in emitted:
                        option_id = option["id"]
                        self.assertIn(option_id, supported)
                        values = supported[option_id]
                        if values:
                            self.assertIn(option["value"], values)

    def test_t3_cache_capabilities_override_static_fallback(self):
        self._write_cache("claudeAgent", [
            {
                "slug": "claude-opus-4-8",
                "capabilities": {
                    "optionDescriptors": [
                        {
                            "id": "effort",
                            "options": [{"id": "low"}, {"id": "high"}],
                        },
                        {"id": "fastMode"},
                    ],
                },
            },
        ])
        settings = AgentSettings(
            model="opus",
            effort="ultrathink",
            context_window="1m",
            fast_mode=True,
        )

        self.assertEqual(settings.effective_effort(), "high")
        self.assertEqual(settings.effective_context_window(), "200k")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "effort", "value": "high"},
            {"id": "fastMode", "value": True},
        ])

    def test_known_alias_raises_when_missing_from_t3_cache(self):
        self._write_cache("claudeAgent", [
            {"slug": "claude-known", "capabilities": {"optionDescriptors": []}},
        ])
        settings = AgentSettings(model="opus")

        with self.assertRaisesRegex(RuntimeError, "not advertising"):
            settings.model_selection_options()

    def test_custom_model_passes_through_when_missing_from_t3_cache(self):
        self._write_cache("claudeAgent", [
            {"slug": "claude-known", "capabilities": {"optionDescriptors": []}},
        ])
        settings = AgentSettings(model="claude-new-experimental")

        self.assertEqual(settings.resolved_model, "claude-new-experimental")
        self.assertEqual(settings.model_selection_options(), [])

    def test_cached_model_match_is_case_insensitive_for_gpt_slugs(self):
        self._write_cache("codex", [
            {
                "slug": "GPT-5.5-Sol",
                "capabilities": {
                    "optionDescriptors": [
                        {
                            "id": "reasoningEffort",
                            "options": [{"id": "low"}, {"id": "xhigh"}],
                        },
                    ],
                },
            },
        ])
        settings = AgentSettings(model="gpt-5.5-sol", effort="mini")

        self.assertEqual(settings.provider, "codex")
        self.assertEqual(settings.resolved_model, "GPT-5.5-Sol")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "reasoningEffort", "value": "xhigh"},
        ])

    def _write_cache(self, provider, models):
        path = os.path.join(self.tmp.name, f"{provider}.json")
        with open(path, "w") as f:
            json.dump({"models": models}, f)
        models_mod._cached_provider_model_options.cache_clear()


if __name__ == "__main__":
    unittest.main()
