# codeberg-bridge

Self-hosted bot that mirrors Codeberg/Gitea pull requests to GitHub pull requests (one-way).

## Quick start (Docker)

1. Copy `config.example.yml` to `config.yml` and edit it.
2. Create a GitHub bot account and export its token.

```bash
export GITHUB_TOKEN=...              # GitHub fine-grained PAT for the bot account
export CODEBERG_TOKEN=...            # optional; recommended for private repos / API access
export CODEBERG_WEBHOOK_SECRET=...   # optional; recommended
docker compose up --build
```

The webhook endpoint is:

`POST /webhook/codeberg`

Health endpoint:

`GET /healthz`

With the default `docker-compose.yml`, the service is exposed on host port `8888`.

## Debugging

- Set `DEBUG_ERRORS=1` to include more error detail in logs.
- Check container logs: `docker logs -f codeberg-bridge`

## Notes

- The bot mirrors only PRs authored by `allowed_codeberg_users`.
- The bot creates deterministic, sanitized GitHub branch names under `branch_prefix`.
- The mirrored GitHub PR targets the same base branch as the Codeberg PR (e.g. `master`, `release/*`).
- Secrets are read from environment variables (see `config.yml`).
- Optional: you can also force-sync the upstream GitHub base branch back into the Codeberg repo on a timer (see below).

## Optional: sync GitHub -> Codeberg

If you want your Codeberg repo’s base branch to be force-updated to match the upstream GitHub repo periodically, set a per-mirror interval:

```yml
mirrors:
  - name: monero
    github_repo: plowsof/monero
    codeberg_repo: montero-project/monero
    base_branch: master           # GitHub branch to sync from
    codeberg_base_branch: master  # optional; defaults to base_branch
    sync_upstream_to_codeberg_interval: 8h
```

Duration format: `30s`, `10m`, `8h`, `1d`. If you omit the unit (e.g. `1`), hours are assumed.

## Optional: reconcile GitHub PR status -> Codeberg

If you don’t want to rely on GitHub webhooks, you can periodically reconcile mirrored PR status:

- If a mirrored GitHub PR is **closed** (merged or not), the bridge will **close the original Codeberg PR** and mark the mapping as closed.

Enable per mirror:

```yml
mirrors:
  - name: monero
    github_repo: plowsof/monero
    codeberg_repo: montero-project/monero
    reconcile_github_to_codeberg_interval: 8h
```

This requires `CODEBERG_TOKEN`.

## Optional: backfill open Codeberg PRs (stateless)

If the bridge is offline for a while, you can periodically scan Codeberg for open PRs and ensure each one has a mirrored GitHub PR.

This does **not** require the SQLite mapping; it checks GitHub for an existing PR by deterministic `head` (`<bot_login>:<mirror_branch>`).

Enable per mirror:

```yml
mirrors:
  - name: monero
    github_repo: plowsof/monero
    codeberg_repo: montero-project/monero
    backfill_codeberg_open_prs_interval: 8h
```

## Branch cleanup

When a Codeberg PR is closed (via webhook) or a mirrored GitHub PR is detected as closed during reconciliation, the bridge will best-effort delete the mirrored branch from the bot fork.

## Adding approved users

Edit `config.yml` and add usernames under `mirrors[].allowed_codeberg_users`, then restart the service.

For your current setup, `config.example.yml` is pre-filled for:

- GitHub upstream: `plowsof/monero`
- GitHub bot fork: `bridgedburden/monero`
- Codeberg landing repo: `montero-project/monero`
