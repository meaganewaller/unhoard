import hashlib
import json
from pathlib import Path

import pytest
import responses

from unhoard.adapters.generic_json import GenericJSONAdapter, _get_nested, _guess_field


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data))


def test_fetch_guesses_common_field_names(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, [{"name": "Article", "link": "https://example.com/a", "tags": "a, b"}])

    items = list(GenericJSONAdapter(source_path=str(path)).fetch())

    assert len(items) == 1
    item = items[0]
    assert item.title == "Article"
    assert item.url == "https://example.com/a"
    assert item.tags == ["a", "b"]
    assert item.source == "json:export"


def test_fetch_uses_field_map_override(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, [{"headline": "Custom Title", "href": "https://example.com/b"}])

    items = list(GenericJSONAdapter(
        source_path=str(path), field_map={"title": "headline", "url": "href"},
    ).fetch())

    assert items[0].title == "Custom Title"
    assert items[0].url == "https://example.com/b"


def test_fetch_missing_id_falls_back_to_deterministic_hash(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, [{"title": "T", "url": "https://example.com/c"}])

    items = list(GenericJSONAdapter(source_path=str(path)).fetch())

    expected = hashlib.sha1("Thttps://example.com/c".encode()).hexdigest()[:16]
    assert items[0].source_id == expected


def test_fetch_records_path_resolves_nested_key(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, {"data": {"items": [{"title": "Nested", "url": "https://example.com/n"}]}})

    items = list(GenericJSONAdapter(source_path=str(path), records_path="data.items").fetch())

    assert [i.title for i in items] == ["Nested"]


def test_fetch_dict_response_uses_first_list_valued_key(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, {"meta": {"count": 1}, "results": [{"title": "R", "url": "https://example.com/r"}]})

    items = list(GenericJSONAdapter(source_path=str(path)).fetch())

    assert [i.title for i in items] == ["R"]


def test_fetch_raises_when_no_list_of_records_found(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, {"nothing": "list-shaped"})

    with pytest.raises(ValueError, match="Couldn't find a list of records"):
        list(GenericJSONAdapter(source_path=str(path)).fetch())


def test_fetch_skips_non_dict_records(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, ["not-a-dict", {"title": "Valid", "url": "https://example.com/v"}])

    items = list(GenericJSONAdapter(source_path=str(path)).fetch())

    assert [i.title for i in items] == ["Valid"]


def test_fetch_source_label_uses_source_name_when_given(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    _write_json(path, [{"title": "T", "url": "https://example.com"}])

    items = list(GenericJSONAdapter(source_path=str(path), source_name="pocket").fetch())

    assert items[0].source == "json:pocket"


@responses.activate
def test_fetch_from_http_url(tmp_path: Path) -> None:
    responses.add(
        responses.GET, "https://example.com/export.json",
        json=[{"title": "Remote", "url": "https://example.com/remote"}], status=200,
    )

    items = list(GenericJSONAdapter(source_path="https://example.com/export.json").fetch())

    assert [i.title for i in items] == ["Remote"]


def test_get_nested_returns_none_for_missing_path() -> None:
    assert _get_nested({"a": {"b": 1}}, "a.missing") is None
    assert _get_nested({"a": 1}, "a.b") is None
    assert _get_nested({"a": {"b": 2}}, "a.b") == 2


def test_guess_field_falls_through_on_empty_override() -> None:
    record = {"name": "guessed"}
    assert _guess_field(record, "title", override="") == "guessed"
    assert _guess_field(record, "title", override=None) == "guessed"


def test_guess_field_returns_none_when_nothing_matches() -> None:
    assert _guess_field({}, "title", override=None) is None
