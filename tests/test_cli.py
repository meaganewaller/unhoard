from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import responses

from unhoard import cli as cli_module
from unhoard import digest as digest_module
from unhoard.adapters.raindrop import API_BASE
from unhoard.config import Config
from unhoard.schema import Item
from unhoard.state import StateStore

ItemFactory = Callable[..., Item]


def _days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return " ".join(_ANSI_RE.sub("", text).split())


@pytest.fixture
def capsys(capsys: pytest.CaptureFixture[str]) -> Any:
    """unhoard.ui's Console singletons detect color/width once at import time,
    before pytest's capsys has redirected stdout/stderr -- so captured CLI
    output keeps ANSI codes and Rich's terminal-width line-wrapping no matter
    what capsys does later. Strip both so substring assertions on messages
    aren't at the mercy of the color/width Rich happened to detect."""

    class _Plain:
        def readouterr(self) -> SimpleNamespace:
            result = capsys.readouterr()
            return SimpleNamespace(out=_plain(result.out), err=_plain(result.err))

    return _Plain()


@pytest.fixture
def store(cfg: Config) -> StateStore:
    """A StateStore opened at the same path cmd_* functions resolve via their
    own internal load_config() call, so seeding/inspecting here is visible to
    whatever connection a CLI invocation opens next."""
    return StateStore(cfg.state_db_path)


def _seed(store: StateStore, make_item: ItemFactory, **overrides: Any) -> Item:
    item = make_item(**overrides)
    store.upsert_items([item])
    return item


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data))


