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
