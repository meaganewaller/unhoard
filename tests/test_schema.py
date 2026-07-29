from datetime import datetime, timedelta, timezone

from unhoard.schema import Item


def test_key_composes_source_and_source_id() -> None:
    item = Item(source="raindrop", source_id="12345", title="t", url="https://example.com")
    assert item.key == "raindrop:12345"


def test_defaults() -> None:
    before = datetime.now(timezone.utc)
    item = Item(source="chrome", source_id="1", title="t", url="https://example.com")
    after = datetime.now(timezone.utc)

    assert item.tags == []
    assert item.excerpt == ""
    assert item.collection == ""
    assert before <= item.created_at <= after


def test_tags_default_is_not_shared_between_instances() -> None:
    a = Item(source="chrome", source_id="1", title="t", url="https://example.com")
    b = Item(source="chrome", source_id="2", title="t", url="https://example.com")
    a.tags.append("mutated")
    assert b.tags == []


def test_parse_dt_none_returns_provided_default() -> None:
    default = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert Item.parse_dt(None, default=default) == default


def test_parse_dt_none_without_default_returns_now() -> None:
    before = datetime.now(timezone.utc)
    result = Item.parse_dt(None)
    after = datetime.now(timezone.utc)
    assert before <= result <= after


def test_parse_dt_plain_epoch_seconds() -> None:
    # 10 digits -- well below the ms/chrome-epoch thresholds.
    assert Item.parse_dt(1700000000) == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_parse_dt_epoch_milliseconds() -> None:
    # 13 digits (> 10**12) -- Chrome's Reading List and some JSON exports use ms.
    ms_value = 1700000000123
    assert Item.parse_dt(ms_value) == datetime.fromtimestamp(ms_value / 1000, tz=timezone.utc)


def test_parse_dt_chrome_epoch_microseconds() -> None:
    # 17 digits (> 10**15) -- Chrome's Bookmarks file uses microseconds since 1601-01-01.
    value = 13390000000000000
    expected = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value)
    assert Item.parse_dt(value) == expected


def test_parse_dt_float_uses_same_branch_as_int() -> None:
    assert Item.parse_dt(1700000000.0) == Item.parse_dt(1700000000)


def test_parse_dt_iso_string_with_offset() -> None:
    result = Item.parse_dt("2023-11-14T22:13:20+00:00")
    assert result == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_parse_dt_iso_string_with_z_suffix() -> None:
    result = Item.parse_dt("2023-11-14T22:13:20Z")
    assert result == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_parse_dt_invalid_string_returns_default() -> None:
    default = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert Item.parse_dt("not-a-timestamp", default=default) == default


def test_parse_dt_unsupported_type_returns_default() -> None:
    default = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert Item.parse_dt(["not", "a", "timestamp"], default=default) == default
