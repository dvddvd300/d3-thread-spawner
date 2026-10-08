# d3-thread-spawner

Programmatic T3 Code thread launcher: spawns Claude/Codex agents in isolated git worktrees through T3 Code's local HTTP API (one-off prompts, prompt files, or JSONL batches). Other subcommands: `pr` (address PR review threads), `review` (local PR review), `triage` (one-shot triage across open PRs), `conflicts` (resolve merge conflicts across branches), `approve-plans` (schedule captured plans in quota-aware batches), `output` (read or wait for a spawned thread's reply), `status`, `clean`, `config`.

## Stack
- Python 3.11+ (stdlib `tomllib`). **Stdlib only: no pip deps, no pyproject/requirements/Makefile.**
- External CLIs at runtime: `git` (always), `gh` (for `pr`/`triage`/`conflicts`).
- Transport: plain HTTP to the local T3 Code server (`util.http_post` → `/api/orchestration/dispatch` in `t3.py`).

## Layout
- `d3-spawn`: executable shim → `d3_thread_spawner/cli.py` (same as `python3 -m d3_thread_spawner`).
- `d3_thread_spawner/`: `cli.py` (argparse), `config.py` (TOML load/merge + `D3TS_*` env), `models.py` (model aliases, provider routing, per-model option sets), `t3.py` (token/project-id discovery + dispatch), `cache.py`, `reader.py`, `plan_approval.py`, `batch.py`, `github.py`, `prompts.py` + `review_prompt.md`, `worktree.py`, `commands/`.
- `tests/`: stdlib `unittest`. `examples/`: `config.toml`, task JSONL, prompt files. `docs/model-validation.md`.

## Run / test
- Run: `./d3-spawn <cmd>`. **Global flags go BEFORE the subcommand** (`--model --provider-instance --mode --access --effort --context-window --repo --config --dry-run`).
- Tests: `python3 -m unittest discover -s tests -p 'test_*.py'` (plain `python3 -m unittest` finds 0 tests).
- Install: none; optionally symlink `d3-spawn` onto your `PATH`.

## Config and auth
- Precedence: defaults < `~/.config/d3ts/config.toml` < per-project `.d3ts.toml` (gitignored, found by walking up from cwd) < `D3TS_*` env vars < CLI flags.
- T3 token resolution (`t3.py`): `D3TS_T3_TOKEN` → rebuilt from the `auth_sessions` store in `~/.t3/userdata/state.sqlite` plus the server signing key → legacy T3 Cookies DB. Host/port come from `~/.t3/userdata/server-runtime.json`; the project id is matched by repo path in `state.sqlite`.

## Gotchas
- **T3 Code must be running locally**: d3 is a thin HTTP dispatcher, not a model runner. Workers run inside T3 Code with its bundled CLIs; "native binary not found at claude" is a T3-host/PATH issue, not a d3 bug.
- **Provider routing:** the driver comes from the model slug (`gpt-*`, case-insensitive → `codex`, else `claudeAgent`); the T3 provider instance is auto-discovered (ready instance advertising the model) or pinned with `--provider-instance` / `D3TS_PROVIDER_INSTANCE`.
- **Model metadata cache wins when present**: `~/.t3/caches/<provider-instance>.json` (e.g. `codex.json`) supplies available models and option descriptors. A configured alias/known model missing from the cache fails before any worktree is created; raw custom model ids pass through with no option assumptions.
- **Options are normalized before launch**: unsupported effort → highest effort that model exposes; unsupported/missing `contextWindow` → 200k; only supported option ids are sent; Codex `serviceTier=default` is pinned only for models exposing `serviceTier`. Codex `ultra` effort is not Claude's `ultrathink`.
- **JSONL batches take user-facing fields only** (`model`, `mode`, `access`, `effort`, `context_window`, `thinking`, `fast_mode`), never T3-internal ids (`reasoningEffort`, `serviceTier`). Per-item overrides are copied into each task's `AgentSettings` (`commands/spawn.py`).
- **Model list maintenance**: validate with a real ping-pong spawn per `docs/model-validation.md`, then update `models.py` from observed results.
- **Quota-aware plan approval needs fresh local provider events**: `approve-plans` reads Claude `account.rate-limits.updated` records from `~/.t3/userdata/logs/provider`; missing signals stop the next batch instead of guessing that quota remains.
