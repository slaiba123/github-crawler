-- ============================================================
-- GitHub Crawler Schema
--
-- Design principles:
--   1. github_id is the stable unique key — survives repo renames
--      and transfers (unlike name_with_owner which can change)
--   2. UPSERT targets github_id so daily re-crawls update existing
--      rows instead of creating duplicates
--   3. updated_at tracks when the row last changed in our DB
-- ============================================================

CREATE TABLE IF NOT EXISTS repositories (
    id               BIGSERIAL   PRIMARY KEY,
    github_id        BIGINT      NOT NULL UNIQUE,  -- GitHub's stable internal ID
    name_with_owner  TEXT        NOT NULL,          -- e.g. "torvalds/linux"
    name             TEXT        NOT NULL,
    owner            TEXT        NOT NULL,
    stars            INTEGER     NOT NULL DEFAULT 0,
    forks            INTEGER     NOT NULL DEFAULT 0,
    is_archived      BOOLEAN     NOT NULL DEFAULT FALSE,
    primary_language TEXT,
    description      TEXT,
    created_at       TIMESTAMPTZ,                   -- when repo was created on GitHub
    pushed_at        TIMESTAMPTZ,                   -- last push timestamp from GitHub
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast lookups by GitHub ID (used by every UPSERT)
CREATE INDEX IF NOT EXISTS idx_repositories_github_id
    ON repositories (github_id);

-- Fast queries sorted by popularity
CREATE INDEX IF NOT EXISTS idx_repositories_stars
    ON repositories (stars DESC);

-- Fast lookups by owner (e.g. "find all repos by torvalds")
CREATE INDEX IF NOT EXISTS idx_repositories_owner
    ON repositories (owner);

-- ============================================================
-- Future extensibility — add these tables later without
-- changing the repositories schema at all:
--
-- CREATE TABLE issues (
--     github_id      BIGINT NOT NULL UNIQUE,
--     repo_github_id BIGINT REFERENCES repositories(github_id),
--     number         INTEGER NOT NULL,
--     title          TEXT,
--     state          TEXT,
--     comment_count  INTEGER NOT NULL DEFAULT 0,
--     updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
-- );
--
-- CREATE TABLE pull_requests (
--     github_id      BIGINT NOT NULL UNIQUE,
--     repo_github_id BIGINT REFERENCES repositories(github_id),
--     number         INTEGER NOT NULL,
--     title          TEXT,
--     state          TEXT,
--     comment_count  INTEGER NOT NULL DEFAULT 0,  -- store the count, not each comment
--     updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
-- );
--
-- A PR going from 10 → 20 comments = one single UPDATE to comment_count.
-- Minimal rows affected, exactly as required.
-- ============================================================