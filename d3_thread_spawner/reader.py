"""Read a spawned thread's output back from T3's state database.

`launch_t3` (see t3.py) is fire-and-forget: it dispatches a turn and returns a
thread id, but never reads the model's reply. This module closes that gap by
reading T3's local projection DB (`~/.t3/userdata/state.sqlite`) read-only — the
same DB and access pattern `auto_detect_project_id`/`cmd_status` already use — so
a caller can view or poll for what the agent produced without any HTTP route.

T3 records each turn in `projection_turns` (state: pending → running →
completed/error) and each assistant/user message in `projection_thread_messages`
(joined by `turn_id`). A turn's answer is the concatenation of its assistant
messages ordered by `created_at`.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

DEFAULT_STATE_DB = "~/.t3/userdata/state.sqlite"

# projection_turns.state values that mean the turn is finished (stop polling).
TERMINAL_STATES = frozenset({"completed", "error"})


@dataclass
class TurnStatus:
    """State of a single turn."""

    thread_id: str
    turn_id: Optional[str]
    state: str
    requested_at: str
    completed_at: Optional[str]


@dataclass
class ThreadOutput:
    """A turn's assistant reply, assembled from its messages."""

    thread_id: str
    turn_id: Optional[str]
    state: str
    completed_at: Optional[str]
    text: str
    message_count: int

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def _connect(state_db: str = DEFAULT_STATE_DB) -> sqlite3.Connection:
    """Open T3's state DB read-only. Raises RuntimeError if it is missing."""
    path = os.path.expanduser(state_db)
    if not os.path.isfile(path):
        raise RuntimeError(
            f"T3 state database not found at {path}. Is T3 Code installed?"
        )
    # mode=ro so a concurrent, running T3 process is never disturbed.
    return sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True)


def resolve_thread_id(thread_ref: str, state_db: str = DEFAULT_STATE_DB) -> str:
    """Resolve a full thread id or a short prefix (e.g. the 8-char id `status`
    prints) to the one matching thread id. Raises on no/ambiguous match."""
    ref = thread_ref.strip()
    if not ref:
        raise RuntimeError("empty thread id")
    conn = _connect(state_db)
    try:
        rows = conn.execute(
            "SELECT thread_id FROM projection_threads "
            "WHERE thread_id = ? OR thread_id LIKE ? "
            "ORDER BY created_at DESC",
            (ref, ref + "%"),
        ).fetchall()
    finally:
        conn.close()
    ids = [r[0] for r in rows]
    if not ids:
        raise RuntimeError(f"no thread matches '{thread_ref}'")
    # An exact hit wins even if it is also a prefix of others.
    if ref in ids:
        return ref
    if len(ids) > 1:
        raise RuntimeError(
            f"'{thread_ref}' is ambiguous ({len(ids)} threads); use the full id"
        )
    return ids[0]


def latest_turn(thread_id: str, state_db: str = DEFAULT_STATE_DB) -> Optional[TurnStatus]:
    """The most recently requested turn for a thread, or None if it has none."""
    conn = _connect(state_db)
    try:
        row = conn.execute(
            "SELECT thread_id, turn_id, state, requested_at, completed_at "
            "FROM projection_turns WHERE thread_id = ? "
            "ORDER BY requested_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    finally:
        conn.close()
    return TurnStatus(*row) if row else None


def read_output(
    thread_id: str,
    state_db: str = DEFAULT_STATE_DB,
    turn_id: Optional[str] = None,
) -> ThreadOutput:
    """Assemble the assistant reply for a turn (the latest turn if `turn_id` is
    omitted). `text` is empty until the agent has produced assistant messages."""
    conn = _connect(state_db)
    try:
        if turn_id is None:
            turn = conn.execute(
                "SELECT turn_id, state, completed_at FROM projection_turns "
                "WHERE thread_id = ? ORDER BY requested_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if turn is None:
                return ThreadOutput(thread_id, None, "unknown", None, "", 0)
            turn_id, state, completed_at = turn
        else:
            turn = conn.execute(
                "SELECT state, completed_at FROM projection_turns "
                "WHERE thread_id = ? AND turn_id = ?",
                (thread_id, turn_id),
            ).fetchone()
            state, completed_at = turn if turn else ("unknown", None)

        msgs = conn.execute(
            "SELECT text FROM projection_thread_messages "
            "WHERE thread_id = ? AND turn_id = ? AND role = 'assistant' "
            "ORDER BY created_at",
            (thread_id, turn_id),
        ).fetchall()
    finally:
        conn.close()

    text = "\n".join(m[0] for m in msgs if m[0])
    return ThreadOutput(thread_id, turn_id, state, completed_at, text, len(msgs))


def wait_for_output(
    thread_id: str,
    state_db: str = DEFAULT_STATE_DB,
    timeout: float = 600.0,
    interval: float = 3.0,
    on_tick: Optional[Callable[[TurnStatus], None]] = None,
) -> ThreadOutput:
    """Poll until the thread's latest turn reaches a terminal state or `timeout`
    (seconds) elapses, then return its output. `on_tick` is called with each
    observed TurnStatus so callers can render progress."""
    deadline = time.monotonic() + timeout
    while True:
        turn = latest_turn(thread_id, state_db)
        if on_tick and turn:
            on_tick(turn)
        if turn and turn.state in TERMINAL_STATES:
            return read_output(thread_id, state_db, turn.turn_id)
        if time.monotonic() >= deadline:
            # Return whatever exists so far; state signals it is not terminal.
            return read_output(thread_id, state_db, turn.turn_id if turn else None)
        time.sleep(interval)
