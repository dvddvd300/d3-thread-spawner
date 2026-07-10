"""config command — Show resolved config, init template."""

from __future__ import annotations

import os
from dataclasses import asdict

from ..config import DEFAULTS, get_config_paths
from ..models import AgentSettings
from ..util import log


TEMPLATE = """\
# d3-thread-spawner project config
# Place this file as .d3ts.toml in your repo root.
# See: https://github.com/dvddvd300/d3-thread-spawner

[general]
# model = "opus"              # opus, sonnet, haiku, mini, or full model ID
# mode = "build"              # build (act immediately) or plan (propose first)
# access = "full"             # full, auto-accept, supervised
# effort = "high"             # Codex: low, medium, high, xhigh; GPT-5.6
#                             #   Sol/Terra add max, ultra; Luna adds max
#                             #   (never Claude's ultrathink)
#                             # Claude: low, medium, high, xhigh, max,
#                             #   ultracode, ultrathink; unsupported values
#                             #   normalize to the model's max
# base_branch = "main"        # default base branch for new worktrees
# repo_dir = "."              # "." = auto-detect from CWD

[batch]
# size = 5                    # threads per batch
# delay = 0                   # minutes between batches
# launch_delay = 0.5          # seconds between individual launches
# initial_wait = 0              # minutes to wait before first batch

[t3]
# project_id = ""             # T3 project UUID (auto-detected if empty)
# host/port are auto-detected from ~/.t3/userdata/server-runtime.json

[worktree]
# dir = "~/d3ts-worktrees/{project}"   # {project} = repo directory name

[github]
# repo = ""                   # owner/name (auto-detected from git remote)

[pr]
# max_prompt_chars = 100000   # split bundled PR reviews above this size

[review]
# Custom reviewer methodology for the `review` command (which spawns a local
# reviewer thread per PR). Unset ⇒ the bundled generic senior-reviewer guide.
# prompt_file = "~/my-review-guide.md"

[conflicts]
# strategy = "merge"          # "merge" (base into branch) or "rebase" (onto base)
# Batch pacing for the `conflicts` command (and `triage --resolve-conflicts`),
# overriding [batch] for conflict resolution only. Unset keys inherit [batch],
# so conflicts run at the normal pace unless you slow them here — handy for
# --rebase, which force-pushes each branch one autonomous thread at a time.
# batch_size = 1              # conflict threads per batch (default: inherit [batch])
# batch_delay = 2             # minutes between conflict batches (default: inherit)
# launch_delay = 1.0          # seconds between individual conflict launches (default: inherit)
# initial_wait = 0            # minutes before the first conflict batch (default: inherit)

[models]
# Define model aliases. The key is what you pass to --model.
# Opus 4.8 requires T3 Code's bundled Claude Code CLI >= 2.1.154.
# opus = "claude-opus-4-8"
# sonnet = "claude-sonnet-4-6"
# haiku = "claude-haiku-4-5"
# mini = "gpt-5.4-mini"

[model_options]
# T3 provider metadata is preferred when available. d3 sends only supported
# options and normalizes unsupported select values before launch:
#   context_window → unsupported 1m falls back to 200k
#   thinking       → Haiku 4.5 only
#   fast_mode      → Opus 4.5/4.6 only
# context_window = "1m"       # 200k or 1m
# thinking = true
# fast_mode = false
"""


def cmd_config(args, settings: AgentSettings):
    """Show resolved config or create a template."""

    if args.init:
        target = os.path.join(os.getcwd(), ".d3ts.toml")
        if os.path.exists(target):
            log("⚠️ ", f"File already exists: {target}")
            return
        with open(target, "w") as f:
            f.write(TEMPLATE)
        log("✅", f"Created {target}")
        log("📝", "Edit the file to configure your project, then run: d3-spawn config")
        return

    if args.path:
        paths = get_config_paths(args)
        for source, path in paths.items():
            if path:
                log("📄", f"{source}: {path}")
            else:
                log("  ", f"{source}: (not found)")
        return

    # Show resolved config
    d = asdict(settings)
    # Don't dump the aliases dict inline, show it cleaner
    aliases = d.pop("model_aliases", {})

    print("\n  Resolved configuration:\n")
    for key, val in sorted(d.items()):
        if key.startswith("_"):
            continue
        print(f"    {key:20s} = {val}")

    if aliases:
        print(f"\n    {'model_aliases':20s} =")
        for alias, model_id in sorted(aliases.items()):
            print(f"      {alias:10s} → {model_id}")

    print()
