"""Data models for d3-thread-spawner."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Dict, List, Optional


# Per-model option capabilities for T3 providers.
#
# T3 caches the live provider metadata in ~/.t3/caches/*.json. We prefer that
# metadata when present and keep these tables as a fallback for dry systems or
# tests. Each model only consumes the option ids it declares here; sending
# provider-specific options to the wrong provider either gets ignored or can
# produce invalid model ids, so selection and options are both provider-aware.
CLAUDE_MODEL_OPTIONS: Dict[str, frozenset] = {
    "claude-fable-5": frozenset({"effort", "contextWindow"}),
    "claude-opus-4-8": frozenset({"effort", "fastMode"}),
    "claude-opus-4-7": frozenset({"effort", "fastMode"}),
    "claude-opus-4-6": frozenset({"effort", "fastMode", "contextWindow"}),
    "claude-opus-4-5": frozenset({"effort", "fastMode"}),
    "claude-sonnet-5": frozenset({"effort", "contextWindow"}),
    "claude-sonnet-4-6": frozenset({"effort", "contextWindow"}),
    "claude-haiku-4-5": frozenset({"thinking"}),
}

CODEX_MODEL_OPTIONS: Dict[str, frozenset] = {
    "gpt-5.5": frozenset({"reasoningEffort", "serviceTier"}),
    "gpt-5.4": frozenset({"reasoningEffort", "serviceTier"}),
    "gpt-5.4-mini": frozenset({"reasoningEffort"}),
    "gpt-5.3-codex": frozenset({"reasoningEffort"}),
    "gpt-5.2": frozenset({"reasoningEffort"}),
}

# Conservative fallbacks for unknown slugs. Live cache metadata can still
# enable richer options for custom models; without metadata, avoid risky
# options like Claude contextWindow because T3 may rewrite the API model id.
DEFAULT_CLAUDE_MODEL_OPTIONS: frozenset = frozenset({"effort"})
DEFAULT_CODEX_MODEL_OPTIONS: frozenset = frozenset({"reasoningEffort"})

PROVIDER_CACHE_FILES: Dict[str, str] = {
    "claudeAgent": "~/.t3/caches/claudeAgent.json",
    "codex": "~/.t3/caches/codex.json",
}

SERVICE_TIER_ALIASES: Dict[str, str] = {
    "standard": "default",
    "default": "default",
    "fast": "priority",
    "priority": "priority",
}


@lru_cache(maxsize=None)
def _cached_provider_options(provider: str) -> Dict[str, frozenset]:
    """Return model -> option ids from T3's live provider cache, if present."""
    path = PROVIDER_CACHE_FILES.get(provider)
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    models: Dict[str, frozenset] = {}
    for entry in data.get("models", []):
        slug = entry.get("slug")
        if not slug:
            continue
        descriptors = (
            entry.get("capabilities", {}).get("optionDescriptors", []) or []
        )
        ids = frozenset(
            d.get("id") for d in descriptors
            if isinstance(d, dict) and d.get("id")
        )
        models[slug] = ids
    return models


def _normalize_model_id(model: str) -> str:
    """Normalize common shorthand that T3 itself does not accept as a slug."""
    raw = (model or "").strip()
    lowered = raw.lower()
    if lowered.startswith("gpt") and not lowered.startswith("gpt-"):
        suffix = lowered[3:]
        if suffix and suffix[0].isdigit():
            return f"gpt-{suffix}"
    return raw


# Shared/long-lived integration branches that must NEVER be rebased + force-pushed.
# Force-pushing such a branch rewrites history that every open PR based on it — and
# every teammate's clone — depends on. The conflicts flow auto-downgrades --rebase to
# merge for any head branch matching this list (override: conflict_rebase_protected).
DEFAULT_PROTECTED_BRANCHES: List[str] = [
    "main", "master", "develop", "dev", "staging", "stage",
    "production", "prod", "release", "next", "trunk",
]


