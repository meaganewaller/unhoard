from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest
import responses

from unhoard import digest as digest_module
from unhoard.config import Config
from unhoard.digest import (
    _age_days,
    _fetch_collection_names,
    _load_suggested_tags,
    needs_fresh_summary,
    _processing_badge,
    _render_metadata_item,
    _suggestion_line,
    _tags_str,
    build_digest,
)
from unhoard.schema import Item
from unhoard.state import StateStore
from unhoard.summarize import context_hash


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _insert_row(state_store: StateStore, **fields: Any) -> sqlite3.Row:
    """Inserts a raw row into the real items table (bypassing upsert_items'
    Item-shaped API) so tests can exercise sqlite3.Row-typed helpers with
    column combinations (e.g. NULL tags) upsert_items never produces."""
    fields = {"key": "test:1", "source": "test", "source_id": "1", **fields}
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    state_store.conn.execute(
        f"INSERT INTO items ({columns}) VALUES ({placeholders})", list(fields.values())
    )
    state_store.conn.commit()
    row = state_store.conn.execute("SELECT * FROM items WHERE key=?", (fields["key"],)).fetchone()
    assert row is not None
    return row


class TestAgeDays:
    def test_naive_datetime_is_treated_as_utc(self) -> None:
        naive_iso = (datetime.now(timezone.utc) - timedelta(days=5)).replace(tzinfo=None).isoformat()
        assert _age_days(naive_iso) == 5

    def test_aware_datetime_is_used_as_is(self) -> None:
        aware_iso = _days_ago(3).isoformat()
        assert _age_days(aware_iso) == 3


class TestTagsStr:
    def test_valid_json_list_joined_with_commas(self, state_store: StateStore) -> None:
        row = _insert_row(state_store, tags=json.dumps(["a", "b"]))
        assert _tags_str(row) == "a, b"

    def test_none_tags_returns_empty_string(self, state_store: StateStore) -> None:
        row = _insert_row(state_store, tags=None)
        assert _tags_str(row) == ""

    def test_invalid_json_returns_empty_string(self, state_store: StateStore) -> None:
        row = _insert_row(state_store, tags="not json")
        assert _tags_str(row) == ""

    def test_empty_list_returns_empty_string(self, state_store: StateStore) -> None:
        row = _insert_row(state_store, tags="[]")
        assert _tags_str(row) == ""


class TestRenderMetadataItem:
    def test_includes_title_url_age_source_and_key(self, state_store: StateStore) -> None:
        row = _insert_row(
            state_store, key="chrome:1", title="A Post", url="https://x.example/1",
            source="chrome", tags="[]", excerpt="",
        )
        rendered = _render_metadata_item(row, age=12)
        assert "[A Post](https://x.example/1)" in rendered
        assert "12d old" in rendered
        assert "`chrome`" in rendered
        assert "key: `chrome:1`" in rendered

    def test_includes_tags_when_present(self, state_store: StateStore) -> None:
        row = _insert_row(
            state_store, key="chrome:1", title="A Post", url="https://x.example/1",
            source="chrome", tags=json.dumps(["a", "b"]), excerpt="",
        )
        assert "`a, b`" in _render_metadata_item(row, age=1)

    def test_omits_excerpt_block_when_no_excerpt(self, state_store: StateStore) -> None:
        row = _insert_row(
            state_store, key="chrome:1", title="A Post", url="https://x.example/1",
            source="chrome", tags="[]", excerpt="",
        )
        assert ">" not in _render_metadata_item(row, age=1)

    def test_includes_excerpt_block_when_present(self, state_store: StateStore) -> None:
        row = _insert_row(
            state_store, key="chrome:1", title="A Post", url="https://x.example/1",
            source="chrome", tags="[]", excerpt="a neat excerpt",
        )
        assert "> a neat excerpt" in _render_metadata_item(row, age=1)


class TestLoadSuggestedTags:
    def test_none_returns_empty_list(self) -> None:
        assert _load_suggested_tags(None) == []

    def test_empty_string_returns_empty_list(self) -> None:
        assert _load_suggested_tags("") == []

    def test_valid_json_returns_list(self) -> None:
        assert _load_suggested_tags(json.dumps(["a", "b"])) == ["a", "b"]

    def test_invalid_json_returns_empty_list(self) -> None:
        assert _load_suggested_tags("not json") == []


