from __future__ import annotations

import re
from dataclasses import dataclass


_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


@dataclass(frozen=True)
class DiffAnchor:
    line: int
    side: str  # "LEFT" or "RIGHT"


def extract_unified_diff_file_patch(*, diff_text: str, path: str) -> list[str] | None:
    """
    Extract the unified-diff "file patch" lines for *path* from a multi-file diff.

    The returned list includes the `diff --git ...` header and all following lines
    up to (but not including) the next `diff --git ...` header.
    """
    if not diff_text or not path:
        return None

    # Use an exact `diff --git` header match. We only support non-renamed paths for now.
    needle = f"diff --git a/{path} b/{path}"
    start = diff_text.find(needle)
    if start < 0:
        return None

    # The next file header begins on a new line.
    next_start = diff_text.find("\ndiff --git ", start + 1)
    chunk = diff_text[start:] if next_start < 0 else diff_text[start:next_start]
    return chunk.splitlines()


def unified_diff_position_to_anchor(
    *, file_patch_lines: list[str], position: int
) -> DiffAnchor | None:
    """
    Convert a 1-based unified diff line index (as exposed by Codeberg/Gitea review comments
    via `position`) into a GitHub-compatible (line, side) anchor.

    Important: This assumes the position is the 1-based index *within the file patch*,
    including the `diff --git`, `index`, `---`, `+++`, and `@@ ... @@` lines.
    """
    if not file_patch_lines:
        return None
    if not isinstance(position, int) or position <= 0 or position > len(file_patch_lines):
        return None

    old_line: int | None = None
    new_line: int | None = None

    for idx, raw in enumerate(file_patch_lines, start=1):
        # Hunk header sets the running old/new line numbers for subsequent hunk lines.
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if m:
                old_line = int(m.group(1))
                new_line = int(m.group(3))
            continue

        # We only know how to anchor once we're inside a hunk.
        if old_line is None or new_line is None:
            continue

        # `\ No newline at end of file` is not a real line in the file.
        if raw.startswith("\\ No newline"):
            continue

        is_target = idx == position
        if raw.startswith("+") and not raw.startswith("+++"):
            if is_target:
                return DiffAnchor(line=new_line, side="RIGHT")
            new_line += 1
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            if is_target:
                return DiffAnchor(line=old_line, side="LEFT")
            old_line += 1
            continue
        if raw.startswith(" "):
            if is_target:
                return DiffAnchor(line=new_line, side="RIGHT")
            old_line += 1
            new_line += 1
            continue

        # Any other patch line type (should be rare inside hunks).
        if is_target:
            return None

    return None

