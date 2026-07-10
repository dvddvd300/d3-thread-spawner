"""approve-plans command — schedule captured T3 plan implementation turns."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from math import ceil
from typing import Callable, List, Optional

from ..models import AgentSettings
from ..plan_approval import (
    ApprovalResult,
    PlanCandidate,
    approve_plan,
    batch_turn_errors,
    evaluate_quota,
    freeze_plans,
    read_rate_limit_events,
)
from ..t3 import auto_detect_project_id, get_t3_token
from ..util import log, log_header


def _parse_start_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(
            "--start-at must be an ISO 8601 timestamp with a UTC offset"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("--start-at must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _wait_until(
    deadline: datetime,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    label: str = "Scheduled start",
) -> None:
    while True:
        remaining = (deadline - now()).total_seconds()
        if remaining <= 0:
            return
        log("⏳", f"{label}: {ceil(remaining / 60)}m remaining...")
        sleep(min(60, remaining))


def _wait_minutes(
    minutes: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    label: str,
) -> None:
    deadline = monotonic() + minutes * 60
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        log("⏳", f"{label}: {ceil(remaining / 60)}m remaining...")
        sleep(min(60, remaining))


def _project_id(settings: AgentSettings) -> str:
    if settings.t3_project_id:
        return settings.t3_project_id
    detected = auto_detect_project_id(settings.repo_dir, settings.t3_state_db)
    if detected:
        return detected
    raise RuntimeError(
        f"Could not find a T3 project for {settings.repo_dir}. "
        "Use --project-id or open the repository in T3 Code."
    )


def _provider_log_dir(state_db: str) -> str:
    userdata = os.path.dirname(os.path.abspath(os.path.expanduser(state_db)))
    return os.path.join(userdata, "logs", "provider")


def _token(settings: AgentSettings) -> str:
    return get_t3_token(
        settings.cookies_path,
        settings.t3_port,
        settings.t3_state_db,
        settings.t3_secrets_dir,
    )


def _print_manifest(plans: List[PlanCandidate], batch_size: int) -> None:
    log_header(f"Frozen plan manifest ({len(plans)})")
    for index, plan in enumerate(plans, 1):
        batch = (index - 1) // batch_size + 1
        branch = f" [{plan.branch}]" if plan.branch else ""
        status = plan.session_status or "no-session"
        print(
            f"  {index:>2}. batch {batch}  {plan.short_id}  "
            f"{plan.title}{branch} ({status})"
        )


def _print_summary(
    plans: List[PlanCandidate],
    results: List[ApprovalResult],
    halted_reason: str,
) -> None:
    approved = [result for result in results if result.status == "approved"]
    skipped = [result for result in results if result.status == "skipped"]
    failed = [result for result in results if result.status == "failed"]
    handled_ids = {result.thread_id for result in approved + skipped}
    remaining = [plan for plan in plans if plan.thread_id not in handled_ids]

    log_header("Plan approval summary")
    log("✅", f"Approved: {len(approved)}")
    log("↪ ", f"Skipped:  {len(skipped)}")
    log("❌", f"Failed:   {len(failed)}")
    log("⏸ ", f"Remaining: {len(remaining)}")
    if halted_reason:
        log("⏸ ", f"Stopped: {halted_reason}")
    if skipped:
        print("\n  Skipped:")
        for result in skipped:
            print(f"    {result.thread_id[:8]}  {result.reason}")
    if failed:
        print("\n  Failed:")
        for result in failed:
            print(f"    {result.thread_id[:8]}  {result.reason}")
    if remaining:
        print("\n  Still pending from the frozen manifest:")
        for plan in remaining:
            print(f"    {plan.short_id}  {plan.title}")


def cmd_approve_plans(args, settings: AgentSettings) -> None:
    """Approve frozen T3 plans in quota-aware batches."""
    if settings.batch_size <= 0:
        raise RuntimeError("--batch-size must be greater than zero")
    if settings.batch_delay < 0:
        raise RuntimeError("--batch-delay cannot be negative")
    if settings.launch_delay < 0:
        raise RuntimeError("--launch-delay cannot be negative")
    threshold = float(args.quota_threshold)
    if threshold <= 0 or threshold > 100:
        raise RuntimeError("--quota-threshold must be greater than 0 and at most 100")

    start_at = _parse_start_at(args.start_at)
    if start_at is not None and settings.initial_wait > 0:
        raise RuntimeError("Use either --start-at or --initial-wait, not both")

    plans = freeze_plans(
        _project_id(settings), settings.t3_state_db, args.thread_refs
    )
    if not plans:
        log("⚠️ ", "No actionable, unimplemented plans found.")
        return
    _print_manifest(plans, settings.batch_size)

    if settings.dry_run:
        log("🏜️ ", "DRY RUN — no wait, authentication, or approval requests performed")
        return

    if not args.yes:
        confirm = input(
            f"Schedule approval of these {len(plans)} frozen plan(s)? [y/N] "
        ).strip()
        if confirm.lower() not in {"y", "yes"}:
            log("⏹ ", "Cancelled.")
            return

    if start_at is not None:
        local = start_at.astimezone()
        log("🕒", f"First batch scheduled for {local.isoformat(timespec='seconds')}")
        _wait_until(start_at)
    elif settings.initial_wait > 0:
        log("🕒", f"Waiting {settings.initial_wait}m before the first batch")
        _wait_minutes(settings.initial_wait, label="Initial wait")

    results: List[ApprovalResult] = []
    halted_reason = ""
    log_dir = _provider_log_dir(settings.t3_state_db)
    total_batches = (len(plans) + settings.batch_size - 1) // settings.batch_size

    for batch_index in range(total_batches):
        start = batch_index * settings.batch_size
        batch = plans[start : start + settings.batch_size]
        batch_started = datetime.now(timezone.utc)
        batch_results: List[ApprovalResult] = []
        log_header(
            f"Approval batch {batch_index + 1}/{total_batches} ({len(batch)} plans)"
        )

        try:
            token = _token(settings)
        except Exception as exc:
            halted_reason = f"could not refresh T3 token: {exc}"
            break

        for item_index, plan in enumerate(batch):
            result = approve_plan(
                plan,
                settings,
                token,
                lambda: _token(settings),
            )
            batch_results.append(result)
            results.append(result)
            if result.status == "approved":
                log("✅", f"{plan.short_id}  {plan.title}")
            elif result.status == "skipped":
                log("↪ ", f"{plan.short_id} skipped — {result.reason}")
            else:
                log("❌", f"{plan.short_id} failed — {result.reason}")
                halted_reason = result.reason
                break
            if item_index < len(batch) - 1 and settings.launch_delay > 0:
                time.sleep(settings.launch_delay)

        if halted_reason:
            break
        if batch_index == total_batches - 1:
            break

        approved_in_batch = [
            result for result in batch_results if result.status == "approved"
        ]
        if not approved_in_batch:
            halted_reason = "batch approved no plans, so quota could not be evaluated"
            break

        _wait_minutes(
            settings.batch_delay,
            label=f"Quota check before batch {batch_index + 2}",
        )
        errors = batch_turn_errors(
            settings.t3_state_db, approved_in_batch, batch_started
        )
        if errors:
            halted_reason = "; ".join(errors)
            break
        quota = evaluate_quota(
            read_rate_limit_events(log_dir, batch_started), threshold
        )
        if not quota.can_continue:
            halted_reason = quota.reason
            break
        log("✅", f"Quota gate passed — {quota.reason} ({quota.signal_count} signals)")

    _print_summary(plans, results, halted_reason)