@dataclass
class AgentSettings:
    """Resolved settings for a single agent launch."""

    model: str = "opus"
    mode: str = "build"           # "build" or "plan" (interaction mode)
    access: str = "full"          # "full", "auto-accept", "supervised" (runtime mode)
    effort: str = "high"
    service_tier: str = "default"  # GPT/Codex: "default"/"standard" or "priority"/"fast"
    base_branch: str = "main"
    repo_dir: str = "."
    context_window: str = "1m"
    thinking: bool = True
    fast_mode: bool = False
    batch_size: int = 5
    batch_delay: int = 0
    launch_delay: float = 0.5
    initial_wait: int = 0
    dry_run: bool = False
    max_prompt_chars: int = 100_000

    # Local PR review (the `review` command). Empty ⇒ use the bundled generic
    # reviewer guide (d3_thread_spawner/review_prompt.md).
    review_prompt_file: str = ""

    # T3
    t3_host: str = "127.0.0.1"
    t3_port: int = 3773
    t3_project_id: str = ""

    # Worktree
    worktree_dir: str = ""

    # Paths
    cookies_path: str = ""

    # GitHub
    github_repo: str = ""
    wait: bool = False                # auto-wait for rate-limit reset
    wait_max_seconds: int = 300       # cap on auto-wait
    cache: bool = True                # use the local PR-thread cache
    cache_dir: str = "~/.config/d3ts/cache"

    # Conflict resolution
    conflict_strategy: str = "merge"  # "merge" (default) or "rebase"

    # Branch-safety guard for the rebase strategy. Rebasing + force-pushing a
    # shared/long-lived integration branch (dev, main, release/*) rewrites history
    # every dependent PR and clone relies on, so under --rebase a head branch in
    # this list is auto-downgraded to merge. Set conflict_rebase_protected (the
    # --force-rebase-protected flag) to override for the rare intentional case.
    conflict_protected_branches: List[str] = field(
        default_factory=lambda: list(DEFAULT_PROTECTED_BRANCHES)
    )
    conflict_rebase_protected: bool = False

    # Conflict-resolution batch pacing. Each is an override for the matching
    # batch_* field above, applied only to the conflicts flow (the `conflicts`
    # command and `triage --resolve-conflicts`). None ⇒ inherit the global value.
    conflict_batch_size: Optional[int] = None
    conflict_batch_delay: Optional[int] = None
    conflict_launch_delay: Optional[float] = None
    conflict_initial_wait: Optional[int] = None

    # Model aliases
    model_aliases: Dict[str, str] = field(default_factory=lambda: {
        "opus": "claude-opus-4-8",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
        "gpt55": "gpt-5.5",
        "gpt5.5": "gpt-5.5",
    })

    @property
    def t3_api(self) -> str:
        return f"http://{self.t3_host}:{self.t3_port}"

    @property
    def resolved_model(self) -> str:
        """Resolve model alias to full model ID."""
        return _normalize_model_id(self.model_aliases.get(self.model, self.model))

    @property
    def model_provider(self) -> str:
        """Resolve the T3 provider/instance id for the selected model."""
        model = self.resolved_model
        for provider in ("codex", "claudeAgent"):
            if model in _cached_provider_options(provider):
                return provider
        if model.startswith("gpt-"):
            return "codex"
        return "claudeAgent"

    @property
    def normalized_service_tier(self) -> str:
        tier = (self.service_tier or "").strip().lower()
        return SERVICE_TIER_ALIASES.get(tier, tier)

    def _supported_option_ids(self) -> frozenset:
        provider = self.model_provider
        model = self.resolved_model
        cached = _cached_provider_options(provider).get(model)
        if cached is not None:
            return cached
        if provider == "codex":
            return CODEX_MODEL_OPTIONS.get(model, DEFAULT_CODEX_MODEL_OPTIONS)
        return CLAUDE_MODEL_OPTIONS.get(model, DEFAULT_CLAUDE_MODEL_OPTIONS)

    def model_selection_options(self) -> List[Dict[str, Any]]:
        """Build the canonical ``[{id, value}]`` options array for T3.

        T3 Code expects ``modelSelection.options`` as an array of
        ``{"id": ..., "value": ...}`` entries. We emit only the options the
        resolved model/provider actually supports: Claude uses ``effort`` while
        Codex/GPT uses ``reasoningEffort`` and optional ``serviceTier``.
        """
        supported = self._supported_option_ids()
        options: List[Dict[str, Any]] = []
        if "effort" in supported and self.effort:
            options.append({"id": "effort", "value": self.effort})
        if "reasoningEffort" in supported and self.effort:
            options.append({"id": "reasoningEffort", "value": self.effort})
        if "serviceTier" in supported and self.normalized_service_tier:
            options.append({"id": "serviceTier", "value": self.normalized_service_tier})
        if "contextWindow" in supported and self.context_window:
            options.append({"id": "contextWindow", "value": self.context_window})
        if "fastMode" in supported and self.fast_mode:
            options.append({"id": "fastMode", "value": self.fast_mode})
        if "thinking" in supported:
            options.append({"id": "thinking", "value": self.thinking})
        return options

    def model_selection(self) -> Dict[str, Any]:
        """Build T3's modelSelection payload."""
        provider = self.model_provider
        return {
            "instanceId": provider,
            "provider": provider,
            "model": self.resolved_model,
            "options": self.model_selection_options(),
        }

    def for_conflict_batch(self) -> "AgentSettings":
        """Return a copy whose batch pacing reflects the ``[conflicts]`` overrides.

        The ``conflicts`` command (and ``triage --resolve-conflicts``) launch
        autonomous threads that resolve and *push* — under ``--rebase`` they
        even force-push. So they can be paced independently of ordinary spawns,
        ``[conflicts]`` may carry its own ``batch_size``/``batch_delay``/
        ``launch_delay``/``initial_wait``. Each unset override (``None``)
        inherits the global ``[batch]`` value, so conflicts run at the normal
        pace unless explicitly slowed down.
        """
        return replace(
            self,
            batch_size=(
                self.batch_size if self.conflict_batch_size is None
                else self.conflict_batch_size
            ),
            batch_delay=(
                self.batch_delay if self.conflict_batch_delay is None
                else self.conflict_batch_delay
            ),
            launch_delay=(
                self.launch_delay if self.conflict_launch_delay is None
                else self.conflict_launch_delay
            ),
            initial_wait=(
                self.initial_wait if self.conflict_initial_wait is None
                else self.conflict_initial_wait
            ),
        )

    def is_protected_branch(self, branch: str) -> bool:
        """True if *branch* is a shared/long-lived integration branch that must
        never be rebased + force-pushed.

        Matches the full ref name OR its first path segment (case-insensitive), so
        ``dev`` and ``release/2.28`` match while ``feature/x`` / ``bugfix/y`` do not.
        """
        if not branch:
            return False
        protected = {b.strip().lower() for b in self.conflict_protected_branches}
        name = branch.strip().lower()
        return name in protected or name.split("/", 1)[0] in protected

    @property
    def github_owner(self) -> str:
        parts = self.github_repo.split("/")
        return parts[0] if len(parts) == 2 else ""

    @property
    def github_name(self) -> str:
        parts = self.github_repo.split("/")
        return parts[1] if len(parts) == 2 else ""


