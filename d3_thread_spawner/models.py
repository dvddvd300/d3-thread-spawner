"""Data models for d3-thread-spawner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AgentSettings:
    """Resolved settings for a single agent launch."""

    model: str = "opus"
    mode: str = "plan"
    effort: str = "high"
    base_branch: str = "main"
    repo_dir: str = "."
    context_window: str = "1m"
    thinking: bool = True
    fast_mode: bool = False
    batch_size: int = 5
    batch_delay: int = 0
    launch_delay: float = 0.5
    dry_run: bool = False

    # T3
    t3_host: str = "127.0.0.1"
    t3_port: int = 3773
    t3_project_id: str = ""

    # Worktree
    worktree_dir: str = ""

    # GitHub
    github_repo: str = ""

    # Model aliases
    model_aliases: Dict[str, str] = field(default_factory=lambda: {
        "opus": "claude-opus-4-6",
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
