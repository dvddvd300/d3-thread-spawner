<p align="center">
  <img src="favicon.svg" alt="d3-thread-spawner" width="64">
</p>

# d3-thread-spawner

Programmatic [T3 Code](https://t3.chat) thread launcher. Spawn Claude Code agents in isolated git worktrees via the T3 Code API — one at a time or in configurable batches.

## Features

- **Spawn T3 threads** with any prompt, inline or from files
- **Full T3 settings control** — model, mode, effort, context window, thinking, fast mode
- **Branch management** — work on existing branches or create new ones (with fork support)
- **Batch processing** — launch 30+ tasks with configurable batch size and delays
- **PR review** — fetch GitHub PR review threads and spawn agents to address them
- **Auto-detection** — T3 connection, project ID, and GitHub repo detected automatically
- **Config system** — TOML config files (global + per-project) with env var and CLI overrides

## Requirements

- Python 3.11+ (uses `tomllib` from stdlib)
- [T3 Code](https://t3.chat) running locally
- `git` CLI
- `gh` CLI (only for the `pr` command)
- No pip dependencies — stdlib only

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
```

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
model = "opus"              # opus, sonnet, haiku, or full model ID
mode = "build"              # build | plan (interaction mode)
access = "full"             # full | auto-accept | supervised (access level)
effort = "high"             # low | medium | high | max
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

[models]
opus = "claude-opus-4-6"
sonnet = "claude-sonnet-4-6"
haiku = "claude-haiku-4-5"

[model_options]
context_window = "1m"       # 200k or 1m
thinking = true
fast_mode = false
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
| `D3TS_BASE_BRANCH` | Default base branch |
| `D3TS_BATCH_SIZE` | Default batch size |
| `D3TS_INITIAL_WAIT` | Minutes to wait before first batch |
| `D3TS_GITHUB_REPO` | GitHub repo (owner/name) |

## Batch Processing

For launching many tasks, create a JSONL file (one JSON object per line):

```jsonl
{"name": "fix-auth", "prompt": "Fix the auth timeout bug", "new_branch": "bugfix/auth"}
{"name": "add-pagination", "prompt": "Add pagination to /users", "new_branch": "feature/pagination"}
{"name": "update-tests", "prompt": "Update payment service tests", "branch": "dev"}
```

### JSONL fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Thread name (used for worktree directory) |
| `prompt` | one of these | Inline prompt text |
| `prompt_file` | one of these | Path to a prompt file |
| `branch` | no | Existing branch to work on |
| `new_branch` | no | Create a new branch |
| `fork_from` | no | Branch to fork from (with `new_branch`) |
| `model` | no | Override model for this task |
| `mode` | no | Override interaction mode (build/plan) |
| `access` | no | Override access level (full/auto-accept/supervised) |
| `effort` | no | Override effort for this task |

Launch with:

```bash
d3-spawn spawn --from-file tasks.jsonl --batch-size 10 --batch-delay 5

# Wait 2 hours before starting, then launch 2 threads every 32 minutes
d3-spawn spawn --from-file tasks.jsonl --initial-wait 120 --batch-size 2 --batch-delay 32
```

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
| `--model MODEL` | Claude model alias or full ID |
| `--mode MODE` | Interaction mode: build or plan |
| `--access LEVEL` | Access level: full, auto-accept, or supervised |
| `--effort LEVEL` | low, medium, high, or max |
| `--context-window SIZE` | 200k or 1m |
| `--thinking / --no-thinking` | Enable/disable thinking |
| `--fast-mode / --no-fast-mode` | Enable/disable fast mode |
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
