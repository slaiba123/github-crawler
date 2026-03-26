"""
GitHub GraphQL API client — async, concurrent.

Responsibilities:
  1. Send GraphQL queries to GitHub
  2. Handle rate limiting (primary + secondary limits)
  3. Retry on transient failures with exponential backoff
  4. Translate raw API responses → clean domain models (anti-corruption layer)

How we reach 100,000 repos despite GitHub's 1,000-result-per-query cap:
  GitHub caps every search query at 1,000 results. We bypass this by
  crossing two dimensions:
    _STAR_BUCKETS  — 38 star ranges
    _LANGUAGES     — 16 language filters (+ empty = no filter)

  Each (bucket, language) pair is an independent query with its own
  1,000-result cap:
    "stars:1 language:Python"      → up to 1,000 repos
    "stars:1 language:JavaScript"  → up to 1,000 different repos
    ...

  38 × 16 = 608 combinations × 1,000 = 608,000 potential repos.
  A seen_ids set deduplicates repos appearing in multiple combinations.

Why async?
  Sync:  fetch → wait 300ms → fetch → wait 300ms  (idle every round trip)
  Async: fetch combo 1 ─┐
         fetch combo 2 ─┤  all waiting simultaneously
         fetch combo 3 ─┘  done in the time of ONE wait

  Roughly 5× faster for the same number of requests.

Semaphore:
  Without a limit, all 608 combos would fire at once and GitHub would
  block the token. asyncio.Semaphore(5) caps in-flight requests to 5
  at any moment — fast enough to be significantly quicker than sync,
  conservative enough to avoid abuse detection.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from models import GraphQLPage, Repository

logger = logging.getLogger(__name__)

_SEARCH_QUERY = """
query SearchRepos($query: String!, $first: Int!, $after: String) {
  search(query: $query, type: REPOSITORY, first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on Repository {
        databaseId
        nameWithOwner
        name
        owner { login }
        stargazerCount
        forkCount
        isArchived
        primaryLanguage { name }
        description
        createdAt
        pushedAt
      }
    }
  }
  rateLimit {
    remaining
    resetAt
    cost
  }
}
"""

_STAR_BUCKETS = [
    "stars:>100000",
    "stars:50000..99999",
    "stars:20000..49999",
    "stars:10000..19999",
    "stars:7000..9999",
    "stars:5000..6999",
    "stars:4000..4999",
    "stars:3000..3999",
    "stars:2000..2999",
    "stars:1500..1999",
    "stars:1000..1499",
    "stars:800..999",
    "stars:600..799",
    "stars:500..599",
    "stars:400..499",
    "stars:300..399",
    "stars:250..299",
    "stars:200..249",
    "stars:150..199",
    "stars:100..149",
    "stars:90..99",
    "stars:80..89",
    "stars:70..79",
    "stars:60..69",
    "stars:50..59",
    "stars:45..49",
    "stars:40..44",
    "stars:35..39",
    "stars:30..34",
    "stars:25..29",
    "stars:20..24",
    "stars:15..19",
    "stars:10..14",
    "stars:8..9",
    "stars:6..7",
    "stars:4..5",
    "stars:2..3",
    "stars:1",
]

_LANGUAGES = [
    "language:JavaScript",
    "language:Python",
    "language:Java",
    "language:TypeScript",
    "language:Go",
    "language:Rust",
    "language:C++",
    "language:C",
    "language:Ruby",
    "language:PHP",
    "language:Swift",
    "language:Kotlin",
    "language:Shell",
    "language:HTML",
    "language:CSS",
    "",   # no language filter — catches repos GitHub couldn't classify
]

# Max concurrent requests. Higher = faster, but risks GitHub's abuse detection.
MAX_CONCURRENT = 5


class GitHubClient:
    """
    Async GitHub GraphQL client.

    Anti-corruption layer: all knowledge about GitHub's API shape lives here.
    The rest of the codebase only ever sees clean Repository domain models.
    """

    API_URL       = "https://api.github.com/graphql"
    PAGE_SIZE     = 100
    MIN_REMAINING = 50   # sleep and wait for reset if below this
    MAX_RETRIES   = 5

    def __init__(self, token: str) -> None:
        self._token     = token
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create the aiohttp session (must be inside an event loop)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={
                "Authorization":           f"Bearer {self._token}",
                "Content-Type":            "application/json",
                "Accept":                  "application/vnd.github+json",
                "X-Github-Next-Global-ID": "1",
            })
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def collect_repositories(
        self,
        target_count: int = 100_000,
    ) -> list[Repository]:
        """
        Collect up to target_count unique repositories concurrently.

        All (bucket, language) combinations run as concurrent tasks,
        limited to MAX_CONCURRENT in-flight requests at a time via the
        semaphore. Returns a deduplicated list of Repository objects.
        """
        seen_ids: set[int]         = set()
        results:  list[Repository] = []
        lock       = asyncio.Lock()      # protects seen_ids + results
        stop_event = asyncio.Event()

        combos = [
            (bucket, lang)
            for bucket in _STAR_BUCKETS
            for lang in _LANGUAGES
        ]

        async def crawl_combo(bucket: str, lang: str) -> None:
            if stop_event.is_set():
                return

            search_query = f"{bucket} {lang}".strip()
            cursor: str | None = None

            while not stop_event.is_set():
                async with self._semaphore:
                    page = await self._fetch_page(
                        search_query=f"{search_query} sort:stars-desc",
                        cursor=cursor,
                    )

                async with lock:
                    for repo in page.repositories:
                        if repo.github_id not in seen_ids:
                            seen_ids.add(repo.github_id)
                            results.append(repo)
                            if len(results) >= target_count:
                                stop_event.set()
                                return

                if page.is_last_page:
                    break
                cursor = page.end_cursor

        async with asyncio.TaskGroup() as tg:
            for b, l in combos:
                tg.create_task(crawl_combo(b, l))

        return results[:target_count]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_page(
        self,
        search_query: str,
        cursor: str | None = None,
    ) -> GraphQLPage:
        """Fetch one page of results with exponential backoff retry."""
        variables: dict[str, Any] = {
            "query": search_query,
            "first": self.PAGE_SIZE,
        }
        if cursor:
            variables["after"] = cursor

        session = self._get_session()

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                async with session.post(
                    self.API_URL,
                    json={"query": _SEARCH_QUERY, "variables": variables},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    return await self._parse_response(response)

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                wait = 2 ** attempt
                logger.warning(
                    "Request failed (attempt %d/%d): %s. Retrying in %ds.",
                    attempt, self.MAX_RETRIES, exc, wait,
                )
                if attempt == self.MAX_RETRIES:
                    raise
                await asyncio.sleep(wait)

        raise RuntimeError("Unreachable")

    async def _parse_response(self, response: aiohttp.ClientResponse) -> GraphQLPage:
        """Parse aiohttp response into a GraphQLPage domain object."""
        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning("Secondary rate limit hit. Sleeping %ds.", retry_after)
            await asyncio.sleep(retry_after)
            raise aiohttp.ClientError("Secondary rate limit — will retry")

        response.raise_for_status()
        data = await response.json()

        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")

        rate_limit = data["data"]["rateLimit"]
        remaining  = rate_limit["remaining"]
        logger.debug("Rate limit: %d remaining, cost: %d", remaining, rate_limit["cost"])

        if remaining < self.MIN_REMAINING:
            reset_at = datetime.fromisoformat(rate_limit["resetAt"])
            wait_seconds = max(
                0,
                (reset_at - datetime.now(timezone.utc)).total_seconds() + 5,
            )
            logger.info(
                "Rate limit low (%d remaining). Sleeping %.0fs.",
                remaining, wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

        search    = data["data"]["search"]
        page_info = search["pageInfo"]

        repositories = [
            self._parse_repo_node(node)
            for node in search["nodes"]
            if node
        ]

        return GraphQLPage(
            repositories=repositories,
            end_cursor=page_info.get("endCursor"),
            has_next=page_info["hasNextPage"],
        )

    @staticmethod
    def _parse_repo_node(node: dict[str, Any]) -> Repository:
        """Translate one GraphQL node → Repository domain model."""

        def parse_dt(s: str | None) -> datetime | None:
            if not s:
                return None
            return datetime.fromisoformat(s)  # 3.11+ handles Z suffix natively

        return Repository(
            github_id        = node["databaseId"],
            name_with_owner  = node["nameWithOwner"],
            name             = node["name"],
            owner            = node["owner"]["login"],
            stars            = node["stargazerCount"],
            forks            = node["forkCount"],
            is_archived      = node.get("isArchived", False),
            primary_language = (node.get("primaryLanguage") or {}).get("name"),
            description      = node.get("description"),
            created_at       = parse_dt(node.get("createdAt")),
            pushed_at        = parse_dt(node.get("pushedAt")),
        )