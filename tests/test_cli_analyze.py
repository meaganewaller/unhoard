"""Integration tests for the `unhoard analyze` CLI command.

Tests the full pipeline: fetch untagged items → suggest collections →
review collections → suggest tags → review tags → bulk store suggestions.

Uses monkeypatching to isolate all LLM, DB, and interactive-review calls so
these tests run offline and deterministically.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from unhoard import cli as cli_module
from unhoard import state as state_module
from unhoard.schema import Item
from unhoard.types import CollectionSuggestion, TagSuggestion


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return " ".join(_ANSI_RE.sub("", text).split())


@pytest.fixture
def capsys(capsys: pytest.CaptureFixture[str]) -> Any:
    """Strip ANSI codes and normalize whitespace so assertions are stable."""
    class _Plain:
        def readouterr(self) -> SimpleNamespace:
            result = capsys.readouterr()
            return SimpleNamespace(out=_plain(result.out), err=_plain(result.err))

    return _Plain()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_items(n: int) -> list[Item]:
    return [
        Item(source="test", source_id=str(i), title=f"Item {i}", url=f"https://example.com/{i}")
        for i in range(1, n + 1)
    ]


def _make_collection_suggestions(items: list[Item]) -> list[CollectionSuggestion]:
    return [
        CollectionSuggestion(
            item_id=int(item.source_id),
            item_title=item.title,
            suggested_collection="Development",
            confidence="high",
        )
        for item in items
    ]


def _make_tag_suggestions(items: list[Item]) -> list[TagSuggestion]:
    return [
        TagSuggestion(
            item_id=int(item.source_id),
            item_title=item.title,
            use_case_tags=["reference"],
            status_tags=["reviewed"],
        )
        for item in items
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAnalyzeCommandEndToEnd:
    def test_analyze_command_end_to_end(
        self,
        isolated_paths: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test full analyze command flow: fetch → suggest → review → store."""
        items = _make_items(2)
        collection_suggestions = _make_collection_suggestions(items)
        tag_suggestions = _make_tag_suggestions(items)

        # Mock fetch_untagged_items on StateStore (called as store.fetch_untagged_items)
        monkeypatch.setattr(
            state_module.StateStore,
            "fetch_untagged_items",
            lambda self, limit: items,
        )

        # Mock LLM suggestion functions imported into cli
        monkeypatch.setattr(cli_module, "suggest_collections", lambda *a, **kw: collection_suggestions)
        monkeypatch.setattr(cli_module, "suggest_tags", lambda *a, **kw: tag_suggestions)

        # Mock interactive review functions — return suggestions unchanged
        monkeypatch.setattr(
            cli_module, "review_collections_interactive", lambda suggestions, **kw: suggestions
        )
        monkeypatch.setattr(
            cli_module, "review_tags_interactive", lambda suggestions, **kw: suggestions
        )

        # Mock bulk_store_suggestions on StateStore — no-op
        monkeypatch.setattr(
            state_module.StateStore,
            "bulk_store_suggestions",
            lambda self, *a, **kw: None,
        )

        # Ensure _is_interactive returns False so input() is never called
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)

        exit_code = cli_module.main(["analyze", "--items", "2"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Analyzing" in captured.out or "Fetching" in captured.out
        assert "2" in captured.out

    def test_analyze_handles_no_items(
        self,
        isolated_paths: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that analyze gracefully handles no untagged items."""
        monkeypatch.setattr(
            state_module.StateStore,
            "fetch_untagged_items",
            lambda self, limit: [],
        )

        exit_code = cli_module.main(["analyze"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "No untagged items found" in captured.err

    def test_analyze_auto_apply_skips_interactive_review(
        self,
        isolated_paths: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that --auto-apply bypasses the interactive review steps."""
        items = _make_items(2)
        collection_suggestions = _make_collection_suggestions(items)
        tag_suggestions = _make_tag_suggestions(items)

        monkeypatch.setattr(
            state_module.StateStore,
            "fetch_untagged_items",
            lambda self, limit: items,
        )
        monkeypatch.setattr(cli_module, "suggest_collections", lambda *a, **kw: collection_suggestions)
        monkeypatch.setattr(cli_module, "suggest_tags", lambda *a, **kw: tag_suggestions)
        monkeypatch.setattr(
            state_module.StateStore,
            "bulk_store_suggestions",
            lambda self, *a, **kw: None,
        )
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)

        # review_* should never be called under --auto-apply; raise if they are
        def _should_not_call(*a: Any, **kw: Any) -> Any:
            raise AssertionError("review function called under --auto-apply")

        monkeypatch.setattr(cli_module, "review_collections_interactive", _should_not_call)
        monkeypatch.setattr(cli_module, "review_tags_interactive", _should_not_call)

        exit_code = cli_module.main(["analyze", "--auto-apply"])

        captured = capsys.readouterr()
        assert exit_code == 0

    def test_analyze_no_collection_suggestions_exits_with_error(
        self,
        isolated_paths: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that analyze exits with code 1 when the LLM returns no collection suggestions."""
        items = _make_items(2)

        monkeypatch.setattr(
            state_module.StateStore,
            "fetch_untagged_items",
            lambda self, limit: items,
        )
        monkeypatch.setattr(cli_module, "suggest_collections", lambda *a, **kw: [])
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)

        exit_code = cli_module.main(["analyze"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "no collection suggestions" in captured.err.lower() or "LLM" in captured.err

    def test_analyze_review_canceled_exits_cleanly(
        self,
        isolated_paths: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that canceling the collection review exits cleanly with code 0."""
        items = _make_items(2)
        collection_suggestions = _make_collection_suggestions(items)

        monkeypatch.setattr(
            state_module.StateStore,
            "fetch_untagged_items",
            lambda self, limit: items,
        )
        monkeypatch.setattr(cli_module, "suggest_collections", lambda *a, **kw: collection_suggestions)
        # Simulate user canceling the review (empty list returned)
        monkeypatch.setattr(cli_module, "review_collections_interactive", lambda *a, **kw: [])
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)

        exit_code = cli_module.main(["analyze"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "canceled" in captured.out.lower() or "no changes" in captured.out.lower()


# ---------------------------------------------------------------------------
# Persist-before-review (cost reduction)
# ---------------------------------------------------------------------------

class TestPersistBeforeReview:
    """LLM results used to be persisted only at the very end of the pipeline, so
    cancelling either review -- or hitting EOFError on piped input -- discarded
    everything that had just been paid for, and the next run re-sent the whole
    corpus at full price."""

    def _setup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        items: list[Item],
        collection_review: Callable[..., Any],
        tag_review: Callable[..., Any] | None = None,
    ) -> list[tuple[int, int]]:
        """Wires the pipeline and returns a log of (collections, tags) counts
        passed to each bulk_store_suggestions call."""
        stored: list[tuple[int, int]] = []

        monkeypatch.setattr(
            state_module.StateStore, "fetch_untagged_items", lambda self, limit: items
        )
        monkeypatch.setattr(
            cli_module, "suggest_collections", lambda *a, **kw: _make_collection_suggestions(items)
        )
        monkeypatch.setattr(
            cli_module, "suggest_tags", lambda *a, **kw: _make_tag_suggestions(items)
        )
        monkeypatch.setattr(cli_module, "review_collections_interactive", collection_review)
        monkeypatch.setattr(
            cli_module, "review_tags_interactive", tag_review or (lambda s, **kw: s)
        )
        monkeypatch.setattr(
            state_module.StateStore,
            "bulk_store_suggestions",
            lambda self, items_, cols, tags: stored.append((len(cols), len(tags))),
        )
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)
        return stored

    def test_collection_suggestions_stored_before_review_runs(
        self, isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        items = _make_items(3)
        seen_at_review_time: list[int] = []
        stored: list[tuple[int, int]] = []

        def _review(suggestions: Any, **kw: Any) -> Any:
            seen_at_review_time.append(len(stored))
            return suggestions

        stored = self._setup(monkeypatch, items, _review)
        cli_module.main(["analyze", "--items", "3"])

        assert seen_at_review_time[0] >= 1, "suggestions must be persisted before review is offered"

    def test_cancelling_collection_review_keeps_paid_for_suggestions(
        self, isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        items = _make_items(3)
        stored = self._setup(monkeypatch, items, lambda s, **kw: [])

        exit_code = cli_module.main(["analyze", "--items", "3"])

        assert exit_code == 0
        assert stored, "cancelling review must not discard the LLM results"
        assert stored[0][0] == 3

    def test_cancelling_tag_review_keeps_collection_and_tag_suggestions(
        self, isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        items = _make_items(3)
        stored = self._setup(
            monkeypatch, items,
            collection_review=lambda s, **kw: s,
            tag_review=lambda s, **kw: [],
        )

        exit_code = cli_module.main(["analyze", "--items", "3"])

        assert exit_code == 0
        assert sum(cols for cols, _ in stored) >= 3
        assert sum(tags for _, tags in stored) >= 3, "tag results were paid for; keep them"

    def test_cancel_message_tells_user_suggestions_were_saved(
        self, isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        items = _make_items(3)
        self._setup(monkeypatch, items, lambda s, **kw: [])

        cli_module.main(["analyze", "--items", "3"])

        out = capsys.readouterr().out
        assert "saved" in out.lower() or "stored" in out.lower()


def test_analyze_items_default_is_bounded() -> None:
    """1504 was a frozen snapshot of one user's item count, which made every
    accidental or cancelled run a full-corpus run."""
    parser = cli_module.build_parser()
    args = parser.parse_args(["analyze"])
    assert args.items == 200


class TestConfigReachesTheModel:
    """End-to-end proof that `model` in config.toml changes which model analyze
    calls -- it used to be read from os.environ inside analyze and ignored."""

    def _run_with_config(
        self, isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, toml: str
    ) -> list[Any]:
        isolated_paths.config_dir.mkdir(parents=True, exist_ok=True)
        isolated_paths.config_path.write_text(toml)

        items = _make_items(1)
        monkeypatch.setattr(
            state_module.StateStore, "fetch_untagged_items", lambda self, limit: items
        )
        monkeypatch.setattr(
            state_module.StateStore, "bulk_store_suggestions", lambda self, *a, **kw: None
        )
        monkeypatch.setattr(cli_module, "review_collections_interactive", lambda s, **kw: s)
        monkeypatch.setattr(cli_module, "review_tags_interactive", lambda s, **kw: s)
        monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)

        calls: list[Any] = []

        class _Resp:
            @staticmethod
            def raise_for_status() -> None: ...
            @staticmethod
            def json() -> dict:
                return {"content": [{"type": "text", "text": "1 | Development | high | none"}]}

        def _post(*args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return _Resp()

        monkeypatch.setattr("unhoard.analyze.requests.post", _post)
        cli_module.main(["analyze", "--items", "1"])
        return calls

    def test_model_from_config_file_is_used(
        self, isolated_paths: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = self._run_with_config(
            isolated_paths, monkeypatch,
            'anthropic_api_key = "sk-from-config"\nmodel = "claude-haiku-4-5"\n',
        )

        assert calls, "no API call was made"
        assert all(c["json"]["model"] == "claude-haiku-4-5" for c in calls)
        assert all(c["headers"]["x-api-key"] == "sk-from-config" for c in calls)
