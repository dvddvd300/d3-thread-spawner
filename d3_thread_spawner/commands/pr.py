"""pr command — Address PR review comments via T3 threads."""

from __future__ import annotations

from typing import Dict, List

from ..batch import launch_batch
from ..github import (
    GitHubRateLimitError,
    fetch_open_prs,
    fetch_prs_info,
    graphql_budget_low,
    reviewer_matches,
    set_graphql_disabled,
)
from ..models import AgentSettings, WorkItem
from ..prompts import (
    build_pr_review_chunk_prompt,
    build_pr_review_prompt,
    build_pr_thread_prompt,
    split_threads_into_chunks,
)
from ..util import log, slugify


def cmd_pr(args, settings: AgentSettings):
    """Address PR review comments."""

    if not settings.github_repo:
        log("❌", "GitHub repo not detected. Set [github] repo in config or use --repo.")
        return

    try:
        _cmd_pr(args, settings)
    except GitHubRateLimitError as e:
        print()
        log("🚫", str(e))
        log("💡", "Wait for the limit to reset, then retry (or pass --wait).")
        log("   ", "Check status: gh api rate_limit --jq '.resources.graphql'")


def _build_resume_command(args, skipped: List[int]) -> str:
    """Compose a ready-to-paste command that re-runs only the skipped PRs."""
    parts = ["d3-spawn pr", " ".join(str(n) for n in skipped)]
    if args.reviewer:
        parts.append(f"--reviewer {args.reviewer}")
    if args.include_resolved:
        parts.append("--include-resolved")
    if args.include_outdated:
        parts.append("--include-outdated")
    if args.per_thread:
        parts.append("--per-thread")
    parts.append("--wait")
    return " ".join(parts)


def _cmd_pr(args, settings: AgentSettings):
    """Inner implementation (separated so rate-limit errors propagate cleanly)."""
    owner = settings.github_owner
    name = settings.github_name

    # Reset the per-process GraphQL latch so a previous run can't bleed into this one.
    set_graphql_disabled(False)

    # --no-cache overrides config.
    if getattr(args, "no_cache", False):
        settings.cache = False

    # ── Determine which PRs to process ──
    pr_numbers: List[int] = []
    pr_hints: Dict[int, dict] = {}

    if args.pr_numbers:
        pr_numbers = args.pr_numbers
    elif args.open:
        log("🔍", "Fetching open PRs...")
        prs = fetch_open_prs(settings.github_repo, mine_only=args.mine)
        if not prs:
            log("⚠️ ", "No open PRs found.")
            return
        pr_numbers = [p["number"] for p in prs]
        pr_hints = {p["number"]: p for p in prs}
        log("✅", f"Found {len(prs)} open PR(s):")
        for p in prs:
            print(f"  #{p['number']} — {p['title']}")
        print()
    else:
        log("❌", "Specify PR numbers or use --open [--mine].")
        return

    # ── Preflight: if the GraphQL budget is already low, go straight to REST ──
    if graphql_budget_low():
        log("⚠️ ", "GraphQL budget low — using REST API for all PRs "
                   "(resolution status unavailable; showing all threads).")
        set_graphql_disabled(True)

    # ── Fetch review threads for all PRs (batched + cached) ──
    log("📥", f"Fetching review threads for {len(pr_numbers)} PR(s)...")
    infos, skipped = fetch_prs_info(
        owner, name, pr_numbers,
        include_resolved=args.include_resolved,
        include_outdated=args.include_outdated,
        reviewer=args.reviewer,
        settings=settings,
        pr_hints=pr_hints,
    )

    # ── Build work items ──
    items: List[WorkItem] = []

    for pr_num in pr_numbers:
        pr = infos.get(pr_num)
        if pr is None:
            continue  # skipped (rate-limited) — reported below

        # Filter by reviewer if specified
        threads = pr.threads
        if args.reviewer:
            threads = [t for t in threads if reviewer_matches(t.reviewer, args.reviewer)]

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
            # One agent per PR (all threads bundled), unless the rendered
            # prompt would exceed the max — then split into sibling chunks.
            prompt = build_pr_review_prompt(pr, threads)
            if len(prompt) <= settings.max_prompt_chars:
                items.append(WorkItem(
                    name=f"pr-{pr_num}-review",
                    branch=pr.branch,
                    prompt=prompt,
                    settings=settings,
                    create_branch=False,
                    worktree_from=None,
                ))
            else:
                chunks = split_threads_into_chunks(
                    pr, threads, settings.max_prompt_chars
                )
                total = len(chunks)
                log("✂️ ",
                    f"PR #{pr_num}: prompt {len(prompt):,} chars > "
                    f"{settings.max_prompt_chars:,} — splitting into {total} chunks")
                for i, chunk in enumerate(chunks, 1):
                    agent_branch = f"pr-{pr_num}/review-{i}of{total}"
                    items.append(WorkItem(
                        name=f"pr-{pr_num}-review-{i}of{total}",
                        branch=agent_branch,
                        prompt=build_pr_review_chunk_prompt(
                            pr, chunk, agent_branch, i, total
                        ),
                        settings=settings,
                        create_branch=True,
                        worktree_from=pr.branch,
                    ))

    # ── Partial-results notice ──
    if skipped:
        print()
        log("🚫", f"Rate limit hit — {len(skipped)} PR(s) not fetched: "
                  f"{', '.join('#' + str(n) for n in skipped)}")
        log("💡", "Resume the rest once the limit resets:")
        log("   ", _build_resume_command(args, skipped))

    if not items:
        if not skipped:
            log("⚠️ ", "No work items to launch.")
        return

    print()
    if not settings.dry_run and len(items) > 1:
        suffix = " (partial — rate limit hit)" if skipped else ""
        confirm = input(f"Launch {len(items)} thread(s){suffix}? [y/N] ").strip()
        if confirm.lower() != "y":
            log("⚠️ ", "Aborted.")
            return

    launch_batch(items, settings)
