from __future__ import annotations

import asyncio
import logging

from .clients import CodebergClient, GitHubClient
from .config import AppConfig, LoadedSecrets, MirrorConfig
from .db import Database
from .utils import parse_duration_seconds


log = logging.getLogger("codeberg_bridge.reconcile")

def _closed_comment(*, github_pr_url: str) -> str:
    return "\n".join(
        [
            f"Mirrored GitHub PR was closed: {github_pr_url}",
            "",
            "<!-- cbb:mirror src=github_system id=close_notice -->",
        ]
    )


def _merged_comment(*, github_pr_url: str, github_merge_commit_url: str | None) -> str:
    if github_merge_commit_url:
        return "\n".join(
            [
                f"Mirrored GitHub PR was merged: {github_pr_url}",
                f"Merge commit: {github_merge_commit_url}",
                "",
                "<!-- cbb:mirror src=github_system id=merge_notice -->",
            ]
        )
    return "\n".join(
        [
            f"Mirrored GitHub PR was merged: {github_pr_url}",
            "",
            "<!-- cbb:mirror src=github_system id=merge_notice -->",
        ]
    )


async def reconcile_once(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.reconcile_github_to_codeberg_interval
    if not interval:
        return
    if not secrets.codeberg_token:
        raise RuntimeError("CODEBERG_TOKEN is required for reconcile_github_to_codeberg_interval")

    github = GitHubClient(token=secrets.github_token)
    codeberg = CodebergClient(base_url=config.codeberg.base_url, token=secrets.codeberg_token)

    mappings = db.list_open_mappings(codeberg_repo=mirror.codeberg_repo, github_repo=mirror.github_repo)
    for m in mappings:
        if not m.github_pr_number:
            continue
        pr_number = int(m.github_pr_number)
        pr = await github.get_pr(upstream_repo=mirror.github_repo, number=pr_number)
        state = (pr.state or "").strip().lower()
        if state == "open":
            continue

        github_merge_commit_url: str | None = None
        if pr.merge_commit_sha:
            github_merge_commit_url = f"https://github.com/{mirror.github_repo}/commit/{pr.merge_commit_sha}"

        try:
            if pr.merged_at:
                comment = _merged_comment(
                    github_pr_url=pr.html_url, github_merge_commit_url=github_merge_commit_url
                )
            else:
                comment = _closed_comment(github_pr_url=pr.html_url)
            await codeberg.create_issue_comment(
                repo=mirror.codeberg_repo, issue_number=m.codeberg_pr_number, body=comment
            )
        except Exception:
            log.exception(
                "codeberg_close_comment_failed",
                extra={
                    "mirror": mirror.name,
                    "github_repo": mirror.github_repo,
                    "github_pr": pr.number,
                    "codeberg_repo": mirror.codeberg_repo,
                    "codeberg_pr": m.codeberg_pr_number,
                },
            )

        # If GitHub PR is closed (merged or not), close Codeberg PR to keep things tidy.
        await codeberg.update_pull_request_state(
            repo=mirror.codeberg_repo, number=m.codeberg_pr_number, state="closed"
        )
        db.update_status(
            codeberg_repo=mirror.codeberg_repo,
            codeberg_pr_number=m.codeberg_pr_number,
            github_repo=mirror.github_repo,
            status="closed",
        )
        try:
            await github.delete_branch(repo=m.github_fork_repo, branch=m.github_branch)
        except Exception:
            log.exception(
                "branch_cleanup_failed",
                extra={
                    "mirror": mirror.name,
                    "github_fork_repo": m.github_fork_repo,
                    "github_branch": m.github_branch,
                },
            )
        log.info(
            "reconciled_closed",
            extra={
                "mirror": mirror.name,
                "github_repo": mirror.github_repo,
                "github_pr": pr.number,
                "github_merged": bool(pr.merged_at),
                "codeberg_repo": mirror.codeberg_repo,
                "codeberg_pr": m.codeberg_pr_number,
            },
        )


async def run_reconcile_worker(
    *,
    config: AppConfig,
    secrets: LoadedSecrets,
    db: Database,
    mirror: MirrorConfig,
) -> None:
    interval = mirror.reconcile_github_to_codeberg_interval
    if not interval:
        return
    seconds = parse_duration_seconds(interval)

    log.info(
        "reconcile_worker_started",
        extra={
            "mirror": mirror.name,
            "github_repo": mirror.github_repo,
            "codeberg_repo": mirror.codeberg_repo,
            "interval_s": seconds,
        },
    )

    while True:
        try:
            await reconcile_once(config=config, secrets=secrets, db=db, mirror=mirror)
        except Exception:
            log.exception(
                "reconcile_failed",
                extra={"mirror": mirror.name, "github_repo": mirror.github_repo, "codeberg_repo": mirror.codeberg_repo},
            )
        await asyncio.sleep(seconds)
