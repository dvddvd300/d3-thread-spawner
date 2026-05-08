"""spawn command — Launch T3 threads with prompts."""

from __future__ import annotations

import json
import os
from typing import List

from ..batch import launch_batch
from ..models import AgentSettings, WorkItem
from ..prompts import build_spawn_prompt, load_prompt_template, render_prompt
from ..util import log, run, slugify


def _load_jsonl(path: str, settings: AgentSettings) -> List[WorkItem]:
    """Load work items from a JSONL file.

    Each line is a JSON object with fields:
      name (required), prompt or prompt_file (one required),
      branch (existing) or new_branch + optional fork_from,
      optional overrides: model, mode, effort
    """
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        raise FileNotFoundError(f"JSONL file not found: {expanded}")

    items: List[WorkItem] = []

    with open(expanded) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON on line {line_num}: {e}")

            name = entry.get("name")
            if not name:
                raise ValueError(f"Line {line_num}: 'name' is required")

            # Resolve prompt
            if "prompt" in entry:
                prompt_text = entry["prompt"]
            elif "prompt_file" in entry:
                pf = os.path.expanduser(entry["prompt_file"])
                if not os.path.isfile(pf):
                    raise FileNotFoundError(
                        f"Line {line_num}: prompt_file not found: {pf}"
                    )
                with open(pf) as pf_handle:
                    prompt_text = pf_handle.read().strip()
            else:
                raise ValueError(
                    f"Line {line_num}: 'prompt' or 'prompt_file' is required"
                )

            # Per-item settings overrides
            item_settings = AgentSettings(
                model=entry.get("model", settings.model),
                mode=entry.get("mode", settings.mode),
                access=entry.get("access", settings.access),
                effort=entry.get("effort", settings.effort),
                base_branch=settings.base_branch,
                repo_dir=settings.repo_dir,
                context_window=settings.context_window,
                thinking=settings.thinking,
                fast_mode=settings.fast_mode,
                batch_size=settings.batch_size,
                batch_delay=settings.batch_delay,
                launch_delay=settings.launch_delay,
                initial_wait=settings.initial_wait,
                dry_run=settings.dry_run,
                t3_host=settings.t3_host,
                t3_port=settings.t3_port,
                t3_project_id=settings.t3_project_id,
                worktree_dir=settings.worktree_dir,
                github_repo=settings.github_repo,
                model_aliases=settings.model_aliases,
            )

            # Branch strategy
            if "new_branch" in entry:
                branch = entry["new_branch"]
                create_branch = True
                worktree_from = entry.get("fork_from")
            elif "branch" in entry:
                branch = entry["branch"]
                create_branch = False
                worktree_from = None
            else:
                branch = settings.base_branch
                create_branch = True
                worktree_from = None

            # Wrap with default template if prompt looks like a plain task
            wrapped_prompt = build_spawn_prompt(prompt_text)

            items.append(WorkItem(
                name=name,
                branch=branch,
                prompt=wrapped_prompt,
                settings=item_settings,
                create_branch=create_branch,
                worktree_from=worktree_from,
            ))

    return items


def cmd_spawn(args, settings: AgentSettings):
    """Launch T3 thread(s) with a prompt."""

    # ── Batch from JSONL ──
    if args.from_file:
        log("📄", f"Loading tasks from {args.from_file}")
        items = _load_jsonl(args.from_file, settings)
        if not items:
            log("⚠️ ", "No tasks found in file.")
            return
        log("✅", f"Loaded {len(items)} task(s)")

        if not settings.dry_run and len(items) > 1:
            confirm = input(f"Launch {len(items)} thread(s)? [y/N] ").strip()
            if confirm.lower() != "y":
                log("⚠️ ", "Aborted.")
                return

        launch_batch(items, settings)
        return

    # ── Single prompt ──
    if args.file:
        prompt_path = os.path.expanduser(args.file)
        if not os.path.isfile(prompt_path):
            log("❌", f"File not found: {prompt_path}")
            return
        with open(prompt_path) as f:
            user_prompt = f.read().strip()
    elif args.prompt:
        user_prompt = args.prompt
    else:
        log("❌", "No prompt specified. Use a string argument, --file, or --from-file.")
        return

    # Apply template if specified
    if args.template:
        template = load_prompt_template(args.template)
        variables = {"task": user_prompt}
        # Parse --var key=value pairs
        if args.var:
            for v in args.var:
                if "=" in v:
                    k, val = v.split("=", 1)
                    variables[k] = val
        prompt = render_prompt(template, variables)
    else:
        prompt = build_spawn_prompt(user_prompt)

    name = args.name or f"spawn-{slugify(user_prompt[:30])}"

    # Branch strategy
    if args.new_branch:
        branch = args.new_branch
        create_branch = True
        worktree_from = args.fork_from
    elif args.branch:
        branch = args.branch
        create_branch = False
        worktree_from = None
    else:
        branch = f"d3ts/{slugify(name, 50)}"
        create_branch = True
        worktree_from = None

    # Sync base branch
    if not settings.dry_run:
        log("⏳", f"Fetching {settings.base_branch}...")
        run(["git", "-C", settings.repo_dir, "fetch", "origin", settings.base_branch],
            check=False)

    item = WorkItem(
        name=name,
        branch=branch,
        prompt=prompt,
        settings=settings,
        create_branch=create_branch,
        worktree_from=worktree_from,
    )

    launch_batch([item], settings)
