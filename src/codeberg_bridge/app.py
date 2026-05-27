from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response

from .config import AppConfig, LoadedSecrets, load_config, load_secrets
from .db import Database
from .logging import setup_logging
from .backfill import run_backfill_worker
from .reconcile import run_reconcile_worker
from .mirror import _mirror_branch_name, mirror_pr
from .sync_upstream import run_sync_worker
from .utils import constant_time_equals, hmac_sha256_hex
from .clients import GitHubClient


log = logging.getLogger("codeberg_bridge.app")


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
                asyncio.create_task(run_backfill_worker(config=config, secrets=secrets, mirror=mirror))
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
    if event != "pull_request":
        return Response(status_code=202, content="ignored event")

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        return Response(status_code=400, content="invalid json")

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
            # Best-effort cleanup of the mirrored branch in the bot fork.
            try:
                github = GitHubClient(token=secrets.github_token)
                await github.delete_branch(repo=existing.github_fork_repo, branch=existing.github_branch)
            except Exception:
                log.exception(
                    "branch_cleanup_failed",
                    extra={"github_fork_repo": existing.github_fork_repo, "github_branch": existing.github_branch},
                )
        else:
            # Stateless cleanup: compute expected branch and delete it from the token's fork.
            try:
                github = GitHubClient(token=secrets.github_token)
                login = await github.get_authenticated_user_login()
                branch = _mirror_branch_name(mirror, codeberg_repo=mirror.codeberg_repo, pr_number=number)
                fork_repo = f"{login}/{mirror.github_repo.split('/', 1)[1]}"
                await github.delete_branch(repo=fork_repo, branch=branch)
            except Exception:
                log.exception("branch_cleanup_failed_stateless", extra={"pr": number, "repo": mirror.github_repo})
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
