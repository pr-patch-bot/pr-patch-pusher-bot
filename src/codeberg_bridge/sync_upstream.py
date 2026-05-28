from __future__ import annotations

import asyncio
import logging

from .config import AppConfig, LoadedSecrets, MirrorConfig
from .git_ops import sync_upstream_to_codeberg
from .utils import parse_duration_seconds


log = logging.getLogger("codeberg_bridge.sync_upstream")


async def sync_upstream_once(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.sync_upstream_to_codeberg_interval
    if not interval:
        return
    if not secrets.codeberg_token:
        raise RuntimeError("CODEBERG_TOKEN is required for sync_upstream_to_codeberg_interval")

    upstream_branch = mirror.base_branch
    codeberg_branch = mirror.codeberg_base_branch or mirror.base_branch

    await asyncio.to_thread(
        sync_upstream_to_codeberg,
        working_dir=config.git.working_directory,
        upstream_repo=mirror.github_repo,
        upstream_branch=upstream_branch,
        codeberg_repo=mirror.codeberg_repo,
        codeberg_branch=codeberg_branch,
        codeberg_token=secrets.codeberg_token,
    )


async def run_sync_worker(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.sync_upstream_to_codeberg_interval
    if not interval:
        return
    seconds = parse_duration_seconds(interval)

    log.info(
        "sync_upstream_worker_started",
        extra={
            "mirror": mirror.name,
            "github_repo": mirror.github_repo,
            "codeberg_repo": mirror.codeberg_repo,
            "interval_s": seconds,
        },
    )

    while True:
        try:
            await sync_upstream_once(config=config, secrets=secrets, mirror=mirror)
            log.info(
                "sync_upstream_ok",
                extra={"mirror": mirror.name, "github_repo": mirror.github_repo, "codeberg_repo": mirror.codeberg_repo},
            )
        except Exception:
            log.exception(
                "sync_upstream_failed",
                extra={"mirror": mirror.name, "github_repo": mirror.github_repo, "codeberg_repo": mirror.codeberg_repo},
            )
        await asyncio.sleep(seconds)
