"""conflicts command — Resolve merge conflicts across many PR branches at once.

Finds open PRs that GitHub reports as CONFLICTING (or the PRs you name) and
spawns one autonomous T3 thread per branch to merge in the base, resolve the
conflicts, verify, and push — one command, all the branches.
"""

from __future__ import annotations

from typing import List, Tuple

from ..batch import launch_batch
from ..github import (
    GitHubRateLimitError,
    fetch_prs_status,
    refresh_unknown_mergeable,
)
from ..models import AgentSettings, PRStatus, WorkItem
from ..prompts import build_conflict_resolution_prompt
from ..util import log, slugify


def resolve_strategy(args, settings: AgentSettings) -> str:
    """Decide merge vs rebase from flags, falling back to the configured default."""
    if getattr(args, "rebase", False):
        return "rebase"
    if getattr(args, "merge", False):
        return "merge"
    return settings.conflict_strategy


def effective_strategy(pr: PRStatus, strategy: str, settings: AgentSettings) -> str:
    """Per-PR strategy with the shared-branch safety guard applied.

    Rebasing + force-pushing a shared/long-lived head branch (``dev``, ``main``,
    ``release/*``) rewrites history that every open PR based on it — and every
    teammate's clone — depends on (this is what turned a 1-file PR into a 100k-LOC
    diff once). For such a branch ``merge`` is always safe and ``rebase`` never is,
    so we auto-downgrade ``rebase`` → ``merge`` unless ``conflict_rebase_protected``
    is set (the ``--force-rebase-protected`` flag).
    """
    if (
        strategy == "rebase"
        and settings.is_protected_branch(pr.branch)
        and not settings.conflict_rebase_protected
    ):
        return "merge"
    return strategy


def build_conflict_items(
    prs: List[PRStatus], settings: AgentSettings, strategy: str
) -> List[WorkItem]:
    """One WorkItem per conflicting PR, checked out on the PR's own branch.

    Applies the shared-branch guard (:func:`effective_strategy`) per PR, so a
    ``--rebase`` run never emits a force-push prompt for a protected branch.
    """
    items: List[WorkItem] = []
    for pr in prs:
        eff = effective_strategy(pr, strategy, settings)
        if eff != strategy:
            log("⚠️ ", f"PR #{pr.number}: '{pr.branch}' is a shared/long-lived branch — "
                       f"resolving via MERGE, not rebase (no force-push; preserves the "
                       f"history every dependent PR and clone relies on). "
                       f"Override: --force-rebase-protected.")
        leaf = pr.branch.split("/")[-1]
        slug = slugify(leaf, 30) or str(pr.number)
        items.append(WorkItem(
            name=f"conflict-{pr.number}-{slug}",
            branch=pr.branch,
            prompt=build_conflict_resolution_prompt(pr, eff),
            settings=settings,
            create_branch=False,   # check out the existing PR branch, don't fork
            worktree_from=None,
        ))
    return items


def conflicting_open(prs: List[PRStatus]) -> Tuple[List[PRStatus], List[PRStatus]]:
    """Split conflicting PRs into ``(open, closed_or_merged)``.

    Conflict resolution pushes to the PR branch, so closed/merged PRs are never
    resolved — they are returned separately so the caller can warn about them.
    """
    conflicting = [p for p in prs if p.conflicting]
    return (
        [p for p in conflicting if p.is_open],
        [p for p in conflicting if not p.is_open],
    )


def launch_conflict_resolution(
    prs: List[PRStatus], settings: AgentSettings, strategy: str
) -> None:
    """Build conflict work items for *prs* and launch them (with a confirm).

    Conflict launches are paced by ``[conflicts]`` batch overrides (falling back
    to the global ``[batch]`` settings), so a run can be slowed down — small
    batches, delays between them — independently of ordinary ``spawn`` batching.
    """
    settings = settings.for_conflict_batch()
    items = build_conflict_items(prs, settings, strategy)
    if not items:
        return

    # Always confirm: unlike the pr-review flow (which stops for a plan), these
    # agents resolve and push autonomously — and force-push under --rebase.
    print()
    if not settings.dry_run:
        confirm = input(
            f"Launch {len(items)} conflict-resolution thread(s) "
            f"[{strategy}]? [y/N] "
        ).strip()
        if confirm.lower() != "y":
            log("⚠️ ", "Aborted.")
            return

    launch_batch(items, settings)


def cmd_conflicts(args, settings: AgentSettings):
    """Resolve merge conflicts on conflicting PRs."""
    if not settings.github_repo:
        log("❌", "GitHub repo not detected. Set [github] repo in config or use --repo.")
        return

    strategy = resolve_strategy(args, settings)

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

    if not prs:
        log("⚠️ ", "No PRs to check.")
        return

    # GitHub computes mergeability asynchronously and may answer UNKNOWN at first;
    # refresh those so the conflicting/clean split is accurate.
    prs = refresh_unknown_mergeable(settings.github_repo, prs)

    conflicting, closed = conflicting_open(prs)
    unknown = [p for p in prs if p.mergeable == "UNKNOWN"]

    if closed:
        log("⚠️ ", f"{len(closed)} closed/merged PR(s) conflict but won't be touched: "
                   f"{', '.join('#' + str(p.number) for p in closed)}")

    if not conflicting:
        log("✅", f"No open conflicting PRs among {len(prs)} checked. 🎉")
        if unknown:
            log("⚠️ ", f"{len(unknown)} PR(s) had UNKNOWN mergeability (GitHub still "
                       f"computing): {', '.join('#' + str(p.number) for p in unknown)}")
        return

    log("🔴", f"{len(conflicting)} open PR(s) with merge conflicts "
              f"(default strategy: {strategy}):")
    for p in conflicting:
        eff = effective_strategy(p, strategy, settings)
        title = p.title[:50]
        downgrade = "" if eff == strategy else "  → MERGE (shared branch, no force-push)"
        print(f"    #{p.number} ← {p.branch}  (base: {p.base_branch})  {title}{downgrade}")

    if unknown:
        print()
        log("⚠️ ", f"{len(unknown)} PR(s) still UNKNOWN — skipped: "
                   f"{', '.join('#' + str(p.number) for p in unknown)}")

    launch_conflict_resolution(conflicting, settings, strategy)
