"""Canonical item schema. Every adapter yields these, regardless of source."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class Item:
    source: str            # e.g. "raindrop", "chrome", "safari", "json:filename"
    source_id: str         # id stable within that source (string, so any source can supply one)
    title: str
    url: str
    tags: list[str] = field(default_factory=list)
    excerpt: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    collection: str = ""   # folder / collection / list name, if the source has one

    @property
    def key(self) -> str:
        """Composite key used in the state DB so sources never collide."""
        return f"{self.source}:{self.source_id}"

    @staticmethod
    def parse_dt(value: Any, default: Optional[datetime] = None) -> datetime:
        """Best-effort parse of a timestamp from very heterogeneous sources."""
        if value is None:
            return default or datetime.now(timezone.utc)
        if isinstance(value, (int, float)):
            # Chrome stores WebKit/Chrome epoch: microseconds since 1601-01-01.
            # Heuristic: > 10^15 -> chrome-epoch microseconds, > 10^12 -> ms since 1970, else seconds.
            if value > 10**15:
                epoch_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)
                from datetime import timedelta
                return epoch_1601 + timedelta(microseconds=value)
            if value > 10**12:
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return default or datetime.now(timezone.utc)
        return default or datetime.now(timezone.utc)

