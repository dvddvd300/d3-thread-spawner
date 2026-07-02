<p align="center">
  <img src="favicon.svg" alt="d3-thread-spawner" width="64">
</p>

# d3-thread-spawner

Programmatic [T3 Code](https://t3.chat) thread launcher. Spawn Claude or GPT/Codex agents in isolated git worktrees via the T3 Code API — one at a time or in configurable batches.

## Features

- **Spawn T3 threads** with any prompt, inline or from files
- **Full T3 settings control** — model, mode, access, effort, GPT service tier, context window, thinking, fast mode
- **Branch management** — work on existing branches or create new ones (with fork support)
- **Batch processing** — launch 30+ tasks with configurable batch size and delays
- **PR review** — fetch GitHub PR review threads and spawn agents to address them
- **Local PR review** — spawn a reviewer thread per PR that generates a full review (verdict + paste-ready comments) you can read
- **PR triage** — one-shot status report across all open PRs (conflicts, CI, reviews, ready)
- **Conflict resolution** — resolve merge conflicts across every conflicting branch with one command
- **Auto-detection** — T3 connection, project ID, and GitHub repo detected automatically
- **Config system** — TOML config files (global + per-project) with env var and CLI overrides

## Requirements

- Python 3.11+ (uses `tomllib` from stdlib)
- [T3 Code](https://t3.chat) running locally
- `git` CLI
- `gh` CLI (for the `pr`, `triage`, and `conflicts` commands)
- No pip dependencies — stdlib only

> **Models & effort.** The `opus` alias maps to **Claude Opus 4.8**, which needs
> T3 Code's bundled Claude Code CLI **≥ 2.1.154** (Opus 4.7 needs ≥ 2.1.111).
> GPT models support `low`/`medium`/`high`/`xhigh`; Opus 4.8 also exposes
> `max`, `ultracode`, and `ultrathink`.
> GPT models use the `codex` provider with `reasoningEffort` and optional
> `serviceTier`; Claude models use `claudeAgent` with Claude-specific option ids.
> d3 sends only the options each model/provider supports, and T3 clamps an
> unsupported effort to the model's default.

## Provider Routing

d3 chooses the T3 provider automatically from the resolved model slug. There is
no separate provider flag.

| Model slug / alias | T3 provider | Effort option sent to T3 | Provider-specific options |
|--------------------|-------------|--------------------------|---------------------------|
| `opus`, `sonnet`, `haiku`, `claude-*` | `claudeAgent` | `effort` | `context_window`, `thinking`, `fast_mode` only when that Claude model exposes them |
| `gpt55`, `gpt5.5`, `gpt-*` | `codex` | `reasoningEffort` | `service_tier` only when that GPT/Codex model exposes it |

Agent rules:

- Put global flags before the subcommand: `d3-spawn --model gpt55 spawn "..."`.
- Use user-facing fields only: `model`, `effort`, `service_tier`, `context_window`, `thinking`, `fast_mode`. Do not put T3 internal option ids such as `reasoningEffort` or `serviceTier` in JSONL.
- `service_tier = "standard"` maps to T3's `default`; `service_tier = "fast"` maps to T3's `priority`.
- Claude tasks ignore `service_tier`; GPT/Codex tasks ignore Claude-only options such as `context_window`, `thinking`, and `fast_mode`.
- Mixed-provider batches are supported by setting `model` per JSONL line. Batch size controls launch grouping only; it does not need to match the provider.
- Use `--dry-run` before launching a mixed batch. The preview prints `provider`, resolved `model`, and final `options`.

## Quick Start

```bash
# Clone the repo
git clone https://github.com/dvddvd300/d3-thread-spawner.git
cd d3-thread-spawner

# Make the entry point executable (already done if you cloned)
chmod +x d3-spawn

# Spawn a single thread
./d3-spawn spawn "Fix the authentication bug" --repo ~/my-project

# Dry run — see what would launch without doing it
./d3-spawn spawn "Fix it" --dry-run

# Create a project config
./d3-spawn config --init
```

## Installation

No installation needed. Clone and run directly:

```bash
# Option 1: Run the entry point
./d3-spawn spawn "your prompt"

# Option 2: Run as Python module
python3 -m d3_thread_spawner spawn "your prompt"

# Option 3: Symlink to your PATH
ln -s $(pwd)/d3-spawn ~/.local/bin/d3-spawn
```

## Commands

### `spawn` — Launch threads with prompts

```bash
# Inline prompt
d3-spawn spawn "Refactor the payment service error handling"

# From a file
d3-spawn spawn --file ~/prompts/refactor-payments.txt

# Batch from JSONL (one task per line)
d3-spawn spawn --from-file tasks.jsonl

# With a custom name and branch
d3-spawn spawn "Add test coverage" --name add-tests --new-branch feature/tests

# Work on an existing branch (no new branch)
d3-spawn spawn "Fix lint errors" --branch feature/my-feature

# Fork from a specific branch
d3-spawn spawn "Cherry-pick fix" --new-branch hotfix/auth --fork-from release/v2

# Use a custom prompt template
d3-spawn spawn "PROJ-123: Fix login timeout" --template ~/prompts/my-template.txt

# Override settings
d3-spawn --model sonnet --mode build --access full --effort max spawn "Quick fix"
d3-spawn --model gpt55 --service-tier standard --effort xhigh spawn "Quick fix"

# Preview exact provider routing without launching
d3-spawn --model gpt55 --service-tier standard --effort xhigh --dry-run \
  spawn "Check provider payload"
```

### `pr` — Address PR review comments

Fetches unresolved review threads from GitHub PRs and spawns agents to address them. Works with CodeRabbit, human reviewers, or any tool that leaves PR comments.

```bash
# All unresolved comments on PR #58
d3-spawn pr 58

# Only CodeRabbit comments
d3-spawn pr 58 --reviewer coderabbitai

# Multiple PRs
d3-spawn pr 58 61

# All my open PRs with pending reviews
d3-spawn pr --open --mine

# One agent per review thread (most granular)
d3-spawn pr 58 --per-thread

# Include resolved/outdated threads
d3-spawn pr 58 --include-resolved --include-outdated

# Wait out a short rate-limit reset and resume (instead of aborting)
d3-spawn pr --open --mine --reviewer coderabbitai --wait

# Ignore the local cache and re-fetch everything
d3-spawn pr --open --no-cache
```

**Rate limits & caching.** Review threads are fetched with batched GraphQL
queries and cached locally (keyed by each PR's `updatedAt`), so repeated runs
only re-fetch PRs that actually changed. When GraphQL is rate-limited the tool
automatically falls back to the REST API (a separate budget); `--wait` instead
sleeps until the GraphQL budget resets (capped by `--wait-max-seconds`, default
300) and resumes, which preserves resolved/outdated filtering. If a hard limit is
hit mid-run, already-fetched PRs are still launched and a resume command for the
rest is printed.

| `pr` flag | Description |
|---|---|
| `--reviewer LOGIN` | Only threads whose author matches `LOGIN` (`[bot]` suffix ignored) |
| `--per-thread` | One agent per thread (default: one per PR) |
| `--include-resolved` / `--include-outdated` | Include resolved / outdated threads |
| `--wait` | On rate-limit, sleep until reset and resume on GraphQL |
| `--wait-max-seconds N` | Cap for `--wait`; beyond it, fall back to REST (default 300) |
| `--no-cache` | Ignore the local PR-thread cache and re-fetch |

### `review` — Spawn a local reviewer thread per PR

Where `pr` **addresses** existing review comments, `review` **generates** the
review. It spawns one autonomous T3 thread per pull request that acts as a
senior code reviewer: each thread checks out the PR branch **read-only**,
follows a thorough review methodology (scope, sync, N+1 / efficiency, data-type
& contract analysis, bug-risk audit, comment coverage, conventions), and posts a
complete review — verdict, paste-ready comments tagged 🔴/🟡/🟢, and an action
items table — as its thread output. One thread per PR, so you can read each
review on its own.

```bash
# Review one PR
d3-spawn review 58

# Review several
d3-spawn review 58 61

# Review all my open PRs (one reviewer thread each)
d3-spawn review --open --mine

# Review every open PR
d3-spawn review --open

# Use your own reviewer methodology instead of the bundled one
d3-spawn review --open --mine --review-prompt ~/my-review-guide.md
```

The reviewer threads are **read-only** — they don't edit, commit, or push; the
review itself is the deliverable. The methodology is shipped with the tool
([`d3_thread_spawner/review_prompt.md`](d3_thread_spawner/review_prompt.md)) and
is stack-agnostic (it detects the language/framework/ORM from the repo). Point
`--review-prompt` (or `[review] prompt_file`) at your own file to customize it.

| `review` flag | Description |
|---|---|
| `--open` | Review all open PRs (combine with `--mine`) |
| `--mine` | Only my PRs |
| `--review-prompt PATH` | Custom reviewer methodology file (default: bundled guide) |

> **`pr` vs `review`:** `pr --reviewer coderabbitai` fetches CodeRabbit's
> comments and spawns agents to *fix* them; `review` spawns agents to *be* the
> reviewer and write the review for you to read.

### `triage` — Classify open PRs at a glance

Prints a grouped status report for every open PR — merge conflicts, failing CI,
changes requested, behind base, awaiting review, ready to merge, draft — from a
single `gh` call. Read-only by default; add `--resolve-conflicts` to also spawn
conflict-resolution threads for the conflicting ones.

```bash
# Status report for all open PRs
d3-spawn triage

# Only my PRs
d3-spawn triage --mine

# Specific PRs
d3-spawn triage 58 61

# Report, then resolve every conflicting PR in one go
d3-spawn triage --resolve-conflicts
```

Example output:

```
🔴 CONFLICTS (2)
    #58 ← feature/auth   Fix auth timeout         @alice   ci:✅  rev:✋
    #61 ← feature/pages  Add pagination           @bob     ci:❌
🟢 READY TO MERGE (3)
    ...

  Summary: 2 conflicts · 1 ci failing · 3 ready to merge
💡 Resolve all 2 conflict(s): d3-spawn conflicts
```

### `conflicts` — Resolve merge conflicts across all branches

Finds open PRs that conflict with their base branch (or the PRs you name) and
spawns one autonomous T3 thread per branch. Each agent merges the base in,
resolves the conflicts (preserving both sides' intent), runs tests/lint, and
pushes — stopping only if a conflict is genuinely ambiguous or verification
fails. One command, all the branches.

```bash
# Resolve conflicts on every conflicting open PR (merge strategy)
d3-spawn conflicts

# Only my PRs
d3-spawn conflicts --mine

# Specific PRs
d3-spawn conflicts 58 61

# Rebase onto base instead of merging (force-pushes with lease)
d3-spawn conflicts --rebase

# Go slow: one branch at a time, two minutes between launches
d3-spawn conflicts --mine --rebase --batch-size 1 --batch-delay 2
```

Conflict launches are batched like everything else, but can be paced
**independently** of ordinary `spawn` runs via the `[conflicts]` config keys
(`batch_size`, `batch_delay`, `launch_delay`, `initial_wait`) — unset keys
inherit `[batch]`. This is handy for `--rebase`, which force-pushes each branch:
set `[conflicts] batch_size = 1` and a `batch_delay` to roll them out one at a
time. The global `--batch-size`/`--batch-delay` flags also apply for a one-off
slow run.

| `conflicts` / `triage` flag | Description |
|---|---|
| `--mine` | Only my PRs |
| `--merge` | Merge base into the branch (no force-push) — the default |
| `--rebase` | Rebase the branch onto base (force-pushes with `--force-with-lease`) |
| `--force-rebase-protected` | Let `--rebase` force-push a protected (shared/long-lived) branch; off by default |
| `--resolve-conflicts` | (`triage` only) launch conflict resolution after the report |

> **Shared-branch guard.** `--rebase` force-pushes, which **rewrites history**. On a
> shared/long-lived branch (`dev`, `main`, `develop`, `release/*`, …) that breaks every
> open PR based on it (their diffs explode) and forces teammates' clones to diverge. So
> under `--rebase`, a head branch matching `[conflicts] protected_branches` is
> **automatically downgraded to merge** (a normal push, no rewrite) — the conflict is
> still resolved, safely. Pass `--force-rebase-protected` (or set
> `[conflicts] rebase_protected = true`) only when you truly intend to rewrite that
> branch. Feature/PR branches rebase as normal.

GitHub computes mergeability asynchronously, so PRs that report `UNKNOWN` are
re-checked once before the conflicting/clean split is finalized.

### `status` — Show active threads

```bash
d3-spawn status
```

### `clean` — Remove worktrees and temp files

```bash
d3-spawn clean                   # remove launcher scripts
d3-spawn clean --worktrees       # also remove ALL worktrees (destructive!)
```

### `config` — Configuration management

```bash
d3-spawn config                  # show resolved configuration
d3-spawn config --init           # create .d3ts.toml template in CWD
d3-spawn config --path           # show which config files are loaded
```

## Configuration

Configuration is resolved in order (later wins):

1. **Built-in defaults**
2. **Global config**: `~/.config/d3ts/config.toml`
3. **Project config**: `.d3ts.toml` in your repo root
4. **Environment variables**: `D3TS_*` prefix
5. **CLI flags**

### Config file

Create with `d3-spawn config --init` or manually:

```toml
# .d3ts.toml (in your repo root)

[general]
model = "opus"              # opus, sonnet, haiku, gpt55, or full model ID
mode = "build"              # build | plan (interaction mode)
access = "full"             # full | auto-accept | supervised (access level)
effort = "high"             # low | medium | high | xhigh | max | ultracode | ultrathink
base_branch = "main"

[batch]
size = 5                    # threads per batch
delay = 0                   # minutes between batches
launch_delay = 0.5          # seconds between individual launches
initial_wait = 0              # minutes to wait before first batch

[t3]
project_id = ""             # auto-detected from T3 state if empty

[worktree]
dir = "~/d3ts-worktrees/{project}"   # {project} = repo dir name

[github]
# repo = "owner/name"      # auto-detected from git remote

[review]
# prompt_file = "~/my-review-guide.md"   # custom reviewer methodology for the
                            # `review` command (default: the bundled generic guide)

[conflicts]
strategy = "merge"          # "merge" (base into branch) or "rebase" (onto base)
# Safety guard: under "rebase", a head branch matching protected_branches is
# auto-downgraded to merge (force-pushing a shared branch rewrites history that
# dependent PRs and clones rely on). Override with --force-rebase-protected.
# protected_branches = ["main", "master", "develop", "dev", "staging", "stage", "production", "prod", "release", "next", "trunk"]
# rebase_protected = false  # true ⇒ allow rebasing protected branches anyway
# Batch pacing for conflict resolution, overriding [batch] for this command only.
# Unset keys inherit [batch], so conflicts run at the normal pace unless slowed
# here — useful for --rebase (force-pushes each branch) to avoid hammering CI.
# batch_size = 1            # conflict threads per batch (default: inherit [batch])
# batch_delay = 2           # minutes between conflict batches (default: inherit)
# launch_delay = 1.0        # seconds between individual conflict launches (default: inherit)
# initial_wait = 0          # minutes before the first conflict batch (default: inherit)

[models]
# Provider routing is automatic from the resolved model slug:
#   claude-* → T3 provider "claudeAgent"
#   gpt-*    → T3 provider "codex"
opus = "claude-opus-4-8"    # needs T3's Claude Code CLI >= 2.1.154
sonnet = "claude-sonnet-4-6"
haiku = "claude-haiku-4-5"
gpt55 = "gpt-5.5"

[model_options]
# Sent only when the chosen model/provider supports them.
# JSONL tasks may override these per line.
service_tier = "standard"   # GPT/Codex: standard/default or fast/priority
context_window = "1m"       # Claude models that expose 200k/1m
thinking = true             # Haiku 4.5 only
fast_mode = false           # Claude models that expose Fast Mode
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `D3TS_T3_TOKEN` | Explicit T3 session token (skips cookies DB lookup) |
| `D3TS_T3_PROJECT_ID` | T3 project UUID |
| `D3TS_MODEL` | Default model |
| `D3TS_MODE` | Interaction mode (build/plan) |
| `D3TS_ACCESS` | Access level (full/auto-accept/supervised) |
| `D3TS_EFFORT` | Default effort level |
| `D3TS_SERVICE_TIER` | GPT/Codex service tier (`standard`/`default` or `fast`/`priority`) |
| `D3TS_BASE_BRANCH` | Default base branch |
| `D3TS_BATCH_SIZE` | Default batch size |
| `D3TS_INITIAL_WAIT` | Minutes to wait before first batch |
| `D3TS_GITHUB_REPO` | GitHub repo (owner/name) |
| `D3TS_WAIT` | Auto-wait for rate-limit reset (true/false) |
| `D3TS_WAIT_MAX_SECONDS` | Cap for auto-wait (default 300) |
| `D3TS_CACHE` | Use the local PR-thread cache (true/false) |
| `D3TS_CACHE_DIR` | PR-thread cache location |
| `D3TS_REVIEW_PROMPT` | Custom reviewer methodology file for the `review` command |
| `D3TS_CONFLICT_STRATEGY` | Conflict resolution strategy (`merge` or `rebase`) |
| `D3TS_CONFLICT_REBASE_PROTECTED` | Allow rebasing protected/shared branches (true/false; default false) |
| `D3TS_CONFLICT_BATCH_SIZE` | Conflict threads per batch (default: inherit `[batch]`) |
| `D3TS_CONFLICT_BATCH_DELAY` | Minutes between conflict batches (default: inherit) |
| `D3TS_CONFLICT_LAUNCH_DELAY` | Seconds between individual conflict launches (default: inherit) |
| `D3TS_CONFLICT_INITIAL_WAIT` | Minutes before the first conflict batch (default: inherit) |

## Batch Processing

For launching many tasks, create a JSONL file (one JSON object per line):

```jsonl
{"name": "fix-auth", "prompt": "Fix the auth timeout bug", "new_branch": "bugfix/auth"}
{"name": "add-pagination", "prompt": "Add pagination to /users", "new_branch": "feature/pagination"}
{"name": "update-tests", "prompt": "Update payment service tests", "branch": "dev"}
```

Each line can override the model/provider. Global CLI flags and config provide
defaults; JSONL fields override them for that one thread.

### JSONL fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Thread name (used for worktree directory) |
| `prompt` | one of these | Inline prompt text |
| `prompt_file` | one of these | Path to a prompt file |
| `branch` | no | Existing branch to work on |
| `new_branch` | no | Create a new branch |
| `fork_from` | no | Branch to fork from (with `new_branch`) |
| `model` | no | Override model for this task (`gpt55`, `opus`, `claude-*`, `gpt-*`) |
| `mode` | no | Override interaction mode (build/plan) |
| `access` | no | Override access level (full/auto-accept/supervised) |
| `effort` | no | Override effort for this task |
| `service_tier` | no | Override GPT/Codex service tier for this task |
| `context_window` | no | Override Claude context window for this task |
| `thinking` | no | Override Claude thinking toggle for this task |
| `fast_mode` | no | Override Claude fast-mode toggle for this task |

Launch with:

```bash
d3-spawn spawn --from-file tasks.jsonl --batch-size 10 --batch-delay 5

# Three at a time, full access, GPT/Codex Standard tier.
d3-spawn --batch-size 3 --access full --service-tier standard \
  spawn --from-file tasks.jsonl

# Wait 2 hours before starting, then launch 2 threads every 32 minutes
d3-spawn spawn --from-file tasks.jsonl --initial-wait 120 --batch-size 2 --batch-delay 32
```

Mixed-provider JSONL example:

```jsonl
{"name": "gpt-a", "prompt": "Task A", "new_branch": "d3ts/gpt-a", "model": "gpt55", "effort": "xhigh", "service_tier": "standard"}
{"name": "gpt-b", "prompt": "Task B", "new_branch": "d3ts/gpt-b", "model": "gpt55", "effort": "xhigh", "service_tier": "standard"}
{"name": "opus-max", "prompt": "Task C", "new_branch": "d3ts/opus-max", "model": "opus", "effort": "max"}
```

Launch it three at a time with full access:

```bash
d3-spawn --batch-size 3 --access full spawn --from-file examples/tasks-mixed-providers.jsonl
```

Dry-run first to verify provider routing:

```bash
d3-spawn --batch-size 3 --access full --dry-run \
  spawn --from-file examples/tasks-mixed-providers.jsonl
```

Expected provider lines in dry-run output:

```text
provider: codex        model: gpt-5.5          options: [reasoningEffort=xhigh, serviceTier=default]
provider: claudeAgent  model: claude-opus-4-8  options: [effort=max]
```

For agents: prefer this pattern when the user asks for "some GPT and some
Claude". Put shared execution behavior (`--batch-size`, `--access`, delays) in
global flags, then put model-specific choices (`model`, `effort`,
`service_tier`) on each JSONL line.

## T3 Connection

d3-spawn connects to T3 Code's local HTTP API. Connection details are auto-detected:

- **Host/Port**: Read from `~/.t3/userdata/server-runtime.json`
- **Session Token**: Extracted from T3's cookies database, or set `D3TS_T3_TOKEN`
- **Project ID**: Matched from `~/.t3/userdata/state.sqlite` by repo path, or set in config

### How it works

1. Creates a git worktree for the task
2. Dispatches `thread.create` to T3's orchestration API
3. Dispatches `thread.turn.start` with the prompt
4. The thread appears in T3 Code's sidebar, running autonomously

## Global Flags

All flags go **before** the subcommand:

```bash
d3-spawn [flags] <command> [command-flags]
```

| Flag | Description |
|------|-------------|
| `--model MODEL` | Model alias (`opus`→Claude Opus 4.8, `sonnet`, `haiku`, `gpt55`) or full ID |
| `--mode MODE` | Interaction mode: build or plan |
| `--access LEVEL` | Access level: full, auto-accept, or supervised |
| `--effort LEVEL` | low, medium, high, xhigh, max, ultracode, or ultrathink |
| `--service-tier TIER` | GPT/Codex service tier: standard/default or fast/priority |
| `--context-window SIZE` | 200k or 1m for Claude models that expose it |
| `--thinking / --no-thinking` | Enable/disable thinking (Haiku 4.5 only) |
| `--fast-mode / --no-fast-mode` | Enable/disable fast mode (Opus 4.5/4.6 only) |
| `--batch-size N` | Threads per batch |
| `--batch-delay M` | Minutes between batches |
| `--launch-delay S` | Seconds between launches |
| `--initial-wait M` | Minutes to wait before first batch |
| `--base-branch BRANCH` | Base git branch |
| `--repo PATH` | Path to repository |
| `--project-id UUID` | T3 project ID |
| `--dry-run` | Preview without launching |
| `--verbose / -v` | Verbose output |
| `--config PATH` | Explicit config file |

## Custom Prompt Templates

Create your own prompt templates with `{variable}` placeholders:

```text
You are working on a NestJS backend. Read CLAUDE.md for conventions.

TASK: {task}

Follow these steps:
1. Understand the problem
2. Plan your approach
3. Implement and test
4. Commit and push
```

Use with:

```bash
d3-spawn spawn "Fix the login bug" --template my-template.txt
d3-spawn spawn "PROJ-123" --template my-template.txt --var task_id=PROJ-123
```

See `examples/prompts/` for more template examples.

## License

GPLv3 — see [LICENSE](LICENSE).
