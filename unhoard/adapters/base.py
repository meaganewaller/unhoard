"""Base interface every source adapter implements."""
from __future__ import annotations

from typing import Iterator, Optional, Protocol

from ..schema import Item


class Adapter(Protocol):
    name: str

    def fetch(self) -> Iterator[Item]:
        """Yield Items. Never raises for a single bad record -- skip and warn instead."""
        ...


class WritebackAdapter(Adapter, Protocol):
    """An adapter that can reflect an 'unhoarded' marker back to its source.

    Not every adapter implements this (local file/export sources like chrome,
    safari, and generic_json have nothing to write back to) -- check with
    hasattr(adapter, "mark_unhoarded") rather than assuming it's present.
    """

    def mark_unhoarded(self, source_id: str, note: Optional[str] = None) -> None:
        """Mark the item as unhoarded in the source app itself. Best-effort:
        raise on failure, callers should treat this as optional enrichment
        rather than a hard requirement."""
        ...

