"""CLI parser and main entry point."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

from . import __version__
from .config import load_config
from .util import log, set_verbose


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
              %(prog)s pr 58 --reviewer coderabbitai
              %(prog)s pr --open --mine
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
        help="Claude model alias or full ID (default: opus)",
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
        "--effort", choices=["low", "medium", "high", "max"], default=None,
        help="Effort level (default: high)",
    )
    parser.add_argument(
        "--context-window", choices=["200k", "1m"], default=None,
        help="Context window size (default: 1m)",
    )
    parser.add_argument(
        "--thinking", action="store_true", default=None, dest="thinking",
        help="Enable thinking mode (default: on)",
    )
    parser.add_argument(
        "--no-thinking", action="store_false", dest="thinking",
        help="Disable thinking mode",
    )
    parser.add_argument(
        "--fast-mode", action="store_true", default=None, dest="fast_mode",
        help="Enable fast mode",
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

    # Verify repo exists (not needed for status/clean/config)
    if args.command not in ("status", "clean", "config"):
        git_dir = os.path.join(settings.repo_dir, ".git")
        if not os.path.exists(git_dir):
            log("❌", f"Not a git repo: {settings.repo_dir}")
            return 1

    log("⚙️ ", f"model={settings.model}  mode={settings.mode}  access={settings.access}  "
        f"effort={settings.effort}  ctx={settings.context_window}")

    from .commands.spawn import cmd_spawn
    from .commands.pr import cmd_pr
    from .commands.status import cmd_status
    from .commands.clean import cmd_clean
    from .commands.config_cmd import cmd_config

    handlers = {
        "spawn": cmd_spawn,
        "pr": cmd_pr,
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
