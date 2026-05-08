"""Batch orchestration — launch work items in configurable batches."""

from __future__ import annotations

import time
from math import ceil
from typing import List, Tuple

from .models import AgentSettings, WorkItem
from .t3 import get_t3_token, launch_t3
from .util import log, log_header


def launch_batch(items: List[WorkItem], settings: AgentSettings) -> Tuple[int, int]:
    """Launch a batch of work items. Returns (created_count, failed_count)."""
    if not items:
        log("⚠️ ", "Nothing to launch.")
        return 0, 0

    total = len(items)
    bs = settings.batch_size
    total_batches = (total + bs - 1) // bs

    log_header(
        f"Launching {total} thread(s) "
        f"({total_batches} batch{'es' if total_batches > 1 else ''})"
    )

    if settings.dry_run:
        log("🏜️ ", "DRY RUN — nothing will be launched\n")
        for item in items:
            s = item.settings
            branch_info = f"→ {item.branch}"
            if item.create_branch:
                src = item.worktree_from or s.base_branch
                branch_info = f"→ NEW {item.branch} (from {src})"

            print(
                f"  [{s.model}|{s.mode}|{s.access}|{s.effort}|"
                f"ctx:{s.context_window}] "
                f"{item.name} {branch_info}"
            )
            preview = item.prompt.replace("\n", " ")[:120]
            print(f"    {preview}...")
            print()
        return 0, 0

    if settings.initial_wait > 0:
        deadline = time.monotonic() + settings.initial_wait * 60
        log("⏳", f"Waiting {settings.initial_wait}m before starting...")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            mins = ceil(remaining / 60)
            log("⏳", f"Initial wait: {mins}m remaining...")
            time.sleep(min(60, remaining))
        log("✅", "Wait complete — starting launches")

    t3_token = get_t3_token(settings.cookies_path, settings.t3_port)
    log("🔑", "Got T3 session token")

    created = 0
    failed = 0

    for batch_num in range(total_batches):
        batch_start = batch_num * bs
        batch = items[batch_start : batch_start + bs]

        # Wait between batches (not before the first one)
        if batch_num > 0 and settings.batch_delay > 0:
            wait = settings.batch_delay * 60
            log("⏳", f"Waiting {settings.batch_delay}m before batch {batch_num + 1}...")
            time.sleep(wait)
            # Refresh token in case session rotated
            t3_token = get_t3_token(settings.cookies_path, settings.t3_port)

        if total_batches > 1:
            log("▶ ", f"Batch {batch_num + 1}/{total_batches} — {len(batch)} thread(s)")

        for item in batch:
            try:
                thread_id = launch_t3(item, t3_token)
                log("🚀", f"{item.name} → {item.branch} (thread: {thread_id[:8]}...)")
                created += 1
            except Exception as e:
                log("❌", f"{item.name} — {e}")
                failed += 1

            time.sleep(settings.launch_delay)

    # Summary
    log_header("Summary")
    log("✅", f"Launched: {created}")
    if failed:
        log("❌", f"Failed:  {failed}")
    print("\n  Open T3 Code → all threads are in the sidebar.\n")

    return created, failed
