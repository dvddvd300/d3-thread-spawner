"""triage command — Classify open PRs at a glance, optionally resolve conflicts.

Prints a grouped status report (conflicts / CI failing / changes requested /
behind / awaiting review / ready / draft) for every open PR, then — with
``--resolve-conflicts`` — hands the conflicting ones to the conflict-resolution
flow (shared with the ``conflicts`` command).
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from ..github import (
    GitHubRateLimitError,
    fetch_prs_status,
    refresh_unknown_mergeable,
)
from ..models import AgentSettings, PRStatus
from ..util import log, log_header
from .conflicts import conflicting_open, launch_conflict_resolution, resolve_strategy


# Category order = most-actionable first. Each entry is (key, icon, label).
_CATEGORIES: List[Tuple[str, str, str]] = [
    ("conflicts", "🔴", "CONFLICTS"),
    ("ci_failing", "🟠", "CI FAILING"),
    ("changes_requested", "🟡", "CHANGES REQUESTED"),
    ("behind", "🔵", "BEHIND BASE"),
    ("ci_pending", "⏳", "CI PENDING"),
    ("review_required", "👀", "AWAITING REVIEW"),
    ("ready", "🟢", "READY TO MERGE"),
    ("draft", "📝", "DRAFT"),
    ("other", "⚪", "OTHER"),
]
_LABELS: Dict[str, Tuple[str, str]] = {k: (icon, label) for k, icon, label in _CATEGORIES}


def triage_category(p: PRStatus) -> str:
    """Map a PRStatus to a single primary triage category key.

    Drafts are reported separately; otherwise the most actionable signal wins
    (conflicts > failing CI > changes requested > behind > pending > ...).
    """
    if p.is_draft:
        return "draft"
    if p.conflicting:
        return "conflicts"
    if p.ci_failing:
        return "ci_failing"
    if p.review_decision == "CHANGES_REQUESTED":
        return "changes_requested"
    if p.merge_state == "BEHIND":
        return "behind"
    if p.ci_state == "PENDING":
        return "ci_pending"
    if p.review_decision == "REVIEW_REQUIRED":
        return "review_required"
    if (
        p.mergeable == "MERGEABLE"
        and p.ci_state in ("SUCCESS", "NONE")
        and p.review_decision in ("APPROVED", "")
    ):
        return "ready"
    return "other"


def _badges(p: PRStatus) -> str:
    """Compact CI + review badges for a PR line."""
    parts: List[str] = []
    ci = {"SUCCESS": "ci:✅", "FAILURE": "ci:❌", "PENDING": "ci:⏳"}.get(p.ci_state, "")
    if ci:
        parts.append(ci)
    rev = {
        "APPROVED": "rev:✅",
        "CHANGES_REQUESTED": "rev:✋",
        "REVIEW_REQUIRED": "rev:👀",
    }.get(p.review_decision, "")
    if rev:
        parts.append(rev)
    return "  ".join(parts)


def _print_report(prs: List[PRStatus], repo: str) -> None:
    log_header(f"Triage: {repo} — {len(prs)} open PR(s)")
    if not prs:
        log("⚠️ ", "No open PRs.")
        return

    grouped: Dict[str, List[PRStatus]] = {}
    for p in prs:
        grouped.setdefault(triage_category(p), []).append(p)

    summary: List[str] = []
    for key, icon, label in _CATEGORIES:
        members = grouped.get(key)
        if not members:
            continue
        summary.append(f"{len(members)} {label.lower()}")
        print(f"{icon} {label} ({len(members)})")
        for p in sorted(members, key=lambda x: x.number):
            badges = _badges(p)
            line = f"    #{p.number} ← {p.branch}   {p.title[:50]}   @{p.author}"
            if badges:
                line += f"   {badges}"
            print(line.rstrip())
        print()

    print(f"  Summary: {' · '.join(summary)}")
    conflicts = grouped.get("conflicts")
    if conflicts:
        nums = " ".join(str(p.number) for p in conflicts)
        log("💡", f"Resolve all {len(conflicts)} conflict(s): "
                  f"d3-spawn conflicts   (or: d3-spawn triage --resolve-conflicts)")
        log("   ", f"Conflicting PRs: {nums}")
    print()


def cmd_triage(args, settings: AgentSettings):
    """Show a triage report for open PRs; optionally launch conflict resolution."""
    if not settings.github_repo:
        log("❌", "GitHub repo not detected. Set [github] repo in config or use --repo.")
        return

    try:
        prs = fetch_prs_status(
            settings.github_repo,
            pr_numbers=args.pr_numbers or None,
            mine_only=args.mine,
        )
    except GitHubRateLimitError as e:
        print()
        log("🚫", str(e))
        log("💡", "Wait for the limit to reset, then retry.")
        log("   ", "Check status: gh api rate_limit --jq '.resources.graphql'")
        return

    # Resolve async-UNKNOWN mergeability so the conflicts row is trustworthy.
    prs = refresh_unknown_mergeable(settings.github_repo, prs)

    _print_report(prs, settings.github_repo)

    if getattr(args, "resolve_conflicts", False):
        conflicting, closed = conflicting_open(prs)
        if closed:
            log("⚠️ ", f"{len(closed)} closed/merged PR(s) conflict but won't be touched: "
                       f"{', '.join('#' + str(p.number) for p in closed)}")
        if not conflicting:
            log("✅", "No open conflicting PRs to resolve.")
            return
        launch_conflict_resolution(conflicting, settings, resolve_strategy(args, settings))
