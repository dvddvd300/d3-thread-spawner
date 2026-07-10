"""Tests for frozen, quota-aware T3 proposed-plan approvals."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from d3_thread_spawner.commands import approve_plans as command  # noqa: E402
from d3_thread_spawner.models import AgentSettings  # noqa: E402
from d3_thread_spawner.plan_approval import (  # noqa: E402
    ApprovalResult,
    PlanCandidate,
    RateLimitEvent,
    approve_plan,
    evaluate_quota,
    freeze_plans,
    list_actionable_plans,
    read_rate_limit_events,
    validate_frozen_plan,
)


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projection_threads (
            thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT,
            branch TEXT, worktree_path TEXT, created_at TEXT, updated_at TEXT,
            deleted_at TEXT, archived_at TEXT,
            has_actionable_proposed_plan INTEGER,
            runtime_mode TEXT, interaction_mode TEXT
        );
        CREATE TABLE projection_thread_proposed_plans (
            plan_id TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT,
            plan_markdown TEXT, created_at TEXT, updated_at TEXT,
            implemented_at TEXT, implementation_thread_id TEXT
        );
        CREATE TABLE projection_thread_sessions (
            thread_id TEXT PRIMARY KEY, status TEXT, provider_name TEXT,
            last_error TEXT
        );
        CREATE TABLE projection_turns (
            thread_id TEXT, turn_id TEXT, state TEXT, requested_at TEXT,
            source_proposed_plan_id TEXT
        );
        """
    )
    return conn


def _insert_plan(
    conn,
    thread_id,
    *,
    project="project-a",
    title=None,
    created="2026-07-10T00:00:00Z",
    actionable=1,
    deleted=None,
    archived=None,
    implemented=None,
    markdown=None,
    status="ready",
    interaction="plan",
):
    title = title or thread_id
    markdown = markdown or f"# Plan {thread_id}"
    plan_id = f"plan:{thread_id}"
    conn.execute(
        "INSERT INTO projection_threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            thread_id,
            project,
            title,
            f"fix/{thread_id}",
            f"/tmp/{thread_id}",
            created,
            created,
            deleted,
            archived,
            actionable,
            "full-access",
            interaction,
        ),
    )
    conn.execute(
        "INSERT INTO projection_thread_proposed_plans VALUES (?,?,?,?,?,?,?,?)",
        (
            plan_id,
            thread_id,
            f"turn:{thread_id}",
            markdown,
            created,
            created,
            implemented,
            thread_id if implemented else None,
        ),
    )
    conn.execute(
        "INSERT INTO projection_thread_sessions VALUES (?,?,?,?)",
        (thread_id, status, "claudeAgent", None),
    )
    return plan_id


def _candidate(index=0):
    thread_id = f"thread-{index:02d}"
    return PlanCandidate(
        thread_id,
        f"plan:{thread_id}",
        f"Plan {index}",
        f"fix/{index}",
        f"/tmp/{index}",
        f"# Plan {index}",
        "2026-07-10T00:00:00Z",
        "full-access",
        "plan",
        "ready",
        "claudeAgent",
    )


class PlanDatabaseTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = _make_db(self.db)
        _insert_plan(conn, "aaaa1111", created="2026-07-10T00:00:01Z")
        _insert_plan(conn, "bbbb2222", created="2026-07-10T00:00:02Z", status="stopped")
        _insert_plan(conn, "other333", project="project-b")
        _insert_plan(conn, "done4444", implemented="2026-07-10T01:00:00Z")
        _insert_plan(conn, "gone5555", archived="2026-07-10T01:00:00Z")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.db)

    def test_project_scoping_order_and_prefix_resolution(self):
        plans = list_actionable_plans("project-a", self.db)
        self.assertEqual([plan.thread_id for plan in plans], ["aaaa1111", "bbbb2222"])
        selected = freeze_plans("project-a", self.db, ["bbbb", "aaaa"])
        self.assertEqual([plan.thread_id for plan in selected], ["bbbb2222", "aaaa1111"])

    def test_missing_and_duplicate_refs_fail(self):
        with self.assertRaisesRegex(RuntimeError, "No actionable"):
            freeze_plans("project-a", self.db, ["other"])
        with self.assertRaisesRegex(RuntimeError, "more than once"):
            freeze_plans("project-a", self.db, ["aaaa", "aaaa1111"])

    def test_changed_busy_and_stopped_validation(self):
        ready, stopped = list_actionable_plans("project-a", self.db)
        self.assertEqual(validate_frozen_plan(stopped, self.db).status, "ready")

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET status='running' WHERE thread_id=?",
            (ready.thread_id,),
        )
        conn.commit()
        conn.close()
        self.assertEqual(validate_frozen_plan(ready, self.db).status, "busy")

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET status='ready' WHERE thread_id=?",
            (ready.thread_id,),
        )
        conn.execute(
            "UPDATE projection_thread_proposed_plans "
            "SET plan_markdown='# changed', updated_at='2026-07-10T02:00:00Z' "
            "WHERE plan_id=?",
            (ready.plan_id,),
        )
        conn.commit()
        conn.close()
        self.assertEqual(validate_frozen_plan(ready, self.db).status, "skipped")


class ApprovalTransportTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = _make_db(self.db)
        _insert_plan(conn, "aaaa1111")
        conn.commit()
        conn.close()
        self.plan = list_actionable_plans("project-a", self.db)[0]
        self.settings = AgentSettings(
            t3_state_db=self.db,
            t3_host="127.0.0.1",
            t3_port=3773,
        )

    def tearDown(self):
        os.remove(self.db)

    def _complete_turn(self, turn_command):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_proposed_plans "
            "SET implemented_at=?, implementation_thread_id=? WHERE plan_id=?",
            (
                "2026-07-10T00:01:00Z",
                self.plan.thread_id,
                self.plan.plan_id,
            ),
        )
        conn.execute(
            "INSERT INTO projection_turns VALUES (?,?,?,?,?)",
            (
                self.plan.thread_id,
                "implementation-turn",
                "running",
                "2026-07-10T00:01:00Z",
                turn_command["sourceProposedPlan"]["planId"],
            ),
        )
        conn.commit()
        conn.close()

    def test_native_mode_and_linked_turn_payload(self):
        calls = []

        def post(url, payload, headers):
            calls.append((url, payload, headers))
            if payload["type"] == "thread.turn.start":
                self._complete_turn(payload)
            return {}

        result = approve_plan(
            self.plan,
            self.settings,
            "token-a",
            lambda: "token-b",
            post=post,
            verification_timeout=0,
        )
        self.assertEqual(result.status, "approved")
        self.assertEqual([call[1]["type"] for call in calls], [
            "thread.interaction-mode.set", "thread.turn.start",
        ])
        turn = calls[1][1]
        self.assertEqual(turn["runtimeMode"], "full-access")
        self.assertEqual(turn["interactionMode"], "default")
        self.assertEqual(
            turn["sourceProposedPlan"],
            {"threadId": self.plan.thread_id, "planId": self.plan.plan_id},
        )
        self.assertEqual(
            turn["message"]["text"],
            f"PLEASE IMPLEMENT THIS PLAN:\n{self.plan.plan_markdown}",
        )

    def test_retry_reuses_command_id_and_refreshes_token(self):
        turn_attempts = []
        refreshes = []

        def post(url, payload, headers):
            if payload["type"] == "thread.turn.start":
                turn_attempts.append((payload["commandId"], headers["Cookie"]))
                if len(turn_attempts) == 1:
                    raise OSError("temporary")
                self._complete_turn(payload)
            return {}

        result = approve_plan(
            self.plan,
            self.settings,
            "token-a",
            lambda: refreshes.append(True) or "token-b",
            post=post,
            verification_timeout=0,
        )
        self.assertEqual(result.status, "approved")
        self.assertEqual(turn_attempts[0][0], turn_attempts[1][0])
        self.assertIn("token-b", turn_attempts[1][1])
        self.assertEqual(len(refreshes), 1)


class QuotaTest(unittest.TestCase):
    def event(self, kind, status="allowed_warning", utilization=None, reset=200):
        return RateLimitEvent(
            f"{kind}-{status}-{utilization}-{reset}",
            datetime.now(timezone.utc),
            kind,
            status,
            utilization,
            reset,
        )

    def test_threshold_and_rejection(self):
        self.assertTrue(evaluate_quota([self.event("five_hour", utilization=0.89)], 90).can_continue)
        self.assertFalse(evaluate_quota([self.event("five_hour", utilization=0.90)], 90).can_continue)
        self.assertFalse(evaluate_quota([self.event("seven_day", status="rejected")], 90).can_continue)

    def test_weekly_only_is_usable_but_missing_is_unknown(self):
        decision = evaluate_quota([self.event("seven_day", utilization=0.35)], 90)
        self.assertTrue(decision.can_continue)
        self.assertFalse(evaluate_quota([], 90).can_continue)

    def test_new_reset_window_ignores_old_rejection(self):
        events = [
            self.event("five_hour", status="rejected", reset=100),
            self.event("five_hour", status="allowed", utilization=0.1, reset=200),
        ]
        self.assertTrue(evaluate_quota(events, 90).can_continue)

    def test_reads_canonical_events_from_rotated_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            payload = {
                "eventId": "event-1",
                "createdAt": now.isoformat(),
                "providerInstanceId": "claudeAgent",
                "type": "account.rate-limits.updated",
                "payload": {"rateLimits": {"rate_limit_info": {
                    "status": "allowed_warning",
                    "rateLimitType": "five_hour",
                    "utilization": 0.42,
                    "resetsAt": 123,
                }}},
            }
            path = os.path.join(tmp, "thread.log.1")
            with open(path, "w") as handle:
                handle.write(f"[time] CANON: {json.dumps(payload, separators=(',', ':'))}\n")
            events = read_rate_limit_events(tmp, now - timedelta(seconds=1))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].utilization, 0.42)


