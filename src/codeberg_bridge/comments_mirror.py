from __future__ import annotations

import asyncio
import logging

from .clients import CodebergClient, GitHubClient
from .comments import MirrorComment, format_mirrored_comment
from .config import AppConfig, LoadedSecrets, MirrorConfig
from .db import Database
from .utils import parse_duration_seconds


log = logging.getLogger("codeberg_bridge.comments_mirror")

_MARKER_NEEDLE = "<!-- cbb:mirror"


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
    codeberg_bot = await codeberg.get_authenticated_user_login()

    mappings = db.list_open_mappings(codeberg_repo=mirror.codeberg_repo, github_repo=mirror.github_repo)
    for m in mappings:
        if not m.github_pr_number:
            continue
        github_pr_number = int(m.github_pr_number)
        codeberg_pr_number = int(m.codeberg_pr_number)

        # Phase 1: Codeberg issue comments -> GitHub issue comments
        page = 1
        while True:
            comments = await codeberg.list_issue_comments(
                repo=mirror.codeberg_repo, issue_number=codeberg_pr_number, page=page
            )
            if not comments:
                break
            for c in comments:
                if c.author == codeberg_bot:
                    continue
                if _has_marker(c.body):
                    continue
                if db.has_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    src_platform="codeberg_issue",
                    src_comment_id=c.id,
                    dst_platform="github_issue",
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
                created = await github.create_issue_comment(
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
                    dst_comment_id=created.id,
                )
            page += 1

        # Phase 1: GitHub issue comments -> Codeberg issue comments
        page = 1
        while True:
            comments = await github.list_issue_comments(
                repo=mirror.github_repo, issue_number=github_pr_number, page=page
            )
            if not comments:
                break
            for c in comments:
                if c.author == github_bot:
                    continue
                if _has_marker(c.body):
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
            page += 1

        # Phase 2: GitHub inline review comments -> Codeberg as normal issue comments (one-way)
        page = 1
        while True:
            comments = await github.list_review_comments(
                repo=mirror.github_repo, pull_number=github_pr_number, page=page
            )
            if not comments:
                break
            for c in comments:
                if c.author == github_bot:
                    continue
                if _has_marker(c.body):
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
                context = _inline_context_block(path=c.path, line=c.line, position=c.position)
                mirrored_body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="github_review",
                        src_author=c.author,
                        src_url=c.html_url,
                        src_id=c.id,
                        body="\n".join([context, c.body]).strip(),
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
            page += 1


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

