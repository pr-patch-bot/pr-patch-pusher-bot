from __future__ import annotations

import asyncio
import logging
import re

from .clients import CodebergClient, GitHubClient
from .comments import MirrorComment, format_mirrored_comment
from .config import AppConfig, LoadedSecrets, MirrorConfig
from .db import Database
from .utils import parse_duration_seconds


log = logging.getLogger("codeberg_bridge.comments_mirror")

_MARKER_NEEDLE = "<!-- cbb:mirror"
_GITHUB_REVIEW_DISCUSSION_RE = re.compile(r"(?:discussion_r|#r)(\d+)")


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
    for m in mappings:
        if not m.github_pr_number:
            continue
        github_pr_number = int(m.github_pr_number)
        codeberg_pr_number = int(m.codeberg_pr_number)

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
                created = await codeberg.create_issue_comment(
                    repo=mirror.codeberg_repo, issue_number=codeberg_pr_number, body=mirrored_body
                )
                db.upsert_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    github_pr_number=github_pr_number,
                    src_platform="github_review",
                    src_comment_id=c.id,
                    dst_platform="codeberg_issue",
                    dst_comment_id=created.id,
                )
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
