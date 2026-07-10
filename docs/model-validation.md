# Model Validation Runbook

Use this monthly, or whenever T3 Code adds/removes models, to refresh
`d3_thread_spawner/models.py` with evidence from real launches.

## Goal

Validate the model option matrix by launching tiny "ping pong" tasks against
every model/option combination T3 currently advertises. Known built-in models
and aliases should be limited to the validated option values. Raw custom/new
model ids should pass through without option assumptions until they are tested.

## Steps

1. Start T3 Code and sign in.

2. Snapshot the current provider metadata.

```bash
python3 - <<'PY'
import json
from d3_thread_spawner.models import _cached_provider_model_options

for provider in ("claudeAgent", "codex"):
    print(f"\n== {provider}")
    models = _cached_provider_model_options(provider) or {}
    for model, options in sorted(models.items()):
        print(model)
        for option_id, values in sorted(options.items()):
            print(f"  {option_id}: {', '.join(values) if values else '(boolean/no values)'}")
PY
```

3. Generate ping-pong JSONL tasks from the advertised options.

```bash
tmp="$(mktemp)"
python3 - <<'PY' > "$tmp"
import json

from d3_thread_spawner.models import _cached_provider_model_options
from d3_thread_spawner.util import slugify

for provider, effort_key in (
    ("claudeAgent", "effort"),
    ("codex", "reasoningEffort"),
):
    models = _cached_provider_model_options(provider) or {}
    for model, options in sorted(models.items()):
        efforts = options.get(effort_key) or [None]
        contexts = options.get("contextWindow") or [None]
        for effort in efforts:
            for context in contexts:
                bits = [model, effort or "no-effort", context or "default-context"]
                name = "model-check-" + "-".join(slugify(bit, 30) for bit in bits)
                task = {
                    "name": name,
                    "new_branch": f"d3ts/{name}",
                    "model": model,
                    "prompt": "Reply exactly: pong",
                    "raw": True,
                }
                if effort:
                    task["effort"] = effort
                if context:
                    task["context_window"] = context
                print(json.dumps(task))
PY
echo "$tmp"
wc -l "$tmp"
```

4. Dry-run the matrix first.

```bash
./d3-spawn --dry-run --batch-size 1 --launch-delay 1 spawn --from-file "$tmp"
```

5. Launch the real ping-pong matrix when the dry-run looks right.

```bash
./d3-spawn --batch-size 1 --launch-delay 2 spawn --from-file "$tmp"
```

Answer `y` at the confirmation prompt. Check T3 Code, `./d3-spawn status`, and
`./d3-spawn output <thread_id> --wait` until every task either replies exactly
`pong` or shows a model/option error.

6. Update the code from the results.

- If T3 advertises a model and ping-pong succeeds, add/update its option values
  in `CLAUDE_MODEL_OPTIONS` or `CODEX_MODEL_OPTIONS`.
- If T3 no longer advertises a built-in model, remove or update the alias that
  points to it.
- Keep Codex efforts to actual `reasoningEffort` values from the cache. As of
  2026-07-10, GPT-5.6 Sol/Terra add `max` and `ultra`; GPT-5.6 Luna adds
  `max`. `ultra` is not Claude's `ultrathink`.
- Do not add custom/new raw model ids to the static matrix until they have
  passed this ping-pong check.

7. Verify the repo.

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q d3_thread_spawner tests
git diff --check
```