class TestSuggestionLine:
    def test_no_tags_or_collection_returns_empty_string(self, state_store: StateStore) -> None:
        row = _insert_row(state_store)
        assert _suggestion_line(row, [], "") == ""

    def test_unapplied_tags_are_included(self, state_store: StateStore) -> None:
        row = _insert_row(state_store)
        line = _suggestion_line(row, ["a", "b"], "")
        assert "tags: a, b" in line
        assert "apply with `unhoard apply <key>`" in line

    def test_already_applied_tags_are_excluded(self, state_store: StateStore) -> None:
        row = _insert_row(state_store, tags_applied_at="2026-01-01T00:00:00+00:00")
        assert _suggestion_line(row, ["a", "b"], "") == ""

    def test_unapplied_collection_is_included(self, state_store: StateStore) -> None:
        row = _insert_row(state_store)
        assert "collection: Reading" in _suggestion_line(row, [], "Reading")

    def test_already_applied_collection_is_excluded(self, state_store: StateStore) -> None:
        row = _insert_row(state_store, collection_applied_at="2026-01-01T00:00:00+00:00")
        assert _suggestion_line(row, [], "Reading") == ""

    def test_both_unapplied_are_joined_with_pipe(self, state_store: StateStore) -> None:
        row = _insert_row(state_store)
        line = _suggestion_line(row, ["a"], "Reading")
        assert "tags: a | collection: Reading" in line


class TestNeedsFreshSummary:
    def test_no_cached_summary_needs_fresh(self, cfg: Config, state_store: StateStore) -> None:
        row = _insert_row(state_store, summary=None, context_hash=None)
        assert needs_fresh_summary(cfg, row) is True

    def test_matching_context_hash_does_not_need_fresh(self, cfg: Config, state_store: StateStore) -> None:
        cfg.context = "some context"
        row = _insert_row(state_store, summary="cached", context_hash=context_hash(cfg.context))
        assert needs_fresh_summary(cfg, row) is False

    def test_mismatched_context_hash_needs_fresh(self, cfg: Config, state_store: StateStore) -> None:
        cfg.context = "new context"
        row = _insert_row(state_store, summary="cached", context_hash=context_hash("old context"))
        assert needs_fresh_summary(cfg, row) is True

    def test_synthesized_item_never_needs_fresh_even_with_mismatched_context(
        self, cfg: Config, state_store: StateStore
    ) -> None:
        cfg.context = "new context"
        row = _insert_row(
            state_store, summary=None, context_hash=None,
            synthesized_at="2026-01-01T00:00:00+00:00",
        )
        assert needs_fresh_summary(cfg, row) is False


class TestProcessingBadge:
    def test_freshly_synced_item_has_no_badge(self, state_store: StateStore) -> None:
        row = _insert_row(state_store)
        assert _processing_badge(row) == ""

    def test_acted_on_item_lists_which_actions_were_taken(self, state_store: StateStore) -> None:
        row = _insert_row(state_store, summary="a summary", suggested_tags=json.dumps(["a"]))
        badge = _processing_badge(row)
        assert "summarized" in badge
        assert "tagged" in badge
        assert "collected" not in badge

    def test_synthesized_item_shows_synthesized_badge_not_acted_on(self, state_store: StateStore) -> None:
        row = _insert_row(
            state_store, summary="a summary", suggested_tags=json.dumps(["a"]),
            synthesized_at="2026-01-01T00:00:00+00:00",
        )
        assert "synthesized" in _processing_badge(row)
        assert "tagged" not in _processing_badge(row)


class TestFetchCollectionNames:
    def test_no_configured_adapter_returns_empty_list(self, cfg: Config) -> None:
        assert _fetch_collection_names(cfg) == []

    @responses.activate
    def test_returns_titles_from_configured_raindrop_adapter(self, cfg: Config) -> None:
        cfg.raindrop_token = "tok"
        responses.add(
            responses.GET, "https://api.raindrop.io/rest/v1/collections",
            json={"items": [{"_id": 1, "title": "Reading"}]}, status=200,
        )
        responses.add(
            responses.GET, "https://api.raindrop.io/rest/v1/collections/childrens",
            json={"items": []}, status=200,
        )

        assert _fetch_collection_names(cfg) == ["Reading"]

    @responses.activate
    def test_adapter_failure_is_swallowed_and_returns_empty_list(self, cfg: Config) -> None:
        cfg.raindrop_token = "tok"
        responses.add(
            responses.GET, "https://api.raindrop.io/rest/v1/collections",
            json={}, status=500,
        )

        assert _fetch_collection_names(cfg) == []


ItemFactory = Callable[..., Item]


