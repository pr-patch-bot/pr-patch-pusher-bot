from __future__ import annotations

import asyncio
import logging
import re
import os

import httpx

from .clients import CodebergClient, GitHubClient
from .comments import MirrorComment, format_mirrored_comment
from .config import AppConfig, LoadedSecrets, MirrorConfig
from .db import Database
from .diff_positions import extract_unified_diff_file_patch, unified_diff_position_to_anchor
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


def _review_thread_key(rc: dict) -> tuple:
    """Stable grouping key for Codeberg review comments that belong to the same diff thread."""
    return (
        str(rc.get("review") or rc.get("pull_request_review_id") or ""),
        rc.get("path") or "",
        rc.get("position") or 0,
        rc.get("commit_id") or "",
        rc.get("diff_hunk") or "",
    )


class _ReviewThreadInfo:
    """All metadata needed to mirror a Codeberg review comment or reply to GitHub."""

    __slots__ = (
        "thread_root_id",
        "is_root",
        "review_id",
        "path",
        "position",
        "line",
        "commit_id",
        "diff_hunk",
    )

    def __init__(
        self,
        *,
        thread_root_id: int,
        is_root: bool,
        review_id: int,
        path: str | None,
        position: int | None,
        line: int | None,
        commit_id: str | None,
        diff_hunk: str | None,
    ) -> None:
        self.thread_root_id = thread_root_id
        self.is_root = is_root
        self.review_id = review_id
        self.path = path
        self.position = position
        self.line = line
        self.commit_id = commit_id
        self.diff_hunk = diff_hunk


