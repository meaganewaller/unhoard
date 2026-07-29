"""Base interface every source adapter implements."""
from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional, Protocol, runtime_checkable

from ..schema import Item


class Adapter(Protocol):
    name: str

    def fetch(self) -> Iterator[Item]:
        """Yield Items. Never raises for a single bad record -- skip and warn instead."""
        ...


@runtime_checkable
class WritebackAdapter(Adapter, Protocol):
    """An adapter that can reflect state back to its source: an 'unhoarded'
    marker, or arbitrary tag/collection/note updates.

    Not every adapter implements this (local file/export sources like chrome,
    safari, and generic_json have nothing to write back to) -- check with
    isinstance(adapter, WritebackAdapter) rather than assuming it's present.
    """

    def mark_unhoarded(self, source_id: str, note: Optional[str] = None) -> None:
        """Mark the item as unhoarded in the source app itself. Best-effort:
        raise on failure, callers should treat this as optional enrichment
        rather than a hard requirement."""
        ...

    def apply_updates(
        self,
        source_id: str,
        tags: Optional[Iterable[str]] = None,
        collection_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> None:
        """Best-effort partial update (merge tags, move collection, and/or set
        note) independent of any local 'unhoarded' state. Any argument can be
        omitted to leave that aspect untouched."""
        ...

    def list_collections(self) -> list[dict[str, Any]]:
        """Returns [{'id': ..., 'title': str}, ...] for every collection this
        source has, used to ground suggestions and resolve names to ids."""
        ...

