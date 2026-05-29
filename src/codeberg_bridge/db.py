from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


SCHEMA = """
CREATE TABLE IF NOT EXISTS mirrored_prs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  codeberg_repo TEXT NOT NULL,
  codeberg_pr_number INTEGER NOT NULL,
  github_repo TEXT NOT NULL,
  github_pr_number INTEGER,
  github_fork_repo TEXT NOT NULL,
  github_branch TEXT NOT NULL,
  last_synced_commit TEXT,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(codeberg_repo, codeberg_pr_number, github_repo)
);

CREATE TABLE IF NOT EXISTS mirrored_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  codeberg_repo TEXT NOT NULL,
  codeberg_pr_number INTEGER NOT NULL,
  github_repo TEXT NOT NULL,
  github_pr_number INTEGER NOT NULL,
  src_platform TEXT NOT NULL,
  src_comment_id INTEGER NOT NULL,
  dst_platform TEXT NOT NULL,
  dst_comment_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(codeberg_repo, codeberg_pr_number, github_repo, src_platform, src_comment_id, dst_platform)
);

CREATE TABLE IF NOT EXISTS comment_cursors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  codeberg_repo TEXT NOT NULL,
  codeberg_pr_number INTEGER NOT NULL,
  github_repo TEXT NOT NULL,
  github_pr_number INTEGER NOT NULL,
  platform TEXT NOT NULL,
  last_seen_id INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  UNIQUE(codeberg_repo, codeberg_pr_number, github_repo, platform)
);
"""


@dataclass(frozen=True)
class MirroredPR:
    codeberg_repo: str
    codeberg_pr_number: int
    github_repo: str
    github_pr_number: int | None
    github_fork_repo: str
    github_branch: str
    last_synced_commit: str | None
    status: str


@dataclass(frozen=True)
class MirroredComment:
    codeberg_repo: str
    codeberg_pr_number: int
    github_repo: str
    github_pr_number: int
    src_platform: str
    src_comment_id: int
    dst_platform: str
    dst_comment_id: int


