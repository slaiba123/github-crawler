"""
Main crawl orchestrator — entry point for the crawl-stars step.
Wires together the GitHub client and the database store.
Contains no GitHub API logic and no SQL — it only coordinates.
Separation of concerns:
  github_client.py → knows about GitHub
  repository.py    → knows about Postgres
  crawler.py       → knows about neither; just coordinates them
"""
import asyncio
import logging
import os
import sys
import time
from itertools import islice

import psycopg

from github_client import GitHubClient
from repository import RepositoryStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TARGET_COUNT = int(os.environ.get("TARGET_REPOS", "100000"))
BATCH_SIZE   = int(os.environ.get("BATCH_SIZE", "500"))
DB_DSN       = os.environ["DATABASE_URL"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]


def batched(iterable, n: int):
    """Yield successive n-sized chunks from an iterable."""
    it = iter(iterable)
    while chunk := list(islice(it, n)):
        yield chunk


async def main() -> None:
    logger.info("=== GitHub Crawler starting ===")
    logger.info("Target: %d repos | Batch size: %d", TARGET_COUNT, BATCH_SIZE)

    sslmode = "require" if "supabase" in DB_DSN else "prefer"
    conn = psycopg.connect(DB_DSN, sslmode=sslmode)    
    store = RepositoryStore(conn)
    client = GitHubClient(token=GITHUB_TOKEN)

    start_time = time.monotonic()

    try:
        logger.info("Fetching %d repos concurrently...", TARGET_COUNT)
        repos = await client.collect_repositories(target_count=TARGET_COUNT)

        fetch_elapsed = time.monotonic() - start_time
        logger.info(
            "Fetch complete: %d repos in %.1fs (%.0f repos/s). Writing to DB...",
            len(repos), fetch_elapsed, len(repos) / fetch_elapsed,
        )

        total_written = 0
        total_changed = 0

        for batch in batched(repos, BATCH_SIZE):
            changed = store.upsert_batch(batch)
            total_written += len(batch)
            total_changed += changed
            logger.info(
                "Written: %6d / %d  |  rows changed: %d",
                total_written, len(repos), total_changed,
            )

        elapsed = time.monotonic() - start_time
        logger.info(
            "=== Done. %d repos in %.1fs (%.0f repos/s). %d rows changed. ===",
            total_written, elapsed, total_written / elapsed, total_changed,
        )

    except Exception as exc:
        logger.exception("Crawl failed: %s", exc)
        sys.exit(1)

    finally:
        await client.close()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())