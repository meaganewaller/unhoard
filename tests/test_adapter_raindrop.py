import json
from typing import Any

import pytest
import responses

from unhoard.adapters.raindrop import API_BASE, PAGE_SIZE, RaindropAdapter, RaindropError


def _raw_item(i: int) -> dict[str, Any]:
    return {
        "_id": i, "title": f"Item {i}", "link": f"https://example.com/{i}",
        "tags": ["t"], "excerpt": "e", "created": "2023-11-14T22:13:20Z", "collectionId": 7,
    }


def _last_request_body() -> dict[str, Any]:
    body = responses.calls[-1].request.body
    assert isinstance(body, (str, bytes))
    return json.loads(body)


def test_init_without_token_raises() -> None:
    with pytest.raises(RaindropError):
        RaindropAdapter(token="")


@responses.activate
def test_fetch_maps_fields() -> None:
    responses.add(responses.GET, f"{API_BASE}/raindrops/0", json={"items": [_raw_item(1)]}, status=200)

    items = list(RaindropAdapter(token="tok").fetch())

    assert len(items) == 1
    item = items[0]
    assert item.source == "raindrop"
    assert item.source_id == "1"
    assert item.title == "Item 1"
    assert item.url == "https://example.com/1"
    assert item.tags == ["t"]
    assert item.excerpt == "e"
    assert item.collection == "7"


@responses.activate
def test_fetch_title_falls_back_to_link() -> None:
    raw = _raw_item(1)
    raw["title"] = None
    responses.add(responses.GET, f"{API_BASE}/raindrops/0", json={"items": [raw]}, status=200)

    items = list(RaindropAdapter(token="tok").fetch())

    assert items[0].title == "https://example.com/1"


@responses.activate
def test_fetch_stops_on_empty_first_page() -> None:
    responses.add(responses.GET, f"{API_BASE}/raindrops/0", json={"items": []}, status=200)

    assert list(RaindropAdapter(token="tok").fetch()) == []
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_paginates_until_a_partial_page() -> None:
    full_page = [_raw_item(i) for i in range(PAGE_SIZE)]
    partial_page = [_raw_item(PAGE_SIZE)]
    responses.add(responses.GET, f"{API_BASE}/raindrops/0", json={"items": full_page}, status=200)
    responses.add(responses.GET, f"{API_BASE}/raindrops/0", json={"items": partial_page}, status=200)

    items = list(RaindropAdapter(token="tok").fetch())

    assert len(items) == PAGE_SIZE + 1
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_raises_on_401() -> None:
    responses.add(responses.GET, f"{API_BASE}/raindrops/0", json={}, status=401)

    with pytest.raises(RaindropError):
        list(RaindropAdapter(token="bad-token").fetch())


@responses.activate
def test_whoami() -> None:
    responses.add(responses.GET, f"{API_BASE}/user", json={"user": {"_id": 1}}, status=200)

    assert RaindropAdapter(token="tok").whoami() == {"user": {"_id": 1}}


@responses.activate
def test_apply_updates_merges_tags_rather_than_replacing() -> None:
    responses.add(responses.GET, f"{API_BASE}/raindrop/123", json={"item": {"tags": ["existing"]}}, status=200)
    responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

    RaindropAdapter(token="tok").apply_updates("123", tags=["new"])

    body = _last_request_body()
    assert body["tags"] == ["existing", "new"]


@responses.activate
def test_apply_updates_sets_collection_and_note() -> None:
    responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

    RaindropAdapter(token="tok").apply_updates("123", collection_id=42, note="a note")

    body = _last_request_body()
    assert body == {"collection": {"$id": 42}, "note": "a note"}


@responses.activate
def test_apply_updates_with_nothing_makes_no_request() -> None:
    RaindropAdapter(token="tok").apply_updates("123")

    assert len(responses.calls) == 0


@responses.activate
def test_mark_unhoarded_uses_configured_tag_and_collection() -> None:
    responses.add(responses.GET, f"{API_BASE}/raindrop/123", json={"item": {"tags": []}}, status=200)
    responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

    RaindropAdapter(token="tok", unhoarded_tag="folded-in", unhoarded_collection_id=99).mark_unhoarded("123")

    body = _last_request_body()
    assert body["tags"] == ["folded-in"]
    assert body["collection"] == {"$id": 99}


@responses.activate
def test_list_collections_aggregates_both_endpoints() -> None:
    responses.add(
        responses.GET, f"{API_BASE}/collections",
        json={"items": [{"_id": 1, "title": "Top level"}]}, status=200,
    )
    responses.add(
        responses.GET, f"{API_BASE}/collections/childrens",
        json={"items": [{"_id": 2, "title": "Nested"}]}, status=200,
    )

    collections = RaindropAdapter(token="tok").list_collections()

    assert collections == [{"id": 1, "title": "Top level"}, {"id": 2, "title": "Nested"}]


@responses.activate
def test_create_collection() -> None:
    responses.add(
        responses.POST, f"{API_BASE}/collection",
        json={"collection": {"_id": 99, "title": "New Collection"}}, status=201,
    )

    adapter = RaindropAdapter(token="tok")
    collection_id = adapter.create_collection("New Collection")

    assert collection_id == 99
    body = _last_request_body()
    assert body == {"title": "New Collection"}


@responses.activate
def test_create_collection_fails_on_missing_id() -> None:
    responses.add(
        responses.POST, f"{API_BASE}/collection",
        json={"collection": {}}, status=201,
    )

    adapter = RaindropAdapter(token="tok")
    with pytest.raises(RaindropError, match="No collection ID"):
        adapter.create_collection("New Collection")


@responses.activate
def test_create_collection_fails_on_http_error() -> None:
    responses.add(
        responses.POST, f"{API_BASE}/collection",
        json={"error": "Invalid request"}, status=400,
    )

    adapter = RaindropAdapter(token="tok")
    with pytest.raises(RaindropError, match="Failed to create collection"):
        adapter.create_collection("New Collection")