class Database:
    def __init__(self, sqlite_path: str):
        self._sqlite_path = sqlite_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def upsert_mapping(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        github_fork_repo: str,
        github_branch: str,
        status: str,
        github_pr_number: int | None = None,
        last_synced_commit: str | None = None,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mirrored_prs (
                  codeberg_repo, codeberg_pr_number,
                  github_repo, github_pr_number,
                  github_fork_repo, github_branch,
                  last_synced_commit, status,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(codeberg_repo, codeberg_pr_number, github_repo)
                DO UPDATE SET
                  github_pr_number=COALESCE(excluded.github_pr_number, mirrored_prs.github_pr_number),
                  github_fork_repo=excluded.github_fork_repo,
                  github_branch=excluded.github_branch,
                  last_synced_commit=excluded.last_synced_commit,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (
                    codeberg_repo,
                    codeberg_pr_number,
                    github_repo,
                    github_pr_number,
                    github_fork_repo,
                    github_branch,
                    last_synced_commit,
                    status,
                    now,
                    now,
                ),
            )

    def get_mapping(
        self, *, codeberg_repo: str, codeberg_pr_number: int, github_repo: str
    ) -> MirroredPR | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT codeberg_repo, codeberg_pr_number, github_repo, github_pr_number,
                       github_fork_repo, github_branch, last_synced_commit, status
                FROM mirrored_prs
                WHERE codeberg_repo=? AND codeberg_pr_number=? AND github_repo=?
                """,
                (codeberg_repo, codeberg_pr_number, github_repo),
            ).fetchone()
            if not row:
                return None
            return MirroredPR(
                codeberg_repo=row["codeberg_repo"],
                codeberg_pr_number=int(row["codeberg_pr_number"]),
                github_repo=row["github_repo"],
                github_pr_number=row["github_pr_number"],
                github_fork_repo=row["github_fork_repo"],
                github_branch=row["github_branch"],
                last_synced_commit=row["last_synced_commit"],
                status=row["status"],
            )

    def update_status(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        status: str,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE mirrored_prs
                SET status=?, updated_at=?
                WHERE codeberg_repo=? AND codeberg_pr_number=? AND github_repo=?
                """,
                (status, now, codeberg_repo, codeberg_pr_number, github_repo),
            )

    def list_open_mappings(
        self, *, codeberg_repo: str, github_repo: str
    ) -> list[MirroredPR]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT codeberg_repo, codeberg_pr_number, github_repo, github_pr_number,
                       github_fork_repo, github_branch, last_synced_commit, status
                FROM mirrored_prs
                WHERE codeberg_repo=? AND github_repo=? AND status='open'
                """,
                (codeberg_repo, github_repo),
            ).fetchall()
        out: list[MirroredPR] = []
        for row in rows:
            out.append(
                MirroredPR(
                    codeberg_repo=row["codeberg_repo"],
                    codeberg_pr_number=int(row["codeberg_pr_number"]),
                    github_repo=row["github_repo"],
                    github_pr_number=row["github_pr_number"],
                    github_fork_repo=row["github_fork_repo"],
                    github_branch=row["github_branch"],
                    last_synced_commit=row["last_synced_commit"],
                    status=row["status"],
                )
            )
        return out

    def has_mirrored_comment(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        src_platform: str,
        src_comment_id: int,
        dst_platform: str,
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM mirrored_comments
                WHERE codeberg_repo=?
                  AND codeberg_pr_number=?
                  AND github_repo=?
                  AND src_platform=?
                  AND src_comment_id=?
                  AND dst_platform=?
                LIMIT 1
                """,
                (
                    codeberg_repo,
                    int(codeberg_pr_number),
                    github_repo,
                    src_platform,
                    int(src_comment_id),
                    dst_platform,
                ),
            ).fetchone()
        return bool(row)

    def has_mirrored_comment_any_dst(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        src_platform: str,
        src_comment_id: int,
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM mirrored_comments
                WHERE codeberg_repo=?
                  AND codeberg_pr_number=?
                  AND github_repo=?
                  AND src_platform=?
                  AND src_comment_id=?
                LIMIT 1
                """,
                (
                    codeberg_repo,
                    int(codeberg_pr_number),
                    github_repo,
                    src_platform,
                    int(src_comment_id),
                ),
            ).fetchone()
        return bool(row)

    def upsert_mirrored_comment(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        github_pr_number: int,
        src_platform: str,
        src_comment_id: int,
        dst_platform: str,
        dst_comment_id: int,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mirrored_comments (
                  codeberg_repo, codeberg_pr_number,
                  github_repo, github_pr_number,
                  src_platform, src_comment_id,
                  dst_platform, dst_comment_id,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(codeberg_repo, codeberg_pr_number, github_repo, src_platform, src_comment_id, dst_platform)
                DO UPDATE SET
                  github_pr_number=excluded.github_pr_number,
                  dst_comment_id=excluded.dst_comment_id,
                  updated_at=excluded.updated_at
                """,
                (
                    codeberg_repo,
                    int(codeberg_pr_number),
                    github_repo,
                    int(github_pr_number),
                    src_platform,
                    int(src_comment_id),
                    dst_platform,
                    int(dst_comment_id),
                    now,
                    now,
                ),
            )

    def get_mirrored_comment_dst(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        src_platform: str,
        src_comment_id: int,
    ) -> tuple[str, int] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT dst_platform, dst_comment_id
                FROM mirrored_comments
                WHERE codeberg_repo=?
                  AND codeberg_pr_number=?
                  AND github_repo=?
                  AND src_platform=?
                  AND src_comment_id=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (
                    codeberg_repo,
                    int(codeberg_pr_number),
                    github_repo,
                    src_platform,
                    int(src_comment_id),
                ),
            ).fetchone()
        if not row:
            return None
        try:
            return (str(row["dst_platform"]), int(row["dst_comment_id"]))
        except Exception:
            return None

    def get_comment_cursor(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        platform: str,
    ) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT last_seen_id
                FROM comment_cursors
                WHERE codeberg_repo=? AND codeberg_pr_number=? AND github_repo=? AND platform=?
                """,
                (codeberg_repo, int(codeberg_pr_number), github_repo, platform),
            ).fetchone()
        if not row:
            return 0
        try:
            return int(row["last_seen_id"])
        except Exception:
            return 0

    def set_comment_cursor(
        self,
        *,
        codeberg_repo: str,
        codeberg_pr_number: int,
        github_repo: str,
        github_pr_number: int,
        platform: str,
        last_seen_id: int,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO comment_cursors (
                  codeberg_repo, codeberg_pr_number,
                  github_repo, github_pr_number,
                  platform, last_seen_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(codeberg_repo, codeberg_pr_number, github_repo, platform)
                DO UPDATE SET
                  github_pr_number=excluded.github_pr_number,
                  last_seen_id=excluded.last_seen_id,
                  updated_at=excluded.updated_at
                """,
                (
                    codeberg_repo,
                    int(codeberg_pr_number),
                    github_repo,
                    int(github_pr_number),
                    platform,
                    int(last_seen_id),
                    now,
                ),
            )
