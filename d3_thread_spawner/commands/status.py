"""status command — Show running T3 threads."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from ..models import AgentSettings
from ..util import log, log_header


def cmd_status(args, settings: AgentSettings):
    """List active T3 Code threads."""
    state_db = os.path.expanduser("~/.t3/userdata/state.sqlite")

    if not os.path.isfile(state_db):
        log("⚠️ ", "T3 state database not found. Is T3 Code installed?")
        return

    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)

        # Get active threads (not deleted)
        rows = conn.execute("""
            SELECT t.thread_id, t.title, t.branch, t.worktree_path,
                   t.created_at, p.title as project_title
            FROM projection_threads t
            LEFT JOIN projection_projects p ON t.project_id = p.project_id
            WHERE t.deleted_at IS NULL AND t.archived_at IS NULL
            ORDER BY t.created_at DESC
            LIMIT 50
        """).fetchall()

        conn.close()
    except sqlite3.Error as e:
        log("❌", f"Could not read T3 state: {e}")
        return

    if not rows:
        log("⚠️ ", "No active T3 threads found.")
        return

    log_header(f"T3 Threads ({len(rows)})")

    for row in rows:
        thread_id, title, branch, wt_path, created_at, proj = row
        short_id = thread_id[:8] if thread_id else "?"
        branch_str = f" [{branch}]" if branch else ""
        proj_str = f" ({proj})" if proj else ""
        time_str = ""
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                time_str = f" @ {dt.strftime('%m/%d %H:%M')}"
            except (ValueError, AttributeError):
                pass

        print(f"  🤖 {title or short_id}{branch_str}{proj_str}{time_str}")

    print("\n  Open T3 Code sidebar to manage threads.\n")
