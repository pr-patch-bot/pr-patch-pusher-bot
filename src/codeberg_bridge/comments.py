from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MirrorComment:
    # Example values: "github_issue", "github_review", "codeberg_issue"
    src_platform: str
    src_author: str
    src_url: str
    src_id: int
    body: str


def format_mirrored_comment(*, c: MirrorComment) -> str:
    # Keep this stable: it's used for loop prevention and idempotency.
    author = c.src_author
    if c.src_url:
        author = f"< [{c.src_author}]({c.src_url}) >"
    else:
        author = f"< {c.src_author} >"

    header = f"{author}:"
    marker = f"<!-- cbb:mirror src={c.src_platform} id={c.src_id} -->"
    body = (c.body or "").strip()
    if not body:
        return "\n".join([header, "", marker]).strip()
    return "\n".join([header, body, "", marker])
