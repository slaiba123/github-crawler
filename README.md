# GitHub Repository Crawler

A pipeline that crawls 100,000 GitHub repositories and stores their metadata in PostgreSQL, running daily via GitHub Actions.

Uses **GitHub's GraphQL API exclusively**. No REST endpoints.

---

## How it reaches 100,000 repos

GitHub's Search API caps every query at **1,000 results maximum**. With star ranges alone the crawler only reached ~36,000 repos before running out of combinations.

**The fix: cross two dimensions.**

- `_STAR_BUCKETS` — 38 star ranges (`stars:>100000` down to `stars:1`)
- `_LANGUAGES` — 16 language filters (Python, JavaScript, Java, etc.)

Every combination becomes an independent GraphQL query:

```
"stars:1 language:Python"      → up to 1,000 repos
"stars:1 language:JavaScript"  → up to 1,000 different repos
"stars:1 language:Java"        → up to 1,000 different repos
```

38 × 16 = **608 independent queries × 1,000 results = 608,000 potential repos.**  
The crawler stops the moment it hits 100,000. A `seen_ids` set prevents the same repo being counted twice.

---

## Architecture

```
GitHub GraphQL API
       │
       ▼
 github_client.py   ← Anti-corruption layer
                      All GitHub knowledge lives here.
                      Translates GitHub JSON → clean domain models.
       │
       ▼
    models.py        ← Domain layer
                      Pure Python frozen dataclasses.
                      No GitHub. No Postgres. Just data.
       │
       ▼
  repository.py      ← Persistence layer
                      Translates domain models → SQL.
                      All Postgres knowledge lives here.
       │
       ▼
   PostgreSQL         ← 1 table: repositories
```

`crawler.py` sits above all of this — it coordinates the three layers but contains no GitHub API logic and no SQL.

---

## File responsibilities

| File | Single responsibility |
|------|-----------------------|
| `github_client.py` | Talk to GitHub GraphQL API. Nothing else. |
| `models.py` | Define what Repository and GraphQLPage look like. Nothing else. |
| `repository.py` | Read and write Postgres. Nothing else. |
| `crawler.py` | Wire the others together. Track progress. Handle failures. |
| `export.py` | Read the DB and write CSV/JSON files. Nothing else. |
| `schema.sql` | Define the database schema. |
| `crawl.yml` | Define the GitHub Actions pipeline. |

---

## Design principles

### 1. Anti-corruption layer

`github_client.py` is the only file that knows GitHub exists. It translates GitHub's API — camelCase field names, deeply nested objects, null nodes for deleted repos — into clean Python objects. Every other file is completely shielded from GitHub's API shape.

If GitHub renames `stargazerCount` to `starCount` tomorrow, you change one line in `_parse_repo_node()`. Nothing else breaks.

### 2. Immutability

All domain models use `frozen=True`. Once created, a field cannot be changed. To update a field you use `dataclasses.replace()` which creates a new instance — the original is untouched.

```python
# Cannot do this — frozen=True blocks it:
repo.stars = 999   # FrozenInstanceError

# Do this instead — creates a new object:
repo = replace(repo, stars=999)
```

### 3. Separation of concerns

Each file has exactly one job:
- `github_client.py` never imports `psycopg2`
- `repository.py` never imports `aiohttp`
- `crawler.py` imports both but calls neither for actual logic — it only coordinates

If you swap GitHub for GitLab, only `github_client.py` changes.  
If you swap Postgres for MySQL, only `repository.py` changes.

### 4. Efficient UPSERT

Daily re-crawls use `ON CONFLICT (github_id) DO UPDATE` with a `WHERE` clause that only writes rows when data actually changed:

```sql
ON CONFLICT (github_id) DO UPDATE SET
    stars     = EXCLUDED.stars,
    forks     = EXCLUDED.forks,
    ...
WHERE
    repositories.stars       != EXCLUDED.stars     OR
    repositories.forks       != EXCLUDED.forks     OR
    repositories.is_archived != EXCLUDED.is_archived
```

If 90,000 repos have the same star count today as yesterday, those 90,000 rows generate **zero disk writes**.

### 5. Batch inserts

Instead of one SQL statement per repo, 500 repos are written in a single SQL statement using `execute_values()`. This is 50-100x faster because it reduces round-trips to Postgres from 500 down to 1.

---

## Schema

```sql
CREATE TABLE repositories (
    github_id        BIGINT      NOT NULL UNIQUE,
    name_with_owner  TEXT        NOT NULL,   -- e.g. "torvalds/linux"
    name             TEXT        NOT NULL,
    owner            TEXT        NOT NULL,
    stars            INTEGER     NOT NULL DEFAULT 0,
    forks            INTEGER     NOT NULL DEFAULT 0,
    is_archived      BOOLEAN     NOT NULL DEFAULT FALSE,
    primary_language TEXT,
    description      TEXT,
    created_at       TIMESTAMPTZ,
    pushed_at        TIMESTAMPTZ,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Why `github_id` as the unique key?

Repos can be renamed or transferred to a new owner. `github_id` is GitHub's internal integer ID — it never changes. Using it as the unique key means renames never create duplicate rows.

---

## Concurrency — async crawler

The crawler uses `asyncio` and `aiohttp` to fetch multiple query combinations simultaneously.

### Why async is faster

```
Sync:  fetch → wait 300ms → fetch → wait 300ms   (idle every round trip)

Async: fetch combo 1 ─┐
       fetch combo 2 ─┤  all 5 waiting simultaneously
       fetch combo 3 ─┘  done in the time of ONE wait
