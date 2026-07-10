"""CLI parser and main entry point."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from dataclasses import replace

from . import __version__
from .config import load_config
from .util import log, set_verbose


def _add_conflict_strategy_flags(p: argparse.ArgumentParser) -> None:
    """Add mutually-exclusive --merge/--rebase flags to a conflict-aware parser."""
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--merge", action="store_true",
        help="Resolve by merging base into the branch (no force-push; default)",
    )
    grp.add_argument(
        "--rebase", action="store_true",
        help="Resolve by rebasing the branch onto base (force-pushes with lease)",
    )
    p.add_argument(
        "--force-rebase-protected", action="store_true",
        help="Allow --rebase to force-push a shared/long-lived branch "
             "(dev, main, release/*). Off by default: such branches auto-downgrade "
             "to merge so dependent PRs and clones aren't broken.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="d3-spawn",
        description="Programmatic T3 Code thread launcher.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s spawn "Refactor the auth middleware"
              %(prog)s spawn --file prompt.txt --name refactor-auth
              %(prog)s spawn --from-file tasks.jsonl --batch-size 10
              %(prog)s output 1a2b3c4d --wait
              %(prog)s pr 58 --reviewer coderabbitai
              %(prog)s pr --open --mine
              %(prog)s review --open --mine
              %(prog)s triage
              %(prog)s conflicts
              %(prog)s approve-plans --start-at 2026-07-10T02:50:00-06:00 --yes
              %(prog)s status
              %(prog)s config --init
        """),
    )

    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )

    # ── Global flags ──
    parser.add_argument(
        "--model", default=None,
        help="Model alias or full ID. Aliases: opus → claude-opus-4-8 "
             "(default), sonnet, haiku, mini → gpt-5.4-mini. gpt-* IDs route "
             "to the Codex provider (e.g. gpt-5.5, gpt-5.4)",
    )
    parser.add_argument(
        "--mode", choices=["build", "plan"], default=None,
        help="Interaction mode: build=act immediately, "
             "plan=research and propose first (default: build)",
    )
    parser.add_argument(
        "--access", choices=["full", "auto-accept", "supervised"], default=None,
        help="Access level: full=no prompts, auto-accept=approve edits only, "
             "supervised=approve everything (default: full)",
    )
    parser.add_argument(
        "--effort",
        default=None,
        help="Reasoning effort (default: high). Availability varies by model — "
             "unsupported or unknown values are normalized to the highest real "
             "effort for the chosen model before launch.",
    )
    parser.add_argument(
        "--context-window", default=None,
        help="Context window size (default: 1m). Unsupported values normalize "
             "to the largest context the chosen model exposes; models without "
             "context-window support use 200k.",
    )
    parser.add_argument(
        "--thinking", action="store_true", default=None, dest="thinking",
        help="Enable thinking mode (default: on; only Haiku 4.5 exposes this)",
    )
    parser.add_argument(
        "--no-thinking", action="store_false", dest="thinking",
        help="Disable thinking mode",
    )
    parser.add_argument(
        "--fast-mode", action="store_true", default=None, dest="fast_mode",
        help="Enable fast mode (only Opus 4.5/4.6 expose this)",
    )
    parser.add_argument(
        "--no-fast-mode", action="store_false", dest="fast_mode",
        help="Disable fast mode (default)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="Threads per batch (default: 5)",
    )
    parser.add_argument(
        "--batch-delay", type=int, default=None,
        help="Minutes between batches (default: 0)",
    )
    parser.add_argument(
        "--launch-delay", type=float, default=None,
        help="Seconds between individual launches (default: 0.5)",
    )
    parser.add_argument(
        "--initial-wait", type=int, default=None,
        help="Minutes to wait before launching the first batch (default: 0)",
    )
    parser.add_argument(
        "--base-branch", default=None,
        help="Base git branch (default: main)",
    )
    parser.add_argument(
        "--repo", default=None,
        help="Path to the repo (default: current directory)",
    )
    parser.add_argument(
        "--project-id", default=None,
        help="T3 project UUID (auto-detected if omitted)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Show what would be launched without doing it",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="Verbose output",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to config file (overrides auto-detection)",
    )

    subs = parser.add_subparsers(dest="command", required=True)

    # ── spawn ──
    p_spawn = subs.add_parser(
        "spawn",
        help="Launch T3 threads with prompts",
        description="Launch one or more T3 Code threads with a prompt.",
    )
    p_spawn.add_argument(
        "prompt", nargs="?",
        help="The prompt text (inline)",
    )
    p_spawn.add_argument(
        "--file", "-f",
        help="Read the prompt from a file",
    )
    p_spawn.add_argument(
        "--from-file",
        help="Batch launch from a JSONL file (one task per line)",
    )
    p_spawn.add_argument(
        "--template", "-t",
        help="Prompt template file (supports {task} variable)",
    )
    p_spawn.add_argument(
        "--var", action="append",
        help="Template variable: --var key=value (repeatable)",
    )
    p_spawn.add_argument(
        "--name",
        help="Thread name (auto-generated if omitted)",
    )
    p_spawn.add_argument(
        "--branch",
        help="Work on this existing branch (no new branch created)",
    )
    p_spawn.add_argument(
        "--new-branch",
        help="Create a new branch with this name",
    )
    p_spawn.add_argument(
        "--fork-from",
        help="Branch to fork from (use with --new-branch; default: base branch)",
    )

    # ── output ──
    p_output = subs.add_parser(
        "output",
        help="View or wait for a spawned thread's reply",
        description=(
            "Read back the assistant output of a thread spawned earlier. "
            "`spawn` dispatches a turn and returns a thread id but does not read "
            "the reply; this reads it from T3's local state DB (read-only)."
        ),
    )
    p_output.add_argument(
        "thread_id",
        help="Thread id from `spawn`/`status` (full UUID or short prefix)",
    )
    p_output.add_argument(
        "--wait", action="store_true",
        help="Block until the current turn finishes (completed/error)",
    )
    p_output.add_argument(
        "--timeout", type=float, default=600.0,
        help="Max seconds to wait with --wait (default: 600)",
    )
    p_output.add_argument(
        "--interval", type=float, default=3.0,
        help="Seconds between polls with --wait (default: 3)",
    )
    p_output.add_argument(
        "--json", action="store_true",
        help="Emit the result (state + text) as JSON",
    )

    # ── pr ──
    p_pr = subs.add_parser(
        "pr",
        help="Address PR review comments",
        description=(
            "Launch T3 threads to address unresolved review comments on pull requests. "
            "Works with CodeRabbit, human reviewers, and any tool that leaves PR comments."
        ),
    )
    p_pr.add_argument(
        "pr_numbers", nargs="*", type=int,
        help="PR numbers to process (e.g., 58 61)",
    )
    p_pr.add_argument(
        "--open", action="store_true",
        help="Process all open PRs with unresolved review threads",
    )
    p_pr.add_argument(
        "--mine", action="store_true",
        help="Only my PRs (use with --open)",
    )
    p_pr.add_argument(
        "--reviewer",
        help="Filter threads by reviewer login (e.g., coderabbitai)",
    )
    p_pr.add_argument(
        "--per-thread", action="store_true",
        help="Launch one thread per review comment (default: one per PR)",
    )
    p_pr.add_argument(
        "--max-prompt-chars", type=int, default=None,
        help="Auto-split bundled review prompts above this size into "
             "sibling-branch chunks (default: 100000; T3 server limit ≈ 120000)",
    )
    p_pr.add_argument(
        "--include-resolved", action="store_true",
        help="Include already-resolved threads",
    )
    p_pr.add_argument(
        "--include-outdated", action="store_true",
        help="Include outdated threads (code has changed since comment)",
    )
    p_pr.add_argument(
        "--wait", action="store_true", default=None,
        help="On GitHub rate-limit, sleep until the budget resets and resume "
             "(stays on GraphQL, preserving resolved/outdated filtering)",
    )
    p_pr.add_argument(
        "--wait-max-seconds", type=int, default=None,
        help="Cap for --wait; if the reset is further away, fall back to REST "
             "instead of sleeping (default: 300)",
    )
    p_pr.add_argument(
        "--no-cache", action="store_true",
        help="Ignore the local PR-thread cache and re-fetch everything",
    )

    # ── review ──
    p_review = subs.add_parser(
        "review",
        help="Spawn a local reviewer thread per PR",
        description=(
            "Spawn one autonomous T3 thread per pull request that ACTS as a "
            "senior code reviewer: it reads the PR diff read-only and posts a "
            "full review (verdict, paste-ready comments, action items) as its "
            "output. Unlike `pr` (which addresses existing review comments), "
            "`review` generates the review locally — one thread per PR so you "
            "can read each one on its own."
        ),
    )
    p_review.add_argument(
        "pr_numbers", nargs="*", type=int,
        help="PR numbers to review (e.g., 58 61)",
    )
    p_review.add_argument(
        "--open", action="store_true",
        help="Review all open PRs",
    )
    p_review.add_argument(
        "--mine", action="store_true",
        help="Only my PRs (use with --open)",
    )
    p_review.add_argument(
        "--review-prompt", dest="review_prompt", default=None,
        help="Path to a custom reviewer prompt file "
             "(default: the bundled generic reviewer guide)",
    )

    # ── triage ──
    p_triage = subs.add_parser(
        "triage",
        help="Classify open PRs (conflicts, CI, reviews, ready)",
        description=(
            "Show a grouped status report for open PRs — merge conflicts, failing "
            "CI, changes requested, behind base, awaiting review, ready to merge — "
            "and optionally launch conflict-resolution threads."
        ),
    )
    p_triage.add_argument(
        "pr_numbers", nargs="*", type=int,
        help="Specific PR numbers to triage (default: all open PRs)",
    )
    p_triage.add_argument(
        "--mine", action="store_true",
        help="Only my PRs",
    )
    p_triage.add_argument(
        "--resolve-conflicts", action="store_true",
        help="After the report, spawn agents to resolve every conflicting PR",
    )
    _add_conflict_strategy_flags(p_triage)

    # ── conflicts ──
    p_conflicts = subs.add_parser(
        "conflicts",
        help="Resolve merge conflicts across all PR branches",
        description=(
            "Find open PRs that conflict with their base branch (or the PRs you "
            "name) and spawn one autonomous T3 thread per branch to merge in the "
            "base, resolve the conflicts, verify, and push."
        ),
    )
    p_conflicts.add_argument(
        "pr_numbers", nargs="*", type=int,
        help="Specific PR numbers to resolve (default: all conflicting open PRs)",
    )
    p_conflicts.add_argument(
        "--mine", action="store_true",
        help="Only my PRs",
    )
    _add_conflict_strategy_flags(p_conflicts)

    # ── approve-plans ──
    p_approve = subs.add_parser(
        "approve-plans",
        help="Approve captured T3 plans in quota-aware batches",
        description=(
            "Freeze actionable proposed plans for the selected T3 project, then "
            "start their linked implementation turns in paced batches."
        ),
    )
    p_approve.add_argument(
        "thread_refs", nargs="*",
        help="Ordered full thread ids or unique prefixes (default: all actionable plans)",
    )
    p_approve.add_argument(
        "--start-at",
        help="Offset-aware ISO 8601 time for the first batch; a past time starts now",
    )
    p_approve.add_argument(
        "--quota-threshold", type=float, default=90.0,
        help="Stop when five-hour Claude utilization reaches this percent (default: 90)",
    )
    p_approve.add_argument(
        "--yes", action="store_true",
        help="Confirm the frozen manifest without an interactive prompt",
    )

    # ── status ──
    subs.add_parser(
        "status",
        help="List active T3 threads",
        description="Show active T3 Code threads from the state database.",
    )

    # ── clean ──
    p_clean = subs.add_parser(
        "clean",
        help="Remove finished worktrees and temp files",
        description="Clean up launcher scripts and optionally remove worktrees.",
    )
    p_clean.add_argument(
        "--worktrees", action="store_true",
        help="Also remove ALL worktrees (destructive!)",
    )

    # ── config ──
    p_config = subs.add_parser(
        "config",
        help="Show resolved config / init template",
        description="Show the resolved configuration or create a .d3ts.toml template.",
    )
    p_config.add_argument(
        "--init", action="store_true",
        help="Create a .d3ts.toml template in the current directory",
    )
    p_config.add_argument(
        "--path", action="store_true",
        help="Show which config files are being loaded",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        set_verbose(True)

    settings = load_config(args)

    # A shared/long-lived head branch auto-downgrades --rebase to merge (see
    # AgentSettings.is_protected_branch); --force-rebase-protected opts out.
    if getattr(args, "force_rebase_protected", False):
        settings = replace(settings, conflict_rebase_protected=True)

    # Read-only/state-only commands do not need a local Git checkout. In
    # particular, approve-plans can target an exact --project-id from a
    # background service that intentionally cannot access the source repo.
    if args.command not in (
        "status", "output", "approve-plans", "clean", "config"
    ):
        git_dir = os.path.join(settings.repo_dir, ".git")
        if not os.path.exists(git_dir):
            log("❌", f"Not a git repo: {settings.repo_dir}")
            return 1

    launch_commands = {"spawn", "pr", "review", "triage", "conflicts"}

    # `output` launches no model and may emit machine-readable JSON on stdout, so
    # skip the settings banner for it (keep it for the existing human commands).
    if args.command not in ("output", "approve-plans"):
        validate_now = args.command in launch_commands and not (
            args.command == "spawn" and getattr(args, "from_file", None)
        )
        if validate_now:
            try:
                settings.validate_model_selection()
            except RuntimeError as e:
                log("❌", str(e))
                return 1
        extra = f"  wait={settings.initial_wait}m" if settings.initial_wait > 0 else ""
        effort = settings.effective_effort() or "-"
        log("⚙️ ", f"model={settings.model}→{settings.resolved_model}  mode={settings.mode}  "
            f"access={settings.access}  effort={effort}  "
            f"ctx={settings.effective_context_window()}{extra}")
        for note in settings.model_selection_adjustments():
            log("↪ ", note)

    from .commands.spawn import cmd_spawn
    from .commands.output import cmd_output
    from .commands.pr import cmd_pr
    from .commands.review import cmd_review
    from .commands.triage import cmd_triage
    from .commands.conflicts import cmd_conflicts
    from .commands.approve_plans import cmd_approve_plans
    from .commands.status import cmd_status
    from .commands.clean import cmd_clean
    from .commands.config_cmd import cmd_config

    handlers = {
        "spawn": cmd_spawn,
        "output": cmd_output,
        "pr": cmd_pr,
        "review": cmd_review,
        "triage": cmd_triage,
        "conflicts": cmd_conflicts,
        "approve-plans": cmd_approve_plans,
        "status": cmd_status,
        "clean": cmd_clean,
        "config": cmd_config,
    }

    try:
        handlers[args.command](args, settings)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except RuntimeError as e:
        log("❌", str(e))
        return 1

    return 0
