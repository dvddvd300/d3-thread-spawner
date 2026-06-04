"""Data models for d3-thread-spawner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
CLAUDE_MODEL_OPTIONS: Dict[str, frozenset] = {
    "claude-opus-4-8": frozenset({"effort", "contextWindow"}),
    "claude-opus-4-7": frozenset({"effort", "contextWindow"}),
    "claude-opus-4-6": frozenset({"effort", "fastMode", "contextWindow"}),
    "claude-opus-4-5": frozenset({"effort", "fastMode"}),
    "claude-sonnet-4-6": frozenset({"effort", "contextWindow"}),
    "claude-haiku-4-5": frozenset({"thinking"}),
}

# Fallback for custom / unknown model slugs: the modern Claude default of
# effort + contextWindow. A custom model that doesn't support a 1M window
# should set context_window = "200k" (which never suffixes the model id).
DEFAULT_CLAUDE_MODEL_OPTIONS: frozenset = frozenset({"effort", "contextWindow"})


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

    # Model aliases
    model_aliases: Dict[str, str] = field(default_factory=lambda: {
        "opus": "claude-opus-4-8",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
    })

    @property
    def t3_api(self) -> str:
        return f"http://{self.t3_host}:{self.t3_port}"

    @property
    def resolved_model(self) -> str:
        """Resolve model alias to full model ID."""
        return self.model_aliases.get(self.model, self.model)

    def model_selection_options(self) -> List[Dict[str, Any]]:
        """Build the canonical ``[{id, value}]`` options array for T3.

        T3 Code expects ``modelSelection.options`` as an array of
        ``{"id": ..., "value": ...}`` entries (migration 026). We emit only the
        options the resolved model actually supports so we never send, e.g.,
        ``contextWindow`` to a model that lacks it (which would corrupt the API
        model id). Effort/thinking/fastMode are also filtered to the model's
        capabilities; unsupported ids are simply omitted.
        """
        supported = CLAUDE_MODEL_OPTIONS.get(
            self.resolved_model, DEFAULT_CLAUDE_MODEL_OPTIONS
        )
        options: List[Dict[str, Any]] = []
        if "effort" in supported and self.effort:
            options.append({"id": "effort", "value": self.effort})
        if "contextWindow" in supported and self.context_window:
            options.append({"id": "contextWindow", "value": self.context_window})
        if "fastMode" in supported:
            options.append({"id": "fastMode", "value": self.fast_mode})
        if "thinking" in supported:
            options.append({"id": "thinking", "value": self.thinking})
        return options

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
