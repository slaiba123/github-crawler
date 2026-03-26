"""
Domain models — pure Python dataclasses.

Design principles:
  - frozen=True: immutable once created, no accidental mutation
  - No external dependencies: nothing here knows about GitHub or Postgres
  - Single responsibility: represent data shapes only
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Repository:
    """
    A single GitHub repository.

    Immutable by design. To "change" a field, use dataclasses.replace()
    which creates a new instance — the original is untouched.
    """
    github_id:        int
    name_with_owner:  str           # e.g. "torvalds/linux"
    name:             str
    owner:            str
    stars:            int
    forks:            int = 0
    is_archived:      bool = False
    primary_language: str | None = None
    description:      str | None = None
    created_at:       datetime | None = None
    pushed_at:        datetime | None = None


@dataclass(frozen=True)
class GraphQLPage:
    """
    A single page of results from the GitHub GraphQL API.
    Encapsulates pagination state so callers never parse raw API responses.
    """
    repositories: list[Repository]
    end_cursor:   str | None    # None = last page
    has_next:     bool

    @property
    def is_last_page(self) -> bool:
        return not self.has_next