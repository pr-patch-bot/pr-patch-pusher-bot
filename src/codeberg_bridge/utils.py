from __future__ import annotations

import hashlib
import hmac
import re


_SAFE = re.compile(r"[^a-zA-Z0-9._/-]+")


def sanitize_branch_component(value: str) -> str:
    value = value.strip().strip("/")
    value = _SAFE.sub("-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value[:200] if value else "unknown"


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)


def parse_duration_seconds(value: str) -> int:
    """
    Parses durations like "30s", "10m", "8h", "1d".
    If the unit is omitted (e.g. "1"), hours are assumed.
    """
    raw_value = value or ""
    match = _DURATION_RE.match(raw_value)
    if not match:
        raise ValueError(f"Invalid duration: {value!r} (expected e.g. '10m', '8h', '1d')")

    amount_str = match.group(1)
    unit_raw = match.group(2) or ""
    unit = unit_raw.strip().lower() if unit_raw else "h"

    try:
        amount = int(amount_str)
    except Exception as e:
        raise ValueError(f"Invalid duration amount: {value!r}") from e

    if unit == "s":
        seconds = amount
    elif unit == "m":
        seconds = amount * 60
    elif unit == "h":
        seconds = amount * 60 * 60
    elif unit == "d":
        seconds = amount * 60 * 60 * 24
    else:
        raise ValueError(f"Invalid duration unit: {value!r} (expected s/m/h/d)")

    if seconds <= 0:
        raise ValueError(f"Duration must be > 0: {value!r}")
    return int(seconds)
