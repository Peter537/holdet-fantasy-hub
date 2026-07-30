"""Shared filename components for scraper output files."""

from __future__ import annotations

from datetime import datetime


def compact_local_timestamp(now: datetime) -> str:
    """Return a local MMDD_HHMMSS timestamp for an output filename."""

    local_now = now.astimezone() if now.tzinfo is not None else now
    return local_now.strftime("%m%d_%H%M%S")


def round_output_stem(prefix: str, round_number: int, now: datetime) -> str:
    """Build the common prefix-roundN_MMDD_HHMMSS output stem."""

    return f"{prefix}-round{round_number}_{compact_local_timestamp(now)}"


def collision_suffix(collision_number: int) -> str:
    """Return no suffix for the original name, then _1, _2, and so on."""

    return "" if collision_number == 0 else f"_{collision_number}"

