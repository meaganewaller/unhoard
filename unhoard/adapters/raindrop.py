"""Raindrop.io adapter -- pulls a collection via the REST API."""
from __future__ import annotations

from typing import Iterator, Optional

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
    ):
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

    def whoami(self) -> dict:
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
        """Adds self.unhoarded_tag to the raindrop's existing tags (merged, not
        replaced -- Raindrop's PUT overwrites whatever tag list you send) and
        moves it to self.unhoarded_collection_id if one is configured. Raises
        on failure; the caller decides how to treat that (best-effort enrichment,
        not a hard requirement)."""
        get_resp = self.session.get(f"{API_BASE}/raindrop/{source_id}", timeout=15)
        get_resp.raise_for_status()
        current_tags = set(get_resp.json().get("item", {}).get("tags", []) or [])
        current_tags.add(self.unhoarded_tag)

        body = {"tags": sorted(current_tags)}
        if note:
            body["note"] = note
        if self.unhoarded_collection_id is not None:
            body["collection"] = {"$id": self.unhoarded_collection_id}

        put_resp = self.session.put(f"{API_BASE}/raindrop/{source_id}", json=body, timeout=15)
        put_resp.raise_for_status()

