"""Prompt templates and variable interpolation."""

from __future__ import annotations

import os
import re
import textwrap
from collections import defaultdict
from typing import Dict, List, Optional

from .models import PRInfo, PRStatus, ReviewThread


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

  STEP 0 — SYNC WITH REMOTE:
    Make sure your worktree matches the latest origin/{pr_branch} before
    looking at anything else.
    - `git fetch origin {pr_branch}`
    - If `git status` shows any uncommitted changes, discard them
      (`git restore .` and `git clean -fd`) — this is a fresh worktree and
      stray edits are unexpected.
    - `git pull --ff-only origin {pr_branch}` (or `git rebase origin/{pr_branch}`
      if not fast-forward).
    - Confirm `git status` is clean and HEAD matches origin/{pr_branch}
      before proceeding.

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

Begin with Step 0 now."""

BUILTIN_PR_REVIEW_CHUNK = """\
You are an autonomous engineer addressing a SUBSET of code review feedback.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PR:          #{pr_number} — {pr_title}
PR BRANCH:   {pr_branch}
URL:         {pr_url}

YOUR BRANCH: {agent_branch} (forked from {pr_branch})
CHUNK:       Part {chunk_index} of {total_chunks} — {thread_count} thread(s) in this chunk
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The full review for this PR was split across {total_chunks} agents because the
combined feedback exceeded the message size limit. You are agent {chunk_index}
of {total_chunks}, responsible only for the threads listed below. Sibling
agents are addressing the rest in parallel on sibling branches
(pr-{pr_number}/review-Nof{total_chunks}); each will be merged into the PR
branch separately.

═══ UNRESOLVED REVIEW THREADS in this chunk ({thread_count}) ═══
{threads_text}
═══════════════════════════════════════════════════════════════

WORKFLOW:

  STEP 0 — SYNC WITH REMOTE:
    Make sure your fork is on top of the latest origin/{pr_branch} before
    looking at anything else.
    - `git fetch origin {pr_branch}`
    - If `git status` shows any uncommitted changes, discard them
      (`git restore .` and `git clean -fd`) — this is a fresh worktree and
      stray edits are unexpected.
    - Your branch ({agent_branch}) is freshly forked locally and has no remote
      yet — that's expected. If origin/{pr_branch} has advanced since your
      fork point, rebase: `git rebase origin/{pr_branch}`.
    - Confirm `git status` is clean before proceeding.

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
    - Apply fixes for each valid review comment in this chunk only.
    - Do NOT touch threads that belong to sibling chunks — they are owned by
      other agents.
    - Run linting and tests to verify.

  STEP 4 — COMMIT & PUSH:
    - Commit message: "address review feedback on #{pr_number} (part {chunk_index}/{total_chunks})"
    - Push to YOUR branch ({agent_branch}) — do NOT push to {pr_branch}.
      Sibling branches will be merged into the PR separately.

Begin with Step 0 now."""

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

  0. SYNC WITH REMOTE: Run `git fetch origin {pr_branch}` and rebase your
     branch onto it (`git rebase origin/{pr_branch}`) so you have the latest
     review-target code. Discard any unexpected uncommitted changes
     (`git restore .` / `git clean -fd`) — this is a fresh worktree.
  1. If there is a CLAUDE.md at the repo root, read it for project conventions.
  2. Read the file at {thread_path} around line {thread_line_or_section}.
  3. Understand the reviewer's concern and verify it against the current code.
  4. If valid, implement the fix following project conventions.
  5. If not applicable (already fixed or incorrect), explain why and stop.
  6. Run linting and tests to verify.
  7. Commit and push to {agent_branch}.

Begin now."""

