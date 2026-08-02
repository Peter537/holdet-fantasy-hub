"""Shared formatting helpers for Danish user-facing text."""

from __future__ import annotations


def count_label(count: int, singular: str, plural: str) -> str:
    """Format a count with its Danish singular or plural noun form."""

    noun = singular if count == 1 else plural
    return f"{count} {noun}"
