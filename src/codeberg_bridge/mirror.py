from __future__ import annotations

import logging
import asyncio
import os

from .clients import CodebergClient, GitHubClient
from .config import AppConfig, LoadedSecrets, MirrorConfig
from .db import Database
from .git_ops import ensure_repo, get_head_sha, sync_branch
from .utils import sanitize_branch_component
from .utils import parse_duration_seconds


log = logging.getLogger("codeberg_bridge.mirror")

_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _lock_for(mirror: MirrorConfig, pr_number: int) -> asyncio.Lock:
    key = (mirror.name, pr_number)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _mirror_branch_name(mirror: MirrorConfig, *, codeberg_repo: str, pr_number: int) -> str:
    owner, name = codeberg_repo.split("/", 1)
    return "/".join(
        [
            sanitize_branch_component(mirror.branch_prefix),
            sanitize_branch_component(owner),
            sanitize_branch_component(name),
            f"pr-{pr_number}",
        ]
    )


def _pr_title(original_title: str) -> str:
    return original_title


def _pr_body(*, pr_url: str, author: str, author_url: str, original_body: str) -> str:
    original_body = (original_body or "").strip()
    header = f"Patch from [{author}]({author_url}): [{pr_url}]({pr_url})"
    if not original_body:
        return header
    return "\n".join([header, "", original_body])


async def mirror_pr(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database | None,
    mirror: MirrorConfig,
    codeberg_pr_number: int,
) -> None:
    async with _lock_for(mirror, codeberg_pr_number):
        await _mirror_pr_inner(
            config=config,
            secrets=secrets,
            db=db,
            mirror=mirror,
            codeberg_pr_number=codeberg_pr_number,
        )


async def _mirror_pr_inner(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database | None,
    mirror: MirrorConfig,
    codeberg_pr_number: int,
) -> None:
    codeberg = CodebergClient(
        base_url=config.codeberg.base_url, token=secrets.codeberg_token
    )
    github = GitHubClient(token=secrets.github_token)

    pr = await codeberg.get_pull_request(repo=mirror.codeberg_repo, number=codeberg_pr_number)

    # Optional (disabled by default): delay mirroring when a marker is present
    # in the Codeberg PR title/body. Enable by setting:
    #   DEBUG_DELAY_MARKER='!customerPaySubscription'
    #   DEBUG_DELAY_DURATION='1m'
    marker = os.environ.get("DEBUG_DELAY_MARKER", "").strip()
    duration = os.environ.get("DEBUG_DELAY_DURATION", "").strip()
    if marker and duration and (marker in pr.title or marker in pr.body):
        try:
            delay_s = parse_duration_seconds(duration)
        except Exception:
            delay_s = 60
        log.info("debug_delay_enabled", extra={"delay_s": delay_s, "marker": marker, "pr": pr.number})
        await asyncio.sleep(delay_s)

    if mirror.allowed_codeberg_users and pr.author not in set(mirror.allowed_codeberg_users):
        log.info("skip_pr_user_not_allowed", extra={"author": pr.author, "pr": pr.number})
        return

    fork_repo = await github.ensure_fork(
        upstream_repo=mirror.github_repo, bot_username=config.github.bot_username
    )
    branch = _mirror_branch_name(mirror, codeberg_repo=mirror.codeberg_repo, pr_number=pr.number)

    fork_owner = fork_repo.split("/", 1)[0]
    head = f"{fork_owner}:{branch}"
    existing = await github.find_pr_by_head(upstream_repo=mirror.github_repo, head=head)
    title = _pr_title(pr.title)
    author_url = f"{config.codeberg.base_url.rstrip('/')}/{pr.author}"
    body = _pr_body(pr_url=pr.html_url, author=pr.author, author_url=author_url, original_body=pr.body)

    # If the PR already exists, update title/body even if git sync later fails.
    if existing:
        await github.update_pr_body(
            upstream_repo=mirror.github_repo, number=existing.number, title=title, body=body
        )

    repo_paths = ensure_repo(
        working_dir=config.git.working_directory,
        upstream_repo=mirror.github_repo,
        fork_repo=fork_repo,
        github_token=secrets.github_token,
    )
    sync_branch(
        repo_path=repo_paths.path,
        upstream_base_branch=mirror.base_branch,
        mirror_branch=branch,
        codeberg_clone_url=pr.head_repo_clone_url,
        codeberg_ref=pr.head_ref,
    )
    head_sha = get_head_sha(repo_path=repo_paths.path)

    if existing:
        if db:
            db.upsert_mapping(
                codeberg_repo=mirror.codeberg_repo,
                codeberg_pr_number=pr.number,
                github_repo=mirror.github_repo,
                github_fork_repo=fork_repo,
                github_branch=branch,
                github_pr_number=existing.number,
                last_synced_commit=head_sha,
                status="open",
            )
        log.info("updated_github_pr", extra={"github_pr": existing.number, "head_sha": head_sha})
        return

    created = await github.create_pr(
        upstream_repo=mirror.github_repo,
        title=title,
        body=body,
        head=head,
        base=mirror.base_branch,
    )
    if db:
        db.upsert_mapping(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=pr.number,
            github_repo=mirror.github_repo,
            github_fork_repo=fork_repo,
            github_branch=branch,
            github_pr_number=created.number,
            last_synced_commit=head_sha,
            status="open",
        )
    log.info("created_github_pr", extra={"github_pr": created.number, "url": created.html_url})