BUILTIN_CONFLICT_MERGE = """\
You are an autonomous engineer resolving MERGE CONFLICTS on a pull request.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PR:     #{pr_number} — {pr_title}
BRANCH: {pr_branch}  (your worktree is checked out here)
BASE:   {base_branch}
URL:    {pr_url}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GitHub reports that {pr_branch} CONFLICTS with {base_branch}. Your job is to
merge {base_branch} into {pr_branch}, resolve every conflict correctly, verify,
and push — autonomously. Do NOT wait for human approval unless a conflict is
genuinely ambiguous (see Step 2).

WORKFLOW:

  STEP 0 — SYNC:
    - `git fetch origin {base_branch} {pr_branch}`
    - This is a fresh worktree; discard any stray uncommitted changes:
      `git restore .` then `git clean -fd`.
    - Make sure you are on {pr_branch} and it matches origin:
      `git switch {pr_branch}` (it tracks origin/{pr_branch}), then
      `git pull --ff-only origin {pr_branch}`.
    - Confirm `git status` is clean and HEAD == origin/{pr_branch}.

  STEP 1 — MERGE THE BASE:
    - `git merge --no-edit origin/{base_branch}`
    - It will stop with conflicts. List every conflicted file:
      `git diff --name-only --diff-filter=U`.
    - If git reports NO conflicts (already merges cleanly), there is nothing to
      do — say so and STOP without committing an empty/no-op change.

  STEP 2 — RESOLVE EACH CONFLICT:
    For every conflicted file:
      a) Read the file and inspect BOTH sides of each `<<<<<<< / ======= / >>>>>>>`
         marker (ours = the PR change, theirs = {base_branch}).
      b) Use `git log` / `git blame` if needed to understand the INTENT of each
         side. Resolve so BOTH intents are preserved — never blindly pick a side
         or delete the other's work.
      c) Remove ALL conflict markers, then `git add <file>`.
    - If a conflict is genuinely ambiguous and you cannot determine the correct
      resolution with confidence, STOP and report the exact file, lines, and the
      competing intents. Do NOT guess.

  STEP 3 — VERIFY:
    - If a CLAUDE.md exists at the repo root, follow it and use its test/lint
      commands. Otherwise detect and run the project's test suite + linter, and
      build if applicable.
    - No conflict markers remain: `git diff --check` reports nothing and
      `git grep -nE '^(<<<<<<<|>>>>>>>)'` returns nothing. (The `=======`
      separator alone is intentionally not matched — it collides with Markdown
      and banner underlines.)
    - If verification FAILS, fix the cause. If you cannot, STOP and report —
      do NOT push broken code.

  STEP 4 — COMMIT & PUSH:
    - Complete the merge: `git commit --no-edit` (default message
      "Merge {base_branch} into {pr_branch}" is fine).
    - `git push origin {pr_branch}` (same branch — a merge needs no force-push).

Begin with Step 0 now."""

BUILTIN_CONFLICT_REBASE = """\
You are an autonomous engineer resolving MERGE CONFLICTS on a pull request by
REBASING it onto its base branch.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PR:     #{pr_number} — {pr_title}
BRANCH: {pr_branch}  (your worktree is checked out here)
BASE:   {base_branch}
URL:    {pr_url}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GitHub reports that {pr_branch} CONFLICTS with {base_branch}. Your job is to
rebase {pr_branch} onto {base_branch}, resolve every conflict correctly, verify,
and force-push (with lease) — autonomously. Do NOT wait for human approval
unless a conflict is genuinely ambiguous (see Step 2).

⛔ STOP-FIRST — REFUSE IF THIS REWRITES SHARED HISTORY:
  Rebasing force-pushes {pr_branch}, which REWRITES its commit history. That is
  safe ONLY when {pr_branch} is a disposable feature/PR branch nobody else builds
  on. It is DESTRUCTIVE for a shared/long-lived integration branch: it breaks every
  open PR based on {pr_branch} (their diffs explode to include all the rewritten
  commits) and forces every teammate's clone to diverge. Before touching anything:
    1. If {pr_branch} is (or looks like) a long-lived branch — main, master,
       develop, dev, trunk, next, or a release*/stage*/prod* branch — STOP.
    2. Check for dependents: `gh pr list --base {pr_branch} --state open`. If ANY
       PR targets {pr_branch} as its base, STOP.
  If either holds, do NOT rebase or force-push. Report that this PR must be resolved
  with the MERGE strategy instead (merge {base_branch} into {pr_branch} — a normal
  push, no history rewrite) and stop. Only proceed when {pr_branch} is a disposable
  branch with no dependents.

WORKFLOW:

  STEP 0 — SYNC:
    - `git fetch origin {base_branch} {pr_branch}`
    - This is a fresh worktree; discard any stray uncommitted changes:
      `git restore .` then `git clean -fd`.
    - Be on {pr_branch} matching origin: `git switch {pr_branch}` then
      `git pull --ff-only origin {pr_branch}`. Confirm `git status` is clean.

  STEP 1 — REBASE ONTO THE BASE:
    - Record the current tip first: `git rev-parse HEAD`.
    - `git rebase origin/{base_branch}`
    - The rebase will pause on each conflicting commit.
    - If the rebase completes with NO conflicts to resolve AND the tip is
      unchanged (already on top of {base_branch}), there is nothing to do —
      say so and STOP. Do NOT force-push an unchanged branch.

  STEP 2 — RESOLVE EACH CONFLICT (per paused commit):
    For every conflicted file in the current step:
      a) Inspect BOTH sides of each conflict marker. During a rebase, "ours" is
         {base_branch} and "theirs" is the PR commit being replayed — do not let
         that invert your intent; preserve the PR's change AND the base change.
      b) Use `git log` / `git blame` to understand intent. Resolve so both are
         preserved — never blindly pick a side.
      c) Remove ALL conflict markers, `git add <file>`, then `git rebase --continue`.
    - If a conflict is genuinely ambiguous, run `git rebase --abort`, then STOP
      and report the exact file, lines, and competing intents. Do NOT guess.

  STEP 3 — VERIFY (after the rebase completes):
    - If a CLAUDE.md exists at the repo root, follow it and use its test/lint
      commands. Otherwise detect and run tests + linter, and build if applicable.
    - No conflict markers remain: `git diff --check` reports nothing and
      `git grep -nE '^(<<<<<<<|>>>>>>>)'` returns nothing. (The `=======`
      separator alone is intentionally not matched — it collides with Markdown
      and banner underlines.)
    - If verification FAILS, fix it; if you cannot, STOP and report — do not push.

  STEP 4 — FORCE-PUSH (with lease):
    - Re-confirm the STOP-FIRST check still holds: never force-push a shared branch
      or one with dependent PRs. Force-push rewrites the history every dependent PR
      and every clone relies on — there is no clean undo for collaborators.
    - Only if the rebase actually rewrote the branch (Step 1):
      `git push --force-with-lease origin {pr_branch}`
      (a rebase rewrites history, so a normal push is rejected; --force-with-lease
      refuses to clobber commits you haven't seen).

Begin with Step 0 now."""


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


