"""clean command — Remove finished worktrees and temp files."""

from __future__ import annotations

import os
import shutil
import tempfile

from ..models import AgentSettings
from ..util import log, run


def cmd_clean(args, settings: AgentSettings):
    """Remove finished worktrees and launcher scripts."""

    launchers_dir = os.path.join(tempfile.gettempdir(), "d3ts")
    worktree_dir = settings.worktree_dir

    # Clean launcher scripts
    if os.path.isdir(launchers_dir):
        count = len(os.listdir(launchers_dir))
        shutil.rmtree(launchers_dir)
        log("🧹", f"Removed {count} launcher files from {launchers_dir}")
    else:
        log("✅", "No launcher scripts to clean.")

    # Prune dead git worktrees
    if os.path.isdir(os.path.join(settings.repo_dir, ".git")):
        result = run(
            ["git", "-C", settings.repo_dir, "worktree", "prune"],
            check=False,
        )
        if result.returncode == 0:
            log("🧹", "Pruned dead git worktrees.")

    # List remaining worktrees
    if os.path.isdir(worktree_dir):
        wts = os.listdir(worktree_dir)
        if wts:
            log("📂", f"{len(wts)} worktree(s) in {worktree_dir}:")
            for wt in sorted(wts):
                print(f"    {wt}")
            print()

            if args.worktrees:
                confirm = input(
                    f"Remove ALL {len(wts)} worktrees? This is destructive! [y/N] "
                ).strip()
                if confirm.lower() != "y":
                    log("⚠️ ", "Aborted.")
                    return

                for wt in wts:
                    wt_full = os.path.join(worktree_dir, wt)
                    run(
                        ["git", "-C", settings.repo_dir, "worktree", "remove",
                         wt_full, "--force"],
                        check=False,
                    )
                    if os.path.isdir(wt_full):
                        shutil.rmtree(wt_full)
                    log("🗑️ ", f"Removed: {wt}")

                run(
                    ["git", "-C", settings.repo_dir, "worktree", "prune"],
                    check=False,
                )
        else:
            log("✅", "No worktrees to clean.")
    else:
        log("✅", "No worktrees directory.")
