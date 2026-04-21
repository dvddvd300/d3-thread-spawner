"""GitHub PR integration via gh CLI GraphQL."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import PRInfo, ReviewComment, ReviewThread
from .util import log, log_verbose, run


# ── Rate-limit handling ───────────────────────────────────────────────────────


class GitHubRateLimitError(Exception):
    """Raised when GitHub API rate limit is exceeded."""

    def __init__(self, reset_info: str = ""):
        self.reset_info = reset_info
        msg = "GitHub API rate limit exceeded"
        if reset_info:
            msg += f" — resets {reset_info}"
        super().__init__(msg)


def _get_rate_limit_reset() -> str:
    """Fetch the GraphQL rate limit reset time via REST API."""
    try:
        result = run(["gh", "api", "rate_limit"])
        data = json.loads(result.stdout)
        rl = data["resources"]["graphql"]
        reset_dt = datetime.fromtimestamp(rl["reset"], tz=timezone.utc)
        diff_mins = max(0, int((reset_dt - datetime.now(timezone.utc)).total_seconds() / 60))
        remaining = rl["remaining"]
        limit = rl["limit"]
        return (
            f"{reset_dt.strftime('%H:%M UTC')} (~{diff_mins}m) "
            f"[{remaining}/{limit} remaining]"
        )
    except Exception:
        return ""


def _is_rate_limit_error(e: subprocess.CalledProcessError) -> bool:
    """Return True if the CalledProcessError looks like a GitHub rate-limit error."""
    combined = (e.stderr or "") + (e.stdout or "")
    return "rate limit" in combined.lower()


def _check_rate_limit_error(e: subprocess.CalledProcessError):
    """If *e* is a rate-limit error, raise GitHubRateLimitError; otherwise do nothing."""
    if _is_rate_limit_error(e):
        raise GitHubRateLimitError(_get_rate_limit_reset()) from e


# ── GraphQL helper ────────────────────────────────────────────────────────────


def gh_graphql(query: str) -> Any:
    """Execute a GitHub GraphQL query via gh CLI.

    Automatically injects ``rateLimit`` into the query so every call reports
    its cost and the remaining budget.
    """
    # Inject rateLimit at the top level of the query
    enriched = query
    idx = enriched.find("{")
    if idx >= 0:
        enriched = (
            enriched[: idx + 1]
            + " rateLimit { cost remaining limit resetAt }"
            + enriched[idx + 1 :]
        )

    try:
        result = run(["gh", "api", "graphql", "-f", f"query={enriched}"])
    except subprocess.CalledProcessError as e:
        _check_rate_limit_error(e)
        raise

    data = json.loads(result.stdout)

    # Log cost / remaining budget
    rl = (data.get("data") or {}).get("rateLimit")
    if rl:
        log_verbose(
            "  📊",
            f"API cost: {rl['cost']} · "
            f"{rl['remaining']}/{rl['limit']} remaining",
        )
        if rl["remaining"] < 500:
            log(
                "  ⚠️ ",
                f"GitHub API budget running low: "
                f"{rl['remaining']}/{rl['limit']} remaining "
                f"(resets {rl['resetAt']})",
            )

    return data


def extract_ai_prompt(body: str) -> Optional[str]:
    """Extract the 'Prompt for AI Agents' section from CodeRabbit-style comments.

    Handles both legacy format ("Prompt for AI Agents") and current CodeRabbit
    format ("Prompt for all review comments with AI agents").
    """
    match = re.search(
        r"Prompt for (?:AI Agents|all review comments with AI agents)</summary>\s*```[^\n]*\n(.*?)```",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def _fetch_pr_threads_graphql(
    owner: str, name: str, pr_number: int
) -> Tuple[dict, List[dict]]:
    """Fetch PR metadata and ALL review threads via paginated GraphQL."""
    all_threads: List[dict] = []
    cursor = None
    pr_meta = None

    while True:
        after_clause = f', after: "{cursor}"' if cursor else ""
        query = f"""{{
  repository(owner: "{owner}", name: "{name}") {{
    pullRequest(number: {pr_number}) {{
      title
      headRefName
      baseRefName
      url
      reviewThreads(first: 50{after_clause}) {{
        totalCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 30) {{
            nodes {{
              author {{ login }}
              body
            }}
          }}
        }}
      }}
    }}
  }}
}}"""
        data = gh_graphql(query)
        pr_data = data["data"]["repository"]["pullRequest"]

        if pr_meta is None:
            pr_meta = {
                "title": pr_data["title"],
                "headRefName": pr_data["headRefName"],
                "baseRefName": pr_data["baseRefName"],
                "url": pr_data["url"],
            }

        page = pr_data["reviewThreads"]
        all_threads.extend(page["nodes"])

        if page["pageInfo"]["hasNextPage"]:
            cursor = page["pageInfo"]["endCursor"]
        else:
            break

    return pr_meta, all_threads


def _fetch_pr_threads_rest(
    owner: str, name: str, pr_number: int
) -> Tuple[dict, List[dict]]:
    """Fetch PR metadata and review comments via REST API.

    Used as a fallback when the GraphQL API is rate-limited.
    Note: isResolved and isOutdated are not available via REST; both default to False.
    """
    # PR metadata
    pr_result = run(["gh", "api", f"repos/{owner}/{name}/pulls/{pr_number}"])
    pr_data = json.loads(pr_result.stdout)
    pr_meta = {
        "title": pr_data["title"],
        "headRefName": pr_data["head"]["ref"],
        "baseRefName": pr_data["base"]["ref"],
        "url": pr_data["html_url"],
    }

    # Inline review comments (paginated)
    all_comments: List[dict] = []
    page = 1
    while True:
        result = run([
            "gh", "api",
            f"repos/{owner}/{name}/pulls/{pr_number}/comments?per_page=100&page={page}",
        ])
        page_data = json.loads(result.stdout)
        if not page_data:
            break
        all_comments.extend(page_data)
        if len(page_data) < 100:
            break
        page += 1

    # Group individual comments into pseudo-threads by root comment id.
    # Root comments have no in_reply_to_id; replies reference the root's id.
    threads_map: Dict[int, dict] = {}
    for c in all_comments:
        root_id: int = c.get("in_reply_to_id") or c["id"]
        if root_id not in threads_map:
            root_c = c if c.get("in_reply_to_id") is None else next(
                (x for x in all_comments if x["id"] == root_id), c
            )
            threads_map[root_id] = {
                "id": str(root_id),
                "isResolved": False,
                "isOutdated": False,
                "path": root_c.get("path", ""),
                "line": root_c.get("line") or root_c.get("original_line"),
                "comments": {"nodes": []},
            }
        threads_map[root_id]["comments"]["nodes"].append({
            "author": {"login": c["user"]["login"]} if c.get("user") else None,
            "body": c["body"],
        })

    return pr_meta, list(threads_map.values())


def fetch_pr_threads(
    owner: str, name: str, pr_number: int
) -> Tuple[dict, List[dict]]:
    """Fetch PR metadata and ALL review threads.

    Tries GraphQL first for richer data (isResolved, isOutdated).
    Falls back to the REST API automatically when GraphQL is rate-limited.
    """
    try:
        return _fetch_pr_threads_graphql(owner, name, pr_number)
    except subprocess.CalledProcessError:
        log("⚠️ ", f"PR #{pr_number}: GraphQL unavailable, falling back to REST API...")
        return _fetch_pr_threads_rest(owner, name, pr_number)


def fetch_pr_info(
    owner: str,
    name: str,
    pr_number: int,
    include_resolved: bool = False,
    include_outdated: bool = False,
) -> PRInfo:
    """Fetch a PR's details and filtered review threads."""
    pr_meta, raw_threads = fetch_pr_threads(owner, name, pr_number)

    threads: List[ReviewThread] = []
    for node in raw_threads:
        if not include_resolved and node["isResolved"]:
            continue
        if not include_outdated and node["isOutdated"]:
            continue

        comments: List[ReviewComment] = []
        for c in node["comments"]["nodes"]:
            author = c["author"]["login"] if c.get("author") else "unknown"
            ai_prompt = extract_ai_prompt(c["body"])
            comments.append(ReviewComment(
                author=author,
                body=c["body"],
                ai_prompt=ai_prompt,
            ))

        if comments:
            threads.append(ReviewThread(
                thread_id=node["id"],
                path=node.get("path", ""),
                line=node.get("line"),
                is_resolved=node["isResolved"],
                is_outdated=node["isOutdated"],
                comments=comments,
            ))

    return PRInfo(
        number=pr_number,
        title=pr_meta["title"],
        branch=pr_meta["headRefName"],
        base_branch=pr_meta["baseRefName"],
        url=pr_meta["url"],
        threads=threads,
    )


