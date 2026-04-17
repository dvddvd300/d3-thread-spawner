"""GitHub PR integration via gh CLI GraphQL."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .models import PRInfo, ReviewComment, ReviewThread
from .util import run


def gh_graphql(query: str) -> Any:
    """Execute a GitHub GraphQL query via gh CLI."""
    result = run(["gh", "api", "graphql", "-f", f"query={query}"])
    return json.loads(result.stdout)


def extract_ai_prompt(body: str) -> Optional[str]:
    """Extract the 'Prompt for AI Agents' section from CodeRabbit-style comments."""
    match = re.search(
        r"Prompt for AI Agents</summary>\s*```[^\n]*\n(.*?)```",
        body,
        re.DOTALL,
    )
    return match.group(1).strip() if match else None


def fetch_pr_threads(
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
    """Fetch open PRs from GitHub."""
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,headRefName,author",
        "--limit", "50",
    ]
    if mine_only:
        cmd.extend(["--author", "@me"])

    result = run(cmd)
    return json.loads(result.stdout)
