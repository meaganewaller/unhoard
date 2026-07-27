"""Chrome adapter -- reads the local 'Bookmarks' (and, if present, 'Reading List')
JSON files straight out of your Chrome profile directory. No API, no auth: Chrome
already writes these as plain JSON on disk.

Default profile paths per OS are guessed, but you can always point at an explicit
file with --path if you use a non-default profile (e.g. "Profile 1").
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Iterator, Optional

from ..schema import Item


def _default_profile_dir() -> Optional[Path]:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        base = home / "Library" / "Application Support" / "Google" / "Chrome"
    elif system == "Windows":
        base = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    else:  # Linux and friends
        base = home / ".config" / "google-chrome"
    if not base.exists():
        return None
    # Prefer "Default", else the first "Profile N" found.
    default = base / "Default"
    if default.exists():
        return default
    profiles = sorted(base.glob("Profile *"))
    return profiles[0] if profiles else None


class ChromeAdapter:
    name = "chrome"

    def __init__(self, profile_dir: Optional[str] = None):
        self.profile_dir = Path(profile_dir).expanduser() if profile_dir else _default_profile_dir()

    def _walk_bookmarks(self, node: dict, folder_path: list[str]) -> Iterator[Item]:
        node_type = node.get("type")
        if node_type == "folder":
            name = node.get("name", "")
            for child in node.get("children", []):
                yield from self._walk_bookmarks(child, folder_path + ([name] if name else []))
        elif node_type == "url":
            yield Item(
                source="chrome",
                source_id=str(node.get("id") or node.get("guid") or node.get("url")),
                title=node.get("name") or node.get("url", "(untitled)"),
                url=node.get("url", ""),
                tags=list(folder_path),
                excerpt="",
                created_at=Item.parse_dt(_safe_int(node.get("date_added"))),
                collection=folder_path[-1] if folder_path else "",
            )

    def _fetch_bookmarks_file(self) -> Iterator[Item]:
        bm_path = self.profile_dir / "Bookmarks"
        if not bm_path.exists():
            return
        try:
            data = json.loads(bm_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[chrome] couldn't read {bm_path}: {e}", file=sys.stderr)
            return
        for root in (data.get("roots") or {}).values():
            if isinstance(root, dict):
                yield from self._walk_bookmarks(root, [])

    def _fetch_reading_list_file(self) -> Iterator[Item]:
        # Chrome's on-disk format for the reading list has varied across versions;
        # this is best-effort and silently yields nothing if the shape doesn't match.
        rl_path = self.profile_dir / "Reading List"
        if not rl_path.exists():
            return
        try:
            data = json.loads(rl_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[chrome] couldn't read {rl_path}: {e}", file=sys.stderr)
            return
        entries = (data.get("roots") or {}).get("reading_list") or []
        if isinstance(entries, dict):
            entries = entries.get("children", [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            yield Item(
                source="chrome",
                source_id=str(entry.get("url", "")),
                title=entry.get("title") or entry.get("url", "(untitled)"),
                url=entry.get("url", ""),
                tags=["reading-list"],
                excerpt="",
                created_at=Item.parse_dt(_safe_int(entry.get("creation_time"))),
                collection="Reading List",
            )

    def fetch(self) -> Iterator[Item]:
        if not self.profile_dir or not self.profile_dir.exists():
            print(
                "[chrome] no profile directory found/configured -- pass profile_dir explicitly "
                "if Chrome isn't in its default install location.",
                file=sys.stderr,
            )
            return
        yield from self._fetch_bookmarks_file()
        yield from self._fetch_reading_list_file()


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

