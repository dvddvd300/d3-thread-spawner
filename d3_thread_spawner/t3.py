"""T3 Code backend: authentication, thread creation, turn dispatch."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
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


SIGNING_KEY_FILENAME = "server-signing-key.bin"
DEFAULT_STATE_DB = "~/.t3/userdata/state.sqlite"
DEFAULT_SECRETS_DIR = "~/.t3/userdata/secrets"
_ORCHESTRATION_OPERATE_SCOPE = "orchestration:operate"


def _b64url_nopad(raw: bytes) -> str:
    """base64url-encode without ``=`` padding (matches T3's token encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _iso_to_epoch_ms(value: str) -> int:
    """Parse an ISO-8601 UTC timestamp (e.g. ``2026-07-06T15:33:34.020Z``) to
    integer epoch milliseconds, matching JS ``DateTime.epochMilliseconds``.

    Uses integer arithmetic (no float ``timestamp()``) so the millisecond
    component round-trips exactly — the HMAC covers these digits.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86_400_000
        + delta.seconds * 1000
        + delta.microseconds // 1000
    )


def _reconstruct_session_token(row: dict, signing_secret: bytes) -> str:
    """Rebuild the signed session token for an ``auth_sessions`` row.

    T3 stores only the session *claims* (not the token) in the DB, but the
    token is deterministic: ``base64url(JSON claims).base64url(HMAC-SHA256)``.
    We recreate the exact claims JSON (key order + compact separators must match
    T3's ``JSON.stringify``) and re-sign it with the server signing key, so
    ``SessionCredentialService.verify()`` accepts it.
    """
    claims = {
        "v": 1,
        "kind": "session",
        "sid": row["session_id"],
        "sub": row["subject"],
    }
    # Newer builds carry a `scopes` array; older builds a single `role`.
    if row.get("scopes") is not None:
        claims["scopes"] = json.loads(row["scopes"])
    elif row.get("role") is not None:
        claims["role"] = row["role"]
    claims["method"] = row["method"]
    claims["iat"] = _iso_to_epoch_ms(row["issued_at"])
    claims["exp"] = _iso_to_epoch_ms(row["expires_at"])

    payload = json.dumps(claims, separators=(",", ":"), ensure_ascii=False)
    encoded = _b64url_nopad(payload.encode("utf-8"))
    signature = _b64url_nopad(
        hmac.new(signing_secret, encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}"


def _token_from_state_db(state_db_path: str, secrets_dir: str) -> Optional[str]:
    """Reconstruct a T3 session token from the ``auth_sessions`` store.

    The updated T3 Code no longer writes a browser session cookie; its desktop
    shell authenticates via a bootstrap-issued bearer session persisted in
    ``state.sqlite``. We read the server signing key and the best usable session
    (unrevoked, unexpired, non-DPoP, ideally with ``orchestration:operate``) and
    re-sign its claims into a token the server will accept.

    Returns None on any problem (missing files/table/session), so callers fall
    back to the legacy cookie path.
    """
    key_path = os.path.expanduser(os.path.join(secrets_dir, SIGNING_KEY_FILENAME))
    db_path = os.path.expanduser(state_db_path)
    if not os.path.isfile(key_path) or not os.path.isfile(db_path):
        return None

    try:
        with open(key_path, "rb") as f:
            signing_secret = f.read()
        if not signing_secret:
            return None

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            columns = {
                c[1] for c in conn.execute("PRAGMA table_info(auth_sessions)")
            }
            if not columns:
                return None
            has_scopes = "scopes" in columns
            has_role = "role" in columns

            select_cols = ["session_id", "subject", "method", "issued_at", "expires_at"]
            if has_scopes:
                select_cols.insert(2, "scopes")
            elif has_role:
                select_cols.insert(2, "role")

            rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM auth_sessions "
                "WHERE revoked_at IS NULL AND method != 'dpop-access-token' "
                "ORDER BY issued_at DESC"
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None

    now_ms = int(time.time() * 1000)
    fallback: Optional[dict] = None
    for raw in rows:
        row = dict(zip(select_cols, raw))
        try:
            if _iso_to_epoch_ms(row["expires_at"]) <= now_ms:
                continue
        except (ValueError, TypeError):
            continue
        # Prefer a session that can actually drive orchestration; keep the first
        # (newest) usable session as a fallback if none advertise the scope.
        if has_scopes:
            try:
                scopes = json.loads(row["scopes"]) if row["scopes"] else []
            except (ValueError, TypeError):
                scopes = []
            if _ORCHESTRATION_OPERATE_SCOPE not in scopes:
                if fallback is None:
                    fallback = row
                continue
        try:
            return _reconstruct_session_token(row, signing_secret)
        except (ValueError, TypeError, KeyError):
            continue

    if fallback is not None:
        try:
            return _reconstruct_session_token(fallback, signing_secret)
        except (ValueError, TypeError, KeyError):
            return None
    return None


def _token_from_cookies(cookies_path: str, port: int) -> Optional[str]:
    """Try to read the session token from the Cookies SQLite DB.

    Works on macOS (advisory file locks). On Windows, Chromium holds an
    exclusive lock on the Cookies file, so this will return None.
    """
    db_path = os.path.expanduser(cookies_path)
    if not os.path.isfile(db_path):
        return None

    # SQLite URI format requires forward slashes
    db_uri_path = db_path.replace("\\", "/")

    for cookie_name in (f"t3_session_{port}", "t3_session"):
        try:
            conn = sqlite3.connect(f"file:{db_uri_path}?mode=ro", uri=True)
            cursor = conn.execute(
                "SELECT value FROM cookies WHERE name = ? AND host_key = '127.0.0.1'",
                (cookie_name,),
            )
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except (sqlite3.Error, PermissionError, OSError):
            continue
    return None


def get_t3_token(
    cookies_path: str,
    port: int,
    state_db: Optional[str] = None,
    secrets_dir: Optional[str] = None,
) -> str:
    """Extract a T3 session token accepted by the local API.

    Resolution order:
    1. D3TS_T3_TOKEN env var (explicit override)
    2. Reconstruct from the ``auth_sessions`` store in ``state.sqlite`` + the
       server signing key (the current T3 auth model — see below)
    3. Legacy Cookies SQLite DB (older T3 builds that set a browser cookie)

    Recent T3 Code no longer persists a ``t3_session_{port}`` browser cookie;
    the desktop authenticates via a bootstrap-issued bearer session recorded in
    ``state.sqlite``. We rebuild a valid signed token from that session's claims
    plus the raw signing key, which the server verifies identically to a token
    it issued itself. The token works over either the ``Cookie`` header or
    ``Authorization: Bearer``.
    """
    # Allow explicit token via env var
    env_token = os.environ.get("D3TS_T3_TOKEN")
    if env_token:
        log_verbose("🔑", "Using T3 token from D3TS_T3_TOKEN env var")
        return env_token

    # Reconstruct from the auth_sessions store (current T3 auth model)
    token = _token_from_state_db(
        state_db or DEFAULT_STATE_DB,
        secrets_dir or DEFAULT_SECRETS_DIR,
    )
    if token:
        log_verbose("🔑", "Reconstructed T3 token from auth_sessions store")
        return token

    # Legacy fallback: browser session cookie (older T3 builds)
    token = _token_from_cookies(cookies_path, port)
    if token:
        log_verbose("🔑", "Using T3 token from legacy Cookies DB")
        return token

    key_path = os.path.expanduser(
        os.path.join(secrets_dir or DEFAULT_SECRETS_DIR, SIGNING_KEY_FILENAME)
    )
    state_db_path = os.path.expanduser(state_db or DEFAULT_STATE_DB)
    if not os.path.isfile(state_db_path) or not os.path.isfile(key_path):
        raise RuntimeError(
            "Could not read a T3 session token.\n"
            f"  - auth_sessions DB: {state_db_path} "
            f"({'found' if os.path.isfile(state_db_path) else 'missing'})\n"
            f"  - signing key: {key_path} "
            f"({'found' if os.path.isfile(key_path) else 'missing'})\n"
            "Is T3 Code installed and has it been signed in at least once?\n"
            "Set D3TS_T3_TOKEN env var as an alternative."
        )

    raise RuntimeError(
        "Could not derive a T3 session token from the auth_sessions store.\n"
        "Is T3 Code running and signed in? Set D3TS_T3_TOKEN as an alternative."
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

    # T3 routes a model selection by `instanceId` (the provider driver/instance
    # split). For the built-in Claude driver the default instance id is literally
    # "claudeAgent". We also send the legacy `provider` field, which T3 promotes
    # to instanceId on decode — sending both keeps us compatible across versions.
    # `options` is the canonical [{id, value}] array, built per-model so we only
    # send options the chosen model supports.
    model_selection = {
        "instanceId": "claudeAgent",
        "provider": "claudeAgent",
        "model": s.resolved_model,
        "options": s.model_selection_options(),
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