def build_pr_review_chunk_prompt(
    pr: PRInfo,
    threads: List[ReviewThread],
    agent_branch: str,
    chunk_index: int,
    total_chunks: int,
) -> str:
    """Build prompt for one chunk of a split bundled PR review."""
    threads_text = format_threads_text(threads)
    return render_prompt(BUILTIN_PR_REVIEW_CHUNK, {
        "pr_number": str(pr.number),
        "pr_title": pr.title,
        "pr_branch": pr.branch,
        "pr_url": pr.url,
        "agent_branch": agent_branch,
        "chunk_index": str(chunk_index),
        "total_chunks": str(total_chunks),
        "thread_count": str(len(threads)),
        "threads_text": threads_text,
    })


def split_threads_into_chunks(
    pr: PRInfo, threads: List[ReviewThread], max_chars: int
) -> List[List[ReviewThread]]:
    """Greedy bin-pack threads so each chunk's rendered prompt fits max_chars.

    Each chunk is sized using build_pr_review_chunk_prompt — the same renderer
    used to emit the prompt — so the size budget is exact. A high-digit
    sentinel for chunk_index/total_chunks is used since the final count isn't
    known up front; this slightly overestimates and never underestimates.
    A thread that by itself renders larger than max_chars still gets its own
    chunk — caller decides how to handle that downstream.
    """
    chunks: List[List[ReviewThread]] = [[]]
    sent = 999  # 3-digit sentinel — ample for any real PR
    fake_branch = f"pr-{pr.number}/review-{sent}of{sent}"
    for thread in threads:
        candidate = chunks[-1] + [thread]
        rendered = build_pr_review_chunk_prompt(
            pr, candidate, fake_branch, sent, sent
        )
        if len(rendered) <= max_chars or not chunks[-1]:
            chunks[-1] = candidate
        else:
            chunks.append([thread])
    return chunks


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


def build_conflict_resolution_prompt(pr: PRStatus, strategy: str = "merge") -> str:
    """Build a prompt instructing an agent to resolve a PR's merge conflicts.

    ``strategy`` is "merge" (merge base into the PR branch; no force-push) or
    "rebase" (rebase the PR branch onto base; force-push with lease).
    """
    template = BUILTIN_CONFLICT_REBASE if strategy == "rebase" else BUILTIN_CONFLICT_MERGE
    return render_prompt(template, {
        "pr_number": str(pr.number),
        "pr_title": pr.title,
        "pr_branch": pr.branch,
        "base_branch": pr.base_branch,
        "pr_url": pr.url,
    })


def build_spawn_prompt(task: str) -> str:
    """Build the default spawn prompt wrapping a task description."""
    return render_prompt(BUILTIN_SPAWN, {"task": task})