async def _find_codeberg_review_thread_root(
    *,
    codeberg: "CodebergClient",
    repo: str,
    pull_number: int,
    comment_id: int,
) -> _ReviewThreadInfo | None:
    """
    Determine whether *comment_id* is a Codeberg review-thread comment (root or reply).

    Returns a _ReviewThreadInfo if it is a review comment, or None if it is a normal
    PR timeline comment.

    Strategy (per context.md):
      1. Fetch all reviews for the PR.
      2. For each review fetch its comments.
      3. If comment_id is found, group by (review + path + position + commit_id + diff_hunk).
      4. Sort that group by created_at; the earliest is the thread root.
    """
    try:
        reviews = []
        page = 1
        while True:
            batch = await codeberg.list_pull_reviews(repo=repo, pull_number=pull_number, page=page, limit=50)
            if not batch:
                break
            reviews.extend(batch)
            if len(batch) < 50:
                break
            page += 1
    except Exception:
        log.exception(
            "codeberg_list_reviews_failed",
            extra={"repo": repo, "pull_number": pull_number},
        )
        return None

    for review in reviews:
        review_id = review.get("id")
        if not isinstance(review_id, int):
            continue
        try:
            rcomments = await codeberg.list_pull_review_comments(
                repo=repo, pull_number=pull_number, review_id=review_id
            )
        except Exception:
            continue
        if not rcomments:
            continue

        # Is our comment_id in this review?
        target_rc = next((rc for rc in rcomments if rc.get("id") == comment_id), None)
        if target_rc is None:
            continue

        # Group siblings by diff-thread key.
        key = _review_thread_key(target_rc)
        siblings = [rc for rc in rcomments if _review_thread_key(rc) == key]
        siblings.sort(key=lambda rc: rc.get("created_at") or "")
        thread_root = siblings[0]
        thread_root_id = int(thread_root["id"])
        return _ReviewThreadInfo(
            thread_root_id=thread_root_id,
            is_root=(thread_root_id == comment_id),
            review_id=review_id,
            path=thread_root.get("path") or None,
            position=thread_root.get("position") or None,
            line=thread_root.get("line") or thread_root.get("original_line") or None,
            commit_id=thread_root.get("commit_id") or None,
            diff_hunk=thread_root.get("diff_hunk") or None,
        )

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

        # Phase 0: Codeberg review comments -> GitHub review thread comments (root + replies)
        # Poll all reviews and their comments directly. This is the authoritative source
        # for review-thread activity on Codeberg (context.md: "fetch PR reviews, fetch each
        # review's comments, if comment.id found in review comments: ..."). Issue comments
        # (Phase 1) may or may not surface these depending on Codeberg version; we handle
        # them here first so Phase 1 can safely skip any IDs already processed.
        seen_codeberg_review_comment_ids_phase0: set[int] = set()
        cursor_codeberg_review = db.get_comment_cursor(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=codeberg_pr_number,
            github_repo=mirror.github_repo,
            platform="codeberg_review",
        )
        max_seen_codeberg_review = cursor_codeberg_review
        cb_reviews = []
        try:
            review_page = 1
            while True:
                review_batch = await codeberg.list_pull_reviews(
                    repo=mirror.codeberg_repo, pull_number=codeberg_pr_number, page=review_page, limit=50
                )
                if not review_batch:
                    break
                cb_reviews.extend(review_batch)
                if len(review_batch) < 50:
                    break
                review_page += 1
        except Exception:
            cb_reviews = []
            log.exception(
                "codeberg_list_reviews_failed_phase0",
                extra={"mirror": mirror.name, "codeberg_repo": mirror.codeberg_repo, "codeberg_pr": codeberg_pr_number},
            )

        # Collect all review comments across all reviews, grouped by thread key.
        # Structure: thread_key -> sorted list of (created_at, review_id, rc_dict)
        cb_thread_map: dict[tuple, list[tuple[str, int, dict]]] = {}
        for cb_review in (cb_reviews or []):
            rid = cb_review.get("id")
            if not isinstance(rid, int):
                continue
            try:
                rcomments = await codeberg.list_pull_review_comments(
                    repo=mirror.codeberg_repo, pull_number=codeberg_pr_number, review_id=rid
                )
            except Exception:
                continue
            for rc in (rcomments or []):
                rc_id = rc.get("id")
                if not isinstance(rc_id, int):
                    continue
                seen_codeberg_review_comment_ids_phase0.add(rc_id)
                if rc_id > max_seen_codeberg_review:
                    max_seen_codeberg_review = rc_id
                key = _review_thread_key(rc)
                cb_thread_map.setdefault(key, []).append((rc.get("created_at") or "", rid, rc))

        # Sort each thread group by created_at so index 0 is always the root.
        for key in cb_thread_map:
            cb_thread_map[key].sort(key=lambda t: t[0])

        # Build a map from rc_id -> thread_root_id for quick lookup.
        cb_rc_to_root: dict[int, int] = {}
        for entries in cb_thread_map.values():
            root_id = int(entries[0][2]["id"])
            for _, _, rc in entries:
                cb_rc_to_root[int(rc["id"])] = root_id

        # Now mirror each new review comment to GitHub.
        cb_diff_text: str | None = None
        for entries in cb_thread_map.values():
            root_rc = entries[0][2]
            root_id = int(root_rc["id"])
            root_review_id = entries[0][1]

            for _created_at, _review_id, rc in entries:
                rc_id = int(rc["id"])
                # Do not skip by cursor here. Review-thread creates can fail transiently;
                # the DB mapping is the source of truth for whether this item was mirrored.
                rc_author = (rc.get("user") or {}).get("login") or ""
                if codeberg_bot and rc_author == codeberg_bot:
                    continue
                if allowed_codeberg_users and rc_author not in allowed_codeberg_users:
                    continue
                if _has_marker(rc.get("body") or ""):
                    continue
                if db.has_mirrored_comment_any_dst(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    src_platform="codeberg_review",
                    src_comment_id=rc_id,
                ):
                    continue

                rc_body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="codeberg_review",
                        src_author=rc_author,
                        src_url=rc.get("html_url") or "",
                        src_id=rc_id,
                        body=rc.get("body") or "",
                    )
                )

                is_root = (rc_id == root_id)
                if is_root:
                    # Mirror as a new GitHub inline review comment.
                    path = root_rc.get("path")
                    position = root_rc.get("position")
                    if (
                        isinstance(path, str)
                        and path
                        and isinstance(position, int)
                        and position > 0
                        and m.last_synced_commit
                    ):
                        try:
                            # Codeberg/Gitea review comments use a diff-local `position` that is
                            # not compatible with GitHub. Translate it by parsing the Codeberg
                            # PR `.diff` for this PR and anchoring by (line, side) on GitHub.
                            if cb_diff_text is None:
                                cb_diff_text = await codeberg.get_pull_diff_text(
                                    repo=mirror.codeberg_repo, number=codeberg_pr_number
                                )
                            if not cb_diff_text:
                                raise RuntimeError("missing_codeberg_diff")
                            file_patch = extract_unified_diff_file_patch(diff_text=cb_diff_text, path=path)
                            if not file_patch:
                                raise RuntimeError("missing_file_patch_in_diff")
                            anchor = unified_diff_position_to_anchor(
                                file_patch_lines=file_patch, position=int(position)
                            )
                            if not anchor:
                                raise RuntimeError("unable_to_anchor_position")
                            created_gh = await github.create_review_comment(
                                repo=mirror.github_repo,
                                pull_number=github_pr_number,
                                commit_id=m.last_synced_commit,
                                path=path,
                                position=None,
                                line=int(anchor.line),
                                side=str(anchor.side),
                                body=rc_body,
                            )
                            db.upsert_mirrored_comment(
                                codeberg_repo=mirror.codeberg_repo,
                                codeberg_pr_number=codeberg_pr_number,
                                github_repo=mirror.github_repo,
                                github_pr_number=github_pr_number,
                                src_platform="codeberg_review",
                                src_comment_id=rc_id,
                                dst_platform="github_review",
                                dst_comment_id=created_gh.id,
                            )
                            mirrored_counts["cb_to_gh_review_reply"] += 1
                            if delay_s:
                                await asyncio.sleep(delay_s)
                        except httpx.HTTPStatusError as e:
                            log.error(
                                "comments_mirror_cb_review_root_to_gh_failed",
                                extra={
                                    "mirror": mirror.name,
                                    "codeberg_repo": mirror.codeberg_repo,
                                    "codeberg_pr": codeberg_pr_number,
                                    "github_repo": mirror.github_repo,
                                    "github_pr": github_pr_number,
                                    "rc_id": rc_id,
                                    "path": path,
                                    "commit_id": m.last_synced_commit,
                                    "position": position,
                                    "status": getattr(e.response, "status_code", None),
                                    "body": (getattr(e.response, "text", "") or "")[:500],
                                },
                            )
                        except Exception:
                            log.exception(
                                "comments_mirror_cb_review_root_to_gh_failed",
                                extra={
                                    "mirror": mirror.name,
                                    "codeberg_repo": mirror.codeberg_repo,
                                    "codeberg_pr": codeberg_pr_number,
                                    "github_repo": mirror.github_repo,
                                        "github_pr": github_pr_number,
                                        "rc_id": rc_id,
                                    "path": path,
                                },
                            )
                    else:
                        log.warning(
                            "codeberg_review_root_missing_position_anchor",
                            extra={
                                "mirror": mirror.name,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "rc_id": rc_id,
                                "path": path,
                                "codeberg_position": position,
                                "commit_id": m.last_synced_commit,
                            },
                        )
                else:
                    # Reply — look up the mapped GitHub root comment ID.
                    mapped = db.get_mirrored_comment_dst(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=codeberg_pr_number,
                        github_repo=mirror.github_repo,
                        src_platform="codeberg_review",
                        src_comment_id=root_id,
                        dst_platform="github_review",
                    )
                    github_root_id = mapped[1] if mapped and mapped[0] == "github_review" else None

                    # If the root was originally a GitHub review comment that we mirrored
                    # to Codeberg, the mapping is stored in the opposite direction:
                    #   github_review:<github id> -> codeberg_review:<codeberg root id>
                    # In that case, map the Codeberg root back to the GitHub root and
                    # post the new Codeberg reply into that existing GitHub thread.
                    if github_root_id is None:
                        github_root_id = db.get_github_review_id_for_codeberg_review_id(
                            codeberg_repo=mirror.codeberg_repo,
                            codeberg_pr_number=codeberg_pr_number,
                            github_repo=mirror.github_repo,
                            codeberg_review_comment_id=root_id,
                        )

                    if github_root_id:
                        try:
                            try:
                                created_gh = await github.create_review_comment_reply_via_replies_endpoint(
                                    repo=mirror.github_repo,
                                    pull_number=github_pr_number,
                                    comment_id=github_root_id,
                                    body=rc_body,
                                )
                            except Exception:
                                created_gh = await github.create_review_comment_reply(
                                    repo=mirror.github_repo,
                                    pull_number=github_pr_number,
                                    in_reply_to=github_root_id,
                                    body=rc_body,
                                )
                            db.upsert_mirrored_comment(
                                codeberg_repo=mirror.codeberg_repo,
                                codeberg_pr_number=codeberg_pr_number,
                                github_repo=mirror.github_repo,
                                github_pr_number=github_pr_number,
                                src_platform="codeberg_review",
                                src_comment_id=rc_id,
                                dst_platform="github_review",
                                dst_comment_id=created_gh.id,
                            )
                            mirrored_counts["cb_to_gh_review_reply"] += 1
                            if delay_s:
                                await asyncio.sleep(delay_s)
                        except Exception:
                            log.exception(
                                "comments_mirror_cb_review_reply_to_gh_failed",
                                extra={
                                    "mirror": mirror.name,
                                    "codeberg_repo": mirror.codeberg_repo,
                                    "codeberg_pr": codeberg_pr_number,
                                    "github_repo": mirror.github_repo,
                                    "github_pr": github_pr_number,
                                    "rc_id": rc_id,
                                    "root_id": root_id,
                                    "github_root_id": github_root_id,
                                },
                            )
                    else:
                        log.warning(
                            "codeberg_review_reply_no_mapped_github_root",
                            extra={
                                "mirror": mirror.name,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "root_id": root_id,
                                "rc_id": rc_id,
                            },
                        )

        if max_seen_codeberg_review > cursor_codeberg_review:
            db.set_comment_cursor(
                codeberg_repo=mirror.codeberg_repo,
                codeberg_pr_number=codeberg_pr_number,
                github_repo=mirror.github_repo,
                github_pr_number=github_pr_number,
                platform="codeberg_review",
                last_seen_id=max_seen_codeberg_review,
            )

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
                # Skip any comment already handled by Phase 0 (review comments).
                if c.id in seen_codeberg_review_comment_ids_phase0:
                    continue

                # Codeberg can expose review-thread replies through the generic
                # issue-comments feed. Before treating this as a normal PR timeline
                # comment, re-classify it by fetching reviews and their review comments.
                # If it is found there, it must be mirrored as a GitHub review comment
                # or a GitHub review-thread reply, never as a GitHub issue comment.
                thread_info = await _find_codeberg_review_thread_root(
                    codeberg=codeberg,
                    repo=mirror.codeberg_repo,
                    pull_number=codeberg_pr_number,
                    comment_id=c.id,
                )
                if thread_info is not None:
                    if db.has_mirrored_comment_any_dst(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=codeberg_pr_number,
                        github_repo=mirror.github_repo,
                        src_platform="codeberg_review",
                        src_comment_id=c.id,
                    ):
                        continue

                    review_body = format_mirrored_comment(
                        c=MirrorComment(
                            src_platform="codeberg_review",
                            src_author=c.author,
                            src_url=c.html_url,
                            src_id=c.id,
                            body=c.body,
                        )
                    )

                    if thread_info.is_root:
                        gh_line = (
                            thread_info.line if isinstance(thread_info.line, int) and thread_info.line > 0 else None
                        )
                        gh_position = None
                        if thread_info.path and m.last_synced_commit and gh_line is not None:
                            try:
                                created_gh = await github.create_review_comment(
                                    repo=mirror.github_repo,
                                    pull_number=github_pr_number,
                                    commit_id=m.last_synced_commit,
                                    path=thread_info.path,
                                    position=int(gh_position) if isinstance(gh_position, int) else None,
                                    line=int(gh_line) if isinstance(gh_line, int) else None,
                                    body=review_body,
                                )
                                db.upsert_mirrored_comment(
                                    codeberg_repo=mirror.codeberg_repo,
                                    codeberg_pr_number=codeberg_pr_number,
                                    github_repo=mirror.github_repo,
                                    github_pr_number=github_pr_number,
                                    src_platform="codeberg_review",
                                    src_comment_id=c.id,
                                    dst_platform="github_review",
                                    dst_comment_id=created_gh.id,
                                )
                                mirrored_counts["cb_to_gh_review_reply"] += 1
                                if delay_s:
                                    await asyncio.sleep(delay_s)
                            except Exception:
                                log.exception(
                                    "comments_mirror_cb_issue_reclassified_review_root_to_gh_failed",
                                    extra={
                                        "mirror": mirror.name,
                                        "codeberg_repo": mirror.codeberg_repo,
                                        "codeberg_pr": codeberg_pr_number,
                                        "github_repo": mirror.github_repo,
                                        "github_pr": github_pr_number,
                                        "comment_id": c.id,
                                        "path": thread_info.path,
                                    },
                                )
                        else:
                            log.warning(
                                "codeberg_issue_reclassified_review_root_missing_inline_metadata",
                                extra={
                                    "mirror": mirror.name,
                                    "codeberg_repo": mirror.codeberg_repo,
                                    "codeberg_pr": codeberg_pr_number,
                                    "comment_id": c.id,
                                    "path": thread_info.path,
                                    "last_synced_commit": m.last_synced_commit,
                                },
                            )
                        continue

                    # Reply — resolve the GitHub root comment ID. This covers both:
                    #   codeberg_review:<root> -> github_review:<root>
                    # and the reverse mapping when the root originated on GitHub:
                    #   github_review:<root> -> codeberg_review:<root>.
                    mapped = db.get_mirrored_comment_dst(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=codeberg_pr_number,
                        github_repo=mirror.github_repo,
                        src_platform="codeberg_review",
                        src_comment_id=thread_info.thread_root_id,
                        dst_platform="github_review",
                    )
                    github_root_id = mapped[1] if mapped and mapped[0] == "github_review" else None
                    if github_root_id is None:
                        github_root_id = db.get_github_review_id_for_codeberg_review_id(
                            codeberg_repo=mirror.codeberg_repo,
                            codeberg_pr_number=codeberg_pr_number,
                            github_repo=mirror.github_repo,
                            codeberg_review_comment_id=thread_info.thread_root_id,
                        )

                    if github_root_id:
                        try:
                            try:
                                created_gh = await github.create_review_comment_reply_via_replies_endpoint(
                                    repo=mirror.github_repo,
                                    pull_number=github_pr_number,
                                    comment_id=github_root_id,
                                    body=review_body,
                                )
                            except Exception:
                                created_gh = await github.create_review_comment_reply(
                                    repo=mirror.github_repo,
                                    pull_number=github_pr_number,
                                    in_reply_to=github_root_id,
                                    body=review_body,
                                )
                            db.upsert_mirrored_comment(
                                codeberg_repo=mirror.codeberg_repo,
                                codeberg_pr_number=codeberg_pr_number,
                                github_repo=mirror.github_repo,
                                github_pr_number=github_pr_number,
                                src_platform="codeberg_review",
                                src_comment_id=c.id,
                                dst_platform="github_review",
                                dst_comment_id=created_gh.id,
                            )
                            mirrored_counts["cb_to_gh_review_reply"] += 1
                            if delay_s:
                                await asyncio.sleep(delay_s)
                        except Exception:
                            log.exception(
                                "comments_mirror_cb_issue_reclassified_review_reply_to_gh_failed",
                                extra={
                                    "mirror": mirror.name,
                                    "codeberg_repo": mirror.codeberg_repo,
                                    "codeberg_pr": codeberg_pr_number,
                                    "github_repo": mirror.github_repo,
                                    "github_pr": github_pr_number,
                                    "comment_id": c.id,
                                    "root_id": thread_info.thread_root_id,
                                    "github_root_id": github_root_id,
                                },
                            )
                    else:
                        log.warning(
                            "codeberg_issue_reclassified_review_reply_no_mapped_github_root",
                            extra={
                                "mirror": mirror.name,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "root_id": thread_info.thread_root_id,
                                "comment_id": c.id,
                            },
                        )
                    continue

                # This is a real Codeberg PR timeline comment. Avoid duplicates by
                # checking any existing destination mapping before posting to GitHub.
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

                # --- Plain issue comment (normal PR timeline) ---
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
                # Do not skip by cursor here. Review-thread creates can fail transiently;
                # the DB mapping is the source of truth for whether this item was mirrored.
                if db.has_mirrored_comment_any_dst(
                    codeberg_repo=mirror.codeberg_repo,
                    codeberg_pr_number=codeberg_pr_number,
                    github_repo=mirror.github_repo,
                    src_platform="github_review",
                    src_comment_id=c.id,
                ):
                    continue
                mirrored_body = format_mirrored_comment(
                    c=MirrorComment(
                        src_platform="github_review",
                        src_author=c.author,
                        src_url=c.html_url,
                        src_id=c.id,
                        body=c.body,
                    )
                )
                created_id: int = 0
                dst_platform: str = "codeberg_review"
                created_review_path: str | None = None

                # GitHub review replies must stay review-thread replies. Do not fall
                # back to a normal Codeberg issue comment, because that destroys the
                # thread mapping and makes later replies impossible to attach.
                if c.in_reply_to_id:
                    mapped_cb_root = db.get_mirrored_comment_dst(
                        codeberg_repo=mirror.codeberg_repo,
                        codeberg_pr_number=codeberg_pr_number,
                        github_repo=mirror.github_repo,
                        src_platform="github_review",
                        src_comment_id=int(c.in_reply_to_id),
                        dst_platform="codeberg_review",
                    )
                    if not mapped_cb_root or mapped_cb_root[0] != "codeberg_review":
                        log.warning(
                            "github_review_reply_no_mapped_codeberg_root",
                            extra={
                                "mirror": mirror.name,
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "github_comment_id": c.id,
                                "in_reply_to_id": c.in_reply_to_id,
                            },
                        )
                        continue

                    codeberg_root_id = int(mapped_cb_root[1])
                    codeberg_root_info = await _find_codeberg_review_thread_root(
                        codeberg=codeberg,
                        repo=mirror.codeberg_repo,
                        pull_number=codeberg_pr_number,
                        comment_id=codeberg_root_id,
                    )
                    reply_path = (codeberg_root_info.path if codeberg_root_info else None) or c.path
                    reply_line = (codeberg_root_info.line if codeberg_root_info else None) or c.line
                    reply_commit = (codeberg_root_info.commit_id if codeberg_root_info else None) or m.last_synced_commit
                    if not reply_path or reply_line is None or not reply_commit:
                        log.warning(
                            "github_review_reply_missing_codeberg_inline_metadata",
                            extra={
                                "mirror": mirror.name,
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "github_comment_id": c.id,
                                "in_reply_to_id": c.in_reply_to_id,
                                "codeberg_root_id": codeberg_root_id,
                                "path": reply_path,
                                "line": reply_line,
                                "commit_id": reply_commit,
                            },
                        )
                        continue

                    try:
                        created_review = await codeberg.create_pull_review_comment(
                            repo=mirror.codeberg_repo,
                            pull_number=codeberg_pr_number,
                            commit_id=reply_commit,
                            path=reply_path,
                            line=int(reply_line),
                            body=mirrored_body,
                        )
                        created_id = int(created_review.id) if created_review.id else 0
                        created_review_path = reply_path
                        mirrored_counts["gh_review_to_cb_inline"] += 1
                    except Exception:
                        log.exception(
                            "comments_mirror_gh_review_reply_to_cb_inline_failed",
                            extra={
                                "mirror": mirror.name,
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "github_comment_id": c.id,
                                "in_reply_to_id": c.in_reply_to_id,
                                "codeberg_root_id": codeberg_root_id,
                                "path": reply_path,
                                "line": reply_line,
                            },
                        )
                        continue

                elif c.path and c.line and m.last_synced_commit:
                    try:
                        created_review = await codeberg.create_pull_review_comment(
                            repo=mirror.codeberg_repo,
                            pull_number=codeberg_pr_number,
                            commit_id=m.last_synced_commit,
                            path=c.path,
                            line=int(c.line),
                            body=mirrored_body,
                        )
                        created_id = int(created_review.id) if created_review.id else 0
                        created_review_path = c.path
                        mirrored_counts["gh_review_to_cb_inline"] += 1
                    except Exception:
                        log.exception(
                            "comments_mirror_gh_review_root_to_cb_inline_failed",
                            extra={
                                "mirror": mirror.name,
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "github_comment_id": c.id,
                                "path": c.path,
                                "line": c.line,
                            },
                        )
                        continue
                else:
                    log.warning(
                        "github_review_comment_missing_inline_metadata",
                        extra={
                            "mirror": mirror.name,
                            "github_repo": mirror.github_repo,
                            "github_pr": github_pr_number,
                            "codeberg_repo": mirror.codeberg_repo,
                            "codeberg_pr": codeberg_pr_number,
                            "github_comment_id": c.id,
                            "in_reply_to_id": c.in_reply_to_id,
                            "path": c.path,
                            "line": c.line,
                            "last_synced_commit": m.last_synced_commit,
                        },
                    )
                    continue

                # If the create-review call didn't return a concrete comment id, resolve it by
                # fetching the newest review comments and matching by path/body.
                if dst_platform == "codeberg_review" and created_id == 0 and created_review_path:
                    try:
                        reviews = await codeberg.list_pull_reviews(
                            repo=mirror.codeberg_repo, pull_number=codeberg_pr_number, page=1, limit=5
                        )
                        for r in reviews:
                            rid = r.get("id")
                            if not isinstance(rid, int):
                                continue
                            rcomments = await codeberg.list_pull_review_comments(
                                repo=mirror.codeberg_repo, pull_number=codeberg_pr_number, review_id=rid
                            )
                            for rc in rcomments or []:
                                if rc.get("path") != created_review_path:
                                    continue
                                if (rc.get("body") or "").strip() != mirrored_body.strip():
                                    continue
                                rcid = rc.get("id")
                                if isinstance(rcid, int) and rcid > 0:
                                    created_id = rcid
                                    break
                            if created_id:
                                break
                    except Exception:
                        log.exception(
                            "codeberg_review_comment_id_resolve_failed",
                            extra={
                                "mirror": mirror.name,
                                "codeberg_repo": mirror.codeberg_repo,
                                "codeberg_pr": codeberg_pr_number,
                                "github_repo": mirror.github_repo,
                                "github_pr": github_pr_number,
                                "github_review_comment": c.id,
                            },
                        )

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
