"""Data models for d3-thread-spawner."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple


# Per-model option capabilities for the claudeAgent provider.
#
# Mirrors T3 Code's `BUILT_IN_MODELS` option descriptors
# (apps/server/src/provider/Layers/ClaudeProvider.ts). Each model only
# *consumes* the option ids it declares here; T3 reads options by id from a
# canonical array and ignores ids a model does not expose. The one exception
# is `contextWindow`: T3's resolveClaudeApiModelId() appends `[1m]` to the API
# model id for ANY model when contextWindow == "1m", without checking
# capabilities — so sending contextWindow to a model that lacks it (haiku,
# opus-4-5) would produce an invalid model id like `claude-haiku-4-5[1m]`.
# We therefore emit only the options each model actually supports.
CLAUDE_EFFORTS: Tuple[str, ...] = (
    "low", "medium", "high", "xhigh", "max", "ultracode", "ultrathink",
)
CLAUDE_OPUS_4_7_EFFORTS: Tuple[str, ...] = (
    "low", "medium", "high", "xhigh", "max", "ultrathink",
)
CLAUDE_OPUS_4_6_EFFORTS: Tuple[str, ...] = (
    "low", "medium", "high", "max", "ultrathink",
)
CLAUDE_OPUS_4_5_EFFORTS: Tuple[str, ...] = ("low", "medium", "high", "max")
# The order is used when an invalid effort must fall back to the maximum real
# value. T3's GPT-5.6 variants extend the earlier Codex effort scale; this is
# intentionally ``ultra``, not Claude's unrelated ``ultrathink``.
CODEX_STANDARD_EFFORTS: Tuple[str, ...] = ("low", "medium", "high", "xhigh")
CODEX_MAX_EFFORTS: Tuple[str, ...] = (*CODEX_STANDARD_EFFORTS, "max")
CODEX_EFFORTS: Tuple[str, ...] = (*CODEX_MAX_EFFORTS, "ultra")
CONTEXT_WINDOWS: Tuple[str, ...] = ("200k", "1m")
SERVICE_TIERS: Tuple[str, ...] = ("default", "priority")

CLAUDE_MODEL_OPTIONS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "claude-fable-5": {
        "effort": CLAUDE_EFFORTS,
        "contextWindow": CONTEXT_WINDOWS,
    },
    "claude-opus-4-8": {
        "effort": CLAUDE_EFFORTS,
        "contextWindow": CONTEXT_WINDOWS,
    },
    "claude-opus-4-7": {
        "effort": CLAUDE_OPUS_4_7_EFFORTS,
        "contextWindow": CONTEXT_WINDOWS,
    },
    "claude-opus-4-6": {
        "effort": CLAUDE_OPUS_4_6_EFFORTS,
        "fastMode": (),
        "contextWindow": CONTEXT_WINDOWS,
    },
    "claude-opus-4-5": {
        "effort": CLAUDE_OPUS_4_5_EFFORTS,
        "fastMode": (),
    },
    "claude-sonnet-5": {
        "effort": CLAUDE_OPUS_4_7_EFFORTS,
        "contextWindow": CONTEXT_WINDOWS,
    },
    "claude-sonnet-4-6": {
        "effort": CLAUDE_OPUS_4_6_EFFORTS,
        "contextWindow": CONTEXT_WINDOWS,
    },
    "claude-haiku-4-5": {"thinking": ()},
}

# Fallback for custom / unknown model slugs: the modern Claude default of
# effort + contextWindow. A custom model that doesn't support a 1M window
# should set context_window = "200k" (which never suffixes the model id).
DEFAULT_CLAUDE_MODEL_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "effort": CLAUDE_EFFORTS,
    "contextWindow": CONTEXT_WINDOWS,
}

CODEX_MODEL_OPTIONS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "gpt-5.6-sol": {"reasoningEffort": CODEX_EFFORTS, "serviceTier": SERVICE_TIERS},
    "gpt-5.6-terra": {"reasoningEffort": CODEX_EFFORTS, "serviceTier": SERVICE_TIERS},
    "gpt-5.6-luna": {"reasoningEffort": CODEX_MAX_EFFORTS, "serviceTier": SERVICE_TIERS},
    "gpt-5.5": {"reasoningEffort": CODEX_STANDARD_EFFORTS, "serviceTier": SERVICE_TIERS},
    "gpt-5.4": {"reasoningEffort": CODEX_STANDARD_EFFORTS, "serviceTier": SERVICE_TIERS},
    "gpt-5.4-mini": {"reasoningEffort": CODEX_STANDARD_EFFORTS},
    "gpt-5.3-codex": {"reasoningEffort": CODEX_STANDARD_EFFORTS},
    "gpt-5.2": {"reasoningEffort": CODEX_STANDARD_EFFORTS},
}
# Unknown model IDs must reach T3 unchanged. We cannot infer their supported
# options safely when its metadata cache is unavailable.
DEFAULT_CODEX_MODEL_OPTIONS: Dict[str, Tuple[str, ...]] = {}

T3_CACHE_DIR = "~/.t3/caches"


def _option_values_from_descriptor(descriptor: Dict[str, Any]) -> Tuple[str, ...]:
    """Return select option ids from a T3 provider option descriptor."""
    values = descriptor.get("options")
    if not isinstance(values, list):
        return ()
    ids = []
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            ids.append(value["id"])
    return tuple(ids)


@lru_cache(maxsize=None)
def _cached_provider_model_options(
    provider: str,
) -> Optional[Dict[str, Dict[str, Tuple[str, ...]]]]:
    """Load T3's cached provider metadata, if present.

    Returning None means "cache unavailable, use built-in fallbacks." Returning
    a dict means "T3 has advertised this provider's model list," so an absent
    model can be treated as a real configuration error before we dispatch.
    """
    path = os.path.expanduser(os.path.join(T3_CACHE_DIR, f"{provider}.json"))
    if not os.path.isfile(path):
        return None

    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    result: Dict[str, Dict[str, Tuple[str, ...]]] = {}
    models = data.get("models")
    if not isinstance(models, list):
        return None

    for model in models:
        if not isinstance(model, dict):
            continue
        slug = model.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        descriptors = (
            model.get("capabilities", {}).get("optionDescriptors", [])
            if isinstance(model.get("capabilities"), dict)
            else []
        )
        options: Dict[str, Tuple[str, ...]] = {}
        if isinstance(descriptors, list):
            for descriptor in descriptors:
                if not isinstance(descriptor, dict):
                    continue
                option_id = descriptor.get("id")
                if isinstance(option_id, str) and option_id:
                    options[option_id] = _option_values_from_descriptor(descriptor)
        result[slug] = options

    return result or None


def _cached_model_id(provider: str, model: str) -> Optional[str]:
    cached = _cached_provider_model_options(provider)
    if not cached:
        return None
    if model in cached:
        return model
    lower = model.lower()
    for candidate in cached:
        if candidate.lower() == lower:
            return candidate
    return None


def _max_known_value(values: Tuple[str, ...], order: Tuple[str, ...]) -> Optional[str]:
    """Pick the largest supported value according to our known ordering."""
    if not values:
        return None
    ordered = [value for value in order if value in values]
    return ordered[-1] if ordered else values[-1]


def _normalize_select(
    requested: str,
    values: Tuple[str, ...],
    order: Tuple[str, ...],
) -> Optional[str]:
    if not requested or not values:
        return None
    if requested in values:
        return requested
    return _max_known_value(values, order)


def _is_known_builtin_model(model: str) -> bool:
    return model in CLAUDE_MODEL_OPTIONS or model in CODEX_MODEL_OPTIONS


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
    # Where T3 persists auth sessions + the signing key used to derive a
    # session token (the current auth model — no browser cookie is written).
    t3_state_db: str = "~/.t3/userdata/state.sqlite"
    t3_secrets_dir: str = "~/.t3/userdata/secrets"

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
        "mini": "gpt-5.4-mini",
    })

    @property
    def t3_api(self) -> str:
        return f"http://{self.t3_host}:{self.t3_port}"

    @property
    def _alias_model(self) -> str:
        return self.model_aliases.get(self.model, self.model)

    @property
    def resolved_model(self) -> str:
        """Resolve model alias to full model ID."""
        return _cached_model_id(self.provider, self._alias_model) or self._alias_model

    @property
    def provider(self) -> str:
        """T3 provider instance for the resolved model (``gpt-*`` → codex)."""
        return "codex" if self._alias_model.lower().startswith("gpt-") else "claudeAgent"

    def _model_option_values(self) -> Dict[str, Tuple[str, ...]]:
        cached = _cached_provider_model_options(self.provider)
        if cached is not None:
            model_id = self.resolved_model
            if model_id in cached:
                return cached[model_id]
            return {}
        if self.provider == "codex":
            return CODEX_MODEL_OPTIONS.get(
                self.resolved_model, DEFAULT_CODEX_MODEL_OPTIONS
            )
        return CLAUDE_MODEL_OPTIONS.get(
            self.resolved_model, DEFAULT_CLAUDE_MODEL_OPTIONS
        )

    def validate_model_selection(self) -> None:
        """Fail when a configured built-in alias/model is absent from T3's cache.

        Raw custom/new model ids are allowed through with no option assumptions;
        T3 may know about a model before this repository's static fallback table
        does. For aliases and known built-in ids, however, an absent cache entry
        means our local config points at a model T3 is not advertising.
        """
        cached = _cached_provider_model_options(self.provider)
        if cached is None or self.resolved_model in cached:
            return
        if self.model not in self.model_aliases and not _is_known_builtin_model(self._alias_model):
            return
        available = ", ".join(sorted(cached))
        raise RuntimeError(
            f"Configured {self.provider} model {self.model!r} resolves to "
            f"{self.resolved_model!r}, but T3 is not advertising it. "
            f"Available models: {available}"
        )

    def effective_effort(self) -> Optional[str]:
        """Return the effort value that will actually be sent, if any."""
        supported = self._model_option_values()
        if self.provider == "codex":
            values = supported.get("reasoningEffort", ())
            return _normalize_select(self.effort, values, CODEX_EFFORTS)
        return _normalize_select(
            self.effort, supported.get("effort", ()), CLAUDE_EFFORTS
        )

    def effective_context_window(self) -> str:
        """Return the effective context window.

        A missing ``contextWindow`` option means T3 will use the model's default
        API id with no ``[1m]`` suffix, which is the safe 200k path.
        """
        supported = self._model_option_values()
        if self.provider == "codex" or "contextWindow" not in supported:
            return "200k"
        return _normalize_select(
            self.context_window, supported.get("contextWindow", ()), CONTEXT_WINDOWS
        ) or "200k"

    def model_selection_adjustments(self) -> List[str]:
        """Human-readable notes for settings that were normalized."""
        supported = self._model_option_values()
        notes: List[str] = []
        effort = self.effective_effort()

        if self.provider == "codex":
            if self.effort and not supported.get("reasoningEffort"):
                notes.append(f"{self.resolved_model} has no reasoning effort option")
            elif effort and self.effort != effort:
                notes.append(f"effort={self.effort} normalized to {effort}")
        else:
            if self.effort and "effort" not in supported:
                notes.append(f"{self.resolved_model} has no effort option")
            elif effort and self.effort != effort:
                notes.append(f"effort={self.effort} normalized to {effort}")

            context = self.effective_context_window()
            if self.context_window and "contextWindow" not in supported:
                if self.context_window != context:
                    notes.append(f"context_window={self.context_window} normalized to {context}")
            elif self.context_window and self.context_window != context:
                notes.append(f"context_window={self.context_window} normalized to {context}")

            if self.fast_mode and "fastMode" not in supported:
                notes.append(f"{self.resolved_model} has no fast_mode option")

        return notes

    def model_selection_options(self) -> List[Dict[str, Any]]:
        """Build the canonical ``[{id, value}]`` options array for T3.

        T3 Code expects ``modelSelection.options`` as an array of
        ``{"id": ..., "value": ...}`` entries (migration 026). Claude options
        are filtered to the resolved model's capabilities so we never send,
        e.g., ``contextWindow`` to a model that lacks it (which would corrupt
        the API model id). Codex selections always pin ``serviceTier`` to
        Standard because T3's provider default can be Fast.
        """
        self.validate_model_selection()
        supported = self._model_option_values()
        if self.provider == "codex":
            # Codex option descriptors are ``reasoningEffort`` (model-specific,
            # currently through ``ultra``) and ``serviceTier`` — never Claude's
            # effort/contextWindow/
            # thinking/fastMode ids (contextWindow would corrupt the model id
            # into e.g. ``gpt-5.3-codex[1m]``). Invalid values normalize to the
            # highest real Codex effort.
            options: List[Dict[str, Any]] = []
            effort = self.effective_effort()
            if effort:
                options.append({
                    "id": "reasoningEffort",
                    "value": effort,
                })
            if "serviceTier" in supported:
                # serviceTier=default is T3's Standard tier (not Fast).
                options.append({"id": "serviceTier", "value": "default"})
            return options
        options: List[Dict[str, Any]] = []
        effort = self.effective_effort()
        if "effort" in supported and effort:
            options.append({"id": "effort", "value": effort})
        if "contextWindow" in supported:
            options.append({
                "id": "contextWindow",
                "value": self.effective_context_window(),
            })
        if "fastMode" in supported:
            options.append({"id": "fastMode", "value": self.fast_mode})
        if "thinking" in supported:
            options.append({"id": "thinking", "value": self.thinking})
        return options

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
