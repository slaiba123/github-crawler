"""
Repository persistence layer.

Translates domain models → SQL and executes them against Postgres.

Key design decisions:

UPSERT strategy:
  ON CONFLICT (github_id) DO UPDATE ... WHERE <something changed>
  If 90% of repos have the same star count today as yesterday, those
  rows produce zero disk writes. Only changed rows touch the disk.

Batch inserts:
  execute_values() sends 500 repos in a single SQL statement instead of
  one INSERT per repo. This is ~50-100x faster because it reduces
  round-trips to Postgres from 500 down to 1.
"""

import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from models import Repository

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


class RepositoryStore:
    """
    Handles all database reads and writes for repositories.
    Receives a plain psycopg2 connection — no pool, no abstraction layer.
    """

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn

    def upsert_batch(self, repos: list[Repository]) -> int:
        """
        Insert or update a batch of repositories.

        Uses UPSERT so daily re-crawls only touch rows that changed.
        The WHERE clause on the UPDATE means Postgres skips writing rows
        where stars, forks, and is_archived are all unchanged.

        Returns the number of rows actually written.
        """
        if not repos:
            return 0

        rows = [_repo_to_row(r) for r in repos]

        sql = """
            INSERT INTO repositories (
                github_id, name_with_owner, name, owner,
                stars, forks, is_archived, primary_language,
                description, created_at, pushed_at, updated_at
            ) VALUES %s
            ON CONFLICT (github_id) DO UPDATE SET
                name_with_owner  = EXCLUDED.name_with_owner,
                stars            = EXCLUDED.stars,
                forks            = EXCLUDED.forks,
                is_archived      = EXCLUDED.is_archived,
                primary_language = EXCLUDED.primary_language,
                description      = EXCLUDED.description,
                pushed_at        = EXCLUDED.pushed_at,
                updated_at       = NOW()
            WHERE
                repositories.stars       != EXCLUDED.stars     OR
                repositories.forks       != EXCLUDED.forks     OR
                repositories.is_archived != EXCLUDED.is_archived
        """

        with self._conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, rows, page_size=BATCH_SIZE)
            affected = cur.rowcount

        self._conn.commit()
        logger.debug("Upserted %d repos (%d rows changed)", len(repos), affected)
        return affected

    def count(self) -> int:
        """Return total number of repos currently in the database."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repositories")
            return cur.fetchone()[0]


def _repo_to_row(r: Repository) -> tuple:
    """
    Convert a Repository domain model to a SQL row tuple.
    Column order must match the INSERT statement in upsert_batch().
    """
    return (
        r.github_id,
        r.name_with_owner,
        r.name,
        r.owner,
        r.stars,
        r.forks,
        r.is_archived,
        r.primary_language,
        r.description,
        r.created_at,
        r.pushed_at,
        datetime.now(timezone.utc),
    )