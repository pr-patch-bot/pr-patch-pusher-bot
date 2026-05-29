from __future__ import annotations

import asyncio
import logging
import re
import os

from .clients import CodebergClient, GitHubClient
from .comments import MirrorComment, format_mirrored_comment
from .config import AppConfig, LoadedSecrets, MirrorConfig
from .db import Database
from .utils import parse_duration_seconds


log = logging.getLogger("codeberg_bridge.comments_mirror")

_MARKER_NEEDLE = "<!-- cbb:mirror"
_GITHUB_REVIEW_DISCUSSION_RE = re.compile(r"(?:discussion_r|#r)(\d+)")
_DEFAULT_POST_DELAY_S = 2.0


def _has_marker(body: str) -> bool:
    return _MARKER_NEEDLE in (body or "")


def _inline_context_block(*, path: str | None, line: int | None, position: int | None) -> str:
    parts: list[str] = []
    if path:
        parts.append(f"File: `{path}`")
    if line is not None:
        parts.append(f"Line: `{line}`")
    if position is not None:
        parts.append(f"Diff position: `{position}`")
    if not parts:
        return ""
    return "\n".join(["Inline context:", *[f"- {p}" for p in parts], ""])


def _extract_github_review_comment_id(text: str) -> int | None:
    if not text:
        return None
    m = _GITHUB_REVIEW_DISCUSSION_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _post_delay_seconds() -> float:
    raw = (os.environ.get("COMMENT_MIRROR_DELAY_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_POST_DELAY_S
    try:
        v = float(raw)
    except Exception:
        return _DEFAULT_POST_DELAY_S
    if v < 0:
        return 0.0
    return min(v, 30.0)


async def mirror_comments_once(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.mirror_comments_interval
    if not interval:
        return
    if not secrets.codeberg_token:
        raise RuntimeError("CODEBERG_TOKEN is required for mirror_comments_interval")

    github = GitHubClient(token=secrets.github_token)
    codeberg = CodebergClient(base_url=config.codeberg.base_url, token=secrets.codeberg_token)

    github_bot = await github.get_authenticated_user_login()
    # Some Codeberg tokens can be restricted in ways that make /api/v1/user return 403.
    # We can still safely mirror by relying on the hidden marker for loop prevention.
    codeberg_bot: str | None = None
    try:
        codeberg_bot = await codeberg.get_authenticated_user_login()
    except Exception:
        log.warning(
            "codeberg_bot_login_unavailable",
            extra={"mirror": mirror.name, "codeberg_repo": mirror.codeberg_repo},
        )

    mappings = db.list_open_mappings(codeberg_repo=mirror.codeberg_repo, github_repo=mirror.github_repo)
    allowed_codeberg_users = set(mirror.allowed_codeberg_users or [])
    for m in mappings:
        if not m.github_pr_number:
            continue
        github_pr_number = int(m.github_pr_number)
        codeberg_pr_number = int(m.codeberg_pr_number)
        delay_s = _post_delay_seconds()
        mirrored_counts = {
            "cb_to_gh_issue": 0,
            "cb_to_gh_review_reply": 0,
            "gh_issue_to_cb": 0,
            "gh_review_to_cb_issue": 0,
            "gh_review_to_cb_inline": 0,
        }

        # Phase 1: Codeberg issue comments -> GitHub issue comments
        page = 1
        seen_codeberg_comment_ids: set[int] = set()
        cursor_codeberg_issue = db.get_comment_cursor(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=codeberg_pr_number,
            github_repo=mirror.github_repo,
            platform="codeberg_issue",
        )
        max_seen_codeberg_issue = cursor_codeberg_issue
        while True:
            comments = await codeberg.list_issue_comments(
                repo=mirror.codeberg_repo, issue_number=codeberg_pr_number, page=page
            )
            if not comments:
                break
            new_ids = 0
            for c in comments:
                if c.id in seen_codeberg_comment_ids:
                    continue
                seen_codeberg_comment_ids.add(c.id)
                new_ids += 1
            if new_ids == 0:
                log.warning(
                    "codeberg_comments_pagination_stalled",
                    extra={
                        "mirror": mirror.name,
                        "codeberg_repo": mirror.codeberg_repo,
                        "codeberg_pr": codeberg_pr_number,
                        "page": page,
                    },
                )
                break
            for c in comments:
                if c.id > max_seen_codeberg_issue:
                    max_seen_codeberg_issue = c.id
                if codeberg_bot and c.author == codeberg_bot:
                    continue
                if allowed_codeberg_users and c.author not in allowed_codeberg_users:
                    continue
                if _has_marker(c.body):
                    continue
                if c.id <= cursor_codeberg_issue:
                    continue
                # This Codeberg comment may be mirrored either as a GitHub issue comment
                # or as a GitHub review-thread reply (phase 3). Avoid duplicates by
                # checking any existing destination mapping.
                if db.has_mirrored_comment_any_dst(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    src_platform="codeberg_issue",
                    src_comment_id=c.id,
                ):
                    continue
                body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="codeberg_issue",
                        src_author=c.author,
                        src_url=c.html_url,
                        src_id=c.id,
                        body=c.body,
                    )
                )
                # Phase 3 best-effort: if a Codeberg comment references a GitHub inline review
                # comment (by URL like discussion_r123), reply into that thread.
                in_reply_to = _extract_github_review_comment_id(c.body)
                if in_reply_to:
                    try:
                        try:
                            created_review = await github.create_review_comment_reply_via_replies_endpoint(
                                repo=mirror.github_repo,
                                pull_number=github_pr_number,
                                comment_id=in_reply_to,
                                body=body,
                            )
                        except Exception:
                            created_review = await github.create_review_comment_reply(
                                repo=mirror.github_repo,
                                pull_number=github_pr_number,
                                in_reply_to=in_reply_to,
                                body=body,
                            )
                        db.upsert_mirrored_comment(
                            codeberg_repo=mirror.codeberg_repo,
                            codeberg_pr_number=codeberg_pr_number,
                            github_repo=mirror.github_repo,
                            github_pr_number=github_pr_number,
                            src_platform="codeberg_issue",
                            src_comment_id=c.id,
                            dst_platform="github_review",
                            dst_comment_id=created_review.id,
                        )
                        mirrored_counts["cb_to_gh_review_reply"] += 1
                        if delay_s:
                            await asyncio.sleep(delay_s)
                        continue
                    except Exception:
                        log.exception(
                            "comments_mirror_reply_to_review_failed",
                            extra={
                                "mirror": mirror.name,
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "in_reply_to": in_reply_to,
                            },
                        )

                created_issue = await github.create_issue_comment(
                    repo=mirror.github_repo, issue_number=github_pr_number, body=body
                )
                db.upsert_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    github_pr_number=github_pr_number,
                    src_platform="codeberg_issue",
                    src_comment_id=c.id,
                    dst_platform="github_issue",
                    dst_comment_id=created_issue.id,
                )
                mirrored_counts["cb_to_gh_issue"] += 1
                if delay_s:
                    await asyncio.sleep(delay_s)
            if len(comments) < 50:
                break
            page += 1
        if max_seen_codeberg_issue > cursor_codeberg_issue:
            db.set_comment_cursor(
                codeberg_repo=mirror.codeberg_repo,
                codeberg_pr_number=codeberg_pr_number,
                github_repo=mirror.github_repo,
                github_pr_number=github_pr_number,
                platform="codeberg_issue",
                last_seen_id=max_seen_codeberg_issue,
            )

        # Phase 1: GitHub issue comments -> Codeberg issue comments
        page = 1
        seen_github_issue_comment_ids: set[int] = set()
        cursor_github_issue = db.get_comment_cursor(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=codeberg_pr_number,
            github_repo=mirror.github_repo,
            platform="github_issue",
        )
        max_seen_github_issue = cursor_github_issue
        while True:
            comments = await github.list_issue_comments(
                repo=mirror.github_repo, issue_number=github_pr_number, page=page
            )
            if not comments:
                break
            new_ids = 0
            for c in comments:
                if c.id in seen_github_issue_comment_ids:
                    continue
                seen_github_issue_comment_ids.add(c.id)
                new_ids += 1
            if new_ids == 0:
                log.warning(
                    "github_issue_comments_pagination_stalled",
                    extra={
                        "mirror": mirror.name,
                        "github_repo": mirror.github_repo,
                        "github_pr": github_pr_number,
                        "page": page,
                    },
                )
                break
            for c in comments:
                if c.id > max_seen_github_issue:
                    max_seen_github_issue = c.id
                if c.author == github_bot:
                    continue
                if _has_marker(c.body):
                    continue
                if c.id <= cursor_github_issue:
                    continue
                if db.has_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    src_platform="github_issue",
                    src_comment_id=c.id,
                    dst_platform="codeberg_issue",
                ):
                    continue
                body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="github_issue",
                        src_author=c.author,
                        src_url=c.html_url,
                        src_id=c.id,
                        body=c.body,
                    )
                )
                created = await codeberg.create_issue_comment(
                    repo=mirror.codeberg_repo, issue_number=codeberg_pr_number, body=body
                )
                db.upsert_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    github_pr_number=github_pr_number,
                    src_platform="github_issue",
                    src_comment_id=c.id,
                    dst_platform="codeberg_issue",
                    dst_comment_id=created.id,
                )
                mirrored_counts["gh_issue_to_cb"] += 1
                if delay_s:
                    await asyncio.sleep(delay_s)
            if len(comments) < 100:
                break
            page += 1
        if max_seen_github_issue > cursor_github_issue:
            db.set_comment_cursor(
                codeberg_repo=mirror.codeberg_repo,
                codeberg_pr_number=codeberg_pr_number,
                github_repo=mirror.github_repo,
                github_pr_number=github_pr_number,
                platform="github_issue",
                last_seen_id=max_seen_github_issue,
            )

        # Phase 2: GitHub inline review comments -> Codeberg as normal issue comments (one-way)
        page = 1
        seen_github_review_comment_ids: set[int] = set()
        cursor_github_review = db.get_comment_cursor(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=codeberg_pr_number,
            github_repo=mirror.github_repo,
            platform="github_review",
        )
        max_seen_github_review = cursor_github_review
        while True:
            comments = await github.list_review_comments(
                repo=mirror.github_repo, pull_number=github_pr_number, page=page
            )
            if not comments:
                break
            new_ids = 0
            for c in comments:
                if c.id in seen_github_review_comment_ids:
                    continue
                seen_github_review_comment_ids.add(c.id)
                new_ids += 1
            if new_ids == 0:
                log.warning(
                    "github_review_comments_pagination_stalled",
                    extra={
                        "mirror": mirror.name,
                        "github_repo": mirror.github_repo,
                        "github_pr": github_pr_number,
                        "page": page,
                    },
                )
                break
            for c in comments:
                if c.id > max_seen_github_review:
                    max_seen_github_review = c.id
                if c.author == github_bot:
                    continue
                if _has_marker(c.body):
                    continue
                if c.id <= cursor_github_review:
                    continue
                if db.has_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    src_platform="github_review",
                    src_comment_id=c.id,
                    dst_platform="codeberg_issue",
                ):
                    continue
                context = ""
                if c.path:
                    context = f"inline code comment on `{c.path}`\n\n"
                mirrored_body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="github_review",
                        src_author=c.author,
                        src_url=c.html_url,
                        src_id=c.id,
                        body=f"{context}{c.body}".strip(),
                    )
                )
                created_id: int
                dst_platform: str
                # If this is a reply in an existing GitHub inline thread, do NOT create
                # another inline comment on Codeberg; Gitea doesn't support true inline
                # replies via API, and it results in duplicated diff hunks. Mirror as a
                # normal PR comment instead.
                if c.in_reply_to_id:
                    created_issue = await codeberg.create_issue_comment(
                        repo=mirror.codeberg_repo, issue_number=codeberg_pr_number, body=mirrored_body
                    )
                    created_id = created_issue.id
                    dst_platform = "codeberg_issue"
                    mirrored_counts["gh_review_to_cb_issue"] += 1
                elif c.path and c.line and m.last_synced_commit:
                    created_review = await codeberg.create_pull_review_comment(
                        repo=mirror.codeberg_repo,
                        pull_number=codeberg_pr_number,
                        commit_id=m.last_synced_commit,
                        path=c.path,
                        line=int(c.line),
                        body=mirrored_body,
                    )
                    created_id = int(created_review.id) if created_review.id else int(c.id)
                    dst_platform = "codeberg_review"
                    mirrored_counts["gh_review_to_cb_inline"] += 1
                else:
                    created_issue = await codeberg.create_issue_comment(
                        repo=mirror.codeberg_repo, issue_number=codeberg_pr_number, body=mirrored_body
                    )
                    created_id = created_issue.id
                    dst_platform = "codeberg_issue"
                    mirrored_counts["gh_review_to_cb_issue"] += 1

                db.upsert_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    github_pr_number=github_pr_number,
                    src_platform="github_review",
                    src_comment_id=c.id,
                    dst_platform=dst_platform,
                    dst_comment_id=created_id,
                )
                if delay_s:
                    await asyncio.sleep(delay_s)
            if len(comments) < 100:
                break
            page += 1
        if max_seen_github_review > cursor_github_review:
            db.set_comment_cursor(
                codeberg_repo=mirror.codeberg_repo,
                codeberg_pr_number=codeberg_pr_number,
                github_repo=mirror.github_repo,
                github_pr_number=github_pr_number,
                platform="github_review",
                last_seen_id=max_seen_github_review,
            )

        if any(v for v in mirrored_counts.values()):
            log.info(
                "comments_mirror_synced",
                extra={
                    "mirror": mirror.name,
                    "github_repo": mirror.github_repo,
                    "github_pr": github_pr_number,
                    "codeberg_repo": mirror.codeberg_repo,
                    "codeberg_pr": codeberg_pr_number,
                    "delay_s": delay_s,
                    **mirrored_counts,
                },
            )


async def run_comments_mirror_worker(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.mirror_comments_interval
    if not interval:
        return
    seconds = parse_duration_seconds(interval)

    log.info(
        "comments_mirror_worker_started",
        extra={
            "mirror": mirror.name,
            "github_repo": mirror.github_repo,
            "codeberg_repo": mirror.codeberg_repo,
            "interval_s": seconds,
        },
    )

    while True:
        try:
            await mirror_comments_once(config=config, secrets=secrets, db=db, mirror=mirror)
        except Exception:
            log.exception(
                "comments_mirror_failed",
                extra={"mirror": mirror.name, "github_repo": mirror.github_repo, "codeberg_repo": mirror.codeberg_repo},
            )
        await asyncio.sleep(seconds)
