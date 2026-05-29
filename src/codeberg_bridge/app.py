from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response

from .config import AppConfig, LoadedSecrets, load_config, load_secrets
from .db import Database
from .logging import setup_logging
from .backfill import run_backfill_worker
from .comments_mirror import run_comments_mirror_worker
from .reconcile import run_reconcile_worker
from .mirror import _mirror_branch_name, mirror_pr
from .sync_upstream import run_sync_worker
from .utils import constant_time_equals, hmac_sha256_hex
from .clients import GitHubClient
from .comments import MirrorComment, format_mirrored_comment


log = logging.getLogger("codeberg_bridge.app")
_GITHUB_REVIEW_DISCUSSION_RE = re.compile(r"(?:discussion_r|#r)(\d+)")


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


def _get_mirror_for_repo(config: AppConfig, codeberg_repo: str):
    for mirror in config.mirrors:
        if mirror.codeberg_repo == codeberg_repo:
            return mirror
    return None


def _ensure_parent_dir(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

config_path = os.environ.get("CONFIG_PATH", "./config.yml")
config = load_config(config_path)
secrets = load_secrets(config)

_ensure_parent_dir(config.storage.sqlite_path)
db = Database(config.storage.sqlite_path)
db.init()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: list[asyncio.Task[None]] = []
    # Startup sanity checks (best-effort; logs warnings but doesn't crash).
    try:
        gh = GitHubClient(token=secrets.github_token)
        login = await gh.get_authenticated_user_login()
        log.info("github_auth_ok", extra={"login": login})
        for mirror in config.mirrors:
            if not await gh.repo_exists(repo=mirror.github_repo):
                log.warning("github_upstream_repo_not_found", extra={"repo": mirror.github_repo})
    except Exception:
        log.exception("startup_sanity_checks_failed")

    for mirror in config.mirrors:
        if mirror.sync_upstream_to_codeberg_interval:
            tasks.append(
                asyncio.create_task(run_sync_worker(config=config, secrets=secrets, mirror=mirror))
            )
        if mirror.reconcile_github_to_codeberg_interval:
            tasks.append(
                asyncio.create_task(
                    run_reconcile_worker(config=config, secrets=secrets, db=db, mirror=mirror)
                )
            )
        if mirror.backfill_codeberg_open_prs_interval:
            tasks.append(
                asyncio.create_task(
                    run_backfill_worker(config=config, secrets=secrets, db=db, mirror=mirror)
                )
            )
        if mirror.mirror_comments_interval:
            tasks.append(
                asyncio.create_task(
                    run_comments_mirror_worker(config=config, secrets=secrets, db=db, mirror=mirror)
                )
            )
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)
_debug_errors = os.environ.get("DEBUG_ERRORS", "").lower() in {"1", "true", "yes"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/codeberg")
async def webhook_codeberg(request: Request, background: BackgroundTasks) -> Response:
    body = await request.body()

    if secrets.codeberg_webhook_secret:
        sig = request.headers.get("X-Gitea-Signature", "")
        expected = hmac_sha256_hex(secrets.codeberg_webhook_secret, body)
        if not sig or not constant_time_equals(sig, expected):
            return Response(status_code=401, content="invalid signature")

    event = request.headers.get("X-Gitea-Event") or request.headers.get("X-Codeberg-Event") or ""
    event_type = (
        request.headers.get("X-Gitea-Event-Type")
        or request.headers.get("X-Codeberg-Event-Type")
        or ""
    )
    log.info("webhook_incoming", extra={"event": event, "event_type": event_type})
    # Gitea can report a normalized `X-Gitea-Event` plus a more specific `X-Gitea-Event-Type`,
    # but some setups may use the specific name directly as `X-Gitea-Event`.
    if event not in {"pull_request", "issue_comment", "pull_request_review_comment"} and event_type != "pull_request_review_comment":
        log.info("webhook_ignored", extra={"event": event, "event_type": event_type})
        return Response(status_code=202, content="ignored event")

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        return Response(status_code=400, content="invalid json")

    if event == "pull_request":
        action = payload.get("action")
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        base = (pr.get("base") or {}).get("repo") or {}
        base_full_name = base.get("full_name")

        if action not in {"opened", "synchronized", "edited", "reopened", "closed"}:
            return Response(status_code=202, content="ignored action")
        if not isinstance(number, int) or not isinstance(base_full_name, str):
            return Response(status_code=400, content="missing pr data")

        mirror = _get_mirror_for_repo(config, base_full_name)
        if not mirror:
            return Response(status_code=202, content="no mirror configured")

        log.info("webhook_received", extra={"repo": base_full_name, "pr": number, "action": action})

        if action == "closed":
            existing = db.get_mapping(
                codeberg_repo=mirror.codeberg_repo,
                codeberg_pr_number=number,
                github_repo=mirror.github_repo,
            )
            if existing:
                db.update_status(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=number,
                    github_repo=mirror.github_repo,
                    status="closed",
                )
            return Response(status_code=202, content="recorded close")

        async def _run_sync() -> None:
            try:
                await mirror_pr(
                    config=config,
                    secrets=secrets,
                    db=db,
                    mirror=mirror,
                    codeberg_pr_number=number,
                )
            except Exception:
                log.exception(
                    "mirror_failed", extra={"repo": base_full_name, "pr": number, "action": action}
                )

        background.add_task(_run_sync)
        return Response(status_code=202, content="accepted")

    # pull_request_review_comment (Codeberg/Gitea inline PR review comment)
    if event_type == "pull_request_review_comment" or event == "pull_request_review_comment":
        action = payload.get("action")
        repo = (payload.get("repository") or {}).get("full_name")
        pr = payload.get("pull_request") or {}
        comment = payload.get("comment") or {}

        pr_number = pr.get("number") or pr.get("index")
        comment_id = comment.get("id")
        comment_body = comment.get("body") or ""
        comment_url = comment.get("html_url") or ""
        comment_user = ((comment.get("user") or {}).get("login")) or ""

        # Some Codeberg/Gitea setups emit PR inline comment webhooks with action="created",
        # others use "reviewed" for review-related comment deliveries. Treat both as mirrorable.
        if action not in {"created", "reviewed"}:
            log.info(
                "webhook_review_comment_ignored",
                extra={"reason": "action", "action": action, "repo": repo},
            )
            return Response(status_code=202, content="ignored action")
        if not isinstance(repo, str) or not repo:
            log.info("webhook_review_comment_ignored", extra={"reason": "repo"})
            return Response(status_code=400, content="missing repo/pr data")
        if not isinstance(pr_number, int):
            log.info(
                "webhook_review_comment_ignored",
                extra={"reason": "pr_number", "pr_number": pr_number, "repo": repo},
            )
            return Response(status_code=400, content="missing repo/pr data")
        if not isinstance(comment_id, int) or not isinstance(comment_user, str) or not comment_user:
            log.info(
                "webhook_review_comment_ignored",
                extra={
                    "reason": "comment_data",
                    "repo": repo,
                    "pr": pr_number,
                    "comment_id": comment_id,
                    "comment_user": comment_user,
                },
            )
            return Response(status_code=400, content="missing comment data")
        if "<!-- cbb:mirror" in comment_body:
            return Response(status_code=202, content="ignored mirrored comment")

        mirror = _get_mirror_for_repo(config, repo)
        if not mirror:
            log.info(
                "webhook_review_comment_ignored",
                extra={"reason": "no_mirror", "repo": repo, "pr": pr_number},
            )
            return Response(status_code=202, content="no mirror configured")

        mapping = db.get_mapping(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=pr_number,
            github_repo=mirror.github_repo,
        )
        if not mapping or not mapping.github_pr_number:
            log.info(
                "webhook_review_comment_ignored",
                extra={"reason": "no_mapping", "repo": repo, "pr": pr_number},
            )
            return Response(status_code=202, content="no github mapping")

        if db.has_mirrored_comment_any_dst(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=pr_number,
            github_repo=mirror.github_repo,
            src_platform="codeberg_review",
            src_comment_id=comment_id,
        ):
            log.info(
                "webhook_review_comment_ignored",
                extra={"reason": "already_mirrored", "repo": repo, "pr": pr_number, "comment_id": comment_id},
            )
            return Response(status_code=202, content="already mirrored")

        # Best-effort anchoring: if Codeberg provides path/position/commit_id, mirror as inline.
        path = comment.get("path")
        position = comment.get("position")
        commit_id = comment.get("commit_id") or comment.get("commit_sha") or ""
        in_reply_to = comment.get("in_reply_to")

        gh = GitHubClient(token=secrets.github_token)
        body = format_mirrored_comment(
            c=MirrorComment(
                src_platform="codeberg_review",
                src_author=comment_user,
                src_url=comment_url,
                src_id=comment_id,
                body=str(comment_body),
            )
        )

        async def _run_inline_mirror() -> None:
            github_pr_number = int(mapping.github_pr_number)  # type: ignore[arg-type]
            try:
                if isinstance(in_reply_to, int) and in_reply_to > 0:
                    created = await gh.create_review_comment_reply(
                        repo=mirror.github_repo,
                        pull_number=github_pr_number,
                        in_reply_to=in_reply_to,
                        body=body,
                    )
                    db.upsert_mirrored_comment(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=pr_number,
                        github_repo=mirror.github_repo,
                        github_pr_number=github_pr_number,
                        src_platform="codeberg_review",
                        src_comment_id=comment_id,
                        dst_platform="github_review",
                        dst_comment_id=created.id,
                    )
                    return

                if (
                    isinstance(path, str)
                    and path
                    and isinstance(position, int)
                    and position > 0
                    and isinstance(commit_id, str)
                    and commit_id
                ):
                    created = await gh.create_review_comment(
                        repo=mirror.github_repo,
                        pull_number=github_pr_number,
                        commit_id=commit_id,
                        path=path,
                        position=position,
                        body=body,
                    )
                    db.upsert_mirrored_comment(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=pr_number,
                        github_repo=mirror.github_repo,
                        github_pr_number=github_pr_number,
                        src_platform="codeberg_review",
                        src_comment_id=comment_id,
                        dst_platform="github_review",
                        dst_comment_id=created.id,
                    )
                    return

                # Fallback: mirror as a normal PR conversation comment with context.
                created_issue = await gh.create_issue_comment(
                    repo=mirror.github_repo, issue_number=github_pr_number, body=body
                )
                db.upsert_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=pr_number,
                    github_repo=mirror.github_repo,
                    github_pr_number=github_pr_number,
                    src_platform="codeberg_review",
                    src_comment_id=comment_id,
                    dst_platform="github_issue",
                    dst_comment_id=created_issue.id,
                )
            except Exception:
                log.exception(
                    "mirror_inline_comment_failed",
                    extra={"repo": repo, "pr": pr_number, "comment_id": comment_id},
                )

        background.add_task(_run_inline_mirror)
        log.info(
            "webhook_review_comment_accepted",
            extra={"repo": repo, "pr": pr_number, "comment_id": comment_id},
        )
        return Response(status_code=202, content="accepted")

    # event == "issue_comment"
    action = payload.get("action")
    repo = (payload.get("repository") or {}).get("full_name")
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}

    issue_number = issue.get("number")
    is_pr = bool(issue.get("pull_request"))
    comment_id = comment.get("id")
    comment_body = comment.get("body") or ""
    comment_url = comment.get("html_url") or ""
    comment_user = ((comment.get("user") or {}).get("login")) or ""

    if action != "created":
        return Response(status_code=202, content="ignored action")
    if not is_pr:
        return Response(status_code=202, content="ignored non-pr comment")
    if not isinstance(repo, str) or not isinstance(issue_number, int):
        return Response(status_code=400, content="missing repo/issue data")
    if not isinstance(comment_id, int) or not isinstance(comment_user, str):
        return Response(status_code=400, content="missing comment data")
    if "<!-- cbb:mirror" in comment_body:
        return Response(status_code=202, content="ignored mirrored comment")

    mirror = _get_mirror_for_repo(config, repo)
    if not mirror:
        return Response(status_code=202, content="no mirror configured")

    mapping = db.get_mapping(
        codeberg_repo=mirror.codeberg_repo,
        codeberg_pr_number=issue_number,
        github_repo=mirror.github_repo,
    )
    if not mapping or not mapping.github_pr_number:
        return Response(status_code=202, content="no github mapping")

    if db.has_mirrored_comment_any_dst(
        codeberg_repo=mirror.codeberg_repo,
        codeberg_pr_number=issue_number,
        github_repo=mirror.github_repo,
        src_platform="codeberg_issue",
        src_comment_id=comment_id,
    ):
        return Response(status_code=202, content="already mirrored")

    gh = GitHubClient(token=secrets.github_token)
    body = format_mirrored_comment(
        c=MirrorComment(
            src_platform="codeberg_issue",
            src_author=comment_user,
            src_url=comment_url,
            src_id=comment_id,
            body=str(comment_body),
        )
    )

    async def _run_comment_mirror() -> None:
        github_pr_number = int(mapping.github_pr_number)  # type: ignore[arg-type]
        try:
            in_reply_to = _extract_github_review_comment_id(str(comment_body))
            if in_reply_to:
                created = await gh.create_review_comment_reply(
                    repo=mirror.github_repo,
                    pull_number=github_pr_number,
                    in_reply_to=in_reply_to,
                    body=body,
                )
                db.upsert_mirrored_comment(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=issue_number,
                    github_repo=mirror.github_repo,
                    github_pr_number=github_pr_number,
                    src_platform="codeberg_issue",
                    src_comment_id=comment_id,
                    dst_platform="github_review",
                    dst_comment_id=created.id,
                )
                log.info(
                    "mirrored_codeberg_comment",
                    extra={"repo": repo, "pr": issue_number, "dst": "github_review", "dst_id": created.id},
                )
                return

            created_issue = await gh.create_issue_comment(
                repo=mirror.github_repo, issue_number=github_pr_number, body=body
            )
            db.upsert_mirrored_comment(
                codeberg_repo=mirror.codeberg_repo,
                codeberg_pr_number=issue_number,
                github_repo=mirror.github_repo,
                github_pr_number=github_pr_number,
                src_platform="codeberg_issue",
                src_comment_id=comment_id,
                dst_platform="github_issue",
                dst_comment_id=created_issue.id,
            )
            log.info(
                "mirrored_codeberg_comment",
                extra={"repo": repo, "pr": issue_number, "dst": "github_issue", "dst_id": created_issue.id},
            )
        except Exception:
            log.exception(
                "mirror_comment_failed",
                extra={"repo": repo, "pr": issue_number, "comment_id": comment_id},
            )

    background.add_task(_run_comment_mirror)
    return Response(status_code=202, content="accepted")
