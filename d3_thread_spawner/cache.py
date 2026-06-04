"""Local SQLite cache of fetched PR review threads.

Keyed by ``repo + pr_number + scope`` and validated by the PR's ``updatedAt``
timestamp: a cached row is served only when the PR has not changed since it was
fetched. This lets repeated runs (the common "iterate on the same PRs" loop)
skip re-spending GitHub API budget on unchanged PRs.

``scope`` encodes the fetch filters (include_resolved / include_outdated /
reviewer) because the cached ``threads_json`` only contains the threads that
were in scope at fetch time. A run with a different scope is a cache miss.

The cache is strictly best-effort: any error (missing dir, locked db, corrupt
row) degrades to "no cache" rather than failing the run.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import List, Optional, Tuple

DEFAULT_CACHE_DIR = "~/.config/d3ts/cache"

# Returned raw-thread / pr-meta shapes match what github._build_pr_info consumes.
CachedEntry = Tuple[dict, List[dict], Optional[str]]  # (pr_meta, raw_threads, source)


def _db_path(cache_dir: str) -> str:
    d = os.path.expanduser(cache_dir or DEFAULT_CACHE_DIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "pr_threads.sqlite")


def _connect(cache_dir: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(cache_dir), timeout=3.0)
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pr_thread_cache (
            repo         TEXT    NOT NULL,
            pr_number    INTEGER NOT NULL,
            scope        TEXT    NOT NULL,
            updated_at   TEXT,
            etag         TEXT,
            pr_meta_json TEXT    NOT NULL,
            threads_json TEXT    NOT NULL,
            source       TEXT,
            fetched_at   TEXT,
            PRIMARY KEY (repo, pr_number, scope)
        )
        """
    )
    return conn


def scope_key(
    include_resolved: bool, include_outdated: bool, reviewer: Optional[str]
) -> str:
    """Stable string identifying the fetch filters a cache row was built under."""
    rev = (reviewer or "").lower().replace("[bot]", "")
    return (
        f"r={int(bool(include_resolved))};"
        f"o={int(bool(include_outdated))};"
        f"rev={rev}"
    )


def get_cached(
    cache_dir: str,
    repo: str,
    pr_number: int,
    scope: str,
    updated_at: Optional[str],
) -> Optional[CachedEntry]:
    """Return ``(pr_meta, raw_threads, source)`` for a *fresh* row, else ``None``.

    Freshness requires a known ``updated_at`` that matches the stored value.
    """
    if not updated_at:
        return None
    try:
        conn = _connect(cache_dir)
        try:
            row = conn.execute(
                "SELECT updated_at, pr_meta_json, threads_json, source "
                "FROM pr_thread_cache WHERE repo=? AND pr_number=? AND scope=?",
                (repo, pr_number, scope),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None

    if not row:
        return None
    cached_updated_at, pr_meta_json, threads_json, source = row
    if cached_updated_at != updated_at:
        return None
    try:
        return json.loads(pr_meta_json), json.loads(threads_json), source
    except Exception:
        return None


def put_cached(
    cache_dir: str,
    repo: str,
    pr_number: int,
    scope: str,
    updated_at: Optional[str],
    pr_meta: dict,
    raw_threads: List[dict],
    source: str,
    *,
    etag: Optional[str] = None,
    fetched_at: str = "",
) -> None:
    """Upsert a cache row. Best-effort: never raises."""
    try:
        conn = _connect(cache_dir)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO pr_thread_cache "
                "(repo, pr_number, scope, updated_at, etag, pr_meta_json, "
                " threads_json, source, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo,
                    pr_number,
                    scope,
                    updated_at,
                    etag,
                    json.dumps(pr_meta),
                    json.dumps(raw_threads),
                    source,
                    fetched_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # cache is best-effort
