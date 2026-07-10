"""Discover, approve, and quota-gate captured T3 proposed plans."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, List, Optional, Sequence

from .models import AgentSettings
from .util import http_post, iso_now


@dataclass(frozen=True)
class PlanCandidate:
    """A proposed plan frozen before an unattended approval run."""

    thread_id: str
    plan_id: str
    title: str
    branch: Optional[str]
    worktree_path: Optional[str]
    plan_markdown: str
    plan_updated_at: str
    runtime_mode: str
    interaction_mode: str
    session_status: Optional[str]
    provider_name: Optional[str]

    @property
    def short_id(self) -> str:
        return self.thread_id[:8]


@dataclass(frozen=True)
class PlanValidation:
    status: str  # ready | skipped | busy
    reason: str
    current: Optional[PlanCandidate]


@dataclass(frozen=True)
class ApprovalResult:
    status: str  # approved | skipped | failed
    thread_id: str
    plan_id: str
    reason: str = ""


@dataclass(frozen=True)
class RateLimitEvent:
    event_id: str
    created_at: datetime
    limit_type: str
    status: str
    utilization: Optional[float]
    resets_at: Optional[int]


@dataclass(frozen=True)
class QuotaDecision:
    can_continue: bool
    reason: str
    signal_count: int
    five_hour_utilization: Optional[float] = None


def _connect(state_db: str) -> sqlite3.Connection:
    path = os.path.abspath(os.path.expanduser(state_db))
    if not os.path.isfile(path):
        raise RuntimeError(f"T3 state database not found at {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


_PLAN_SELECT = """
    SELECT t.thread_id, pp.plan_id, t.title, t.branch, t.worktree_path,
           pp.plan_markdown, pp.updated_at, t.runtime_mode, t.interaction_mode,
           s.status, s.provider_name
      FROM projection_threads t
      JOIN projection_thread_proposed_plans pp ON pp.thread_id = t.thread_id
 LEFT JOIN projection_thread_sessions s ON s.thread_id = t.thread_id
     WHERE t.project_id = ?
       AND t.deleted_at IS NULL
       AND t.archived_at IS NULL
       AND t.has_actionable_proposed_plan = 1
       AND pp.implemented_at IS NULL
       AND pp.updated_at = (
           SELECT MAX(newer.updated_at)
             FROM projection_thread_proposed_plans newer
            WHERE newer.thread_id = t.thread_id
              AND newer.implemented_at IS NULL
       )
