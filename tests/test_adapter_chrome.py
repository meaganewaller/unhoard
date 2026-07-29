import json
from pathlib import Path

import pytest

from unhoard.adapters.chrome import ChromeAdapter, _default_profile_dir


def _write_bookmarks(profile_dir: Path, roots: dict) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Bookmarks").write_text(json.dumps({"roots": roots}))


def _write_reading_list(profile_dir: Path, reading_list) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Reading List").write_text(json.dumps({"roots": {"reading_list": reading_list}}))


def _url_node(**overrides) -> dict:
    node = {"type": "url", "name": "Example", "url": "https://example.com", "id": "1", "date_added": None}
    node.update(overrides)
    return node


def _folder_node(name: str, children: list) -> dict:
    return {"type": "folder", "name": name, "children": children}


def test_fetch_with_no_profile_dir_warns_and_yields_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    adapter = ChromeAdapter(profile_dir=str(tmp_path / "does-not-exist"))
    assert list(adapter.fetch()) == []
    assert "no profile directory found" in capsys.readouterr().err


def test_fetch_bookmarks_walks_nested_folders(tmp_path: Path) -> None:
    _write_bookmarks(tmp_path, {
        "bookmark_bar": _folder_node("Bookmarks bar", [
            _folder_node("Old Web", [
                _url_node(name="CSS Tricks", url="https://example.com/css", id="10"),
            ]),
        ]),
    })

    items = list(ChromeAdapter(profile_dir=str(tmp_path)).fetch())

    assert len(items) == 1
    item = items[0]
    assert item.title == "CSS Tricks"
    assert item.url == "https://example.com/css"
    assert item.source_id == "10"
    assert item.tags == ["Bookmarks bar", "Old Web"]
    assert item.collection == "Old Web"


def test_fetch_bookmarks_missing_file_yields_nothing(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    assert list(ChromeAdapter(profile_dir=str(tmp_path)).fetch()) == []


def test_fetch_bookmarks_malformed_json_warns_and_yields_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "Bookmarks").write_text("{not valid json")

    assert list(ChromeAdapter(profile_dir=str(tmp_path)).fetch()) == []
    assert "couldn't read" in capsys.readouterr().err


def test_source_id_falls_back_to_guid_then_url(tmp_path: Path) -> None:
    _write_bookmarks(tmp_path, {
        "bookmark_bar": _folder_node("Bar", [
            _url_node(id=None, guid="abc-guid", url="https://example.com/a"),
            _url_node(id=None, guid=None, url="https://example.com/b"),
        ]),
    })

    items = list(ChromeAdapter(profile_dir=str(tmp_path)).fetch())

    assert {i.source_id for i in items} == {"abc-guid", "https://example.com/b"}


def test_fetch_reading_list_entries(tmp_path: Path) -> None:
    _write_reading_list(tmp_path, [
        {"url": "https://example.com/read-later", "title": "Read Later", "creation_time": None},
    ])

    items = list(ChromeAdapter(profile_dir=str(tmp_path)).fetch())

    assert len(items) == 1
    assert items[0].tags == ["reading-list"]
    assert items[0].collection == "Reading List"
    assert items[0].source_id == "https://example.com/read-later"


def test_fetch_reading_list_nested_under_children_key(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "Reading List").write_text(json.dumps({
        "roots": {"reading_list": {"children": [{"url": "https://example.com/x", "title": "X"}]}}
    }))

    items = list(ChromeAdapter(profile_dir=str(tmp_path)).fetch())

    assert [i.url for i in items] == ["https://example.com/x"]


def test_fetch_reading_list_skips_non_dict_entries(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "Reading List").write_text(json.dumps({"roots": {"reading_list": ["not-a-dict", None]}}))

    assert list(ChromeAdapter(profile_dir=str(tmp_path)).fetch()) == []


def test_fetch_combines_bookmarks_and_reading_list(tmp_path: Path) -> None:
    _write_bookmarks(tmp_path, {"bookmark_bar": _folder_node("Bar", [_url_node()])})
    (tmp_path / "Reading List").write_text(json.dumps({
        "roots": {"reading_list": [{"url": "https://example.com/rl", "title": "RL"}]}
    }))

    items = list(ChromeAdapter(profile_dir=str(tmp_path)).fetch())

    assert {i.url for i in items} == {"https://example.com", "https://example.com/rl"}


def test_default_profile_dir_prefers_default_subdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unhoard.adapters.chrome.platform.system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    base = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    (base / "Default").mkdir(parents=True)
    (base / "Profile 1").mkdir(parents=True)

    assert _default_profile_dir() == base / "Default"


def test_default_profile_dir_falls_back_to_first_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unhoard.adapters.chrome.platform.system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    base = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    (base / "Profile 2").mkdir(parents=True)
    (base / "Profile 1").mkdir(parents=True)

    assert _default_profile_dir() == base / "Profile 1"


def test_default_profile_dir_returns_none_when_base_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unhoard.adapters.chrome.platform.system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _default_profile_dir() is None


def test_default_profile_dir_returns_none_when_no_profiles_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("unhoard.adapters.chrome.platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "google-chrome").mkdir(parents=True)

    assert _default_profile_dir() is None
