# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding
**Don't assume. Don't hide confusion. Surface tradeoffs.**
Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First
**Minimum code that solves the problem. Nothing speculative.**
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes
**Touch only what you must. Clean up only your own mess.**
When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.
When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.
The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution
**Define success criteria. Loop until verified.**
Transform tasks into verifiable goals:
- "Add validation" -> "Write tests for invalid inputs, then make them pass"
- "Fix the bug" -> "Write a test that reproduces it, then make it pass"
- "Refactor X" -> "Ensure tests pass before and after"
For multi-step tasks, state a brief plan with per-step verification.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

# Project: d3-thread-spawner

Linear project: **d3-thread-spawner** (team ZEU; project state is "completed" — file follow-up work as maintenance issues there).

Programmatic T3 Code thread launcher: spawns Claude/Codex agents in isolated git worktrees through T3 Code's local HTTP API — one-off prompts, prompt files, or JSONL batches (30+ tasks). Other subcommands: `pr` (address GitHub PR review threads), `review` (full local PR review), `triage` (one-shot triage across open PRs), `conflicts` (resolve merge conflicts across conflicting branches), `approve-plans` (schedule captured plans in quota-aware batches), `status`, `clean`, `config`.

## Stack
- Python 3.11+ (uses stdlib `tomllib`). **Stdlib only — no pip deps, no pyproject/requirements/Makefile.**
- External CLIs at runtime: `git` (always), `gh` (for `pr`/`triage`/`conflicts`).
- Transport is plain HTTP to the local T3 Code server (`util.http_post` → `/api/orchestration/dispatch`, `t3.py:208`).

## Layout
- `d3-spawn` — executable shim → `d3_thread_spawner/cli.py` (equivalent: `python3 -m d3_thread_spawner`).
- `d3_thread_spawner/` — `cli.py` (argparse subcommands), `config.py` (TOML load/merge + `D3TS_*` env), `models.py` (model aliases, provider routing, per-model option sets), `t3.py` (token/project-id discovery + dispatch), `plan_approval.py` (frozen proposed plans + quota signals), `batch.py`, `github.py`, `prompts.py` + `review_prompt.md` (bundled reviewer methodology), `worktree.py`, `commands/` (spawn, pr, review, triage, conflicts, approve_plans, status, clean, config_cmd).
- `tests/` — stdlib `unittest` suites. `examples/` — `config.toml`, task JSONL samples, prompt files.

## Run / test
- Run: `./d3-spawn <cmd>` — **global flags go BEFORE the subcommand** (`--model --mode --access --effort --context-window --repo --config --dry-run`).
- Tests: `python3 -m unittest discover -s tests -p 'test_*.py'` (plain `python3 -m unittest` discovers 0 tests).
- Install: none needed — optional `ln -s $(pwd)/d3-spawn ~/.local/bin/d3-spawn`.

## Config
- Precedence: defaults < `~/.config/d3ts/config.toml` < per-project `.d3ts.toml` (gitignored; found by walking up from cwd) < `D3TS_*` env vars < CLI flags (`config.py:151,286-302`).
- Auth: T3 session token auto-read from T3 Code's Cookies SQLite DB (macOS `~/Library/Application Support/t3code/Cookies`); override with `D3TS_T3_TOKEN`. Host/port come from `~/.t3/userdata/server-runtime.json`; project id is matched by repo path in `~/.t3/userdata/state.sqlite` (`t3.py:114-155`).

## Gotchas (verified in code as of 2026-07-10)
- **T3 Code must be running locally** — d3 is a thin HTTP dispatcher, not a model runner. Spawned workers execute inside T3 Code with its bundled Claude Code CLI (`opus` → Claude Opus 4.8 needs bundled CLI ≥ 2.1.154). If spawned workers die with "native binary not found at claude", that is a T3-host/PATH issue, not a d3 bug.
- **Provider routing is automatic from the resolved model slug** (`claude-*` → claudeAgent, `gpt-*` → codex; `gpt-*` is matched case-insensitively); there is no provider flag (`models.py:297-299`).
- **Model metadata cache is preferred when present** — `~/.t3/caches/{claudeAgent,codex}.json` supplies the available model list and option descriptors. If a configured alias/known built-in model is absent from the cache, d3 fails before creating a worktree or dispatching to T3; raw custom/new model ids pass through with no option assumptions (`models.py:112-158,316-333`; `batch.py:20-21`).
- **Model options are normalized before launch** — unsupported effort values become the highest real effort exposed by that known model; unsupported/missing `contextWindow` falls back to 200k; d3 sends only supported option ids and pins Codex `serviceTier=default` only for models that expose `serviceTier` (`models.py`). As of 2026-07-10, T3 advertises Codex efforts through `ultra` for GPT-5.6 Sol/Terra and through `max` for GPT-5.6 Luna; `ultra` is not Claude's `ultrathink`.
- **JSONL batches take user-facing fields only** (`model`, `mode`, `access`, `effort`, `context_window`, `thinking`, `fast_mode`) — never T3-internal option ids (`reasoningEffort`, `serviceTier`). Per-item option overrides are copied into each task's `AgentSettings` (`commands/spawn.py:24-25,65-74`).
- **Monthly model validation is a real ping-pong spawn check** — use `docs/model-validation.md` to generate tiny tasks from T3's current provider cache, launch them, and update `models.py` from observed successes/failures.
- **Quota-aware plan approval depends on fresh local provider events** — `approve-plans` reads Claude `account.rate-limits.updated` records from `~/.t3/userdata/logs/provider`; missing signals stop the next batch instead of guessing that quota remains (`plan_approval.py`, `commands/approve_plans.py`).
- **Windows:** the Cookies DB is exclusively locked by T3 Code — token auto-read fails; set `D3TS_T3_TOKEN` manually.
