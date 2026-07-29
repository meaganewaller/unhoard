import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from unhoard.schema import Item
from unhoard.state import StateStore


def test_upsert_items_inserts_new_item(state_store: StateStore, make_item: Callable[..., Item]) -> None:
    item = make_item(source_id="1", title="First", tags=["a", "b"])
    seen = state_store.upsert_items([item])

    assert seen == {"test:1"}
    rows = state_store.active_items()
    assert len(rows) == 1
    assert rows[0]["title"] == "First"
    assert json.loads(rows[0]["tags"]) == ["a", "b"]
    assert rows[0]["status"] == "active"


def test_upsert_items_updates_existing_item_in_place(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([make_item(source_id="1", title="Old title")])
    state_store.upsert_items([make_item(source_id="1", title="New title")])

    rows = state_store.active_items()
    assert len(rows) == 1
    assert rows[0]["title"] == "New title"


def test_mark_missing_as_done_closes_out_unsynced_items(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([
        make_item(source="raindrop", source_id="1"),
        make_item(source="raindrop", source_id="2"),
    ])

    closed = state_store.mark_missing_as_done("raindrop", seen_keys={"raindrop:1"})

    assert closed == 1
    rows = {r["key"]: r for r in state_store.find_by_prefix("raindrop:")}
    assert rows["raindrop:1"]["status"] == "active"
    assert rows["raindrop:2"]["status"] == "done"
    assert rows["raindrop:2"]["status_reason"] == "removed from source"


def test_active_items_excludes_snoozed_until_a_future_date(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.mark_snoozed("test:1", until=date.today() + timedelta(days=7))

    assert state_store.active_items() == []


def test_active_items_includes_snoozed_once_due(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.mark_snoozed("test:1", until=date.today() - timedelta(days=1))

    rows = state_store.active_items()
    assert [r["key"] for r in rows] == ["test:1"]


def test_mark_done(state_store: StateStore, make_item: Callable[..., Item]) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.mark_done("test:1", reason="handled manually")

    row = state_store.find_by_prefix("test:1")[0]
    assert row["status"] == "done"
    assert row["status_reason"] == "handled manually"
    assert state_store.active_items() == []


def test_mark_unhoarded(state_store: StateStore, make_item: Callable[..., Item]) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.mark_unhoarded("test:1", note="folded into a retrospective page")

    row = state_store.find_by_prefix("test:1")[0]
    assert row["status"] == "unhoarded"
    assert row["note"] == "folded into a retrospective page"


def test_mark_applied_sets_only_requested_fields(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.mark_applied("test:1", tags=True)

    row = state_store.find_by_prefix("test:1")[0]
    assert row["tags_applied_at"] is not None
    assert row["collection_applied_at"] is None
    assert row["summary_applied_at"] is None


def test_mark_applied_with_no_flags_is_a_safe_no_op(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.mark_applied("test:1")  # must not raise on an empty SET clause

    row = state_store.find_by_prefix("test:1")[0]
    assert row["tags_applied_at"] is None
    assert row["collection_applied_at"] is None
    assert row["summary_applied_at"] is None


def test_mark_synthesized(state_store: StateStore, make_item: Callable[..., Item]) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.mark_synthesized("test:1")

    row = state_store.find_by_prefix("test:1")[0]
    assert row["synthesized_at"] is not None


def test_record_shown_increments_across_calls(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.record_shown(["test:1"])
    state_store.record_shown(["test:1"])

    row = state_store.find_by_prefix("test:1")[0]
    assert row["times_shown"] == 2
    assert row["last_shown_date"] == date.today().isoformat()


def test_save_summary(state_store: StateStore, make_item: Callable[..., Item]) -> None:
    state_store.upsert_items([make_item(source_id="1")])
    state_store.save_summary(
        "test:1", summary="A summary.", model="claude-sonnet-5",
        content_hash="abc123", context_hash="def456",
        suggested_tags='["geocities"]', suggested_collection="Old Web Archive",
    )

    row = state_store.find_by_prefix("test:1")[0]
    assert row["summary"] == "A summary."
    assert row["content_hash"] == "abc123"
    assert row["context_hash"] == "def456"
    assert row["suggested_tags"] == '["geocities"]'
    assert row["suggested_collection"] == "Old Web Archive"


def test_stats_counts_by_status_and_active_by_source(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([
        make_item(source="raindrop", source_id="1"),
        make_item(source="raindrop", source_id="2"),
        make_item(source="chrome", source_id="1"),
    ])
    state_store.mark_done("raindrop:2")

    stats = state_store.stats()

    assert stats["by_status"] == {"active": 2, "done": 1}
    assert stats["active_by_source"] == {"raindrop": 1, "chrome": 1}


def test_find_by_prefix_matches_and_empty(
    state_store: StateStore, make_item: Callable[..., Item]
) -> None:
    state_store.upsert_items([make_item(source="raindrop", source_id="123")])

    assert [r["key"] for r in state_store.find_by_prefix("raindrop:1")] == ["raindrop:123"]
    assert state_store.find_by_prefix("nonexistent:") == []


def test_migrate_adds_columns_to_a_pre_existing_older_schema(tmp_path: Path) -> None:
    """Simulates opening a state DB created by an older unhoard version that
    predates the _MIGRATION_COLUMNS -- StateStore.__init__ should patch the
    table in place rather than erroring."""
    db_path = tmp_path / "old-state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE items (
            key TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
            title TEXT, url TEXT, tags TEXT, excerpt TEXT, collection TEXT,
            created_at TEXT, first_seen_local TEXT, last_synced TEXT,
            times_shown INTEGER DEFAULT 0, last_shown_date TEXT,
            status TEXT DEFAULT 'active', status_reason TEXT, snooze_until TEXT,
            summary TEXT, summary_model TEXT, summary_date TEXT
        )"""
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)  # must not raise

    cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(items)")}
    assert {"context_hash", "note", "suggested_tags", "synthesized_at"} <= cols
