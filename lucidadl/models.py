"""Tiny shared data contracts used across the CLI and download core."""

from __future__ import annotations

from typing import NamedTuple


class FailedItem(NamedTuple):
    """Retryable work, with optional playlist placement context.

    The first two fields retain the historical ``(kind, item)`` shape. New playlist
    failures also carry their collection and original track number so a retry does not
    fall back into Artists/ or renumber a partial playlist from 01.
    """

    kind: str
    item: str
    collection: str = ""
    track_no: str = ""