"""


def list_actionable_plans(project_id: str, state_db: str) -> List[PlanCandidate]:
    """Return the latest actionable, unimplemented plan for each active thread."""
    conn = _connect(state_db)
    try:
        rows = conn.execute(_PLAN_SELECT + " ORDER BY t.created_at, t.thread_id", (project_id,)).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not read T3 proposed plans: {exc}") from exc
    finally:
        conn.close()
    return [PlanCandidate(*row) for row in rows]


def freeze_plans(
    project_id: str,
    state_db: str,
    thread_refs: Sequence[str],
) -> List[PlanCandidate]:
    """Resolve an ordered manifest from current actionable plans.

    With no refs, the database order is frozen. With refs, exact ids win and
    unique prefixes are accepted. A selected plan is never replaced by a newer
    plan that appears after this function returns.
    """
    candidates = list_actionable_plans(project_id, state_db)
    if not thread_refs:
        return candidates

    result: List[PlanCandidate] = []
    seen = set()
    for ref in thread_refs:
        ref = ref.strip()
        if not ref:
            raise RuntimeError("Empty thread reference")
        exact = [candidate for candidate in candidates if candidate.thread_id == ref]
        matches = exact or [
            candidate for candidate in candidates if candidate.thread_id.startswith(ref)
        ]
        if not matches:
            raise RuntimeError(
                f"No actionable, unimplemented plan in this project matches {ref!r}"
            )
        if len(matches) > 1:
            raise RuntimeError(
                f"Thread reference {ref!r} is ambiguous ({len(matches)} matches)"
            )
        candidate = matches[0]
        if candidate.thread_id in seen:
            raise RuntimeError(f"Thread {candidate.short_id} was selected more than once")
        seen.add(candidate.thread_id)
        result.append(candidate)
    return result


def _load_frozen_plan(candidate: PlanCandidate, state_db: str) -> Optional[PlanCandidate]:
    conn = _connect(state_db)
    try:
        row = conn.execute(
            _PLAN_SELECT + " AND t.thread_id = ? AND pp.plan_id = ?",
            # Project id is not stored on the frozen shape because the plan id
            # and thread id are globally unique. Resolve it from the thread.
            (
                _project_id_for_thread(conn, candidate.thread_id),
                candidate.thread_id,
                candidate.plan_id,
            ),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not revalidate plan {candidate.short_id}: {exc}") from exc
    finally:
        conn.close()
    return PlanCandidate(*row) if row else None


def _project_id_for_thread(conn: sqlite3.Connection, thread_id: str) -> str:
    row = conn.execute(
        "SELECT project_id FROM projection_threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    return row[0] if row else ""


def validate_frozen_plan(candidate: PlanCandidate, state_db: str) -> PlanValidation:
    """Revalidate a frozen candidate immediately before approval."""
    current = _load_frozen_plan(candidate, state_db)
    if current is None:
        return PlanValidation("skipped", "plan is no longer actionable", None)
    if (
        current.plan_updated_at != candidate.plan_updated_at
        or current.plan_markdown != candidate.plan_markdown
    ):
        return PlanValidation("skipped", "plan changed after the manifest was frozen", current)
    if current.session_status in {"starting", "running"}:
        return PlanValidation(
            "busy", f"thread session is {current.session_status}", current
        )
    return PlanValidation("ready", "", current)


def _headers(token: str, port: int) -> dict:
    return {
        "Cookie": f"t3_session_{port}={token}; t3_session={token}",
        "Content-Type": "application/json",
    }


def _implementation_state(candidate: PlanCandidate, state_db: str) -> tuple:
    conn = _connect(state_db)
    try:
        plan = conn.execute(
            "SELECT implemented_at, implementation_thread_id "
            "FROM projection_thread_proposed_plans WHERE plan_id = ?",
            (candidate.plan_id,),
        ).fetchone()
        turn = conn.execute(
            "SELECT state FROM projection_turns "
            "WHERE thread_id = ? AND source_proposed_plan_id = ? "
            "ORDER BY requested_at DESC LIMIT 1",
            (candidate.thread_id, candidate.plan_id),
        ).fetchone()
        session = conn.execute(
            "SELECT status, last_error FROM projection_thread_sessions WHERE thread_id = ?",
            (candidate.thread_id,),
        ).fetchone()
    finally:
        conn.close()
    return plan, turn, session


def approve_plan(
    candidate: PlanCandidate,
    settings: AgentSettings,
    initial_token: str,
    refresh_token: Callable[[], str],
    *,
    post: Callable[[str, dict, dict], dict] = http_post,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    verification_timeout: float = 30.0,
) -> ApprovalResult:
    """Approve one frozen plan using T3's native two-command sequence."""
    validation = validate_frozen_plan(candidate, settings.t3_state_db)
    if validation.status != "ready":
        return ApprovalResult(
            "skipped", candidate.thread_id, candidate.plan_id, validation.reason
        )
    current = validation.current
    assert current is not None

    created_at = iso_now()
    mode_command = {
        "type": "thread.interaction-mode.set",
        "commandId": str(uuid.uuid4()),
        "threadId": candidate.thread_id,
        "interactionMode": "default",
        "createdAt": created_at,
    }
    turn_command = {
        "type": "thread.turn.start",
        "commandId": str(uuid.uuid4()),
        "threadId": candidate.thread_id,
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "text": f"PLEASE IMPLEMENT THIS PLAN:\n{candidate.plan_markdown}",
            "attachments": [],
        },
        "runtimeMode": current.runtime_mode,
        "interactionMode": "default",
        "sourceProposedPlan": {
            "threadId": candidate.thread_id,
            "planId": candidate.plan_id,
        },
        "createdAt": created_at,
    }

    token = initial_token

    def send(command: dict) -> None:
        nonlocal token
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                post(
                    f"{settings.t3_api}/api/orchestration/dispatch",
                    command,
                    _headers(token, settings.t3_port),
                )
                return
            except Exception as exc:  # urllib exposes several concrete failures
                last_error = exc
                if attempt == 0:
                    token = refresh_token()
        assert last_error is not None
        raise last_error

    try:
        if current.interaction_mode != "default":
            send(mode_command)
        send(turn_command)
    except Exception as exc:
        return ApprovalResult(
            "failed", candidate.thread_id, candidate.plan_id, f"T3 dispatch failed: {exc}"
        )

    deadline = monotonic() + verification_timeout
    while True:
        plan, turn, session = _implementation_state(candidate, settings.t3_state_db)
        if session and session[0] == "error":
            return ApprovalResult(
                "failed",
                candidate.thread_id,
                candidate.plan_id,
                session[1] or "thread session entered error state",
            )
        if turn and turn[0] == "error":
            return ApprovalResult(
                "failed", candidate.thread_id, candidate.plan_id, "implementation turn failed"
            )
        if plan and plan[0] and turn:
            return ApprovalResult("approved", candidate.thread_id, candidate.plan_id)
        if monotonic() >= deadline:
            return ApprovalResult(
                "failed",
                candidate.thread_id,
                candidate.plan_id,
                "T3 did not confirm the linked implementation turn",
            )
        sleep(0.25)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def read_rate_limit_events(log_dir: str, since: datetime) -> List[RateLimitEvent]:
    """Read fresh canonical Claude rate-limit events from provider log files."""
    path = os.path.abspath(os.path.expanduser(log_dir))
    if not os.path.isdir(path):
        return []
    since_utc = since.astimezone(timezone.utc)
    mtime_floor = since_utc.timestamp() - 2
    events: List[RateLimitEvent] = []
    seen = set()

    for entry in os.scandir(path):
        if not entry.is_file() or ".log" not in entry.name:
            continue
        try:
            if entry.stat().st_mtime < mtime_floor:
                continue
            with open(entry.path, errors="replace") as handle:
                for line in handle:
                    if (
                        "CANON:" not in line
                        or '"type":"account.rate-limits.updated"' not in line
                    ):
                        continue
                    try:
                        payload = json.loads(line.split("CANON:", 1)[1].strip())
                    except (json.JSONDecodeError, IndexError):
                        continue
                    if payload.get("providerInstanceId") != "claudeAgent":
                        continue
                    created_at = _parse_iso(payload.get("createdAt", ""))
                    if created_at is None or created_at < since_utc:
                        continue
                    event_id = str(payload.get("eventId", ""))
                    if event_id and event_id in seen:
                        continue
                    if event_id:
                        seen.add(event_id)
                    info = (
                        payload.get("payload", {})
                        .get("rateLimits", {})
                        .get("rate_limit_info", {})
                    )
                    limit_type = info.get("rateLimitType")
                    status = info.get("status")
                    if not isinstance(limit_type, str) or not isinstance(status, str):
                        continue
                    utilization = info.get("utilization")
                    if not isinstance(utilization, (int, float)):
                        utilization = None
                    resets_at = info.get("resetsAt")
                    if not isinstance(resets_at, int):
                        resets_at = None
                    events.append(
                        RateLimitEvent(
                            event_id,
                            created_at,
                            limit_type,
                            status,
                            float(utilization) if utilization is not None else None,
                            resets_at,
                        )
                    )
        except OSError:
            continue
    return sorted(events, key=lambda event: event.created_at)


