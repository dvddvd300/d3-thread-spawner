"""GitHub PR integration via gh CLI GraphQL.

Fetch strategy (for ``d3-spawn pr``):

- Open-PR list and per-PR review threads are fetched via GraphQL first
  (richer data: ``isResolved`` / ``isOutdated``), falling back to the REST API
  — a *separate* rate-limit bucket — when GraphQL is rate-limited.
- Review threads for many PRs are fetched with batched, aliased GraphQL queries
  (a cheap "skeleton" pass to discover threads + decide cache freshness, then a
  "bodies" pass that pulls comment text only for the threads actually in scope).
- A local cache (``cache.py``) keyed by each PR's ``updatedAt`` skips re-fetching
  unchanged PRs across runs.
- ``--wait`` makes a rate-limited GraphQL call sleep until the budget resets and
  resume, instead of aborting.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import cache as _cache
from .models import AgentSettings, PRInfo, ReviewComment, ReviewThread
from .util import iso_now, log, log_verbose, run


# ── Rate-limit handling ───────────────────────────────────────────────────────


class GitHubRateLimitError(Exception):
    """Raised when GitHub API rate limit is exceeded."""

    def __init__(self, reset_info: str = "", reset_at: Optional[datetime] = None):
        self.reset_info = reset_info
        self.reset_at = reset_at
        msg = "GitHub API rate limit exceeded"
        if reset_info:
            msg += f" — resets {reset_info}"
        super().__init__(msg)


def _graphql_rate_limit() -> Optional[dict]:
    """Read the GraphQL rate-limit bucket via REST (``gh api rate_limit`` is exempt).

    Returns ``{remaining, limit, reset_dt}`` or ``None`` if it can't be read.
    """
    try:
        data = json.loads(run(["gh", "api", "rate_limit"]).stdout)
        rl = data["resources"]["graphql"]
        return {
            "remaining": rl["remaining"],
            "limit": rl["limit"],
            "reset_dt": datetime.fromtimestamp(rl["reset"], tz=timezone.utc),
        }
    except Exception:
        return None


def _format_rate_limit(rl: dict) -> str:
    reset_dt: datetime = rl["reset_dt"]
    diff_mins = max(0, int((reset_dt - datetime.now(timezone.utc)).total_seconds() / 60))
    return (
        f"{reset_dt.strftime('%H:%M UTC')} (~{diff_mins}m) "
        f"[{rl['remaining']}/{rl['limit']} remaining]"
    )


def _get_rate_limit_reset() -> str:
    """Human-readable GraphQL reset summary (empty string if unavailable)."""
    rl = _graphql_rate_limit()
    return _format_rate_limit(rl) if rl else ""


def _is_rate_limit_error(e: subprocess.CalledProcessError) -> bool:
    """Return True if the CalledProcessError looks like a GitHub rate-limit error."""
    combined = (e.stderr or "") + (e.stdout or "")
    return "rate limit" in combined.lower()


def _check_rate_limit_error(e: subprocess.CalledProcessError):
    """If *e* is a rate-limit error, raise GitHubRateLimitError; otherwise do nothing."""
    if _is_rate_limit_error(e):
        rl = _graphql_rate_limit()
        reset_info = _format_rate_limit(rl) if rl else ""
        reset_at = rl["reset_dt"] if rl else None
        raise GitHubRateLimitError(reset_info, reset_at) from e


# Process-wide latch: once GraphQL is known to be rate-limited, route every
# subsequent PR straight to REST instead of re-attempting (and re-failing) GraphQL.
_GRAPHQL_DISABLED = False


def set_graphql_disabled(v: bool):
    global _GRAPHQL_DISABLED
    _GRAPHQL_DISABLED = v


def graphql_disabled() -> bool:
    return _GRAPHQL_DISABLED


def graphql_budget_low(threshold: int = 200) -> bool:
    """One free ``gh api rate_limit`` read; True when GraphQL remaining < threshold.

    Returns False when the budget can't be read (don't disable GraphQL on error —
    the per-PR REST fallback still protects us).
    """
    rl = _graphql_rate_limit()
    return rl is not None and rl["remaining"] < threshold


def _seconds_until(reset_at: Optional[datetime]) -> Optional[float]:
    if reset_at is None:
        return None
    return (reset_at - datetime.now(timezone.utc)).total_seconds()


# ── GraphQL helper ────────────────────────────────────────────────────────────


def _gh_graphql_once(query: str) -> Any:
    """Execute a single GitHub GraphQL query via gh CLI.

    Automatically injects ``rateLimit`` into the query so every call reports
    its cost and the remaining budget.
    """
    # Inject rateLimit at the top level of the query (right after the first "{",
    # which is the operation root even for aliased multi-field queries).
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
            f"API cost: {rl['cost']} · {rl['remaining']}/{rl['limit']} remaining",
        )
        if rl["remaining"] < 500:
            log(
                "  ⚠️ ",
                f"GitHub API budget running low: "
                f"{rl['remaining']}/{rl['limit']} remaining "
                f"(resets {rl['resetAt']})",
            )

    return data


def gh_graphql(query: str, *, wait: bool = False, wait_max_seconds: int = 0) -> Any:
    """Execute a GraphQL query, optionally waiting out a rate-limit reset.

    When ``wait`` is set and the reset is within ``wait_max_seconds``, sleep until
    the budget resets and retry the same query (callers hold their own pagination
    cursors, so a mid-pagination wait resumes correctly). Otherwise the
    ``GitHubRateLimitError`` propagates so the caller can fall back to REST.
    """
    while True:
        try:
            return _gh_graphql_once(query)
        except GitHubRateLimitError as e:
            if not wait:
                raise
            secs = _seconds_until(e.reset_at)
            if secs is None or secs <= 0 or secs > wait_max_seconds:
                raise
            secs = int(secs) + 5  # small buffer past resetAt
            log(
                "⏳",
                f"GraphQL rate limited; sleeping {secs}s until reset "
                f"({e.reset_info}), then resuming...",
            )
            time.sleep(secs)
            # loop and retry the same query


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


# ── Single-PR thread fetch (GraphQL + REST fallback) ───────────────────────────


def _fetch_pr_threads_graphql(
    owner: str,
    name: str,
    pr_number: int,
    *,
    wait: bool = False,
    wait_max_seconds: int = 0,
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
      updatedAt
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
        data = gh_graphql(query, wait=wait, wait_max_seconds=wait_max_seconds)
        pr_data = data["data"]["repository"]["pullRequest"]

        if pr_meta is None:
            pr_meta = {
                "title": pr_data["title"],
                "headRefName": pr_data["headRefName"],
                "baseRefName": pr_data["baseRefName"],
                "url": pr_data["url"],
                "updatedAt": pr_data.get("updatedAt"),
            }

        page = pr_data["reviewThreads"]
        all_threads.extend(page["nodes"])

        if page["pageInfo"]["hasNextPage"]:
            cursor = page["pageInfo"]["endCursor"]
        else:
            break

    return pr_meta, all_threads


def _fetch_pr_threads_rest(
    owner: str,
    name: str,
    pr_number: int,
    *,
    meta_hint: Optional[dict] = None,
) -> Tuple[dict, List[dict]]:
    """Fetch PR metadata and review comments via REST API.

    Used as a fallback when the GraphQL API is rate-limited. ``isResolved`` and
    ``isOutdated`` are not available via REST; both default to False.

    ``meta_hint`` (e.g. from the open-PR list) supplies title/branch/base/url/
    updatedAt so we can skip the per-PR metadata GET entirely.
    """
    if meta_hint and meta_hint.get("baseRefName") and meta_hint.get("headRefName"):
        pr_meta = {
            "title": meta_hint.get("title", ""),
            "headRefName": meta_hint["headRefName"],
            "baseRefName": meta_hint["baseRefName"],
            "url": meta_hint.get("url", ""),
            "updatedAt": meta_hint.get("updatedAt"),
        }
    else:
        pr_result = run(["gh", "api", f"repos/{owner}/{name}/pulls/{pr_number}"])
        pr_data = json.loads(pr_result.stdout)
        pr_meta = {
            "title": pr_data["title"],
            "headRefName": pr_data["head"]["ref"],
            "baseRefName": pr_data["base"]["ref"],
            "url": pr_data["html_url"],
            "updatedAt": pr_data.get("updated_at"),
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
    owner: str,
    name: str,
    pr_number: int,
    *,
    wait: bool = False,
    wait_max_seconds: int = 0,
) -> Tuple[dict, List[dict]]:
    """Fetch PR metadata and ALL review threads for a single PR.

    Tries GraphQL first for richer data (isResolved, isOutdated). Falls back to
    the REST API automatically when GraphQL is rate-limited *or* otherwise fails.
    """
    if graphql_disabled():
        return _fetch_pr_threads_rest(owner, name, pr_number)
    try:
        return _fetch_pr_threads_graphql(
            owner, name, pr_number, wait=wait, wait_max_seconds=wait_max_seconds
        )
    except (subprocess.CalledProcessError, GitHubRateLimitError):
        log(
            "⚠️ ",
            f"PR #{pr_number}: GraphQL rate-limited/unavailable, falling back to "
            f"REST API (resolution status unavailable — showing all threads)...",
        )
        set_graphql_disabled(True)
        return _fetch_pr_threads_rest(owner, name, pr_number)


# ── PRInfo assembly ────────────────────────────────────────────────────────────


def _build_pr_info(
    pr_number: int,
    pr_meta: dict,
    raw_threads: List[dict],
    include_resolved: bool,
    include_outdated: bool,
) -> PRInfo:
    """Turn raw thread dicts (GraphQL or REST shape) into a filtered PRInfo."""
    threads: List[ReviewThread] = []
    for node in raw_threads:
        if not include_resolved and node.get("isResolved"):
            continue
        if not include_outdated and node.get("isOutdated"):
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
                is_resolved=bool(node.get("isResolved")),
                is_outdated=bool(node.get("isOutdated")),
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


def fetch_pr_info(
    owner: str,
    name: str,
    pr_number: int,
    include_resolved: bool = False,
    include_outdated: bool = False,
    *,
    wait: bool = False,
    wait_max_seconds: int = 0,
) -> PRInfo:
    """Fetch a single PR's details and filtered review threads."""
    pr_meta, raw_threads = fetch_pr_threads(
        owner, name, pr_number, wait=wait, wait_max_seconds=wait_max_seconds
    )
    return _build_pr_info(pr_number, pr_meta, raw_threads, include_resolved, include_outdated)


