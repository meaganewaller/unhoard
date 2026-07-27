"""Generic JSON adapter -- the escape hatch. If a tool can export/expose a JSON
list of items, this adapter can probably ingest it without writing new code:
just point it at the file/URL and, if the field names aren't already obvious,
give it a small mapping.

Example field_map: {"title": "name", "url": "link", "created_at": "saved_at"}
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterator, Optional

import requests

from ..schema import Item

# Common aliases we'll guess from, in priority order, if no explicit mapping is given.
_GUESSES = {
    "title": ["title", "name", "text"],
    "url": ["url", "link", "href", "uri"],
    "id": ["id", "_id", "uid", "guid"],
    "tags": ["tags", "labels", "categories"],
    "created_at": ["created_at", "created", "date", "date_added", "timestamp", "saved_at"],
    "excerpt": ["excerpt", "description", "summary", "note", "notes"],
    "collection": ["collection", "folder", "list", "category"],
}


def _get_nested(data, dotted_path: str):
    node = data
    for part in dotted_path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _guess_field(record: dict, field: str, override: Optional[str]):
    if override:
        return record.get(override)
    for candidate in _GUESSES.get(field, []):
        if candidate in record:
            return record[candidate]
    return None


class GenericJSONAdapter:
    name = "json"

    def __init__(
        self,
        source_path: str,
        field_map: Optional[dict] = None,
        records_path: Optional[str] = None,
        source_name: Optional[str] = None,
    ):
        self.source_path = source_path
        self.field_map = field_map or {}
        self.records_path = records_path
        label = source_name or Path(source_path).stem or "json"
        self.source_label = f"json:{label}"

    def _load_raw(self):
        if self.source_path.startswith("http://") or self.source_path.startswith("https://"):
            resp = requests.get(self.source_path, timeout=30)
            resp.raise_for_status()
            return resp.json()
        return json.loads(Path(self.source_path).expanduser().read_text())

    def fetch(self) -> Iterator[Item]:
        data = self._load_raw()
        records = _get_nested(data, self.records_path) if self.records_path else data

        if isinstance(records, dict):
            # Take the first list-valued key as a best guess if not already a list.
            for value in records.values():
                if isinstance(value, list):
                    records = value
                    break

        if not isinstance(records, list):
            raise ValueError(
                f"Couldn't find a list of records in {self.source_path}. "
                "Pass records_path to point at the right key (e.g. 'data.items')."
            )

        for i, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            url = _guess_field(record, "url", self.field_map.get("url")) or ""
            title = _guess_field(record, "title", self.field_map.get("title")) or url or "(untitled)"
            raw_id = _guess_field(record, "id", self.field_map.get("id"))
            if raw_id is None:
                raw_id = hashlib.sha1(f"{title}{url}".encode()).hexdigest()[:16]
            tags = _guess_field(record, "tags", self.field_map.get("tags")) or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            created_raw = _guess_field(record, "created_at", self.field_map.get("created_at"))
            excerpt = _guess_field(record, "excerpt", self.field_map.get("excerpt")) or ""
            collection = _guess_field(record, "collection", self.field_map.get("collection")) or ""

            yield Item(
                source=self.source_label,
                source_id=str(raw_id),
                title=str(title),
                url=str(url),
                tags=list(tags),
                excerpt=str(excerpt),
                created_at=Item.parse_dt(created_raw),
                collection=str(collection),
            )