def evaluate_quota(
    events: Sequence[RateLimitEvent], threshold_percent: float
) -> QuotaDecision:
    """Evaluate the newest reset window for each observed Claude limit type."""
    if not events:
        return QuotaDecision(False, "no fresh Claude quota signal", 0)

    current_windows: List[RateLimitEvent] = []
    for limit_type in sorted({event.limit_type for event in events}):
        typed = [event for event in events if event.limit_type == limit_type]
        known_resets = [event.resets_at for event in typed if event.resets_at is not None]
        if known_resets:
            newest_reset = max(known_resets)
            typed = [event for event in typed if event.resets_at == newest_reset]
        current_windows.extend(typed)

    rejected = [event for event in current_windows if event.status == "rejected"]
    if rejected:
        types = ", ".join(sorted({event.limit_type for event in rejected}))
        return QuotaDecision(
            False, f"Claude quota rejected ({types})", len(events)
        )

    five_hour_values = [
        event.utilization
        for event in current_windows
        if event.limit_type == "five_hour" and event.utilization is not None
    ]
    utilization = max(five_hour_values) if five_hour_values else None
    threshold = threshold_percent / 100.0
    if utilization is not None and utilization >= threshold:
        return QuotaDecision(
            False,
            f"five-hour quota is {utilization:.0%} (threshold {threshold:.0%})",
            len(events),
            utilization,
        )
    detail = (
        f"five-hour quota is {utilization:.0%}"
        if utilization is not None
        else "fresh Claude quota signal is non-rejected"
    )
    return QuotaDecision(True, detail, len(events), utilization)


def batch_turn_errors(
    state_db: str,
    approvals: Iterable[ApprovalResult],
    since: datetime,
) -> List[str]:
    """Return error summaries for linked implementation turns in one batch."""
    approved = [result for result in approvals if result.status == "approved"]
    if not approved:
        return []
    since_text = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    errors: List[str] = []
    conn = _connect(state_db)
    try:
        for result in approved:
            turn = conn.execute(
                "SELECT state FROM projection_turns "
                "WHERE thread_id = ? AND source_proposed_plan_id = ? "
                "AND requested_at >= ? ORDER BY requested_at DESC LIMIT 1",
                (result.thread_id, result.plan_id, since_text),
            ).fetchone()
            session = conn.execute(
                "SELECT status, last_error FROM projection_thread_sessions WHERE thread_id = ?",
                (result.thread_id,),
            ).fetchone()
            if turn and turn[0] == "error":
                errors.append(f"{result.thread_id[:8]} implementation turn failed")
            elif session and session[0] == "error":
                errors.append(
                    f"{result.thread_id[:8]} session error: "
                    f"{session[1] or 'unknown error'}"
                )
    finally:
        conn.close()
    return errors

