"""Tests for spawn command input handling."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner.commands.spawn import _load_jsonl  # noqa: E402
from d3_thread_spawner.models import AgentSettings  # noqa: E402


class LoadJsonlTest(unittest.TestCase):
    def test_per_item_model_option_overrides_are_applied(self):
        entry = {
            "name": "one",
            "prompt": "do it",
            "model": "mini",
            "mode": "plan",
            "access": "supervised",
            "effort": "mini",
            "context_window": "200k",
            "thinking": False,
            "fast_mode": True,
        }

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(json.dumps(entry) + "\n")
            path = f.name

        try:
            item = _load_jsonl(path, AgentSettings())[0]
        finally:
            os.unlink(path)

        self.assertEqual(item.settings.model, "mini")
        self.assertEqual(item.settings.mode, "plan")
        self.assertEqual(item.settings.access, "supervised")
        self.assertEqual(item.settings.effort, "mini")
        self.assertEqual(item.settings.context_window, "200k")
        self.assertFalse(item.settings.thinking)
        self.assertTrue(item.settings.fast_mode)


if __name__ == "__main__":
    unittest.main()
