"""Identifier helpers."""

from __future__ import annotations

import os
import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7): 48-bit ms timestamp + random bits."""
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ts_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= ((rand >> 68) & 0xFFF) << 64  # rand_a (12 bits)
    value |= 0b10 << 62  # variant
    value |= rand & ((1 << 62) - 1)  # rand_b (62 bits)
    return uuid.UUID(int=value)


def token_urlsafe(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
