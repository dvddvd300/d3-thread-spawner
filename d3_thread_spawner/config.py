"""Configuration loading: TOML file + env vars + CLI flags."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

from .models import AgentSettings
from .util import log, log_verbose


def _default_cookies_path() -> str:
    """Return the default T3 Cookies DB path for the current platform."""
    if sys.platform == "darwin":
        return "~/Library/Application Support/t3code/Cookies"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return os.path.join(appdata, "t3code", "Network", "Cookies")
        return os.path.join("~", "AppData", "Roaming", "t3code", "Network", "Cookies")
    else:
        return "~/.config/t3code/Cookies"


# Built-in defaults
DEFAULTS = {
    "general": {
        "model": "opus",
        "mode": "build",
        "access": "full",
        "effort": "high",
        "base_branch": "main",
        "repo_dir": ".",
    },
    "batch": {
        "size": 5,
        "delay": 0,
        "launch_delay": 0.5,
        "initial_wait": 0,
    },
    "t3": {
        "project_id": "",
        "cookies_path": _default_cookies_path(),
        "runtime_json": "~/.t3/userdata/server-runtime.json",
        "state_db": "~/.t3/userdata/state.sqlite",
    },
    "worktree": {
        "dir": "~/d3ts-worktrees/{project}",
    },
    "github": {
        "repo": "",
    },
    "pr": {
        "max_prompt_chars": 100_000,
    },
    "models": {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
    },
    "model_options": {
        "context_window": "1m",
        "thinking": True,
        "fast_mode": False,
    },
}

# Env var mapping: D3TS_KEY -> (section, key)
ENV_MAP = {
    "D3TS_MODEL": ("general", "model"),
    "D3TS_MODE": ("general", "mode"),
    "D3TS_ACCESS": ("general", "access"),
    "D3TS_EFFORT": ("general", "effort"),
    "D3TS_BASE_BRANCH": ("general", "base_branch"),
    "D3TS_REPO_DIR": ("general", "repo_dir"),
    "D3TS_BATCH_SIZE": ("batch", "size"),
    "D3TS_BATCH_DELAY": ("batch", "delay"),
    "D3TS_LAUNCH_DELAY": ("batch", "launch_delay"),
    "D3TS_INITIAL_WAIT": ("batch", "initial_wait"),
    "D3TS_T3_PROJECT_ID": ("t3", "project_id"),
    "D3TS_T3_TOKEN": ("t3", "token"),
    "D3TS_GITHUB_REPO": ("github", "repo"),
    "D3TS_CONTEXT_WINDOW": ("model_options", "context_window"),
    "D3TS_PR_MAX_PROMPT_CHARS": ("pr", "max_prompt_chars"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base (one level deep)."""
    result = {}
    for key in set(list(base.keys()) + list(override.keys())):
        if key in override and key in base:
            if isinstance(base[key], dict) and isinstance(override[key], dict):
                result[key] = {**base[key], **override[key]}
            else:
                result[key] = override[key]
        elif key in override:
            result[key] = override[key]
        else:
            result[key] = base[key]
    return result


def find_project_config(start: Optional[str] = None) -> Optional[Path]:
    """Walk up from start directory looking for .d3ts.toml."""
    d = Path(start) if start else Path.cwd()
    for parent in [d, *d.parents]:
        candidate = parent / ".d3ts.toml"
        if candidate.is_file():
            return candidate
    return None