class CommandTest(unittest.TestCase):
    def args(self, **overrides):
        values = {
            "thread_refs": [],
            "quota_threshold": 90.0,
            "start_at": None,
            "yes": True,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_offset_required_and_past_wait_returns(self):
        with self.assertRaisesRegex(RuntimeError, "include a UTC offset"):
            command._parse_start_at("2026-07-10T02:50:00")
        target = command._parse_start_at("2026-07-10T02:50:00-06:00")
        command._wait_until(
            target,
            now=lambda: target + timedelta(seconds=1),
            sleep=lambda seconds: self.fail("past start must not sleep"),
        )

    def test_dry_run_never_authenticates_or_waits(self):
        settings = AgentSettings(
            dry_run=True,
            t3_project_id="project-a",
            batch_size=5,
            t3_state_db="/tmp/not-used.sqlite",
        )
        with patch.object(command, "freeze_plans", return_value=[_candidate(1)]), \
             patch.object(command, "_token", side_effect=AssertionError("auth called")), \
             patch.object(command, "_wait_until", side_effect=AssertionError("wait called")):
            out = io.StringIO()
            with redirect_stdout(out):
                command.cmd_approve_plans(
                    self.args(start_at="2026-07-10T02:50:00-06:00"), settings
                )
        self.assertIn("DRY RUN", out.getvalue())

    def test_batches_five_five_three_with_two_quota_gates(self):
        plans = [_candidate(index) for index in range(13)]
        settings = AgentSettings(
            t3_project_id="project-a",
            batch_size=5,
            batch_delay=10,
            launch_delay=0,
            t3_state_db="/tmp/not-used.sqlite",
        )
        approved = []
        waits = []

        def fake_approve(plan, *args, **kwargs):
            approved.append(plan.thread_id)
            return ApprovalResult("approved", plan.thread_id, plan.plan_id)

        quota_event = RateLimitEvent(
            "fresh",
            datetime.now(timezone.utc),
            "five_hour",
            "allowed_warning",
            0.4,
            123,
        )
        with patch.object(command, "freeze_plans", return_value=plans), \
             patch.object(command, "_token", return_value="token"), \
             patch.object(command, "approve_plan", side_effect=fake_approve), \
             patch.object(command, "_wait_minutes", side_effect=lambda m, **k: waits.append(m)), \
             patch.object(command, "batch_turn_errors", return_value=[]), \
             patch.object(command, "read_rate_limit_events", return_value=[quota_event]):
            with redirect_stdout(io.StringIO()):
                command.cmd_approve_plans(self.args(), settings)

        self.assertEqual(approved, [plan.thread_id for plan in plans])
        self.assertEqual(waits, [10, 10])

    def test_unknown_quota_stops_after_first_batch(self):
        plans = [_candidate(index) for index in range(8)]
        settings = AgentSettings(
            t3_project_id="project-a",
            batch_size=5,
            batch_delay=0,
            launch_delay=0,
            t3_state_db="/tmp/not-used.sqlite",
        )
        approved = []

        def fake_approve(plan, *args, **kwargs):
            approved.append(plan.thread_id)
            return ApprovalResult("approved", plan.thread_id, plan.plan_id)

        with patch.object(command, "freeze_plans", return_value=plans), \
             patch.object(command, "_token", return_value="token"), \
             patch.object(command, "approve_plan", side_effect=fake_approve), \
             patch.object(command, "_wait_minutes"), \
             patch.object(command, "batch_turn_errors", return_value=[]), \
             patch.object(command, "read_rate_limit_events", return_value=[]):
            with redirect_stdout(io.StringIO()):
                command.cmd_approve_plans(self.args(), settings)
        self.assertEqual(len(approved), 5)


if __name__ == "__main__":
    unittest.main()