# ── Batched multi-PR thread fetch ──────────────────────────────────────────────

# Node-budget tuning. GitHub caps a query at ~500 requested nodes. The skeleton
# pass requests ``threads_page + threads_page`` (threads + first comment) nodes
# per PR; the bodies pass requests ``1 + comments_page`` nodes per thread.
_SKELETON_THREADS_PAGE = 50          # ~100 nodes/PR  -> 5 PRs/request
_SKELETON_BATCH = 5
_BODY_COMMENTS_PAGE = 30             # ~31 nodes/thread -> ~16 threads/request
_BODY_BATCH = 16


def _chunked(seq: List[Any], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _skeleton_thread(node: dict) -> dict:
    """Normalize a GraphQL skeleton thread node into our compact shape."""
    comment_nodes = (node.get("comments") or {}).get("nodes") or []
    reviewer_login = ""
    if comment_nodes and comment_nodes[0].get("author"):
        reviewer_login = comment_nodes[0]["author"].get("login", "") or ""
    return {
        "id": node["id"],
        "isResolved": bool(node.get("isResolved")),
        "isOutdated": bool(node.get("isOutdated")),
        "reviewer_login": reviewer_login,
    }


def _paginate_skeleton_single(
    owner: str, name: str, pr_number: int, cursor: str, *, wait: bool, wait_max_seconds: int
) -> List[dict]:
    """Collect remaining skeleton threads for a single PR with many threads."""
    out: List[dict] = []
    while cursor:
        query = f"""{{
  repository(owner: "{owner}", name: "{name}") {{
    pullRequest(number: {pr_number}) {{
      reviewThreads(first: {_SKELETON_THREADS_PAGE}, after: "{cursor}") {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ id isResolved isOutdated comments(first: 1) {{ nodes {{ author {{ login }} }} }} }}
      }}
    }}
  }}
}}"""
        data = gh_graphql(query, wait=wait, wait_max_seconds=wait_max_seconds)
        page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        out.extend(_skeleton_thread(n) for n in page["nodes"])
        cursor = page["endCursor"] if page["pageInfo"]["hasNextPage"] else None
    return out


def _batch_skeletons(
    owner: str, name: str, pr_numbers: List[int], *, wait: bool, wait_max_seconds: int
) -> Dict[int, dict]:
    """Cheap aliased skeleton pass over many PRs.

    Returns ``{pr_number: {"meta": {...}, "threads": [skeleton, ...]}}``.
    """
    out: Dict[int, dict] = {}
    for chunk in _chunked(pr_numbers, _SKELETON_BATCH):
        alias_to_num = {f"pr{i}": num for i, num in enumerate(chunk)}
        selections = "\n".join(
            f"""    {alias}: pullRequest(number: {num}) {{
      number title headRefName baseRefName url updatedAt
      reviewThreads(first: {_SKELETON_THREADS_PAGE}) {{
        totalCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ id isResolved isOutdated comments(first: 1) {{ nodes {{ author {{ login }} }} }} }}
      }}
    }}"""
            for alias, num in alias_to_num.items()
        )
        query = f'{{\n  repository(owner: "{owner}", name: "{name}") {{\n{selections}\n  }}\n}}'
        data = gh_graphql(query, wait=wait, wait_max_seconds=wait_max_seconds)
        repo = (data.get("data") or {}).get("repository") or {}
        for alias, num in alias_to_num.items():
            pr_data = repo.get(alias)
            if not pr_data:
                continue  # PR not found / null
            meta = {
                "title": pr_data["title"],
                "headRefName": pr_data["headRefName"],
                "baseRefName": pr_data["baseRefName"],
                "url": pr_data["url"],
                "updatedAt": pr_data.get("updatedAt"),
            }
            rt = pr_data["reviewThreads"]
            threads = [_skeleton_thread(n) for n in rt["nodes"]]
            if rt["pageInfo"]["hasNextPage"]:
                threads.extend(_paginate_skeleton_single(
                    owner, name, num, rt["pageInfo"]["endCursor"],
                    wait=wait, wait_max_seconds=wait_max_seconds,
                ))
            out[num] = {"meta": meta, "threads": threads}
    return out


def _batch_bodies(
    thread_ids: List[str], *, wait: bool, wait_max_seconds: int
) -> Dict[str, dict]:
    """Fetch full comment bodies for the given review-thread global IDs.

    Returns ``{thread_id: {"path", "line", "comments": {"nodes": [...]}}}``.
    """
    out: Dict[str, dict] = {}
    for chunk in _chunked(thread_ids, _BODY_BATCH):
        alias_to_id = {f"t{i}": tid for i, tid in enumerate(chunk)}
        selections = "\n".join(
            f"""  {alias}: node(id: "{tid}") {{
    ... on PullRequestReviewThread {{
      path line
      comments(first: {_BODY_COMMENTS_PAGE}) {{ nodes {{ author {{ login }} body }} }}
    }}
  }}"""
            for alias, tid in alias_to_id.items()
        )
        query = f"{{\n{selections}\n}}"
        data = gh_graphql(query, wait=wait, wait_max_seconds=wait_max_seconds)
        nodes = data.get("data") or {}
        for alias, tid in alias_to_id.items():
            nd = nodes.get(alias)
            if not nd:
                continue
            out[tid] = {
                "path": nd.get("path", "") or "",
                "line": nd.get("line"),
                "comments": nd.get("comments") or {"nodes": []},
            }
    return out


def reviewer_matches(login: Optional[str], reviewer: Optional[str]) -> bool:
    """True if a comment author *login* matches the requested ``reviewer``.

    Both sides are lower-cased and have a ``[bot]`` suffix stripped, then compared
    for equality — so ``--reviewer coderabbitai`` matches the GraphQL login
    ``coderabbitai`` and the REST login ``coderabbitai[bot]`` alike, while
    ``--reviewer bot`` does NOT spuriously match ``dependabot``.
    """
    if not reviewer:
        return True

    def _norm(s: Optional[str]) -> str:
        return (s or "").lower().replace("[bot]", "").strip()

    return _norm(login) == _norm(reviewer)


def _thread_in_scope(
    skeleton: dict, include_resolved: bool, include_outdated: bool, reviewer: Optional[str]
) -> bool:
    if not include_resolved and skeleton["isResolved"]:
        return False
    if not include_outdated and skeleton["isOutdated"]:
        return False
    if not reviewer_matches(skeleton.get("reviewer_login"), reviewer):
        return False
    return True


def fetch_prs_info(
    owner: str,
    name: str,
    pr_numbers: List[int],
    *,
    include_resolved: bool = False,
    include_outdated: bool = False,
    reviewer: Optional[str] = None,
    settings: Optional[AgentSettings] = None,
    pr_hints: Optional[Dict[int, dict]] = None,
) -> Tuple[Dict[int, PRInfo], List[int]]:
    """Fetch review threads for many PRs efficiently.

    Returns ``(infos, skipped)`` where ``infos`` maps PR number -> PRInfo and
    ``skipped`` lists PR numbers that could not be fetched because *both* the
    GraphQL and REST buckets were exhausted (for partial-results salvage).

    Strategy: serve unchanged PRs from cache (validated by ``updatedAt``), then
    batch a cheap GraphQL skeleton pass + a bodies pass over the rest, falling
    back to per-PR REST when GraphQL is rate-limited.
    """
    pr_hints = pr_hints or {}
    cache_on = getattr(settings, "cache", True) if settings else True
    cache_dir = getattr(settings, "cache_dir", _cache.DEFAULT_CACHE_DIR) if settings else _cache.DEFAULT_CACHE_DIR
    wait = getattr(settings, "wait", False) if settings else False
    wait_max = getattr(settings, "wait_max_seconds", 0) if settings else 0
    scope = _cache.scope_key(include_resolved, include_outdated, reviewer)
    repo = f"{owner}/{name}"

    infos: Dict[int, PRInfo] = {}
    skipped: List[int] = []

    def _remaining() -> List[int]:
        return [n for n in pr_numbers if n not in infos]

    def _store(n: int, meta: dict, raw_threads: List[dict], source: str):
        infos[n] = _build_pr_info(n, meta, raw_threads, include_resolved, include_outdated)
        ua = meta.get("updatedAt") or (pr_hints.get(n) or {}).get("updatedAt")
        if cache_on and ua:
            _cache.put_cached(
                cache_dir, repo, n, scope, ua, meta, raw_threads, source, fetched_at=iso_now()
            )

    # 1) Serve from cache where updatedAt is known up front (open-PR list hints).
    pending: List[int] = []
    for n in pr_numbers:
        ua = (pr_hints.get(n) or {}).get("updatedAt")
        if cache_on and ua:
            hit = _cache.get_cached(cache_dir, repo, n, scope, ua)
            if hit:
                meta, raw_threads, _src = hit
                infos[n] = _build_pr_info(n, meta, raw_threads, include_resolved, include_outdated)
                log_verbose("  💾", f"PR #{n}: cache hit (unchanged)")
                continue
        pending.append(n)

    if not pending:
        return infos, skipped

    # 2) Batched GraphQL (skeleton -> select -> bodies), unless GraphQL is disabled.
    if not graphql_disabled():
        try:
            skeletons = _batch_skeletons(owner, name, pending, wait=wait, wait_max_seconds=wait_max)

            selected_by_pr: Dict[int, List[dict]] = {}
            need_body_ids: List[str] = []
            for n, sk in skeletons.items():
                ua = sk["meta"].get("updatedAt")
                # Cache check via skeleton updatedAt (covers the explicit-PR path).
                if cache_on and ua:
                    hit = _cache.get_cached(cache_dir, repo, n, scope, ua)
                    if hit:
                        meta, raw_threads, _src = hit
                        infos[n] = _build_pr_info(
                            n, meta, raw_threads, include_resolved, include_outdated
                        )
                        log_verbose("  💾", f"PR #{n}: cache hit (unchanged)")
                        continue
                sel = [
                    t for t in sk["threads"]
                    if _thread_in_scope(t, include_resolved, include_outdated, reviewer)
                ]
                selected_by_pr[n] = sel
                need_body_ids.extend(t["id"] for t in sel)

            bodies = (
                _batch_bodies(need_body_ids, wait=wait, wait_max_seconds=wait_max)
                if need_body_ids else {}
            )

            for n, sel in selected_by_pr.items():
                raw_threads: List[dict] = []
                complete = True
                for t in sel:
                    body = bodies.get(t["id"])
                    if body is None:
                        complete = False
                        break
                    raw_threads.append({
                        "id": t["id"],
                        "isResolved": t["isResolved"],
                        "isOutdated": t["isOutdated"],
                        "path": body["path"],
                        "line": body["line"],
                        "comments": body["comments"],
                    })
                if not complete:
                    continue  # leave for REST fallback / skip
                _store(n, skeletons[n]["meta"], raw_threads, "graphql")
        except (GitHubRateLimitError, subprocess.CalledProcessError) as e:
            # Either a rate-limit or any other gh/GraphQL failure (auth, network,
            # node-limit). Degrade the rest of the run to REST rather than crash.
            reset = getattr(e, "reset_info", "") or ""
            detail = f" (resets {reset})" if reset else ""
            log(
                "⚠️ ",
                f"GraphQL rate-limited/unavailable — switching remaining PRs to "
                f"REST API (resolution status unavailable — showing all "
                f"threads).{detail}",
            )
            set_graphql_disabled(True)

    # 3) REST fallback (separate bucket) for whatever's still pending.
    for n in _remaining():
        hint = pr_hints.get(n) or {}
        try:
            meta, raw_threads = _fetch_pr_threads_rest(owner, name, n, meta_hint=hint)
        except subprocess.CalledProcessError as e:
            if _is_rate_limit_error(e):
                log("🚫", f"PR #{n}: REST API also rate-limited — stopping early.")
                skipped = _remaining()
                return infos, skipped
            log("❌", f"PR #{n}: failed to fetch — {e}")
            skipped.append(n)  # surface the failure rather than dropping it silently
            continue
        _store(n, meta, raw_threads, "rest")

    return infos, skipped


# ── Open PR listing ────────────────────────────────────────────────────────────


def fetch_open_prs(repo: str, mine_only: bool = False) -> List[Dict]:
    """Fetch open PRs from GitHub.

    Uses ``gh pr list`` (GraphQL) first, falling back to the REST API
    when GraphQL is rate-limited. Each PR dict carries number/title/headRefName/
    baseRefName/author/updatedAt (updatedAt + baseRefName power the thread cache
    and let the REST thread path skip its per-PR metadata GET).
    """
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,headRefName,baseRefName,author,updatedAt",
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
                "baseRefName": p["base"]["ref"],
                "author": {"login": author_login},
                "updatedAt": p.get("updated_at"),
            })

        if len(page_data) < 50:
            break
        page += 1

    return all_prs
