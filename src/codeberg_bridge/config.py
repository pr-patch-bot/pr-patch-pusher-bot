from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class GitHubAuthConfig(BaseModel):
    type: Literal["bot_token"] = "bot_token"
    token_env: str = "GITHUB_TOKEN"


class GitHubConfig(BaseModel):
    auth: GitHubAuthConfig
    bot_username: str


class CodebergAuthConfig(BaseModel):
    token_env: str = "CODEBERG_TOKEN"


class CodebergWebhookConfig(BaseModel):
    secret_env: str = "CODEBERG_WEBHOOK_SECRET"


class CodebergConfig(BaseModel):
    base_url: str = "https://codeberg.org"
    auth: CodebergAuthConfig = Field(default_factory=CodebergAuthConfig)
    webhook: CodebergWebhookConfig = Field(default_factory=CodebergWebhookConfig)


class StorageConfig(BaseModel):
    sqlite_path: str = "./data/database.sqlite"


class GitConfig(BaseModel):
    working_directory: str = "./repos"


class MirrorConfig(BaseModel):
    name: str
    github_repo: str
    codeberg_repo: str
    allowed_codeberg_users: list[str] = Field(default_factory=list)
    base_branch: str = "main"
    branch_prefix: str = "codeberg-pr"
    codeberg_base_branch: str | None = None
    sync_upstream_to_codeberg_interval: str | None = None
    reconcile_github_to_codeberg_interval: str | None = None
    backfill_codeberg_open_prs_interval: str | None = None
    mirror_comments_interval: str | None = None


class AppConfig(BaseModel):
    github: GitHubConfig
    codeberg: CodebergConfig = Field(default_factory=CodebergConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    mirrors: list[MirrorConfig] = Field(default_factory=list)


@dataclass(frozen=True)
class LoadedSecrets:
    github_token: str
    codeberg_token: str | None
    codeberg_webhook_secret: str | None


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


def load_secrets(config: AppConfig) -> LoadedSecrets:
    github_token = os.environ.get(config.github.auth.token_env)
    if not github_token:
        raise RuntimeError(f"Missing required env var: {config.github.auth.token_env}")
    codeberg_token = os.environ.get(config.codeberg.auth.token_env)
    webhook_secret = os.environ.get(config.codeberg.webhook.secret_env)
    return LoadedSecrets(
        github_token=github_token,
        codeberg_token=codeberg_token,
        codeberg_webhook_secret=webhook_secret,
    )
