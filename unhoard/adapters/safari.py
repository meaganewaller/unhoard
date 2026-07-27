"""Safari adapter -- reads ~/Library/Safari/Bookmarks.plist directly (macOS only).
Safari's Reading List is just a special folder ("com.apple.ReadingList") inside this
same plist, so one file covers both bookmarks and reading list.
"""
from __future__ import annotations

import plistlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from ..schema import Item

DEFAULT_PATH = Path.home() / "Library" / "Safari" / "Bookmarks.plist"


class SafariAdapter:
    name = "safari"

    def __init__(self, plist_path: Optional[str] = None):
        self.plist_path = Path(plist_path).expanduser() if plist_path else DEFAULT_PATH

    def _walk(self, node: dict, folder_path: list[str]) -> Iterator[Item]:
        node_type = node.get("WebBookmarkType")
        title = node.get("Title") or (node.get("URIDictionary") or {}).get("title", "")

        if node_type == "WebBookmarkTypeList":
            children = node.get("Children", [])
            next_path = folder_path + ([title] if title else [])
            for child in children:
                if isinstance(child, dict):
                    yield from self._walk(child, next_path)

        elif node_type == "WebBookmarkTypeLeaf":
            url = node.get("URLString", "")
            if not url:
                return
            reading_list = node.get("ReadingList")
            tags = list(folder_path)
            created_at = None
            excerpt = ""
            if isinstance(reading_list, dict):
                tags = tags + ["reading-list"]
                excerpt = reading_list.get("PreviewText", "") or ""
                date_added = reading_list.get("DateAdded")
                if isinstance(date_added, datetime):
                    created_at = date_added if date_added.tzinfo else date_added.replace(tzinfo=timezone.utc)
            yield Item(
                source="safari",
                source_id=url,
                title=title or url,
                url=url,
                tags=tags,
                excerpt=excerpt,
                created_at=created_at or datetime.now(timezone.utc),
                collection=folder_path[-1] if folder_path else "",
            )

    def fetch(self) -> Iterator[Item]:
        if not self.plist_path.exists():
            print(
                f"[safari] {self.plist_path} not found (Safari bookmarks are only readable "
                "on macOS with Full Disk Access granted to your terminal).",
                file=sys.stderr,
            )
            return
        try:
            with open(self.plist_path, "rb") as f:
                data = plistlib.load(f)
        except (plistlib.InvalidFileException, OSError) as e:
            print(f"[safari] couldn't read {self.plist_path}: {e}", file=sys.stderr)
            return
        yield from self._walk(data, [])