@dataclass
class WorkItem:
    """A unit of work to launch as a T3 thread."""

    name: str
    branch: str
    prompt: str
    settings: AgentSettings
    create_branch: bool = False
    worktree_from: Optional[str] = None


@dataclass
class ReviewComment:
    """A single comment within a review thread."""

    author: str
    body: str
    ai_prompt: Optional[str] = None


@dataclass
class ReviewThread:
    """A review thread on a PR."""

    thread_id: str
    path: str
    line: Optional[int]
    is_resolved: bool
    is_outdated: bool
    comments: List[ReviewComment] = field(default_factory=list)

    @property
    def reviewer(self) -> str:
        return self.comments[0].author if self.comments else "unknown"

    @property
    def ai_prompt(self) -> Optional[str]:
        for c in self.comments:
            if c.ai_prompt:
                return c.ai_prompt
        return None


@dataclass
class PRInfo:
    """Pull request metadata + review threads."""

    number: int
    title: str
    branch: str
    base_branch: str
    url: str
    threads: List[ReviewThread] = field(default_factory=list)


@dataclass
class PRStatus:
    """High-level mergeability / CI / review status of a pull request.

    Powers the ``triage`` report and the ``conflicts`` command. Populated from a
    single ``gh pr list``/``gh pr view --json`` call (GraphQL under the hood).
    """

    number: int
    title: str
    branch: str                       # headRefName
    base_branch: str                  # baseRefName
    url: str
    author: str = ""
    state: str = "OPEN"               # OPEN | CLOSED | MERGED
    is_draft: bool = False
    mergeable: str = "UNKNOWN"        # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state: str = "UNKNOWN"      # GitHub mergeStateStatus (BEHIND, CLEAN, ...)
    review_decision: str = ""         # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | ""
    ci_state: str = "NONE"            # SUCCESS | FAILURE | PENDING | NONE
    failing_checks: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    updated_at: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    @property
    def conflicting(self) -> bool:
        """True when GitHub reports the PR cannot be merged cleanly."""
        return self.mergeable == "CONFLICTING"

    @property
    def ci_failing(self) -> bool:
        return self.ci_state == "FAILURE"
