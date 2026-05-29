# Comments mirroring (GitHub ↔ Codeberg) — design notes

Goal: mirror pull-request discussion comments between Codeberg (Gitea) and GitHub using two bot accounts:

- GitHub: `GITHUB_TOKEN` (existing)
- Codeberg: `CODEBERG_TOKEN` (existing)

## Phased scope (we're doing 1+2 first, then 3)

### Phase 1 (MVP): PR conversation comments (bi-directional)

Mirror **PR conversation comments only** (top-level PR comments), not inline review comments.

- GitHub: issue comments on the PR (`/issues/{pr_number}/comments`)
- Codeberg: issue comments on the PR (`/issues/{pr_number}/comments`)

Rationale: inline review comments don’t map 1:1 across platforms (diff positions/paths/lines), and can easily create confusing threads.

### Phase 2: GitHub inline review comments → Codeberg as normal comments (one-way)

Mirror GitHub inline review comments to Codeberg, but **as normal PR conversation comments** (not anchored).

Body should include:

- original author
- direct link to the original GitHub inline comment
- (optional) file path + line/position metadata as plain text if available
- hidden mirror marker

This is intentionally lossy but reliable, and keeps Codeberg users in the loop.

Reply behavior in Phase 2:

- If a Codeberg user replies to the mirrored inline comment, mirror that reply back to GitHub as a **normal PR conversation comment** that links to the original GitHub inline comment.
- Do not attempt to post it into the original inline thread yet (that’s Phase 3).

### Phase 3: Threaded inline review comment mirroring (best-effort)

Attempt to preserve inline threads across platforms:

- If a Codeberg comment is a reply to a mirrored GitHub inline comment, the GitHub bot replies into the correct GitHub review thread when possible.
- If the diff anchor is missing/outdated, fall back to a normal PR conversation comment with a link.

This requires more stored metadata (see “Storage”) and careful handling of outdated diffs.

Implementation note (current approach):

- Codeberg does not provide a reliable “reply to comment X” field for PR comments.
- We treat a Codeberg comment as a reply-to-inline when it contains a GitHub review comment URL/id
  (e.g. `discussion_r123456789` / `#r123456789`), and use GitHub’s `in_reply_to` API to reply in-thread.
  Otherwise we mirror as a normal PR conversation comment.

## Codeberg inline comments (review comments) -> GitHub

Codeberg inline comments are delivered via a more specific webhook type:

- `X-Gitea-Event-Type: pull_request_review_comment` citeturn0search0

The bridge now accepts that event type and best-effort mirrors it to GitHub as:

- a GitHub inline review comment when `path` + `position` + `commit_id` are provided, or
- a GitHub reply-in-thread when an `in_reply_to` id is provided, or
- a fallback normal PR conversation comment when anchoring data is missing.

## Authorship model

Mirrored comments are authored by the bot account on the destination platform.

Each mirrored comment must:

- Identify the original author username and platform.
- Include a direct link to the original comment.
- Include a hidden marker to prevent mirror-loops and enable idempotency.

Suggested body format:

```
Comment by @<user> on <platform>: <original_comment_url>

<original_body>

<!-- cbb:mirror src=<github|codeberg> id=<comment_id> -->
```

## Loop prevention / idempotency

When ingesting comments from either platform:

- Ignore any comment authored by the destination bot account.
- Ignore any comment containing `<!-- cbb:mirror ... -->`.
- Maintain a mapping table of mirrored comment IDs (see “Storage”).

This allows:

- Avoiding duplicate mirrors.
- Supporting edits (optional): update the mirrored comment instead of creating a new one.

## Events / triggering

Two options:

1) Webhooks (preferred)
   - GitHub: `issue_comment` (created/edited/deleted) for PR issues.
   - Codeberg: Gitea issue_comment webhook for PR issues.

2) Polling backfill (fallback)
   - Periodically list comments since last cursor per PR mapping.

## Implementation sketch (repo-local)

- Add `clients.py`:
  - `GitHubClient.list_issue_comments(...)`, `GitHubClient.create_issue_comment(...)`
  - `CodebergClient.list_issue_comments(...)`, `CodebergClient.create_issue_comment(...)` (already has create)
- Add `comments.py`:
  - `mirror_codeberg_comment_to_github(...)`
  - `mirror_github_comment_to_codeberg(...)`
- Add storage:
  - new SQLite table `mirrored_comments` (Phase 1+)
  - optional extra columns for Phase 3 anchoring (see below)
- Add webhook endpoints:
  - `/webhook/github` (optional; requires signature verification)
  - extend existing `/webhook/codeberg` to handle issue_comment events

## Storage (suggested)

Minimum for Phase 1+2:

- `codeberg_repo`, `codeberg_pr_number`
- `github_repo`, `github_pr_number`
- `src_platform`, `src_comment_id`
- `dst_platform`, `dst_comment_id`
- `src_comment_url` (optional but convenient)
- `created_at`, `updated_at`

Additional for Phase 3 (GitHub inline anchoring):

- `github_review_comment_id` (if different from `src_comment_id`)
- `github_pull_request_review_id` / `github_review_thread_id` (if used)
- `github_path`
- `github_commit_id`
- `github_position` and/or `github_line` + `github_side`

Rule: always be able to fall back to a normal PR conversation comment when anchor metadata is missing or invalid.

## Gotchas

- Markdown differences: keep transformations minimal; prefer “quote + link back”.
- Mentions: don’t rewrite `@user` across platforms in MVP.
- Rate limiting: batch polling + webhook retry backoff.
- Edits/deletes: decide policy early (mirror edits? annotate deletes?).
