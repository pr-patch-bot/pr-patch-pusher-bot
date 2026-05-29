from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response

from .config import AppConfig, LoadedSecrets, load_config, load_secrets
from .db import Database
from .logging import setup_logging
from .backfill import run_backfill_worker
from .comments_mirror import run_comments_mirror_worker, _find_codeberg_review_thread_root
from .reconcile import run_reconcile_worker
from .mirror import _mirror_branch_name, mirror_pr
from .sync_upstream import run_sync_worker
from .utils import constant_time_equals, hmac_sha256_hex
from .clients import GitHubClient, CodebergClient
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


def _stable_int_id(text: str) -> int:
    # Produce a stable positive int for webhook payloads that lack a numeric comment id.
    # SQLite schema expects integers; crc32 is sufficient for de-duping best-effort events.
    return zlib.crc32(text.encode("utf-8")) & 0x7FFFFFFF


def _get_mirror_for_repo(config: AppConfig, codeberg_repo: str):
    for mirror in config.mirrors:
        if mirror.codeberg_repo == codeberg_repo:
            return mirror
    return None


def _ensure_parent_dir(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


async def _find_codeberg_review_thread_root_with_retry(
    *,
    codeberg: CodebergClient,
    repo: str,
    pull_number: int,
    comment_id: int,
):
    """
    Codeberg/Gitea can emit an in-thread review reply as a generic issue_comment
    webhook before the review-comments API is immediately consistent. Retry briefly
    before deciding that the comment is a normal PR timeline comment.
    """
    for delay_s in (0.0, 0.5, 1.5, 3.0):
        if delay_s:
            await asyncio.sleep(delay_s)
        thread_info = await _find_codeberg_review_thread_root(
            codeberg=codeberg,
            repo=repo,
            pull_number=pull_number,
            comment_id=comment_id,
        )
        if thread_info is not None:
            return thread_info
    return None



async def _find_codeberg_review_comment_by_id(
    *,
    codeberg: CodebergClient,
    repo: str,
    pull_number: int,
    comment_id: int,
) -> tuple[int, dict[str, Any]] | None:
    """Fetch Codeberg PR reviews/comments and return (review_id, comment_dict)."""
    page = 1
    while True:
        try:
            reviews = await codeberg.list_pull_reviews(
                repo=repo,
                pull_number=pull_number,
                page=page,
                limit=50,
            )
        except Exception:
            log.exception(
                "codeberg_find_review_comment_list_reviews_failed",
                extra={"repo": repo, "pr": pull_number, "comment_id": comment_id, "page": page},
            )
            return None
        if not reviews:
            return None

        for review in reviews:
            review_id = review.get("id")
            if not isinstance(review_id, int):
                continue
            try:
                rcomments = await codeberg.list_pull_review_comments(
                    repo=repo,
                    pull_number=pull_number,
                    review_id=review_id,
                )
            except Exception:
                log.exception(
                    "codeberg_find_review_comment_list_comments_failed",
                    extra={"repo": repo, "pr": pull_number, "review_id": review_id, "comment_id": comment_id},
                )
                continue
            for rc in rcomments or []:
                if rc.get("id") == comment_id:
                    return review_id, rc

        if len(reviews) < 50:
            return None
        page += 1


def _iso_sort_key(v: object) -> str:
    # Codeberg/Gitea uses ISO-8601 timestamps; lexical sort works for ISO strings.
    return str(v or "")


def _codeberg_thread_key(*, review_id: int, rc: dict[str, Any]) -> tuple:
    return (
        int(review_id),
        rc.get("path") or "",
        rc.get("position") or 0,
        rc.get("commit_id") or "",
        rc.get("diff_hunk") or "",
    )


def _infer_codeberg_thread_root_id(
    *, review_id: int, all_review_comments: list[dict[str, Any]], comment_id: int
) -> int | None:
    target: dict[str, Any] | None = None
    for rc in all_review_comments:
        if rc.get("id") == comment_id:
            target = rc
            break
    if not target:
        return None
    key = _codeberg_thread_key(review_id=review_id, rc=target)
    group = [rc for rc in all_review_comments if _codeberg_thread_key(review_id=review_id, rc=rc) == key]
    if not group:
        return None
    group.sort(key=lambda r: _iso_sort_key(r.get("created_at")))
    root_id = group[0].get("id")
    return int(root_id) if isinstance(root_id, int) else None


async def _ensure_github_review_root_for_codeberg_thread(
    *,
    github: GitHubClient,
    codeberg: CodebergClient,
    codeberg_repo: str,
    codeberg_pr_number: int,
    github_repo: str,
    github_pr_number: int,
    thread_info: Any,
    last_synced_commit: str | None,
) -> int | None:
    """Return/create the GitHub top-level review comment for a Codeberg thread root.

    This recovers from the common race where Codeberg sends the reply webhook after the
    root inline comment was missed or failed to mirror. Without this, replies are correctly
    classified but cannot attach to anything on GitHub.
    """
    github_root_id = _lookup_github_review_root_for_codeberg_root(
        codeberg_repo=codeberg_repo,
        codeberg_pr_number=codeberg_pr_number,
        github_repo=github_repo,
        codeberg_root_comment_id=thread_info.thread_root_id,
    )
    if github_root_id:
        return github_root_id

    found = await _find_codeberg_review_comment_by_id(
        codeberg=codeberg,
        repo=codeberg_repo,
        pull_number=codeberg_pr_number,
        comment_id=thread_info.thread_root_id,
    )
    if found is None:
        log.warning(
            "codeberg_review_root_lookup_failed",
            extra={"repo": codeberg_repo, "pr": codeberg_pr_number, "root_id": thread_info.thread_root_id},
        )
        return None

    _review_id, root_rc = found
    root_body = root_rc.get("body") or ""
    root_author = ((root_rc.get("user") or {}).get("login")) or ""
    root_url = root_rc.get("html_url") or ""
    root_path = thread_info.path or root_rc.get("path")
    root_position = thread_info.position or root_rc.get("position")
    root_line = thread_info.line or root_rc.get("line") or root_rc.get("original_line")
    root_commit = last_synced_commit or thread_info.commit_id or root_rc.get("commit_id")

    if not (isinstance(root_path, str) and root_path and isinstance(root_commit, str) and root_commit):
        log.warning(
            "codeberg_review_root_missing_inline_metadata",
            extra={
                "repo": codeberg_repo,
                "pr": codeberg_pr_number,
                "root_id": thread_info.thread_root_id,
                "path": root_path,
                "line": root_line,
                "position": root_position,
                "commit_id_present": bool(root_commit),
            },
        )
        return None

    body = format_mirrored_comment(
        c=MirrorComment(
            src_platform="codeberg_review",
            src_author=str(root_author),
            src_url=str(root_url),
            src_id=int(thread_info.thread_root_id),
            body=str(root_body),
        )
    )
    try:
        created_root = await github.create_review_comment(
            repo=github_repo,
            pull_number=github_pr_number,
            commit_id=root_commit,
            path=root_path,
            position=int(root_position) if isinstance(root_position, int) and root_position > 0 else None,
            line=int(root_line) if isinstance(root_line, int) and root_line > 0 else None,
            body=body,
        )
    except Exception:
        log.exception(
            "codeberg_review_root_create_for_reply_failed",
            extra={
                "codeberg_repo": codeberg_repo,
                "codeberg_pr": codeberg_pr_number,
                "github_repo": github_repo,
                "github_pr": github_pr_number,
                "root_id": thread_info.thread_root_id,
                "path": root_path,
                "line": root_line,
                "position": root_position,
            },
        )
        return None

    db.upsert_mirrored_comment(
        codeberg_repo=codeberg_repo,
        codeberg_pr_number=codeberg_pr_number,
        github_repo=github_repo,
        github_pr_number=github_pr_number,
        src_platform="codeberg_review",
        src_comment_id=int(thread_info.thread_root_id),
        dst_platform="github_review",
        dst_comment_id=created_root.id,
    )
    log.info(
        "codeberg_review_root_created_for_reply",
        extra={
            "codeberg_repo": codeberg_repo,
            "codeberg_pr": codeberg_pr_number,
            "github_repo": github_repo,
            "github_pr": github_pr_number,
            "root_id": thread_info.thread_root_id,
            "github_root_id": created_root.id,
        },
    )
    return int(created_root.id)

def _lookup_github_review_root_for_codeberg_root(
    *,
    codeberg_repo: str,
    codeberg_pr_number: int,
    github_repo: str,
    codeberg_root_comment_id: int,
) -> int | None:
    """Return the GitHub top-level review comment corresponding to a Codeberg root.

    Handles both mapping directions:
    - Codeberg-originated root: codeberg_review -> github_review
    - GitHub-originated root mirrored to Codeberg: github_review -> codeberg_review
    """
    try:
        mapped = db.get_mirrored_comment_dst(
            codeberg_repo=codeberg_repo,
            codeberg_pr_number=codeberg_pr_number,
            github_repo=github_repo,
            src_platform="codeberg_review",
            src_comment_id=codeberg_root_comment_id,
            dst_platform="github_review",
        )
    except TypeError:
        # Compatibility with older db.py while the patched db.py is being rolled out.
        mapped = db.get_mirrored_comment_dst(
            codeberg_repo=codeberg_repo,
            codeberg_pr_number=codeberg_pr_number,
            github_repo=github_repo,
            src_platform="codeberg_review",
            src_comment_id=codeberg_root_comment_id,
        )
    except Exception:
        mapped = None

    if mapped:
        try:
            dst_platform, dst_comment_id = mapped
            if dst_platform == "github_review":
                return int(dst_comment_id)
        except Exception:
            pass

    return db.get_github_review_id_for_codeberg_review_id(
        codeberg_repo=codeberg_repo,
        codeberg_pr_number=codeberg_pr_number,
        github_repo=github_repo,
        codeberg_review_comment_id=codeberg_root_comment_id,
    )


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
        review = payload.get("review") or {}
        sender = payload.get("sender") or {}

        pr_number = pr.get("number") or pr.get("index")
        comment_id = comment.get("id")
        comment_body = comment.get("body") or ""
        comment_url = comment.get("html_url") or ""
        comment_user = ((comment.get("user") or {}).get("login")) or ""
        pr_url = pr.get("html_url") or ""
        sender_login = (sender.get("login") or "") if isinstance(sender, dict) else ""

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
            # Some Codeberg/Gitea variants emit a "review submitted" payload here without an
            # explicit inline comment object. Best-effort mirror the review summary content.
            review_content = review.get("content")
            if (
                isinstance(review_content, str)
                and review_content.strip()
                and isinstance(sender_login, str)
                and sender_login
            ):
                synth_id = _stable_int_id(f"{repo}:{pr_number}:{sender_login}:{review_content}")
                mirror = _get_mirror_for_repo(config, repo)
                if not mirror:
                    return Response(status_code=202, content="no mirror configured")
                mapping = db.get_mapping(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=pr_number,
                    github_repo=mirror.github_repo,
                )
                if not mapping or not mapping.github_pr_number:
                    return Response(status_code=202, content="no github mapping")
                if db.has_mirrored_comment_any_dst(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=pr_number,
                    github_repo=mirror.github_repo,
                    src_platform="codeberg_review_summary",
                    src_comment_id=synth_id,
                ):
                    return Response(status_code=202, content="already mirrored")

                gh = GitHubClient(token=secrets.github_token)
                body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="codeberg_review_summary",
                        src_author=sender_login,
                        src_url=pr_url if isinstance(pr_url, str) else "",
                        src_id=synth_id,
                        body=review_content,
                    )
                )

                async def _run_review_summary_mirror() -> None:
                    github_pr_number = int(mapping.github_pr_number)  # type: ignore[arg-type]
                    try:
                        created_issue = await gh.create_issue_comment(
                            repo=mirror.github_repo,
                            issue_number=github_pr_number,
                            body=body,
                        )
                        db.upsert_mirrored_comment(
                            codeberg_repo=mirror.codeberg_repo,
                            codeberg_pr_number=pr_number,
                            github_repo=mirror.github_repo,
                            github_pr_number=github_pr_number,
                            src_platform="codeberg_review_summary",
                            src_comment_id=synth_id,
                            dst_platform="github_issue",
                            dst_comment_id=created_issue.id,
                        )
                        log.info(
                            "mirrored_codeberg_review_summary",
                            extra={"repo": repo, "pr": pr_number, "dst_id": created_issue.id},
                        )
                    except Exception:
                        log.exception(
                            "mirror_review_summary_failed",
                            extra={"repo": repo, "pr": pr_number, "synth_id": synth_id},
                        )

                background.add_task(_run_review_summary_mirror)
                return Response(status_code=202, content="accepted")

            # If we don't have a concrete comment object, accept the webhook and
            # scan Codeberg review comments for any unmirrored inline comments.
            if isinstance(repo, str) and repo and isinstance(pr_number, int) and sender_login:
                mirror = _get_mirror_for_repo(config, repo)
                if not mirror:
                    return Response(status_code=202, content="no mirror configured")
                allowed = set(mirror.allowed_codeberg_users or [])
                if allowed and sender_login not in allowed:
                    return Response(status_code=202, content="user not allowed")

                mapping = db.get_mapping(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=pr_number,
                    github_repo=mirror.github_repo,
                )
                if not mapping or not mapping.github_pr_number:
                    return Response(status_code=202, content="no github mapping")

                async def _scan_review_comments() -> None:
                    cb = CodebergClient(base_url=config.codeberg.base_url, token=secrets.codeberg_token)
                    gh = GitHubClient(token=secrets.github_token)
                    github_pr_number = int(mapping.github_pr_number)  # type: ignore[arg-type]
                    # Retry briefly for eventual consistency.
                    for delay_s in (0.0, 0.5, 1.5, 3.0):
                        if delay_s:
                            await asyncio.sleep(delay_s)
                        try:
                            reviews = await cb.list_pull_reviews(
                                repo=mirror.codeberg_repo, pull_number=pr_number, page=1, limit=50
                            )
                        except Exception:
                            log.exception(
                                "codeberg_review_submitted_scan_list_reviews_failed",
                                extra={"repo": repo, "pr": pr_number},
                            )
                            return
                        if not reviews:
                            continue
                        mirrored_any = False
                        for r in reviews:
                            rid = r.get("id")
                            if not isinstance(rid, int):
                                continue
                            try:
                                rcomments = await cb.list_pull_review_comments(
                                    repo=mirror.codeberg_repo, pull_number=pr_number, review_id=rid
                                )
                            except Exception:
                                log.exception(
                                    "codeberg_review_submitted_scan_list_comments_failed",
                                    extra={"repo": repo, "pr": pr_number, "review_id": rid},
                                )
                                continue
                            for rc in rcomments or []:
                                rcid = rc.get("id")
                                user = ((rc.get("user") or {}).get("login")) or ""
                                if not isinstance(rcid, int) or not user:
                                    continue
                                if user != sender_login:
                                    continue
                                if db.has_mirrored_comment_any_dst(
                                    codeberg_repo=mirror.codeberg_repo,
                                    codeberg_pr_number=pr_number,
                                    github_repo=mirror.github_repo,
                                    src_platform="codeberg_review",
                                    src_comment_id=rcid,
                                ):
                                    continue
                                root_id = _infer_codeberg_thread_root_id(
                                    review_id=rid, all_review_comments=rcomments, comment_id=rcid
                                )
                                if root_id is None:
                                    continue
                                is_root = (root_id == rcid)
                                review_commit = mapping.last_synced_commit or rc.get("commit_id")  # type: ignore[union-attr]
                                path = rc.get("path")
                                pos = rc.get("position")
                                ln = rc.get("line") or rc.get("original_line")
                                url = rc.get("html_url") or pr_url
                                body_text = rc.get("body") or ""
                                mirrored_body = format_mirrored_comment(
                                    c=MirrorComment(
                                        src_platform="codeberg_review",
                                        src_author=user,
                                        src_url=str(url),
                                        src_id=int(rcid),
                                        body=str(body_text),
                                    )
                                )
                                if not is_root:
                                    github_root_id = _lookup_github_review_root_for_codeberg_root(
                                        codeberg_repo=mirror.codeberg_repo,
                                        codeberg_pr_number=pr_number,
                                        github_repo=mirror.github_repo,
                                        codeberg_root_comment_id=int(root_id),
                                    )
                                    if not github_root_id:
                                        # Try to create the root first.
                                        thread_info = await _find_codeberg_review_thread_root_with_retry(
                                            codeberg=cb,
                                            repo=mirror.codeberg_repo,
                                            pull_number=pr_number,
                                            comment_id=int(root_id),
                                        )
                                        if thread_info is not None:
                                            github_root_id = await _ensure_github_review_root_for_codeberg_thread(
                                                github=gh,
                                                codeberg=cb,
                                                codeberg_repo=mirror.codeberg_repo,
                                                codeberg_pr_number=pr_number,
                                                github_repo=mirror.github_repo,
                                                github_pr_number=github_pr_number,
                                                thread_info=thread_info,
                                                last_synced_commit=mapping.last_synced_commit,
                                            )
                                    if not github_root_id:
                                        log.warning(
                                            "codeberg_review_submitted_reply_no_mapped_github_root",
                                            extra={"repo": repo, "pr": pr_number, "comment_id": rcid, "root_id": root_id},
                                        )
                                        continue
                                    try:
                                        created = await gh.create_review_comment_reply_via_replies_endpoint(
                                            repo=mirror.github_repo,
                                            pull_number=github_pr_number,
                                            comment_id=int(github_root_id),
                                            body=mirrored_body,
                                        )
                                    except Exception:
                                        created = await gh.create_review_comment_reply(
                                            repo=mirror.github_repo,
                                            pull_number=github_pr_number,
                                            in_reply_to=int(github_root_id),
                                            body=mirrored_body,
                                        )
                                    db.upsert_mirrored_comment(
                                        codeberg_repo=mirror.codeberg_repo,
                                        codeberg_pr_number=pr_number,
                                        github_repo=mirror.github_repo,
                                        github_pr_number=github_pr_number,
                                        src_platform="codeberg_review",
                                        src_comment_id=int(rcid),
                                        dst_platform="github_review",
                                        dst_comment_id=created.id,
                                    )
                                    mirrored_any = True
                                    continue

                                if isinstance(path, str) and path and isinstance(review_commit, str) and review_commit:
                                    try:
                                        created = await gh.create_review_comment(
                                            repo=mirror.github_repo,
                                            pull_number=github_pr_number,
                                            commit_id=str(review_commit),
                                            path=path,
                                            position=int(pos) if isinstance(pos, int) and pos > 0 else None,
                                            line=int(ln) if isinstance(ln, int) and ln > 0 else None,
                                            body=mirrored_body,
                                        )
                                    except httpx.HTTPStatusError as e:
                                        log.error(
                                            "github_review_comment_create_failed",
                                            extra={
                                                "status": getattr(e.response, "status_code", None),
                                                "body": (getattr(e.response, "text", "") or "")[:500],
                                                "github_repo": mirror.github_repo,
                                                "github_pr": github_pr_number,
                                                "codeberg_repo": mirror.codeberg_repo,
                                                "codeberg_pr": pr_number,
                                                "codeberg_comment_id": rcid,
                                                "commit_id": str(review_commit),
                                                "path": path,
                                                "line": ln,
                                                "position": pos,
                                            },
                                        )
                                        continue
                                    except Exception:
                                        log.exception(
                                            "github_review_comment_create_failed",
                                            extra={
                                                "github_repo": mirror.github_repo,
                                                "github_pr": github_pr_number,
                                                "codeberg_repo": mirror.codeberg_repo,
                                                "codeberg_pr": pr_number,
                                                "codeberg_comment_id": rcid,
                                                "commit_id": str(review_commit),
                                                "path": path,
                                                "line": ln,
                                                "position": pos,
                                            },
                                        )
                                        continue
                                    db.upsert_mirrored_comment(
                                        codeberg_repo=mirror.codeberg_repo,
                                        codeberg_pr_number=pr_number,
                                        github_repo=mirror.github_repo,
                                        github_pr_number=github_pr_number,
                                        src_platform="codeberg_review",
                                        src_comment_id=int(rcid),
                                        dst_platform="github_review",
                                        dst_comment_id=created.id,
                                    )
                                    mirrored_any = True
                        if mirrored_any:
                            log.info(
                                "codeberg_review_submitted_scan_mirrored",
                                extra={"repo": repo, "pr": pr_number, "sender": sender_login},
                            )
                            return

                background.add_task(_scan_review_comments)
                log.info(
                    "codeberg_review_submitted_scan_accepted",
                    extra={"repo": repo, "pr": pr_number, "sender": sender_login},
                )
                return Response(status_code=202, content="accepted")

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
            # Diagnostics: some Codeberg/Gitea variants send review submissions with
            # event_type=pull_request_review_comment but without an explicit `comment` object.
            try:
                log.info(
                    "webhook_review_comment_payload_shape",
                    extra={
                        "repo": repo,
                        "pr": pr_number,
                        "action": action,
                        "has_comment": bool(comment),
                        "comment_keys": sorted(list(comment.keys())) if isinstance(comment, dict) else [],
                        "has_review": bool(review),
                        "review_keys": sorted(list(review.keys())) if isinstance(review, dict) else [],
                        "pr_keys": sorted(list(pr.keys())) if isinstance(pr, dict) else [],
                        "top_keys": sorted(list(payload.keys())),
                    },
                )
            except Exception:
                pass
            return Response(status_code=202, content="accepted")
        if "<!-- cbb:mirror" in comment_body:
            return Response(status_code=202, content="ignored mirrored comment")

        mirror = _get_mirror_for_repo(config, repo)
        if not mirror:
            log.info(
                "webhook_review_comment_ignored",
                extra={"reason": "no_mirror", "repo": repo, "pr": pr_number},
            )
            return Response(status_code=202, content="no mirror configured")
        allowed = set(mirror.allowed_codeberg_users or [])
        if allowed and comment_user not in allowed:
            return Response(status_code=202, content="user not allowed")

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
        line = comment.get("line")
        side = comment.get("side")
        commit_id = comment.get("commit_id") or comment.get("commit_sha") or ""
        in_reply_to = comment.get("in_reply_to")

        gh = GitHubClient(token=secrets.github_token)
        cb = CodebergClient(base_url=config.codeberg.base_url, token=secrets.codeberg_token)
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
                # Do not trust Codeberg's webhook payload for reply linkage. Codeberg/Gitea
                # can send review-thread comments as flat events with no in_reply_to. Fetch
                # review comments and infer the thread root from review/path/position/commit/diff.
                thread_info = await _find_codeberg_review_thread_root_with_retry(
                    codeberg=cb,
                    repo=mirror.codeberg_repo,
                    pull_number=pr_number,
                    comment_id=comment_id,
                )

                if thread_info is not None and not thread_info.is_root:
                    github_root_id = await _ensure_github_review_root_for_codeberg_thread(
                        github=gh,
                        codeberg=cb,
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=pr_number,
                        github_repo=mirror.github_repo,
                        github_pr_number=github_pr_number,
                        thread_info=thread_info,
                        last_synced_commit=mapping.last_synced_commit,
                    )
                    if not github_root_id:
                        log.warning(
                            "codeberg_review_reply_no_mapped_github_root",
                            extra={
                                "repo": repo,
                                "pr": pr_number,
                                "comment_id": comment_id,
                                "root_id": thread_info.thread_root_id,
                            },
                        )
                        return
                    try:
                        created = await gh.create_review_comment_reply_via_replies_endpoint(
                            repo=mirror.github_repo,
                            pull_number=github_pr_number,
                            comment_id=github_root_id,
                            body=body,
                        )
                    except Exception:
                        created = await gh.create_review_comment_reply(
                            repo=mirror.github_repo,
                            pull_number=github_pr_number,
                            in_reply_to=github_root_id,
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
                    log.info(
                        "mirrored_codeberg_review_reply",
                        extra={"repo": repo, "pr": pr_number, "dst_id": created.id, "root_id": github_root_id},
                    )
                    return

                # Root inline review comment. Prefer API-derived metadata; fall back to webhook fields.
                review_path = thread_info.path if thread_info is not None else path
                review_position = thread_info.position if thread_info is not None else position
                review_line = thread_info.line if thread_info is not None else line
                review_commit = mapping.last_synced_commit or (thread_info.commit_id if thread_info is not None else commit_id)

                if isinstance(review_path, str) and review_path and isinstance(review_commit, str) and review_commit:
                    pos = int(review_position) if isinstance(review_position, int) and review_position > 0 else None
                    ln = int(review_line) if isinstance(review_line, int) and review_line > 0 else None
                    sd = side if isinstance(side, str) else None
                    created = await gh.create_review_comment(
                        repo=mirror.github_repo,
                        pull_number=github_pr_number,
                        commit_id=review_commit,
                        path=review_path,
                        position=pos,
                        line=ln,
                        side=sd,
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
                    log.info(
                        "mirrored_codeberg_review_root",
                        extra={"repo": repo, "pr": pr_number, "dst_id": created.id, "path": review_path},
                    )
                    return

                # If this was delivered as a review-comment webhook, do not silently turn it
                # into a normal PR comment. That destroys thread state and causes later replies
                # to have no root mapping.
                log.warning(
                    "codeberg_review_comment_missing_inline_metadata",
                    extra={
                        "repo": repo,
                        "pr": pr_number,
                        "comment_id": comment_id,
                        "thread_info_found": thread_info is not None,
                        "path": review_path,
                        "line": review_line,
                        "position": review_position,
                        "commit_id_present": bool(review_commit),
                    },
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
    comment_in_reply_to = comment.get("in_reply_to") or comment.get("reply") or comment.get("reply_id")

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
    allowed = set(mirror.allowed_codeberg_users or [])
    if allowed and comment_user not in allowed:
        return Response(status_code=202, content="user not allowed")

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
    cb = CodebergClient(base_url=config.codeberg.base_url, token=secrets.codeberg_token)
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
            # Codeberg/Gitea can deliver an inline review comment/reply as a generic
            # issue_comment webhook with no in_reply_to. Classify it by fetching reviews
            # and review comments before allowing it to become a normal GitHub issue comment.
            thread_info = await _find_codeberg_review_thread_root_with_retry(
                codeberg=cb,
                repo=mirror.codeberg_repo,
                pull_number=issue_number,
                comment_id=comment_id,
            )

            if thread_info is not None:
                review_body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="codeberg_review",
                        src_author=comment_user,
                        src_url=comment_url,
                        src_id=comment_id,
                        body=str(comment_body),
                    )
                )

                if db.has_mirrored_comment_any_dst(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=issue_number,
                    github_repo=mirror.github_repo,
                    src_platform="codeberg_review",
                    src_comment_id=comment_id,
                ):
                    log.info(
                        "codeberg_issue_reclassified_review_already_mirrored",
                        extra={"repo": repo, "pr": issue_number, "comment_id": comment_id},
                    )
                    return

                if not thread_info.is_root:
                    github_root_id = await _ensure_github_review_root_for_codeberg_thread(
                        github=gh,
                        codeberg=cb,
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=issue_number,
                        github_repo=mirror.github_repo,
                        github_pr_number=github_pr_number,
                        thread_info=thread_info,
                        last_synced_commit=mapping.last_synced_commit,
                    )
                    if not github_root_id:
                        log.warning(
                            "codeberg_issue_reclassified_review_reply_no_mapped_github_root",
                            extra={
                                "repo": repo,
                                "pr": issue_number,
                                "comment_id": comment_id,
                                "root_id": thread_info.thread_root_id,
                            },
                        )
                        return
                    try:
                        created = await gh.create_review_comment_reply_via_replies_endpoint(
                            repo=mirror.github_repo,
                            pull_number=github_pr_number,
                            comment_id=github_root_id,
                            body=review_body,
                        )
                    except Exception:
                        created = await gh.create_review_comment_reply(
                            repo=mirror.github_repo,
                            pull_number=github_pr_number,
                            in_reply_to=github_root_id,
                            body=review_body,
                        )
                    db.upsert_mirrored_comment(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=issue_number,
                        github_repo=mirror.github_repo,
                        github_pr_number=github_pr_number,
                        src_platform="codeberg_review",
                        src_comment_id=comment_id,
                        dst_platform="github_review",
                        dst_comment_id=created.id,
                    )
                    log.info(
                        "mirrored_codeberg_comment",
                        extra={
                            "repo": repo,
                            "pr": issue_number,
                            "dst": "github_review",
                            "dst_id": created.id,
                            "root_id": github_root_id,
                            "reclassified": True,
                        },
                    )
                    return

                review_commit = mapping.last_synced_commit or thread_info.commit_id
                if thread_info.path and review_commit:
                    try:
                        created = await gh.create_review_comment(
                            repo=mirror.github_repo,
                            pull_number=github_pr_number,
                            commit_id=review_commit,
                            path=thread_info.path,
                            position=thread_info.position,
                            line=thread_info.line,
                            body=review_body,
                        )
                    except httpx.HTTPStatusError as e:
                        log.error(
                            "github_review_comment_create_failed",
                            extra={
                                "status": getattr(e.response, "status_code", None),
                                "body": (getattr(e.response, "text", "") or "")[:500],
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": issue_number,
                                "codeberg_comment_id": comment_id,
                                "commit_id": review_commit,
                                "path": thread_info.path,
                                "line": thread_info.line,
                                "position": thread_info.position,
                                "reclassified": True,
                            },
                        )
                        return
                    except Exception:
                        log.exception(
                            "github_review_comment_create_failed",
                            extra={
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": issue_number,
                                "codeberg_comment_id": comment_id,
                                "commit_id": review_commit,
                                "path": thread_info.path,
                                "line": thread_info.line,
                                "position": thread_info.position,
                                "reclassified": True,
                            },
                        )
                        return
                    db.upsert_mirrored_comment(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=issue_number,
                        github_repo=mirror.github_repo,
                        github_pr_number=github_pr_number,
                        src_platform="codeberg_review",
                        src_comment_id=comment_id,
                        dst_platform="github_review",
                        dst_comment_id=created.id,
                    )
                    log.info(
                        "mirrored_codeberg_comment",
                        extra={
                            "repo": repo,
                            "pr": issue_number,
                            "dst": "github_review",
                            "dst_id": created.id,
                            "reclassified": True,
                            "path": thread_info.path,
                        },
                    )
                    return

                log.warning(
                    "codeberg_issue_reclassified_review_root_missing_inline_metadata",
                    extra={
                        "repo": repo,
                        "pr": issue_number,
                        "comment_id": comment_id,
                        "path": thread_info.path,
                        "line": thread_info.line,
                        "position": thread_info.position,
                        "commit_id_present": bool(review_commit),
                    },
                )
                return

            # If Codeberg provides an explicit reply-to id or the body contains a legacy
            # review marker, try to map it back to a GitHub review thread before treating
            # it as a plain timeline comment.
            github_thread_id: int | None = None
            if isinstance(comment_in_reply_to, int) and comment_in_reply_to > 0:
                github_thread_id = db.get_github_review_id_for_codeberg_review_id(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=issue_number,
                    github_repo=mirror.github_repo,
                    codeberg_review_comment_id=int(comment_in_reply_to),
                )
            if not github_thread_id:
                github_thread_id = _extract_github_review_comment_id(str(comment_body))

            if github_thread_id:
                try:
                    created = await gh.create_review_comment_reply_via_replies_endpoint(
                        repo=mirror.github_repo,
                        pull_number=github_pr_number,
                        comment_id=github_thread_id,
                        body=body,
                    )
                except Exception:
                    created = await gh.create_review_comment_reply(
                        repo=mirror.github_repo,
                        pull_number=github_pr_number,
                        in_reply_to=github_thread_id,
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

            log.info(
                "codeberg_comment_thread_not_found",
                extra={
                    "repo": repo,
                    "pr": issue_number,
                    "comment_id": comment_id,
                    "comment_in_reply_to": comment_in_reply_to,
                },
            )

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
