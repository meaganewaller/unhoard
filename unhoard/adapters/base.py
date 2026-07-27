"""Base interface every source adapter implements."""
from __future__ import annotations

from typing import Iterator, Protocol

from ..schema import Item


class Adapter(Protocol):
    name: str

    def fetch(self) -> Iterator[Item]:
        """Yield Items. Never raises for a single bad record -- skip and warn instead."""
        ...

