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

import psycopg
from psycopg import Connection


from models import Repository

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


class RepositoryStore:
    """
    Handles all database reads and writes for repositories.
    Receives a plain psycopg2 connection — no pool, no abstraction layer.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

# Replace execute_values with psycopg3's executemany
    def upsert_batch(self, repos: list[Repository]) -> int:
        if not repos:
            return 0

        rows = [_repo_to_dict(r) for r in repos]

        sql = """
            INSERT INTO repositories (
                github_id, name_with_owner, name, owner,
                stars, forks, is_archived, primary_language,
                description, created_at, pushed_at, updated_at
            ) VALUES (
                %(github_id)s, %(name_with_owner)s, %(name)s, %(owner)s,
                %(stars)s, %(forks)s, %(is_archived)s, %(primary_language)s,
                %(description)s, %(created_at)s, %(pushed_at)s, %(updated_at)s
            )
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
            cur.executemany(sql, [_repo_to_dict(r) for r in repos])
            affected = cur.rowcount
        self._conn.commit()
        return affected

    def count(self) -> int:
        """Return total number of repos currently in the database."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repositories")
            return cur.fetchone()[0]


def _repo_to_dict(r: Repository) -> dict:
    return {
        "github_id":        r.github_id,
        "name_with_owner":  r.name_with_owner,
        "name":             r.name,
        "owner":            r.owner,
        "stars":            r.stars,
        "forks":            r.forks,
        "is_archived":      r.is_archived,
        "primary_language": r.primary_language,
        "description":      r.description,
        "created_at":       r.created_at,
        "pushed_at":        r.pushed_at,
        "updated_at":       datetime.now(timezone.utc),
    }
   