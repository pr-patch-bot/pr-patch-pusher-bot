from __future__ import annotations

import asyncio
import logging

from .clients import CodebergClient
from .config import AppConfig, LoadedSecrets, MirrorConfig
from .db import Database
from .clients import GitHubClient
from .mirror import mirror_pr
from .mirror import _mirror_branch_name
from .utils import parse_duration_seconds


log = logging.getLogger("codeberg_bridge.backfill")


async def backfill_once(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database | None,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.backfill_codeberg_open_prs_interval
    if not interval:
        return

    codeberg = CodebergClient(base_url=config.codeberg.base_url, token=secrets.codeberg_token)
    github = GitHubClient(token=secrets.github_token)
    bot_login = await github.get_authenticated_user_login()
    allowed_users = set(mirror.allowed_codeberg_users or [])

    page = 1
    while True:
        items = await codeberg.list_pull_requests(repo=mirror.codeberg_repo, state="open", page=page)
        if not items:
            break

        for pr in items:
            if allowed_users and pr.author not in allowed_users:
                continue

            branch = _mirror_branch_name(mirror, codeberg_repo=mirror.codeberg_repo, pr_number=pr.number)
            head = f"{bot_login}:{branch}"
            existing = await github.find_pr_by_head(upstream_repo=mirror.github_repo, head=head)
            if existing:
                continue

            log.info(
                "backfill_mirror_pr",
                extra={"mirror": mirror.name, "repo": mirror.codeberg_repo, "pr": pr.number},
            )
            try:
                await mirror_pr(
                    config=config,
                    secrets=secrets,
                    db=db,
                    mirror=mirror,
                    codeberg_pr_number=int(pr.number),
                )
            except Exception:
                log.exception(
                    "backfill_mirror_failed",
                    extra={"mirror": mirror.name, "repo": mirror.codeberg_repo, "pr": pr.number},
                )

        page += 1


async def run_backfill_worker(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database | None,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.backfill_codeberg_open_prs_interval
    if not interval:
        return
    seconds = parse_duration_seconds(interval)

    log.info(
        "backfill_worker_started",
        extra={
            "mirror": mirror.name,
            "github_repo": mirror.github_repo,
            "codeberg_repo": mirror.codeberg_repo,
            "interval_s": seconds,
        },
    )

    while True:
        try:
            await backfill_once(config=config, secrets=secrets, db=db, mirror=mirror)
        except Exception:
            log.exception(
                "backfill_failed",
                extra={"mirror": mirror.name, "github_repo": mirror.github_repo, "codeberg_repo": mirror.codeberg_repo},
            )
        await asyncio.sleep(seconds)
