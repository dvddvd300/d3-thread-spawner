"""Tests for reading a spawned thread's output back from T3's state DB.

`launch_t3` is fire-and-forget; `reader` closes the loop by reading T3's
projection tables read-only. These build a throwaway SQLite DB with the same
table/column shape T3 uses and pin the resolve / status / assembly / wait paths.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner import reader  # noqa: E402


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projection_threads (thread_id TEXT, created_at TEXT);
        CREATE TABLE projection_turns (
            thread_id TEXT, turn_id TEXT, state TEXT,
            requested_at TEXT, completed_at TEXT
        );
        CREATE TABLE projection_thread_messages (
            thread_id TEXT, turn_id TEXT, role TEXT, text TEXT, created_at TEXT
        );
        """
    )
    return conn


class ReaderTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = _make_db(self.db)
        # Two threads; the first has two turns (older completed, newer running).
        conn.executemany(
            "INSERT INTO projection_threads VALUES (?, ?)",
            [("aaaa1111-0000", "2026-01-01T00:00:00Z"),
             ("bbbb2222-0000", "2026-01-02T00:00:00Z")],
        )
        conn.executemany(
            "INSERT INTO projection_turns VALUES (?, ?, ?, ?, ?)",
            [("aaaa1111-0000", "t-old", "completed", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"),
             ("aaaa1111-0000", "t-new", "running", "2026-01-01T00:02:00Z", None)],
        )
        conn.executemany(
            "INSERT INTO projection_thread_messages VALUES (?, ?, ?, ?, ?)",
            [("aaaa1111-0000", "t-old", "user", "hi", "2026-01-01T00:00:00Z"),
             ("aaaa1111-0000", "t-old", "assistant", "part one.", "2026-01-01T00:00:30Z"),
             ("aaaa1111-0000", "t-old", "assistant", "part two.", "2026-01-01T00:00:40Z")],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.db)

    def test_resolve_exact_and_prefix(self):
        self.assertEqual(reader.resolve_thread_id("aaaa1111-0000", self.db), "aaaa1111-0000")
        self.assertEqual(reader.resolve_thread_id("aaaa", self.db), "aaaa1111-0000")

    def test_resolve_missing_raises(self):
        with self.assertRaises(RuntimeError):
            reader.resolve_thread_id("zzzz", self.db)

    def test_latest_turn_is_most_recent(self):
        turn = reader.latest_turn("aaaa1111-0000", self.db)
        self.assertEqual(turn.turn_id, "t-new")
        self.assertEqual(turn.state, "running")

    def test_read_output_assembles_assistant_only(self):
        # No turn_id → latest turn (t-new, running) has no assistant messages yet.
        out = reader.read_output("aaaa1111-0000", self.db)
        self.assertEqual(out.turn_id, "t-new")
        self.assertEqual(out.text, "")
        self.assertFalse(out.is_terminal)

    def test_read_output_specific_completed_turn(self):
        out = reader.read_output("aaaa1111-0000", self.db, turn_id="t-old")
        self.assertEqual(out.text, "part one.\npart two.")
        self.assertEqual(out.message_count, 2)
        self.assertTrue(out.is_terminal)

    def test_wait_returns_immediately_when_terminal(self):
        # Thread bbbb has a single completed turn → wait must not block.
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO projection_turns VALUES (?, ?, ?, ?, ?)",
            ("bbbb2222-0000", "t-b", "completed", "2026-01-02T00:00:00Z", "2026-01-02T00:01:00Z"),
        )
        conn.execute(
            "INSERT INTO projection_thread_messages VALUES (?, ?, ?, ?, ?)",
            ("bbbb2222-0000", "t-b", "assistant", "done.", "2026-01-02T00:00:30Z"),
        )
        conn.commit()
        conn.close()
        out = reader.wait_for_output("bbbb2222-0000", self.db, timeout=1.0, interval=0.05)
        self.assertEqual(out.state, "completed")
        self.assertEqual(out.text, "done.")

    def test_missing_db_raises(self):
        with self.assertRaises(RuntimeError):
            reader.latest_turn("aaaa1111-0000", "/nonexistent/state.sqlite")


if __name__ == "__main__":
    unittest.main()
