"""Git worktree management."""

from __future__ import annotations

import os
import re
from typing import Optional

from .util import log, run


def ensure_worktree(
    name: str,
    branch: str,
    repo_dir: str,
    *,
    create_branch: bool = False,
    base_branch: str = "main",
    worktree_from: Optional[str] = None,
    worktree_dir: str,
) -> str:
    """
    Create a git worktree for an agent. Returns the absolute worktree path.

    - create_branch=True: creates a new branch from base_branch or worktree_from
    - create_branch=False: checks out an existing branch (e.g., a PR branch)
    """
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "-", name)
    wt_path = os.path.normpath(os.path.join(worktree_dir, sanitized))

    if os.path.isdir(wt_path):
        log("♻️ ", f"Worktree exists: {wt_path}")
        return wt_path

    os.makedirs(worktree_dir, exist_ok=True)

    source = worktree_from or base_branch

    if create_branch:
        run(["git", "-C", repo_dir, "fetch", "origin", source], check=False)
        ref = f"origin/{source}"

        result = run(
            ["git", "-C", repo_dir, "worktree", "add", "-b", branch, wt_path, ref],
            check=False,
        )
        if result.returncode != 0:
            result = run(
                ["git", "-C", repo_dir, "worktree", "add", wt_path, branch],
                check=False,
            )
    else:
        run(["git", "-C", repo_dir, "fetch", "origin", branch], check=False)
        result = run(
            ["git", "-C", repo_dir, "worktree", "add", wt_path, branch],
            check=False,
        )
        if result.returncode != 0:
            result = run(
                ["git", "-C", repo_dir, "worktree", "add",
                 "--detach", wt_path, f"origin/{branch}"],
                check=False,
            )

    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed for '{branch}': {result.stderr.strip()}"
        )

    log("🌿", f"Worktree: {wt_path}")
    return wt_path
