"""
Export database contents to CSV and JSON.
Produces two files for the GitHub Actions artifact upload:
  - repositories.csv   (human-readable, opens in Excel)
  - repositories.json  (machine-readable, includes metadata)
Kept intentionally simple — reads from Postgres, writes two files.
"""
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

DB_DSN     = os.environ["DATABASE_URL"]
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/tmp/crawl-output"))

SQL = """
    SELECT
        github_id,
        name_with_owner,
        owner,
        name,
        stars,
        forks,
        is_archived,
        primary_language,
        description,
        created_at,
        pushed_at,
        first_seen_at,
        updated_at
    FROM repositories
    ORDER BY stars DESC
"""

FIELDS = [
    "github_id", "name_with_owner", "owner", "name",
    "stars", "forks", "is_archived", "primary_language",
    "description", "created_at", "pushed_at",
    "first_seen_at", "updated_at",
]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    sslmode = "require" if "supabase" in DB_DSN else "prefer"
    conn = psycopg.connect(DB_DSN, sslmode=sslmode)

    try:
        rows = []
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(SQL)
            rows = [dict(row) for row in cur]

        # CSV
        csv_path = OUTPUT_DIR / "repositories.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        logger.info("CSV: %d rows → %s", len(rows), csv_path)

        # JSON
        json_path = OUTPUT_DIR / "repositories.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "total": len(rows),
                    "repositories": rows,
                },
                f,
                indent=2,
                default=str,  # handles datetime objects
            )
        logger.info("JSON: %d rows → %s", len(rows), json_path)
        logger.info("Export complete.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()