class TestBuildDigest:
    def test_no_active_items_shows_inbox_zero_message(self, cfg: Config, state_store: StateStore) -> None:
        markdown, filename = build_digest(cfg, state_store)

        assert "inbox zero" in markdown
        assert filename == f"digest-{datetime.now(timezone.utc).date().isoformat()}.md"

    def test_buckets_by_age_relative_to_cfg_thresholds(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        cfg.aging_days = 7
        cfg.stale_days = 30
        new_item = make_item(source_id="new", title="New Item", created_at=_days_ago(1))
        aging_item = make_item(source_id="aging", title="Aging Item", created_at=_days_ago(10))
        stale_item = make_item(source_id="stale", title="Stale Item", created_at=_days_ago(40))
        state_store.upsert_items([new_item, aging_item, stale_item])

        markdown, _ = build_digest(cfg, state_store)

        assert "## \U0001f195 New" in markdown
        assert "New Item" in markdown
        assert "## ⏳ Aging" in markdown
        assert "Aging Item" in markdown
        assert "## \U0001f578️ Stale backlog" in markdown
        assert "Stale Item" in markdown

    def test_boundary_ages_land_in_the_lower_bucket(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        cfg.aging_days = 7
        cfg.stale_days = 30
        exactly_aging_days = make_item(source_id="boundary", title="Boundary New", created_at=_days_ago(7))
        state_store.upsert_items([exactly_aging_days])

        markdown, _ = build_digest(cfg, state_store)

        assert "## \U0001f195 New" in markdown
        assert "## ⏳ Aging" not in markdown

    def test_caps_and_keeps_the_oldest_items_per_bucket(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        cfg.max_new = 2
        items = [
            make_item(source_id=str(days), title=f"Item {days}d", created_at=_days_ago(days))
            for days in (1, 3, 5)
        ]
        state_store.upsert_items(items)

        markdown, _ = build_digest(cfg, state_store)

        assert "Item 5d" in markdown
        assert "Item 3d" in markdown
        assert "Item 1d" not in markdown

    def test_records_shown_items(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        item = make_item(source_id="1", title="Shown Item", created_at=_days_ago(1))
        state_store.upsert_items([item])

        build_digest(cfg, state_store)

        row = state_store.conn.execute(
            "SELECT times_shown FROM items WHERE key=?", (item.key,)
        ).fetchone()
        assert row["times_shown"] == 1

    def test_stale_item_without_anthropic_enabled_falls_back_to_excerpt(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        cfg.anthropic_api_key = ""
        item = make_item(source_id="1", title="Stale Item", excerpt="a saved excerpt", created_at=_days_ago(40))
        state_store.upsert_items([item])

        markdown, _ = build_digest(cfg, state_store)

        assert "a saved excerpt" in markdown
        assert "AI summary unavailable" in markdown

    def test_stale_item_without_anthropic_or_excerpt_shows_placeholder(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        cfg.anthropic_api_key = ""
        item = make_item(source_id="1", title="Stale Item", created_at=_days_ago(40))
        state_store.upsert_items([item])

        markdown, _ = build_digest(cfg, state_store)

        assert "no summary or excerpt available" in markdown

    def test_stale_item_needing_summary_calls_ai_summarize_and_persists_result(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg.anthropic_api_key = "key"
        item = make_item(source_id="1", title="Stale Item", created_at=_days_ago(40))
        state_store.upsert_items([item])

        calls: list[dict[str, Any]] = []

        def _fake_ai_summarize(title: str, url: str, api_key: str, model: str, context: str,
                                collection_names: list[str], max_tokens: int) -> tuple[dict[str, Any], str]:
            calls.append({"title": title, "url": url})
            return (
                {
                    "summary": "Fake summary", "action": "Read",
                    "tags": ["ai-tag"], "collection": "AI Collection", "raw": "raw text",
                },
                "fakehash123",
            )

        monkeypatch.setattr(digest_module, "ai_summarize", _fake_ai_summarize)

        markdown, _ = build_digest(cfg, state_store)

        assert len(calls) == 1
        assert calls[0]["title"] == "Stale Item"
        assert "Fake summary **Action:** Read" in markdown
        assert "tags: ai-tag" in markdown
        assert "collection: AI Collection" in markdown

        row = state_store.conn.execute(
            "SELECT summary, suggested_tags, suggested_collection, content_hash, context_hash FROM items WHERE key=?",
            (item.key,),
        ).fetchone()
        assert row["summary"] == "Fake summary **Action:** Read"
        assert json.loads(row["suggested_tags"]) == ["ai-tag"]
        assert row["suggested_collection"] == "AI Collection"
        assert row["content_hash"] == "fakehash123"
        assert row["context_hash"] == context_hash(cfg.context)

    def test_stale_item_with_fresh_cached_summary_skips_ai_summarize(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg.anthropic_api_key = "key"
        cfg.context = "some context"
        item = make_item(source_id="1", title="Stale Item", created_at=_days_ago(40))
        state_store.upsert_items([item])
        state_store.save_summary(
            item.key, "already cached summary", cfg.model, "chash", context_hash(cfg.context),
        )

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("ai_summarize should not be called when the cache is still fresh")

        monkeypatch.setattr(digest_module, "ai_summarize", _boom)

        markdown, _ = build_digest(cfg, state_store)

        assert "already cached summary" in markdown
        # Acted on and cache-fresh -- belongs under "Ready to finish", not "Stale backlog".
        assert "## ✅ Ready to finish" in markdown
        assert "## \U0001f578️ Stale backlog" not in markdown

    def test_ready_items_are_not_capped_by_max_stale(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        """The whole point of splitting ready-to-finish items out of the stale
        bucket: max_stale bounds AI-analysis work, not how many already-done
        items you can plow through in one sitting."""
        cfg.max_stale = 2
        cfg.context = "ctx"
        items = [
            make_item(source_id=str(i), title=f"Ready {i}", created_at=_days_ago(40 + i))
            for i in range(4)
        ]
        state_store.upsert_items(items)
        for item in items:
            state_store.save_summary(item.key, f"summary for {item.key}", cfg.model, "h", context_hash(cfg.context))

        markdown, _ = build_digest(cfg, state_store)

        for item in items:
            assert item.title in markdown

    def test_max_stale_cap_prioritizes_items_needing_analysis_over_ready_ones(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        cfg.max_stale = 1
        cfg.anthropic_api_key = ""  # no real AI calls -- exercise the excerpt/placeholder fallback
        cfg.context = "ctx"
        ready_item = make_item(source_id="ready", title="Ready Item", created_at=_days_ago(50))
        needs_item = make_item(source_id="needs", title="Needs Item", created_at=_days_ago(40))
        state_store.upsert_items([ready_item, needs_item])
        state_store.save_summary(ready_item.key, "cached", cfg.model, "h", context_hash(cfg.context))

        markdown, _ = build_digest(cfg, state_store)

        assert "Ready Item" in markdown
        assert "Needs Item" in markdown

    def test_synthesized_item_is_not_resummarized_even_with_stale_context(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg.anthropic_api_key = "key"
        cfg.context = "new context"
        item = make_item(source_id="1", title="Synthesized Item", created_at=_days_ago(40))
        state_store.upsert_items([item])
        state_store.mark_synthesized(item.key)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("ai_summarize should not run for an already-synthesized item")

        monkeypatch.setattr(digest_module, "ai_summarize", _boom)

        markdown, _ = build_digest(cfg, state_store)

        assert "## ✅ Ready to finish" in markdown
        assert "Synthesized Item" in markdown
        assert "✨ synthesized" in markdown

    def test_suggestion_omitted_once_already_applied(
        self, cfg: Config, state_store: StateStore, make_item: ItemFactory
    ) -> None:
        cfg.anthropic_api_key = ""
        item = make_item(source_id="1", title="Stale Item", excerpt="excerpt text", created_at=_days_ago(40))
        state_store.upsert_items([item])
        state_store.save_summary(item.key, "", cfg.model, "", "", json.dumps(["a-tag"]), "A Collection")
        state_store.mark_applied(item.key, tags=True, collection=True)

        markdown, _ = build_digest(cfg, state_store)

        assert "suggested --" not in markdown


def test_summaries_use_fast_model(
    cfg: Config, state_store: StateStore, make_item: Callable[..., Item], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-item summarization is the other classification-shaped path, so it
    routes to fast_model rather than the taxonomy model."""
    from unhoard import digest as digest_module

    cfg.anthropic_api_key = "sk-test"
    item = make_item(source_id="1", title="Old", created_at=datetime.now(timezone.utc) - timedelta(days=90))
    state_store.upsert_items([item])
    row = state_store.active_items()[0]

    seen: dict[str, str] = {}

    def _fake_summarize(title, url, api_key, model, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen["model"] = model
        return {}, ""

    monkeypatch.setattr(digest_module, "ai_summarize", _fake_summarize)
    digest_module.ensure_ai_suggestions(cfg, state_store, row, [])

    assert seen["model"] == cfg.fast_model == "claude-haiku-4-5"
