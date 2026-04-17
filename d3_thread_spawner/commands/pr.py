"""pr command — Address PR review comments via T3 threads."""

from __future__ import annotations

from typing import List

from ..batch import launch_batch
from ..github import fetch_open_prs, fetch_pr_info
from ..models import AgentSettings, WorkItem
from ..prompts import build_pr_review_prompt, build_pr_thread_prompt
from ..util import log, slugify


def cmd_pr(args, settings: AgentSettings):
    """Address PR review comments."""

    if not settings.github_repo:
        log("❌", "GitHub repo not detected. Set [github] repo in config or use --repo.")
        return

    owner = settings.github_owner
    name = settings.github_name

    # ── Determine which PRs to process ──
    pr_numbers: List[int] = []

    if args.pr_numbers:
        pr_numbers = args.pr_numbers
    elif args.open:
        log("🔍", "Fetching open PRs...")
        prs = fetch_open_prs(settings.github_repo, mine_only=args.mine)
        if not prs:
            log("⚠️ ", "No open PRs found.")
            return
        pr_numbers = [p["number"] for p in prs]
        log("✅", f"Found {len(prs)} open PR(s):")
        for p in prs:
            print(f"  #{p['number']} — {p['title']}")
        print()
    else:
        log("❌", "Specify PR numbers or use --open [--mine].")
        return

    # ── Fetch review threads and build work items ──
    items: List[WorkItem] = []

    for pr_num in pr_numbers:
        log("📥", f"PR #{pr_num}: fetching review threads...")

        try:
            pr = fetch_pr_info(
                owner, name, pr_num,
                include_resolved=args.include_resolved,
                include_outdated=args.include_outdated,
            )
        except Exception as e:
            log("❌", f"PR #{pr_num}: failed to fetch — {e}")
            continue

        # Filter by reviewer if specified
        threads = pr.threads
        if args.reviewer:
            reviewer_lower = args.reviewer.lower().replace("[bot]", "")
            threads = [
                t for t in threads
                if reviewer_lower in t.reviewer.lower()
            ]

        if not threads:
            log("⚠️ ", f"PR #{pr_num}: no matching unresolved threads")
            continue

        log("📋", f"PR #{pr_num}: {len(threads)} unresolved thread(s):")
        for t in threads:
            icon = "🟡" if not t.is_resolved else "🟢"
            has_ai = " 🤖" if t.ai_prompt else ""
            print(f"    {icon} @{t.reviewer} → {t.path}:{t.line or '?'}{has_ai}")

        if args.per_thread:
            # One agent per thread
            for i, thread in enumerate(threads, 1):
                file_slug = slugify(thread.path.split("/")[-1], 20)
                agent_name = f"pr-{pr_num}-t{i}-{file_slug}"
                agent_branch = f"pr-{pr_num}/fix-t{i}"

                items.append(WorkItem(
                    name=agent_name,
                    branch=agent_branch,
                    prompt=build_pr_thread_prompt(pr, thread, agent_branch),
                    settings=settings,
                    create_branch=True,
                    worktree_from=pr.branch,
                ))
        else:
            # One agent per PR (all threads bundled)
            agent_name = f"pr-{pr_num}-review"
            items.append(WorkItem(
                name=agent_name,
                branch=pr.branch,
                prompt=build_pr_review_prompt(pr, threads),
                settings=settings,
                create_branch=False,
                worktree_from=None,
            ))

    if not items:
        log("⚠️ ", "No work items to launch.")
        return

    print()
    if not settings.dry_run and len(items) > 1:
        confirm = input(f"Launch {len(items)} thread(s)? [y/N] ").strip()
        if confirm.lower() != "y":
            log("⚠️ ", "Aborted.")
            return

    launch_batch(items, settings)
