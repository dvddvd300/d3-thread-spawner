"""output command — View or wait for a spawned thread's reply."""

from __future__ import annotations

import json
import sys

from ..models import AgentSettings
from ..reader import (
    TurnStatus,
    latest_turn,
    read_output,
    resolve_thread_id,
    wait_for_output,
)
from ..util import log, log_header


def cmd_output(args, settings: AgentSettings):
    """Read back the assistant output of a thread spawned earlier.

    `spawn` returns a thread id (see also `status`); pass it (full or the short
    8-char prefix) here to view what the agent produced. `--wait` blocks until
    the current turn finishes.
    """
    thread_id = resolve_thread_id(args.thread_id)

    if args.wait:
        last_state = {"v": None}

        def on_tick(turn: TurnStatus):
            if turn.state != last_state["v"]:
                last_state["v"] = turn.state
                log("⏳", f"turn {(turn.turn_id or '?')[:8]} … {turn.state}")

        result = wait_for_output(
            thread_id,
            timeout=args.timeout,
            interval=args.interval,
            on_tick=on_tick,
        )
    else:
        result = read_output(thread_id)

    if args.json:
        print(json.dumps({
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "state": result.state,
            "completed_at": result.completed_at,
            "message_count": result.message_count,
            "text": result.text,
        }, indent=2))
        return

    log_header(f"Thread {thread_id[:8]} — {result.state}")
    if result.text:
        print(result.text)
    else:
        turn = latest_turn(thread_id)
        if turn is None:
            log("⚠️ ", "No turns for this thread yet.")
        else:
            log("⚠️ ", f"No assistant output yet (turn state: {result.state}). "
                       f"Re-run with --wait to block until it finishes.")

    if not result.is_terminal and not args.wait:
        # Non-zero-ish signal without failing the process: nudge toward --wait.
        print("", file=sys.stderr)