```

Result: roughly **5× faster** for the same number of requests.

### How concurrency is controlled — the semaphore

Without a limit, all 608 combinations would fire at once and GitHub would block the token immediately. A semaphore acts like a parking lot with exactly 5 spaces:

```python
self._semaphore = asyncio.Semaphore(5)

async with self._semaphore:           # wait for a free space
    page = await self._fetch_page()   # do the work
                                      # space freed automatically on exit
```

At most 5 requests are in-flight at any moment.

---

## Rate limiting

GitHub allows 5,000 GraphQL points per hour for authenticated requests. Each page of 100 repos costs ~100 points. 100,000 repos / 100 per page = ~1,000 requests. Well within the hourly limit.

**Primary limit** — checked via `rateLimit.remaining` in every GraphQL response. If under 50 points, sleep until GitHub resets the quota.

**Secondary limit** — GitHub's abuse detection. Returns HTTP 429 with a `Retry-After` header. Sleep exactly that many seconds then retry.

**Exponential backoff** — on any network failure: wait 2s, 4s, 8s, 16s, 32s. Give up after 5 attempts.

---

## GitHub Actions pipeline

```
Trigger: daily 02:00 UTC  or  manual workflow_dispatch
         │
         ├─ Postgres service container starts (postgres:15)
         │
         ├─ 1. Checkout code
         ├─ 2. Setup Python 3.11
         ├─ 3. pip install -r requirements.txt
         ├─ 4. psql schema.sql       → creates repositories table
         ├─ 5. python crawler.py     → fetches 100k repos, saves to DB
         ├─ 6. python export.py      → writes repositories.csv + repositories.json
         ├─ 7. upload-artifact       → files downloadable for 30 days
         └─ 8. print-summary         → SQL stats printed to Actions console
```

Uses only `secrets.GITHUB_TOKEN` — the default Actions token. No extra secrets or elevated permissions needed.

---

## Running locally

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/github-crawler
cd github-crawler

# 2. Start Postgres
docker run -d \
  -e POSTGRES_USER=crawler \
  -e POSTGRES_PASSWORD=crawler \
  -e POSTGRES_DB=github_crawler \
  -p 5432:5432 postgres:15

# 3. Apply schema
psql postgresql://crawler:crawler@localhost:5432/github_crawler \
  -f sql/schema.sql

# 4. Install dependencies
pip install -r requirements.txt

# 5. Run crawler
export DATABASE_URL=postgresql://crawler:crawler@localhost:5432/github_crawler
export GITHUB_TOKEN=ghp_your_token_here
export TARGET_REPOS=1000
python src/crawler.py

# 6. Export results
export OUTPUT_DIR=./output
python src/export.py
```

---

## What I would do differently for 500 million repositories

### 1. Distributed crawling

A single Python process is the bottleneck. At 500M repos you need horizontal scale:

- Split (bucket, language) combinations across N workers using a message queue (SQS or Redis Streams). Each worker pulls a job, processes it, acknowledges on success. Failed jobs retry automatically.
- Use GitHub App installation tokens — each worker gets its own token, multiplying the rate limit. 10 workers × 5,000 points = 50,000 points/hour.

### 2. Database at scale

A single Postgres instance cannot handle 500M rows efficiently:

- **Hash partitioning** — partition `repositories` by `github_id % N`. Queries hit one partition instead of scanning 500M rows.
- **Read replicas** — direct all export and analytics queries to replicas, keeping write throughput fast on the primary.

### 3. Smarter crawl strategy

- **Incremental crawl** — repos not pushed to in a year rarely change star count. Prioritise recently active repos.
- **GitHub Events API** — emits a `WatchEvent` when a repo is starred. Subscribe to this stream instead of polling — only re-crawl repos that actually changed. Reduces API calls by orders of magnitude.

---

## How the schema evolves — issues, PRs, comments

### Core principle: store counts first, content second

```sql
CREATE TABLE pull_requests (
    github_id      BIGINT  NOT NULL UNIQUE,
    repo_github_id BIGINT  REFERENCES repositories(github_id),
    number         INTEGER NOT NULL,
    title          TEXT,
    state          TEXT,
    comment_count  INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE pr_comments (
    github_id    BIGINT NOT NULL UNIQUE,
    pr_github_id BIGINT REFERENCES pull_requests(github_id),
    body         TEXT,
    author       TEXT,
    created_at   TIMESTAMPTZ
);

CREATE TABLE issues (
    github_id      BIGINT  NOT NULL UNIQUE,
    repo_github_id BIGINT  REFERENCES repositories(github_id),
    number         INTEGER NOT NULL,
    title          TEXT,
    state          TEXT,
    comment_count  INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### How "a PR gets 10 comments today, 20 tomorrow" is handled efficiently

Tomorrow's re-crawl runs one single UPDATE:

```sql
UPDATE pull_requests
SET comment_count = 20
WHERE github_id = 12345
  AND comment_count != 20;   -- skip entirely if nothing changed
```

**1 row written.** Not 10 new rows.

For the actual comment text, insert only the new ones:

```sql
INSERT INTO pr_comments (github_id, pr_github_id, body, author)
VALUES (...)
ON CONFLICT (github_id) DO NOTHING;   -- safe to re-crawl, never creates duplicates
```

### Why this schema survives future requirements

- New PR fields → add a column, zero changes to other tables
- CI checks → new `ci_checks` table with `pr_github_id` FK, zero changes to existing tables
- Reviews → new `pr_reviews` table, same pattern
