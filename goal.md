# Build a Codeberg/Gitea → GitHub PR Mirroring Bot

Create a self-hosted bot that mirrors pull requests from Codeberg/Gitea repositories to GitHub repositories.

---

# Core Idea

The bot watches configured Codeberg/Gitea repositories. When a configured Codeberg/Gitea user opens or updates a pull request, the bot creates or updates a matching branch on its GitHub fork, then opens or updates a GitHub pull request against the configured upstream GitHub repository.

This is **one-way only**:

```text
Codeberg/Gitea PR → GitHub PR
```

The bot must never create pull requests back to Codeberg/Gitea.

---

# Configuration

The bot should be configured using a YAML or JSON file.

Example YAML:

```yaml
github:
  auth:
    type: bot_token # future support for github_app is optional
    token_env: GITHUB_TOKEN
  bot_username: my-github-bot

codeberg:
  auth:
    token_env: CODEBERG_TOKEN

storage:
  sqlite_path: ./data/database.sqlite

git:
  working_directory: ./repos

mirrors:
  - name: example-project

    github_repo: owner/project
    codeberg_repo: codeberguser/project

    allowed_codeberg_users:
      - alice
      - bob

    base_branch: master

    branch_prefix: codeberg-pr
```

Each mirror entry means:

- `github_repo`: upstream GitHub repository where mirrored PRs are created
- `codeberg_repo`: Codeberg/Gitea repository to watch
- `allowed_codeberg_users`: only PRs from these users should be mirrored
- `base_branch`: upstream branch to target (`main` or `master`)
- `branch_prefix`: prefix for generated GitHub mirror branches

---

# Required Behavior

For each configured repository pair:

1. Ensure the bot has a fork of the configured GitHub repository.
2. Keep the bot fork updated with the upstream GitHub base branch.
3. Watch pull requests on the configured Codeberg/Gitea repository.
4. Only mirror PRs opened by configured `allowed_codeberg_users`.
5. For each eligible PR:
   - fetch the PR commits
   - create or update a generated branch on the GitHub fork
   - push commits to the GitHub fork
   - create or update a GitHub pull request
   - include a link to the original Codeberg/Gitea PR
   - continuously sync updates

---

# Important Branch Naming Rule

The bot must NEVER create raw branch names from Codeberg/Gitea directly on GitHub.

Example:

If the source branch is:

```text
master
```

the bot MUST NOT create:

```text
master
```

Instead it must generate a safe branch name such as:

```text
codeberg-pr/alice/example-project/pr-42
```

or:

```text
codeberg-fork-master-1
```

Preferred format:

```text
{branch_prefix}/{codeberg_owner}/{codeberg_repo}/pr-{pr_number}
```

Example:

```text
codeberg-pr/alice/example-project/pr-42
```

Requirements:

- deterministic
- sanitized
- unique
- bot-owned
- safe to force-push

The bot may ONLY force-push branches matching the configured prefix.

The bot must NEVER modify branches like:

```text
main
master
develop
release
```

---

# GitHub Pull Request Behavior

The GitHub PR should:

- target the configured upstream GitHub repository
- target the configured base branch
- originate from the bot fork
- use the generated mirror branch

Title format:

```text
[Codeberg] Original PR Title
```

The PR body must include:

- original Codeberg/Gitea PR URL
- original author
- original source branch
- synchronization warning

Example PR body:

```markdown
This pull request was mirrored automatically from Codeberg/Gitea.

Original PR:
https://codeberg.org/alice/project/pulls/42

Original author:
alice

Original source branch:
master

Do not push directly to this branch.
Updates should happen on the original Codeberg/Gitea pull request.
```

---

# State Tracking

The bot must persist mapping state to avoid duplicate PR creation.

Store at least:

```text
codeberg_repo
codeberg_pr_number
github_repo
github_pr_number
github_fork_repo
github_branch
last_synced_commit
status
created_at
updated_at
```

Use SQLite by default.

---

# Webhooks

The bot should expose an HTTP webhook endpoint.

Handle these Codeberg/Gitea webhook events:

- pull_request opened
- pull_request synchronized
- pull_request updated
- pull_request reopened
- pull_request closed

Optional behavior:

- close mirrored GitHub PR when source PR closes
- configurable via settings

---

# Git Synchronization Algorithm

For each mirrored PR:

1. Clone or reuse a local cached repository.
2. Fetch the latest upstream GitHub base branch.
3. Fetch the Codeberg/Gitea PR commits.
4. Create or reset the generated mirror branch.
5. Rebase or replay commits onto the latest GitHub base branch if possible.
6. Push the generated branch to the bot GitHub fork.
7. Create or update the GitHub PR.
8. Store updated synchronization state.

The implementation should prefer:

- deterministic operations
- idempotency
- recoverability after crashes
- minimal git history corruption risk

---

# Security Requirements

- All secrets must come from environment variables.
- Validate webhook signatures where supported.
- Sanitize:
  - usernames
  - repository names
  - branch names
  - PR numbers
- Never execute untrusted shell input.
- Never allow arbitrary git refs.
- Restrict force-push operations to generated bot branches only.
- Ignore PRs from users not listed in `allowed_codeberg_users`.

---

# Architecture Requirements

Implement:

- configuration loader
- webhook HTTP server
- GitHub API client
- Gitea/Codeberg API client
- git synchronization worker
- SQLite database layer
- retry handling
- structured logging
- Dockerfile
- docker-compose example
- README

---

# Preferred Tech Stack

Preferred stacks:

- TypeScript + Node.js + Fastify + SQLite
- Go + SQLite
- Python + FastAPI + SQLite

Before implementation:

1. Explain which stack you choose.
2. Explain the architecture.
3. Explain the git synchronization strategy.
4. Then implement incrementally.

---

# Deliverables

Produce:

1. Architecture overview
2. Configuration schema
3. Database schema
4. Webhook flow
5. Git synchronization algorithm
6. Full implementation
7. Dockerfile
8. docker-compose example
9. Example configuration
10. README with VPS deployment instructions
11. Notes on GitHub bot token vs GitHub App support

The implementation should prioritize:

- reliability
- simplicity
- self-hosting
- maintainability
- deterministic behavior
- safe git operations
