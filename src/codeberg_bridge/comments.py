from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MirrorComment:
    src_platform: str  # "github" | "codeberg"
    src_author: str
    src_url: str
    src_id: int
    body: str


def format_mirrored_comment(*, c: MirrorComment) -> str:
    # Keep this stable: it's used for loop prevention and idempotency.
    header = f"Comment by @{c.src_author} on {c.src_platform}: {c.src_url}"
    marker = f"<!-- cbb:mirror src={c.src_platform} id={c.src_id} -->"
    body = (c.body or "").strip()
    if not body:
        return "\n".join([header, "", marker])
    return "\n".join([header, "", body, "", marker])

