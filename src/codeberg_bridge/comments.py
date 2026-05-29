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
    platform = c.src_platform.split("_", 1)[0]
    if platform == "github":
        profile = f"https://github.com/{c.src_author}"
    elif platform == "codeberg":
        profile = f"https://codeberg.org/{c.src_author}"
    else:
        profile = ""

    author = c.src_author
    if profile:
        author = f"[{c.src_author}]({profile})"

    header = f"{author}:"
    src = f"[src]({c.src_url})" if c.src_url else ""
    marker = f"<!-- cbb:mirror src={c.src_platform} id={c.src_id} -->"
    body = (c.body or "").strip()
    if not body:
        return "\n".join([header, src, "", marker]).strip()
    if src:
        return "\n".join([header, body, src, "", marker])
    return "\n".join([header, body, "", marker])
