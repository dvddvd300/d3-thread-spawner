"""review command — Spawn a local reviewer thread per PR.

Where the ``pr`` command *addresses* existing review comments, ``review``
*generates* the review: it spawns one autonomous T3 thread per pull request that
acts as a senior code reviewer. Each thread checks out the PR branch read-only,
follows the bundled (or a custom) review methodology, and posts a full review —
verdict, paste-ready comments tagged 🔴/🟡/🟢, and action items — as its output,
so you can read each review on its own thread.
"""

from __future__ import annotations

from typing import List

from ..batch import launch_batch
from ..github import GitHubRateLimitError, fetch_prs_status
from ..models import AgentSettings, PRStatus, WorkItem
from ..prompts import build_pr_local_review_prompt, load_review_guide
from ..util import log, slugify


def build_review_items(
    prs: List[PRStatus], settings: AgentSettings, review_guide: str
) -> List[WorkItem]:
    """One read-only review WorkItem per PR, checked out on the PR's own branch."""
    items: List[WorkItem] = []
    for pr in prs:
        leaf = pr.branch.split("/")[-1]
        slug = slugify(leaf, 30) or str(pr.number)
        items.append(WorkItem(
            name=f"review-{pr.number}-{slug}",
            branch=pr.branch,
            prompt=build_pr_local_review_prompt(pr, review_guide),
            settings=settings,
            create_branch=False,   # check out the existing PR branch (read-only)
            worktree_from=None,
        ))
    return items


def cmd_review(args, settings: AgentSettings):
    """Spawn a local reviewer thread for each selected PR."""
    if not settings.github_repo:
        log("❌", "GitHub repo not detected. Set [github] repo in config or use --repo.")
        return

    if not args.pr_numbers and not args.open:
        log("❌", "Specify PR numbers or use --open [--mine].")
        return

    # Load the reviewer methodology up front so a bad --review-prompt path fails
    # before we spend a GitHub call.
    try:
        review_guide = load_review_guide(settings.review_prompt_file)
    except RuntimeError as e:
        log("❌", str(e))
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

    if not prs:
        log("⚠️ ", "No PRs to review.")
        return

    # A merged PR whose head branch was deleted (common after a squash-merge)
    # has no branch to check out — skip it rather than emit a doomed worktree.
    no_branch = [p for p in prs if not p.branch]
    if no_branch:
        log("⚠️ ", f"{len(no_branch)} PR(s) have no head branch (deleted?) — skipping: "
                   f"{', '.join('#' + str(p.number) for p in no_branch)}")
        prs = [p for p in prs if p.branch]
    if not prs:
        log("⚠️ ", "No reviewable PRs (all had deleted head branches).")
        return

    # With --open every PR is already open. When PRs are named explicitly we
    # allow any state — reviewing a closed/merged PR is read-only and harmless —
    # but flag it, since the branch may have moved or been deleted.
    closed = [p for p in prs if not p.is_open]
    if closed:
        log("⚠️ ", f"{len(closed)} named PR(s) are closed/merged — reviewing anyway: "
                   f"{', '.join('#' + str(p.number) for p in closed)}")

    log("🔍", f"Spawning {len(prs)} local reviewer thread(s):")
    for p in prs:
        print(f"    #{p.number} ← {p.branch}  (base: {p.base_branch})  {p.title[:50]}")

    items = build_review_items(prs, settings, review_guide)

    print()
    if not settings.dry_run and len(items) > 1:
        confirm = input(f"Launch {len(items)} reviewer thread(s)? [y/N] ").strip()
        if confirm.lower() != "y":
            log("⚠️ ", "Aborted.")
            return

    launch_batch(items, settings)