def fetch_open_prs(repo: str, mine_only: bool = False) -> List[Dict]:
    """Fetch open PRs from GitHub.

    Uses ``gh pr list`` (GraphQL) first, falling back to the REST API
    when GraphQL is rate-limited.
    """
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,headRefName,author",
        "--limit", "50",
    ]
    if mine_only:
        cmd.extend(["--author", "@me"])

    try:
        result = run(cmd)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        if _is_rate_limit_error(e):
            log("⚠️ ", "GraphQL rate-limited, falling back to REST API...")
            return _fetch_open_prs_rest(repo, mine_only)
        raise


def _fetch_open_prs_rest(repo: str, mine_only: bool = False) -> List[Dict]:
    """Fetch open PRs via the REST API (separate rate-limit bucket)."""
    owner, name = repo.split("/", 1)

    # Resolve current user when filtering by --mine
    me: Optional[str] = None
    if mine_only:
        try:
            me = json.loads(run(["gh", "api", "user"]).stdout)["login"].lower()
        except Exception:
            log("⚠️ ", "Could not resolve current user — returning all open PRs")

    all_prs: List[Dict] = []
    page = 1
    while True:
        result = run([
            "gh", "api",
            f"repos/{owner}/{name}/pulls?state=open&per_page=50&page={page}",
        ])
        page_data = json.loads(result.stdout)
        if not page_data:
            break

        for p in page_data:
            author_login = (p.get("user") or {}).get("login", "")
            if mine_only and me and author_login.lower() != me:
                continue
            all_prs.append({
                "number": p["number"],
                "title": p["title"],
                "headRefName": p["head"]["ref"],
                "author": {"login": author_login},
            })

        if len(page_data) < 50:
            break
        page += 1

    return all_prs