def _load_toml(path: Path) -> dict:
    """Load a TOML file."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def _apply_env(config: dict) -> dict:
    """Apply D3TS_* environment variables."""
    for env_key, (section, key) in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if section not in config:
            config[section] = {}
        # Type coercion based on defaults
        default_val = DEFAULTS.get(section, {}).get(key)
        if isinstance(default_val, bool):
            config[section][key] = val.lower() in ("1", "true", "yes")
        elif isinstance(default_val, int):
            config[section][key] = int(val)
        elif isinstance(default_val, float):
            config[section][key] = float(val)
        else:
            config[section][key] = val
    return config


def _apply_cli(config: dict, cli_args) -> dict:
    """Apply CLI flag overrides."""
    cli_map = {
        ("general", "model"): "model",
        ("general", "mode"): "mode",
        ("general", "access"): "access",
        ("general", "effort"): "effort",
        ("general", "base_branch"): "base_branch",
        ("general", "repo_dir"): "repo",
        ("batch", "size"): "batch_size",
        ("batch", "delay"): "batch_delay",
        ("batch", "launch_delay"): "launch_delay",
        ("batch", "initial_wait"): "initial_wait",
        ("t3", "project_id"): "project_id",
        ("model_options", "context_window"): "context_window",
        ("model_options", "thinking"): "thinking",
        ("model_options", "fast_mode"): "fast_mode",
        ("pr", "max_prompt_chars"): "max_prompt_chars",
    }
    for (section, key), attr in cli_map.items():
        val = getattr(cli_args, attr, None)
        if val is None:
            continue
        if section not in config:
            config[section] = {}
        config[section][key] = val

    if getattr(cli_args, "dry_run", False):
        config.setdefault("general", {})["dry_run"] = True
    if getattr(cli_args, "config", None):
        config["_config_path"] = cli_args.config

    return config


def _resolve_repo_dir(raw: str) -> str:
    """Resolve repo_dir: '.' means CWD, expand ~."""
    if raw == ".":
        return os.getcwd()
    return os.path.expanduser(raw)


def _auto_detect_github_repo(repo_dir: str) -> str:
    """Parse origin remote URL to get owner/name."""
    from .util import run
    try:
        result = run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            check=False,
        )
        if result.returncode != 0:
            return ""
        url = result.stdout.strip()
        # Handle SSH: git@github.com:owner/name.git
        m = __import__("re").match(r"git@github\.com:(.+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
        # Handle HTTPS: https://github.com/owner/name.git
        m = __import__("re").match(r"https://github\.com/(.+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _auto_detect_t3(config: dict) -> tuple:
    """Read host/port from server-runtime.json. Returns (host, port)."""
    import json
    runtime_path = os.path.expanduser(
        config.get("t3", {}).get("runtime_json", DEFAULTS["t3"]["runtime_json"])
    )
    try:
        with open(runtime_path) as f:
            data = json.load(f)
        host = data.get("host", "127.0.0.1")
        # 0.0.0.0 is a valid bind address but not a valid connect address on Windows
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return host, data.get("port", 3773)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return "127.0.0.1", 3773


def load_config(cli_args=None) -> AgentSettings:
    """Load configuration from all sources and return AgentSettings."""
    config = dict(DEFAULTS)

    # 1. Global config
    global_path = Path(os.path.expanduser("~/.config/d3ts/config.toml"))
    if global_path.is_file():
        log_verbose("📄", f"Loading global config: {global_path}")
        config = _deep_merge(config, _load_toml(global_path))

    # 2. Project config
    explicit_config = getattr(cli_args, "config", None) if cli_args else None
    if explicit_config:
        p = Path(os.path.expanduser(explicit_config))
        if p.is_file():
            log_verbose("📄", f"Loading config: {p}")
            config = _deep_merge(config, _load_toml(p))
    else:
        proj = find_project_config()
        if proj:
            log_verbose("📄", f"Loading project config: {proj}")
            config = _deep_merge(config, _load_toml(proj))

    # 3. Environment variables
    config = _apply_env(config)

    # 4. CLI flags
    if cli_args:
        config = _apply_cli(config, cli_args)

    # Auto-detect T3 connection
    t3_host, t3_port = _auto_detect_t3(config)

    # Resolve repo dir
    repo_dir = _resolve_repo_dir(config.get("general", {}).get("repo_dir", "."))

    # Auto-detect GitHub repo
    gh_repo = config.get("github", {}).get("repo", "")
    if not gh_repo:
        gh_repo = _auto_detect_github_repo(repo_dir)

    # Resolve worktree dir (normpath fixes mixed separators on Windows)
    project_name = os.path.basename(repo_dir)
    wt_dir = os.path.normpath(os.path.expanduser(
        config.get("worktree", {}).get("dir", DEFAULTS["worktree"]["dir"])
    ).replace("{project}", project_name))

    # Resolve cookies path
    cookies_path = os.path.normpath(os.path.expanduser(
        config.get("t3", {}).get("cookies_path", DEFAULTS["t3"]["cookies_path"])
    ))

    gen = config.get("general", {})
    batch = config.get("batch", {})
    t3 = config.get("t3", {})
    mo = config.get("model_options", {})
    pr_cfg = config.get("pr", {})

    return AgentSettings(
        model=gen.get("model", "opus"),
        mode=gen.get("mode", "build"),
        access=gen.get("access", "full"),
        effort=gen.get("effort", "high"),
        base_branch=gen.get("base_branch", "main"),
        repo_dir=repo_dir,
        context_window=mo.get("context_window", "1m"),
        thinking=mo.get("thinking", True),
        fast_mode=mo.get("fast_mode", False),
        batch_size=batch.get("size", 5),
        batch_delay=batch.get("delay", 0),
        launch_delay=batch.get("launch_delay", 0.5),
        initial_wait=batch.get("initial_wait", 0),
        dry_run=gen.get("dry_run", False),
        t3_host=t3_host,
        t3_port=t3_port,
        t3_project_id=t3.get("project_id", ""),
        cookies_path=cookies_path,
        worktree_dir=wt_dir,
        github_repo=gh_repo,
        model_aliases=config.get("models", DEFAULTS["models"]),
        max_prompt_chars=pr_cfg.get("max_prompt_chars", 100_000),
    )


def get_config_paths(cli_args=None) -> Dict[str, Optional[str]]:
    """Return which config files are being loaded (for debug)."""
    paths: Dict[str, Optional[str]] = {}

    global_path = Path(os.path.expanduser("~/.config/d3ts/config.toml"))
    paths["global"] = str(global_path) if global_path.is_file() else None

    explicit = getattr(cli_args, "config", None) if cli_args else None
    if explicit:
        paths["explicit"] = explicit
    else:
        proj = find_project_config()
        paths["project"] = str(proj) if proj else None

    return paths
