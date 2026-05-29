# Inline threading limitations (GitHub ↔ Codeberg/Gitea)

This bridge can mirror *inline review comments* between GitHub and Codeberg/Gitea, but it cannot always preserve the exact **“reply in the same inline thread”** behavior you see in each web UI.

The reason is an API mismatch: GitHub exposes a first-class “reply to this inline comment thread” operation; Codeberg/Gitea’s public REST API for creating inline comments does not expose an equivalent `in_reply_to` field for inline comments.

## What GitHub supports (threaded inline replies)

GitHub supports:

- Creating inline PR review comments via `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments`
- Replying in the same inline thread via:
  - `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies`, or
  - `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments` with `in_reply_to`

This makes “threaded replies” a stable, explicit API concept on GitHub. citeturn0search1

## What Codeberg/Gitea exposes (line-attached comments, but not reply threading)

Codeberg runs Gitea. Gitea’s public REST API supports creating inline (line-attached) comments by creating a review with embedded comments:

- `POST /api/v1/repos/{owner}/{repo}/pulls/{index}/reviews`
  - includes `comments[]` entries with:
    - `path`
    - `old_position` / `new_position` (line numbers)

The public schema for creating inline comments does **not** include an `in_reply_to` / “reply to comment id” field. citeturn3view2

Result: when mirroring a reply *from GitHub into Codeberg inline threads*, the bridge cannot tell Gitea “attach this as a reply under comment X”. The only available operations are:

- create another line-attached comment at the same line (can duplicate hunks / look noisy), or
- create a normal PR conversation comment with a source link (clear, but not threaded inline).

## What the bridge does (pragmatic behavior)

- **GitHub → Codeberg**
  - Inline review comments become Codeberg line-attached comments when possible.
  - Inline replies on GitHub are mirrored as *normal Codeberg PR comments* to avoid duplicated hunks, with a `[src]` link back to the original inline thread.

- **Codeberg → GitHub**
  - If Codeberg includes `in_reply_to` in the webhook payload for an inline comment, the bridge can map it and reply in the correct GitHub inline thread (GitHub supports this explicitly).
  - If Codeberg does not include a `comment` object/id (some “review submitted” payloads), the bridge mirrors the review summary as a normal GitHub PR comment.

## Why “but the UI can reply” doesn’t automatically imply “the API can”

Web UIs can use internal endpoints and server-side context that are not exposed (or not stable) in the public REST API. For automation, the bridge relies on documented REST endpoints and stable payload fields.

If Gitea adds a supported “reply to inline comment” field/endpoint in its public API in the future, the bridge can be updated to preserve inline threading for GitHub → Codeberg replies.
