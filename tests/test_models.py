"""Tests for model provider routing and option payloads."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner.models import AgentSettings  # noqa: E402


class ModelSelectionOptionsTest(unittest.TestCase):
    def test_gpt_defaults_to_standard_service_tier_without_fast_mode(self):
        settings = AgentSettings(model="gpt-5.5", effort="high", fast_mode=True)

        self.assertEqual(settings.provider, "codex")
        self.assertEqual(settings.model_selection_options(), [
            {"id": "reasoningEffort", "value": "high"},
            {"id": "serviceTier", "value": "default"},
        ])

    def test_gpt_effort_alias_clamps_to_xhigh(self):
        settings = AgentSettings(model="gpt-5.5", effort="ultrathink")

        self.assertEqual(settings.model_selection_options(), [
            {"id": "reasoningEffort", "value": "xhigh"},
            {"id": "serviceTier", "value": "default"},
        ])


if __name__ == "__main__":
    unittest.main()
