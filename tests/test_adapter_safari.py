import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unhoard.adapters.safari import SafariAdapter


def _write_plist(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(data, f)


def _leaf(url: str, title: str = "", **extra) -> dict:
    node = {"WebBookmarkType": "WebBookmarkTypeLeaf", "URLString": url, "Title": title}
    node.update(extra)
    return node


def _folder(title: str, children: list) -> dict:
    return {"WebBookmarkType": "WebBookmarkTypeList", "Title": title, "Children": children}


def test_fetch_missing_plist_warns_and_yields_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    adapter = SafariAdapter(plist_path=str(tmp_path / "Bookmarks.plist"))
    assert list(adapter.fetch()) == []
    assert "not found" in capsys.readouterr().err


def test_fetch_corrupt_plist_warns_and_yields_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "Bookmarks.plist"
    path.write_bytes(b"not a real plist")

    assert list(SafariAdapter(plist_path=str(path)).fetch()) == []
    assert "couldn't read" in capsys.readouterr().err


def test_fetch_walks_nested_folders(tmp_path: Path) -> None:
    path = tmp_path / "Bookmarks.plist"
    _write_plist(path, _folder("root", [
        _folder("Old Web", [_leaf("https://example.com/css", "CSS Tricks")]),
    ]))

    items = list(SafariAdapter(plist_path=str(path)).fetch())

    assert len(items) == 1
    item = items[0]
    assert item.title == "CSS Tricks"
    assert item.url == "https://example.com/css"
    assert item.source_id == "https://example.com/css"
    assert item.tags == ["root", "Old Web"]
    assert item.collection == "Old Web"


def test_fetch_leaf_without_url_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "Bookmarks.plist"
    _write_plist(path, _folder("root", [_leaf("")]))

    assert list(SafariAdapter(plist_path=str(path)).fetch()) == []


def test_fetch_reading_list_entry_sets_excerpt_and_tag(tmp_path: Path) -> None:
    path = tmp_path / "Bookmarks.plist"
    added = datetime(2023, 5, 1, 12, 0, 0)  # naive -- exercises the tzinfo-attach branch
    _write_plist(path, _folder("root", [
        _leaf(
            "https://example.com/article", "Article",
            ReadingList={"PreviewText": "A preview.", "DateAdded": added},
        ),
    ]))

    items = list(SafariAdapter(plist_path=str(path)).fetch())

    assert len(items) == 1
    item = items[0]
    assert "reading-list" in item.tags
    assert item.excerpt == "A preview."
    assert item.created_at == added.replace(tzinfo=timezone.utc)


def test_fetch_reading_list_entry_preserves_existing_tzinfo(tmp_path: Path) -> None:
    path = tmp_path / "Bookmarks.plist"
    added = datetime(2023, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_plist(path, _folder("root", [
        _leaf("https://example.com/article", "Article", ReadingList={"DateAdded": added}),
    ]))

    items = list(SafariAdapter(plist_path=str(path)).fetch())

    assert items[0].created_at == added


def test_fetch_title_falls_back_to_url_dictionary_then_url(tmp_path: Path) -> None:
    path = tmp_path / "Bookmarks.plist"
    _write_plist(path, _folder("root", [
        {
            "WebBookmarkType": "WebBookmarkTypeLeaf",
            "URLString": "https://example.com/no-title",
            "URIDictionary": {"title": "From URI dict"},
        },
    ]))

    items = list(SafariAdapter(plist_path=str(path)).fetch())

    assert items[0].title == "From URI dict"
