"""Prompt templates and variable interpolation."""

from __future__ import annotations

import os
import re
import textwrap
from collections import defaultdict
from typing import Dict, List, Optional

from .models import PRInfo, ReviewThread


# ── Built-in Templates ──────────────────────────────────────────────────────

BUILTIN_SPAWN = """\
You are an autonomous engineer working on this codebase.

If there is a CLAUDE.md file at the repo root, read it first for project
conventions and follow them strictly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK:
{task}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WORKFLOW:

  STEP 1 — UNDERSTAND:
    - Read the relevant source files to understand the current implementation.
    - Identify the scope of changes needed.

  STEP 2 — PLAN:
    - Write a clear, numbered implementation plan.
    - Identify which files will be modified or created.
    - Consider edge cases and risks.

    STOP HERE. Present the plan and wait for human review before implementing.
    Do NOT write code until a human approves the plan.

  STEP 3 — IMPLEMENT (after human approval):
    - Follow all project conventions.
    - Write or update tests for changed logic.
    - Run linting and tests to verify.

  STEP 4 — COMMIT & PUSH:
    - Write a clear commit message.
    - Push to origin.

Begin with Step 1 now."""

BUILTIN_PR_REVIEW = """\
You are an autonomous engineer addressing code review feedback.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PR:     #{pr_number} — {pr_title}
BRANCH: {pr_branch}
URL:    {pr_url}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are on the PR branch ({pr_branch}). Your job is to address the
unresolved review comments below.

═══ UNRESOLVED REVIEW THREADS ({thread_count} total) ═══
{threads_text}
═══════════════════════════════════════════════════════

WORKFLOW:

  STEP 1 — ANALYZE EACH THREAD:
    For each review thread above:
    a) Read the file at the referenced path and line.
    b) Understand the reviewer's concern fully (read the whole thread).
    c) If the reviewer included an "AI Agent Instruction", follow it but
       ALWAYS verify against the current code first — it may have changed.
    d) Determine if the feedback is valid, already addressed, or not applicable.

  STEP 2 — PLAN YOUR CHANGES:
    Write a brief summary of what you will change for each thread.
    If a thread's feedback is already addressed or not applicable, explain why.

    STOP HERE. Present the plan and wait for human review before implementing.

  STEP 3 — IMPLEMENT (after human approval):
    - If there is a CLAUDE.md at the repo root, follow its conventions.
    - Apply fixes for each valid review comment.
    - Run linting and tests to verify.

  STEP 4 — COMMIT & PUSH:
    - Commit message: "address review feedback on #{pr_number}"
    - Push to the SAME branch ({pr_branch}) — do NOT create a new branch.

Begin with Step 1 now."""

BUILTIN_PR_THREAD = """\
You are addressing a single code review comment on PR #{pr_number}.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PR:     #{pr_number} — {pr_title}
BRANCH: {agent_branch} (forked from {pr_branch})
URL:    {pr_url}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REVIEW COMMENT:
  File:     {thread_path}
  Line:     {thread_line}
  Reviewer: @{thread_reviewer}

{thread_body}
{thread_followups}{thread_ai_section}

WORKFLOW:

  1. If there is a CLAUDE.md at the repo root, read it for project conventions.
  2. Read the file at {thread_path} around line {thread_line_or_section}.
  3. Understand the reviewer's concern and verify it against the current code.
  4. If valid, implement the fix following project conventions.
  5. If not applicable (already fixed or incorrect), explain why and stop.
  6. Run linting and tests to verify.
  7. Commit and push to {agent_branch}.

Begin now."""


# ── Template Loading ────────────────────────────────────────────────────────


def load_prompt_template(template: str) -> str:
    """Load a prompt template.

    - If it's a file path that exists, read it.
    - Otherwise treat as literal template text.
    """
    expanded = os.path.expanduser(template)
    if os.path.isfile(expanded):
        with open(expanded) as f:
            return f.read().strip()
    return template


def render_prompt(template: str, variables: Dict[str, str]) -> str:
    """Interpolate {variables} in the template.

    Uses format_map with a defaultdict that leaves unknown vars as-is.
    """
    safe = defaultdict(str, variables)
    # Preserve unknown {vars} by replacing them back
    class SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(SafeDict(variables))


# ── PR Prompt Builders ──────────────────────────────────────────────────────


def format_threads_text(threads: List[ReviewThread]) -> str:
    """Format review threads into text for inclusion in a prompt."""
    parts = []
    for i, t in enumerate(threads, 1):
        first = t.comments[0]
        body = first.body
        if len(body) > 2000:
            body = body[:2000] + "\n  ... (truncated, read full comment on GitHub)"

        part = f"""
  ── Thread {i} of {len(threads)} ─────────────────────────────
  File:     {t.path}
  Line:     {t.line or "N/A"}
  Reviewer: @{first.author}

{textwrap.indent(body, '  ')}
"""
        if t.ai_prompt:
            part += f"""
  AI AGENT INSTRUCTION (from reviewer — verify before applying):
{textwrap.indent(t.ai_prompt, '  ')}
"""
        if len(t.comments) > 1:
            part += "\n  Follow-up comments in this thread:\n"
            for c in t.comments[1:]:
                short_body = c.body[:600] + "..." if len(c.body) > 600 else c.body
                part += f"    @{c.author}: {short_body}\n"

        parts.append(part)

    return "".join(parts)


def build_pr_review_prompt(pr: PRInfo, threads: List[ReviewThread]) -> str:
    """Build prompt for addressing ALL unresolved threads on a PR."""
    threads_text = format_threads_text(threads)
    return render_prompt(BUILTIN_PR_REVIEW, {
        "pr_number": str(pr.number),
        "pr_title": pr.title,
        "pr_branch": pr.branch,
        "pr_url": pr.url,
        "thread_count": str(len(threads)),
        "threads_text": threads_text,
    })


def build_pr_thread_prompt(
    pr: PRInfo, thread: ReviewThread, agent_branch: str
) -> str:
    """Build prompt for addressing a SINGLE review thread."""
    first = thread.comments[0]

    followups = ""
    if len(thread.comments) > 1:
        followups = "\n\nFollow-up comments in this thread:\n"
        for c in thread.comments[1:]:
            short = c.body[:600] + "..." if len(c.body) > 600 else c.body
            followups += f"  @{c.author}: {short}\n"

    ai_section = ""
    if thread.ai_prompt:
        ai_section = (
            f"\n\nAI AGENT INSTRUCTION "
            f"(from reviewer — verify against current code before applying):\n"
            f"{thread.ai_prompt}"
        )

    return render_prompt(BUILTIN_PR_THREAD, {
        "pr_number": str(pr.number),
        "pr_title": pr.title,
        "pr_branch": pr.branch,
        "pr_url": pr.url,
        "agent_branch": agent_branch,
        "thread_path": thread.path,
        "thread_line": str(thread.line or "N/A"),
        "thread_line_or_section": str(thread.line or "the relevant section"),
        "thread_reviewer": first.author,
        "thread_body": first.body,
        "thread_followups": followups,
        "thread_ai_section": ai_section,
    })


def build_spawn_prompt(task: str) -> str:
    """Build the default spawn prompt wrapping a task description."""
    return render_prompt(BUILTIN_SPAWN, {"task": task})
