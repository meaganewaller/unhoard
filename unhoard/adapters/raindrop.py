"""Raindrop.io adapter -- pulls a collection via the REST API."""
from __future__ import annotations

from typing import Iterator

import requests

from ..schema import Item

API_BASE = "https://api.raindrop.io/rest/v1"
PAGE_SIZE = 50


class RaindropError(RuntimeError):
    pass


class RaindropAdapter:
    name = "raindrop"

    def __init__(self, token: str, collection_id: int = 0):
        if not token:
            raise RaindropError(
                "No Raindrop token configured. Set RAINDROP_TOKEN (get one at "
                "https://app.raindrop.io/settings/integrations -> 'For Developers' -> "
                "'Create test token')."
            )
        self.collection_id = collection_id
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

