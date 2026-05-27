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
    m = _DURATION_RE.match(value or "")
    if not m:
        raise ValueError(f"Invalid duration: {value!r} (expected e.g. '10m', '8h', '1d')")
    amount = int(m.group(1))
    unit = (m.group(2) or "h").lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * mult
    if seconds <= 0:
        raise ValueError(f"Duration must be > 0: {value!r}")
    return seconds
