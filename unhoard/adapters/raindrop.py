"""Raindrop.io adapter -- pulls a collection via the REST API."""
from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional

import requests

from ..schema import Item

API_BASE = "https://api.raindrop.io/rest/v1"
PAGE_SIZE = 50


class RaindropError(RuntimeError):
    pass


class RaindropAdapter:
    name = "raindrop"

    def __init__(
        self,
        token: str,
        collection_id: int = 0,
        unhoarded_tag: str = "unhoarded",
        unhoarded_collection_id: Optional[int] = None,
    ) -> None:
        if not token:
            raise RaindropError(
                "No Raindrop token configured. Set RAINDROP_TOKEN (get one at "
                "https://app.raindrop.io/settings/integrations -> 'For Developers' -> "
                "'Create test token')."
            )
        self.collection_id = collection_id
        self.unhoarded_tag = unhoarded_tag
        self.unhoarded_collection_id = unhoarded_collection_id
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def whoami(self) -> dict[str, Any]:
        resp = self.session.get(f"{API_BASE}/user", timeout=15)
        resp.raise_for_status()
        return resp.json()

    def fetch(self) -> Iterator[Item]:
        page = 0
        while True:
            resp = self.session.get(
                f"{API_BASE}/raindrops/{self.collection_id}",
                params={"perpage": PAGE_SIZE, "page": page, "sort": "created"},
                timeout=30,
            )
            if resp.status_code == 401:
                raise RaindropError("Raindrop rejected the token (401). Check RAINDROP_TOKEN.")
            resp.raise_for_status()
            data = resp.json()
            raw_items = data.get("items", [])
            if not raw_items:
                break
            for raw in raw_items:
                yield Item(
                    source="raindrop",
                    source_id=str(raw.get("_id")),
                    title=raw.get("title") or raw.get("link", "(untitled)"),
                    url=raw.get("link", ""),
                    tags=raw.get("tags", []) or [],
                    excerpt=raw.get("excerpt", "") or "",
                    created_at=Item.parse_dt(raw.get("created")),
                    collection=str(raw.get("collectionId", "")),
                )
            if len(raw_items) < PAGE_SIZE:
                break
            page += 1

    def mark_unhoarded(self, source_id: str, note: Optional[str] = None) -> None:
        """Thin wrapper over apply_updates() using this adapter's configured
        unhoarded tag/collection."""
        self.apply_updates(
            source_id, tags={self.unhoarded_tag}, collection_id=self.unhoarded_collection_id, note=note
        )

    def apply_updates(
        self,
        source_id: str,
        tags: Optional[Iterable[str]] = None,
        collection_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> None:
        """Best-effort partial update to a Raindrop item, independent of any
        local 'unhoarded' state. `tags`, if given, is merged into the item's
        existing tags (not replaced -- Raindrop's PUT overwrites whatever tag
        list you send). `collection_id` moves the item. `note` overwrites the
        item's single note field -- Raindrop has no concept of multiple notes,
        so whichever caller writes last wins. Any of the three can be omitted
        to leave that aspect untouched. Raises on failure; the caller decides
        how to treat that (best-effort enrichment, not a hard requirement)."""
        body: dict[str, Any] = {}
        if tags:
            get_resp = self.session.get(f"{API_BASE}/raindrop/{source_id}", timeout=15)
            get_resp.raise_for_status()
            current_tags = set(get_resp.json().get("item", {}).get("tags", []) or [])
            current_tags.update(tags)
            body["tags"] = sorted(current_tags)
        if collection_id is not None:
            body["collection"] = {"$id": collection_id}
        if note is not None:
            body["note"] = note
        if not body:
            return

        put_resp = self.session.put(f"{API_BASE}/raindrop/{source_id}", json=body, timeout=15)
        put_resp.raise_for_status()

    def list_collections(self) -> list[dict[str, Any]]:
        """Returns [{'id': int, 'title': str}, ...] for every collection in the
        account (top-level and nested children), used to ground AI suggestions
        in real collections and to resolve a suggested name back to the id
        Raindrop's write API needs."""
        collections: list[dict[str, Any]] = []
        for endpoint in ("collections", "collections/childrens"):
            resp = self.session.get(f"{API_BASE}/{endpoint}", timeout=15)
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                collections.append({"id": item.get("_id"), "title": item.get("title", "")})
        return collections

    def create_collection(self, title: str) -> int:
        """Create a new top-level collection on Raindrop.

        Args:
            title: Name of the collection to create.

        Returns:
            The ID of the newly created collection.

        Raises:
            RaindropError: If the collection creation fails.
        """
        body = {"title": title}
        resp = self.session.post(f"{API_BASE}/collection", json=body, timeout=15)
        try:
            resp.raise_for_status()
            data = resp.json()
            collection_id = data.get("collection", {}).get("_id")
            if collection_id is None:
                raise RaindropError(f"No collection ID in response: {data}")
            return collection_id
        except requests.exceptions.RequestException as e:
            raise RaindropError(f"Failed to create collection '{title}': {e}")