class TestMainEntrypoint:
    def test_no_command_prints_help_and_returns_1(
        self, isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_module.main([])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "usage" in captured.out.lower()


class TestInit:
    def test_writes_config_and_prints_next_steps(
        self, isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_module.main(["init"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert isolated_paths.config_path.exists()
        assert "Config written to" in captured.out
        assert "unhoard sync" in captured.out

    def test_force_overwrites_existing_config(self, isolated_paths: SimpleNamespace) -> None:
        cli_module.main(["init"])
        isolated_paths.config_path.write_text("# customized\n")

        cli_module.main(["init", "--force"])

        assert "customized" not in isolated_paths.config_path.read_text()


class TestSync:
    def test_json_source_flag_ingests_items(
        self, store: StateStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "export.json"
        _write_json(path, [{"title": "A", "url": "https://example.com/a"}])

        exit_code = cli_module.main(["sync", "--source", f"json:{path}"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Sync complete: 1 items processed across 1 source(s)." in captured.out
        row = store.conn.execute(
            "SELECT * FROM items WHERE source=?", (f"json:{path.stem}",)
        ).fetchone()
        assert row is not None
        assert row["title"] == "A"

    def test_no_sources_configured_errors(
        self, isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_module.main(["sync"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "no sources configured" in captured.err

    def test_unknown_source_type_errors(
        self, isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_module.main(["sync", "--source", "bogus"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "Unknown source type 'bogus'" in captured.err

    def test_adapter_fetch_failure_is_reported_and_sync_continues(
        self, isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_module.main(["sync", "--source", "json:/no/such/file.json"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "failed:" in captured.err
        assert "Sync complete: 0 items processed across 1 source(s)." in captured.out


class TestDigest:
    def test_writes_digest_and_latest_md(
        self, isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_module.main(["digest"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Digest written to" in captured.out
        digest_files = list(isolated_paths.output_dir.glob("digest-*.md"))
        assert len(digest_files) == 1
        assert "inbox zero" in digest_files[0].read_text()
        assert "inbox zero" in (isolated_paths.output_dir / "latest.md").read_text()

    def test_print_flag_also_renders_to_stdout(
        self, isolated_paths: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_module.main(["digest", "--print"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "inbox zero" in captured.out


class TestMark:
    def test_done_marks_item_done(
        self, store: StateStore, make_item: ItemFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        item = _seed(store, make_item, title="A")

        exit_code = cli_module.main(["mark", item.key, "--done"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Done: A" in captured.out
        row = store.conn.execute("SELECT status FROM items WHERE key=?", (item.key,)).fetchone()
        assert row["status"] == "done"

    def test_snooze_sets_status_and_until_date(self, store: StateStore, make_item: ItemFactory) -> None:
        item = _seed(store, make_item)

        cli_module.main(["mark", item.key, "--snooze", "3"])

        row = store.conn.execute(
            "SELECT status, snooze_until FROM items WHERE key=?", (item.key,)
        ).fetchone()
        assert row["status"] == "snoozed"
        assert row["snooze_until"] == (date.today() + timedelta(days=3)).isoformat()

    def test_unhoarded_without_note_warns(
        self, store: StateStore, make_item: ItemFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        item = _seed(store, make_item)

        exit_code = cli_module.main(["mark", item.key, "--unhoarded"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "consider recording" in captured.err
        row = store.conn.execute("SELECT status FROM items WHERE key=?", (item.key,)).fetchone()
        assert row["status"] == "unhoarded"

    def test_unhoarded_source_without_writeback_warns_local_state_only(
        self, store: StateStore, make_item: ItemFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        item = _seed(store, make_item, source="json:export")

        exit_code = cli_module.main(["mark", item.key, "--unhoarded", "--note", "used it"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "no write-back support for source 'json:export'" in captured.err

    @responses.activate
    def test_unhoarded_raindrop_source_writes_back(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")
        responses.add(responses.GET, f"{API_BASE}/raindrop/123", json={"item": {"tags": []}}, status=200)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

        exit_code = cli_module.main(["mark", item.key, "--unhoarded", "--note", "used it"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "also marked unhoarded in raindrop" in captured.out

    @responses.activate
    def test_unhoarded_writeback_failure_still_saves_local_state(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")
        responses.add(responses.GET, f"{API_BASE}/raindrop/123", json={}, status=500)

        exit_code = cli_module.main(["mark", item.key, "--unhoarded"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "couldn't write back to raindrop" in captured.err
        row = store.conn.execute("SELECT status FROM items WHERE key=?", (item.key,)).fetchone()
        assert row["status"] == "unhoarded"

    def test_no_action_flag_errors(
        self, store: StateStore, make_item: ItemFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        item = _seed(store, make_item)

        exit_code = cli_module.main(["mark", item.key])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "specify --done, --unhoarded, or --snooze N" in captured.err

    def test_no_match_errors(self, store: StateStore, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = cli_module.main(["mark", "nope", "--done"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "no item found matching" in captured.err

    def test_ambiguous_match_non_tty_lists_candidates_and_fails(
        self, store: StateStore, make_item: ItemFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(store, make_item, source_id="1", title="First")
        _seed(store, make_item, source_id="10", title="Second")

        exit_code = cli_module.main(["mark", "test:1", "--done"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "multiple items match 'test:1'" in captured.err
        assert "test:1" in captured.err
        assert "test:10" in captured.err

    def test_ambiguous_match_interactive_uses_picker_selection(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed(store, make_item, source_id="1", title="First")
        second = _seed(store, make_item, source_id="10", title="Second")
        picked_row = store.conn.execute("SELECT * FROM items WHERE key=?", (second.key,)).fetchone()
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
        monkeypatch.setattr(
            cli_module.questionary, "select", lambda *a, **k: SimpleNamespace(ask=lambda: picked_row)
        )

        exit_code = cli_module.main(["mark", "test:1", "--done"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Done: Second" in captured.out
        row = store.conn.execute("SELECT status FROM items WHERE key=?", (second.key,)).fetchone()
        assert row["status"] == "done"
        first_row = store.conn.execute(
            "SELECT status FROM items WHERE source='test' AND source_id='1'"
        ).fetchone()
        assert first_row["status"] == "active"

    def test_ambiguous_match_interactive_cancelled_picker_fails(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(store, make_item, source_id="1")
        _seed(store, make_item, source_id="10")
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
        monkeypatch.setattr(
            cli_module.questionary, "select", lambda *a, **k: SimpleNamespace(ask=lambda: None)
        )

        exit_code = cli_module.main(["mark", "test:1", "--done"])

        assert exit_code == 1


class TestApply:
    def test_no_flags_errors(self, store: StateStore, make_item: ItemFactory) -> None:
        item = _seed(store, make_item)

        exit_code = cli_module.main(["apply", item.key])

        assert exit_code == 1

    def test_source_without_writeback_errors(
        self, store: StateStore, make_item: ItemFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        item = _seed(store, make_item, source="json:export")

        exit_code = cli_module.main(["apply", item.key, "--all"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "no write-back support for source 'json:export'" in captured.err

    @responses.activate
    def test_tags_are_applied_and_recorded(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")
        store.save_summary(item.key, "", "m", "", "", suggested_tags=json.dumps(["a", "b"]))
        responses.add(responses.GET, f"{API_BASE}/raindrop/123", json={"item": {"tags": []}}, status=200)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

        exit_code = cli_module.main(["apply", item.key, "--tags"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "tags (a, b)" in captured.out
        row = store.conn.execute(
            "SELECT tags_applied_at FROM items WHERE key=?", (item.key,)
        ).fetchone()
        assert row["tags_applied_at"] is not None

    def test_tags_requested_but_none_suggested_warns(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")

        exit_code = cli_module.main(["apply", item.key, "--tags"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "no suggested tags for this item" in captured.err
        assert "nothing to apply" in captured.err

    @responses.activate
    def test_collection_is_resolved_by_title_and_applied(
        self, store: StateStore, make_item: ItemFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")
        store.save_summary(item.key, "", "m", "", "", suggested_collection="Reading")
        responses.add(
            responses.GET, f"{API_BASE}/collections",
            json={"items": [{"_id": 5, "title": "Reading"}]}, status=200,
        )
        responses.add(responses.GET, f"{API_BASE}/collections/childrens", json={"items": []}, status=200)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

        exit_code = cli_module.main(["apply", item.key, "--collection"])

        assert exit_code == 0
        request_body = responses.calls[-1].request.body
        assert isinstance(request_body, (str, bytes))
        body = json.loads(request_body)
        assert body == {"collection": {"$id": 5}}

    @responses.activate
    def test_suggested_collection_created_if_missing(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")
        store.save_summary(item.key, "", "m", "", "", suggested_collection="NewCollection")
        responses.add(responses.GET, f"{API_BASE}/collections", json={"items": []}, status=200)
        responses.add(responses.GET, f"{API_BASE}/collections/childrens", json={"items": []}, status=200)
        responses.add(
            responses.POST,
            f"{API_BASE}/collection",
            json={"result": True, "item": {"_id": 99, "title": "NewCollection"}},
            status=201,
        )
        responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

        exit_code = cli_module.main(["apply", item.key, "--collection"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Created collection 'NewCollection'" in captured.out
        assert "collection (NewCollection)" in captured.out

    @responses.activate
    def test_summary_is_applied_as_note(
        self, store: StateStore, make_item: ItemFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")
        store.save_summary(item.key, "a cached summary", "m", "", "")
        responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=200)

        exit_code = cli_module.main(["apply", item.key, "--summary"])

        assert exit_code == 0
        request_body = responses.calls[-1].request.body
        assert isinstance(request_body, (str, bytes))
        body = json.loads(request_body)
        assert body == {"note": "a cached summary"}

    @responses.activate
    def test_writeback_failure_reports_error_and_does_not_mark_applied(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="123")
        store.save_summary(item.key, "a cached summary", "m", "", "")
        responses.add(responses.PUT, f"{API_BASE}/raindrop/123", json={}, status=500)

        exit_code = cli_module.main(["apply", item.key, "--summary"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "couldn't apply to raindrop" in captured.err
        row = store.conn.execute(
            "SELECT summary_applied_at FROM items WHERE key=?", (item.key,)
        ).fetchone()
        assert row["summary_applied_at"] is None


class TestApplyAll:
    def test_no_flags_errors(self, store: StateStore) -> None:
        exit_code = cli_module.main(["apply-all"])

        assert exit_code == 1

    @responses.activate
    def test_processes_multiple_items_oldest_first_up_to_limit(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        old = _seed(store, make_item, source="raindrop", source_id="1", title="Old", created_at=_days_ago(40))
        mid = _seed(store, make_item, source="raindrop", source_id="2", title="Mid", created_at=_days_ago(20))
        new = _seed(store, make_item, source="raindrop", source_id="3", title="New", created_at=_days_ago(5))
        for key in (old.key, mid.key, new.key):
            store.save_summary(key, "", "m", "", "", suggested_tags=json.dumps(["a"]))
        responses.add(responses.GET, f"{API_BASE}/raindrop/1", json={"item": {"tags": []}}, status=200)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/1", json={}, status=200)
        responses.add(responses.GET, f"{API_BASE}/raindrop/2", json={"item": {"tags": []}}, status=200)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/2", json={}, status=200)

        exit_code = cli_module.main(["apply-all", "--tags", "--limit", "2"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Applied to 2 item(s)" in captured.out
        rows = {r["key"]: r["tags_applied_at"] for r in store.conn.execute("SELECT key, tags_applied_at FROM items")}
        assert rows[old.key] is not None
        assert rows[mid.key] is not None
        assert rows[new.key] is None  # beyond the limit -- never touched, no HTTP call registered for it either

    @responses.activate
    def test_item_with_nothing_left_to_apply_is_skipped_without_a_writeback_call(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        item = _seed(store, make_item, source="raindrop", source_id="1")
        store.save_summary(item.key, "", "m", "", "", suggested_tags=json.dumps(["a"]))
        store.mark_applied(item.key, tags=True)

        exit_code = cli_module.main(["apply-all", "--tags"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Applied to 0 item(s); 1 skipped" in captured.out

    @responses.activate
    def test_source_without_writeback_is_skipped_and_does_not_count_against_limit(
        self, store: StateStore, make_item: ItemFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        json_item = _seed(
            store, make_item, source="json:export", source_id="j1", title="Json Item", created_at=_days_ago(40)
        )
        store.save_summary(json_item.key, "", "m", "", "", suggested_tags=json.dumps(["a"]))
        rd_item = _seed(store, make_item, source="raindrop", source_id="1", title="RD Item", created_at=_days_ago(5))
        store.save_summary(rd_item.key, "", "m", "", "", suggested_tags=json.dumps(["b"]))
        responses.add(responses.GET, f"{API_BASE}/raindrop/1", json={"item": {"tags": []}}, status=200)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/1", json={}, status=200)

        exit_code = cli_module.main(["apply-all", "--tags", "--limit", "1"])

        assert exit_code == 0
        row = store.conn.execute("SELECT tags_applied_at FROM items WHERE key=?", (rd_item.key,)).fetchone()
        assert row["tags_applied_at"] is not None

    @responses.activate
    def test_generates_missing_suggestions_then_applies_all(
        self, store: StateStore, make_item: ItemFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        item = _seed(store, make_item, source="raindrop", source_id="1", title="Needs AI")

        def _fake_ai_summarize(
            title: str, url: str, api_key: str, model: str, context: str,
            collection_names: list[str], max_tokens: int,
        ) -> tuple[dict[str, Any], str]:
            return (
                {
                    "summary": "Fake summary", "action": "Read",
                    "tags": ["ai-tag"], "collection": "Reading", "raw": "raw text",
                },
                "chash123",
            )

        monkeypatch.setattr(digest_module, "ai_summarize", _fake_ai_summarize)
        responses.add(
            responses.GET, f"{API_BASE}/collections",
            json={"items": [{"_id": 5, "title": "Reading"}]}, status=200,
        )
        responses.add(responses.GET, f"{API_BASE}/collections/childrens", json={"items": []}, status=200)
        responses.add(responses.GET, f"{API_BASE}/raindrop/1", json={"item": {"tags": []}}, status=200)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/1", json={}, status=200)

        exit_code = cli_module.main(["apply-all", "--all"])

        assert exit_code == 0
        request_body = responses.calls[-1].request.body
        assert isinstance(request_body, (str, bytes))
        body = json.loads(request_body)
        assert body["tags"] == ["ai-tag"]
        assert body["collection"] == {"$id": 5}
        assert body["note"] == "Fake summary **Action:** Read"
        row = store.conn.execute(
            "SELECT suggested_tags, suggested_collection, summary FROM items WHERE key=?", (item.key,)
        ).fetchone()
        assert json.loads(row["suggested_tags"]) == ["ai-tag"]
        assert row["suggested_collection"] == "Reading"
        assert row["summary"] == "Fake summary **Action:** Read"

    @responses.activate
    def test_writeback_failure_is_reported_and_batch_continues(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("RAINDROP_TOKEN", "tok")
        failing = _seed(store, make_item, source="raindrop", source_id="1", title="Failing", created_at=_days_ago(40))
        store.save_summary(failing.key, "a cached summary", "m", "", "")
        ok = _seed(store, make_item, source="raindrop", source_id="2", title="Ok", created_at=_days_ago(5))
        store.save_summary(ok.key, "another summary", "m", "", "")
        responses.add(responses.PUT, f"{API_BASE}/raindrop/1", json={}, status=500)
        responses.add(responses.PUT, f"{API_BASE}/raindrop/2", json={}, status=200)

        exit_code = cli_module.main(["apply-all", "--summary"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "couldn't apply to Failing" in captured.err
        assert "Applied to 1 item(s); 1 skipped" in captured.out
        row = store.conn.execute(
            "SELECT summary_applied_at FROM items WHERE key=?", (failing.key,)
        ).fetchone()
        assert row["summary_applied_at"] is None


class TestSynthesize:
    def test_writes_note_with_frontmatter_and_body(
        self,
        isolated_paths: SimpleNamespace,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        item = _seed(store, make_item, title="A Great Post", url="https://x/1")
        monkeypatch.setattr(cli_module, "fetch_article_text", lambda url, **_: "the full article text")

        exit_code = cli_module.main(["synthesize", item.key])

        captured = capsys.readouterr()
        assert exit_code == 0
        out_path = isolated_paths.output_dir / "synthesized" / "a-great-post.md"
        assert out_path.exists()
        content = out_path.read_text()
        assert 'title: "A Great Post"' in content
        assert "the full article text" in content
        row = store.conn.execute(
            "SELECT synthesized_at FROM items WHERE key=?", (item.key,)
        ).fetchone()
        assert row["synthesized_at"] is not None

    def test_no_article_text_errors(
        self,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        item = _seed(store, make_item)
        monkeypatch.setattr(cli_module, "fetch_article_text", lambda url, **_: "")

        exit_code = cli_module.main(["synthesize", item.key])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "couldn't fetch article text" in captured.err

    def test_existing_note_is_not_overwritten_without_force(
        self,
        isolated_paths: SimpleNamespace,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        item = _seed(store, make_item, title="A Post")
        monkeypatch.setattr(cli_module, "fetch_article_text", lambda url, **_: "text")
        cli_module.main(["synthesize", item.key])

        exit_code = cli_module.main(["synthesize", item.key])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "already exists" in captured.err

    def test_force_overwrites_existing_note(
        self,
        isolated_paths: SimpleNamespace,
        store: StateStore,
        make_item: ItemFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        item = _seed(store, make_item, title="A Post")
        monkeypatch.setattr(cli_module, "fetch_article_text", lambda url, **_: "first version")
        cli_module.main(["synthesize", item.key])
        monkeypatch.setattr(cli_module, "fetch_article_text", lambda url, **_: "second version")

        exit_code = cli_module.main(["synthesize", item.key, "--force"])

        assert exit_code == 0
        out_path = isolated_paths.output_dir / "synthesized" / "a-post.md"
        assert "second version" in out_path.read_text()


class TestStats:
    def test_prints_status_and_source_breakdown(
        self, store: StateStore, make_item: ItemFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(store, make_item, source="raindrop", source_id="1")
        item2 = _seed(store, make_item, source="raindrop", source_id="2")
        store.mark_done(item2.key)

        exit_code = cli_module.main(["stats"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "active" in captured.out
        assert "done" in captured.out
        assert "raindrop" in captured.out
        assert "By processing state" in captured.out
        assert "synced" in captured.out
