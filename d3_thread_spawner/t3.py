"""T3 Code backend: authentication, thread creation, turn dispatch."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Optional

from .models import AgentSettings, WorkItem
from .util import http_post, iso_now, log, log_verbose
from .worktree import ensure_worktree


def auto_detect_t3_connection(runtime_json_path: str) -> tuple:
    """Read host/port from T3's server-runtime.json.

    Returns (host, port). Raises RuntimeError if T3 is not running.
    """
    path = os.path.expanduser(runtime_json_path)
    try:
        with open(path) as f:
            data = json.load(f)
        return data["host"], data["port"]
    except FileNotFoundError:
        raise RuntimeError(
            f"T3 Code does not appear to be running "
            f"(no server-runtime.json at {path}).\n"
            f"Start T3 Code first, or set host/port in config."
        )
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"Could not parse {path}: {e}")


def get_t3_token(cookies_path: str, port: int) -> str:
    """Extract T3 session token from the Cookies SQLite DB.

    T3 Code uses port-specific cookie names (e.g., t3_session_3773).
    Falls back to the generic t3_session if the port-specific one isn't found.
    """
    # Allow explicit token via env var
    env_token = os.environ.get("D3TS_T3_TOKEN")
    if env_token:
        log_verbose("🔑", "Using T3 token from D3TS_T3_TOKEN env var")
        return env_token

    db_path = os.path.expanduser(cookies_path)
    if not os.path.isfile(db_path):
        raise RuntimeError(
            f"T3 Cookies DB not found at {db_path}.\n"
            f"Is T3 Code installed? Set D3TS_T3_TOKEN env var as an alternative."
        )

    for cookie_name in (f"t3_session_{port}", "t3_session"):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.execute(
                "SELECT value FROM cookies WHERE name = ? AND host_key = '127.0.0.1'",
                (cookie_name,),
            )
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except sqlite3.Error:
            continue

    raise RuntimeError(
        "Could not get T3 session token from Cookies DB.\n"
        "Is T3 Code running? Set D3TS_T3_TOKEN env var as an alternative."
    )


def auto_detect_project_id(repo_dir: str, state_db_path: str) -> Optional[str]:
    """Query T3's state.sqlite to find the project matching this repo."""
    db_path = os.path.expanduser(state_db_path)
    if not os.path.isfile(db_path):
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        # Try exact match first
        row = conn.execute(
            "SELECT project_id FROM projection_projects "
            "WHERE workspace_root = ? AND deleted_at IS NULL",
            (repo_dir,),
        ).fetchone()
        if not row:
            # Try with trailing slash variation
            alt = repo_dir.rstrip("/")
            row = conn.execute(
                "SELECT project_id FROM projection_projects "
                "WHERE workspace_root = ? AND deleted_at IS NULL",
                (alt,),
            ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def resolve_project_id(settings: AgentSettings) -> str:
    """Resolve the T3 project ID from config or auto-detection."""
    if settings.t3_project_id:
        return settings.t3_project_id

    state_db = os.path.expanduser("~/.t3/userdata/state.sqlite")
    detected = auto_detect_project_id(settings.repo_dir, state_db)
    if detected:
        log_verbose("🔍", f"Auto-detected T3 project ID: {detected}")
        return detected

    raise RuntimeError(
        f"Could not auto-detect T3 project ID for repo: {settings.repo_dir}\n"
        f"Set project_id in your config file (.d3ts.toml) or use --project-id.\n"
        f"To find your project ID, check T3 Code's state.sqlite or the sidebar."
    )


def launch_t3(item: WorkItem, token: str) -> str:
    """Launch an agent as a T3 Code thread.

    Creates a worktree, dispatches thread.create, then thread.turn.start.
    Returns the thread ID.
    """
    s = item.settings
    now = iso_now()

    project_id = resolve_project_id(s)

    source = item.worktree_from or s.base_branch
    wt_path = ensure_worktree(
        name=item.name,
        branch=item.branch,
        repo_dir=s.repo_dir,
        create_branch=item.create_branch,
        base_branch=s.base_branch,
        worktree_from=source,
        worktree_dir=s.worktree_dir,
    )

    thread_id = str(uuid.uuid4())
    cmd_id = str(uuid.uuid4())
    turn_cmd_id = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())

    model_selection = {
        "provider": "claudeAgent",
        "model": s.resolved_model,
        "options": {
            "effort": s.effort,
            "contextWindow": s.context_window,
            "thinking": s.thinking,
            "fastMode": s.fast_mode,
        },
    }

    runtime_mode = {
        "full": "full-access",
        "auto-accept": "auto-accept-edits",
        "supervised": "approval-required",
    }.get(s.access, "full-access")

    interaction_mode = {
        "build": "default",
        "plan": "plan",
    }.get(s.mode, "default")

    headers = {
        "Cookie": f"t3_session_{s.t3_port}={token}; t3_session={token}",
        "Content-Type": "application/json",
    }

    # Step 1: Create thread
    http_post(f"{s.t3_api}/api/orchestration/dispatch", {
        "type": "thread.create",
        "commandId": cmd_id,
        "threadId": thread_id,
        "projectId": project_id,
        "title": item.name,
        "modelSelection": model_selection,
        "runtimeMode": runtime_mode,
        "interactionMode": interaction_mode,
        "branch": item.branch,
        "worktreePath": wt_path,
        "createdAt": now,
    }, headers)

    time.sleep(0.3)

    # Step 2: Start turn with prompt
    http_post(f"{s.t3_api}/api/orchestration/dispatch", {
        "type": "thread.turn.start",
        "commandId": turn_cmd_id,
        "threadId": thread_id,
        "message": {
            "messageId": msg_id,
            "role": "user",
            "text": item.prompt,
            "attachments": [],
        },
        "runtimeMode": runtime_mode,
        "interactionMode": interaction_mode,
        "createdAt": now,
    }, headers)

    return thread_id
