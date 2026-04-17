"""Shared utilities: HTTP helpers, subprocess wrapper, slugify, logging."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen


# ── Subprocess ──────────────────────────────────────────────────────────────


def run(
    cmd: List[str],
    *,
    capture: bool = True,
    check: bool = True,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess command."""
    return subprocess.run(cmd, capture_output=capture, text=True, check=check, cwd=cwd)


# ── HTTP ────────────────────────────────────────────────────────────────────


def http_post(url: str, data: dict, headers: dict) -> dict:
    """HTTP POST using stdlib."""
    req = Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def http_get(url: str, headers: dict) -> dict:
    """HTTP GET using stdlib."""
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Text ────────────────────────────────────────────────────────────────────


def slugify(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a branch/session-safe slug."""
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"\(.*?\)", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:max_len]


def iso_now() -> str:
    """Current UTC timestamp in ISO format for T3 API."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── Logging ─────────────────────────────────────────────────────────────────

_verbose = False


def set_verbose(v: bool):
    global _verbose
    _verbose = v


def log(icon: str, msg: str):
    print(f"{icon} {msg}")


def log_verbose(icon: str, msg: str):
    if _verbose:
        print(f"{icon} {msg}")


def log_header(title: str):
    w = 60
    print(f"\n{'━' * w}")
    print(f"  {title}")
    print(f"{'━' * w}\n")
