"""Unit tests for unhoard.review — interactive CLI collection review."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from unhoard.review import review_collections_interactive, _edit_collections_interactive
from unhoard.types import CollectionSuggestion


def _make_suggestion(item_id: int, title: str, collection: str) -> CollectionSuggestion:
    return CollectionSuggestion(
        item_id=item_id,
        item_title=title,
        suggested_collection=collection,
        confidence="high",
    )


def _make_suggestions() -> list[CollectionSuggestion]:
    return [
        _make_suggestion(1, "Python async patterns", "Development"),
        _make_suggestion(2, "CSS Grid tutorial", "Design"),
        _make_suggestion(3, "React hooks guide", "Development"),
    ]


# ---------------------------------------------------------------------------
# review_collections_interactive — accept ('y')
# ---------------------------------------------------------------------------

class TestReviewCollectionsAcceptsAll:
    def test_review_collections_accepts_all(self) -> None:
        """User responds 'y' — all suggestions returned unchanged."""
        suggestions = _make_suggestions()
        with patch("builtins.input", return_value="y"):
            result = review_collections_interactive(suggestions)
        assert result == suggestions

    def test_returns_list_of_collection_suggestions(self) -> None:
        suggestions = _make_suggestions()
        with patch("builtins.input", return_value="y"):
            result = review_collections_interactive(suggestions)
        assert all(isinstance(s, CollectionSuggestion) for s in result)

    def test_length_preserved_on_accept(self) -> None:
        suggestions = _make_suggestions()
        with patch("builtins.input", return_value="y"):
            result = review_collections_interactive(suggestions)
        assert len(result) == len(suggestions)


# ---------------------------------------------------------------------------
# review_collections_interactive — cancel ('n')
# ---------------------------------------------------------------------------

class TestReviewCollectionsCancel:
    def test_cancel_returns_empty_list(self) -> None:
        """User responds 'n' — empty list is returned (operation canceled)."""
        suggestions = _make_suggestions()
        with patch("builtins.input", return_value="n"):
            result = review_collections_interactive(suggestions)
        assert result == []


# ---------------------------------------------------------------------------
# review_collections_interactive — edit ('edit') then done
# ---------------------------------------------------------------------------

class TestReviewCollectionsEdit:
    def test_edit_then_done_returns_suggestions(self) -> None:
        """User chooses edit but immediately picks 'done' — loop re-prompts, then 'y' accepts."""
        suggestions = _make_suggestions()
        # After edit/done the main loop shows the summary again and re-prompts.
        with patch("builtins.input", side_effect=["edit", "done", "y"]):
            result = review_collections_interactive(suggestions)
        assert isinstance(result, list)

    def test_rename_collection_updates_all_matching_items(self) -> None:
        """rename -> old name -> new name -> done -> 'y' to confirm."""
        suggestions = _make_suggestions()
        # Sequence: 'edit', 'rename', 'Development', 'Engineering', 'done'
        # Then final prompt: 'y'
        inputs = iter(["edit", "rename", "Development", "Engineering", "done", "y"])
        with patch("builtins.input", side_effect=inputs):
            result = review_collections_interactive(suggestions)
        dev_items = [s for s in result if s.suggested_collection == "Engineering"]
        old_items = [s for s in result if s.suggested_collection == "Development"]
        assert len(dev_items) == 2  # items 1 and 3 were Development
        assert len(old_items) == 0

    def test_merge_collections_into_target(self) -> None:
        """merge -> source name -> target name -> done -> 'y'."""
        suggestions = _make_suggestions()
        # Merge 'Design' into 'Development'
        inputs = iter(["edit", "merge", "Design", "Development", "done", "y"])
        with patch("builtins.input", side_effect=inputs):
            result = review_collections_interactive(suggestions)
        design_items = [s for s in result if s.suggested_collection == "Design"]
        dev_items = [s for s in result if s.suggested_collection == "Development"]
        assert len(design_items) == 0
        assert len(dev_items) == 3  # all 3 items now in Development

    def test_reassign_single_item_to_new_collection(self) -> None:
        """reassign -> item_id -> new collection name -> done -> 'y'."""
        suggestions = _make_suggestions()
        # Reassign item 2 (CSS Grid, Design) to Frontend
        inputs = iter(["edit", "reassign", "2", "Frontend", "done", "y"])
        with patch("builtins.input", side_effect=inputs):
            result = review_collections_interactive(suggestions)
        item_2 = next(s for s in result if s.item_id == 2)
        assert item_2.suggested_collection == "Frontend"

    def test_unknown_edit_action_is_ignored(self) -> None:
        """An unrecognized edit action should not crash; loop continues."""
        suggestions = _make_suggestions()
        inputs = iter(["edit", "frobulate", "done", "y"])
        with patch("builtins.input", side_effect=inputs):
            result = review_collections_interactive(suggestions)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# review_collections_interactive — empty input
# ---------------------------------------------------------------------------

class TestReviewCollectionsEdgeCases:
    def test_empty_suggestions_returns_empty(self) -> None:
        """No suggestions to review — return empty immediately."""
        with patch("builtins.input", return_value="y"):
            result = review_collections_interactive([])
        assert result == []

    def test_single_suggestion_accepted(self) -> None:
        suggestions = [_make_suggestion(1, "Some Article", "Tech")]
        with patch("builtins.input", return_value="y"):
            result = review_collections_interactive(suggestions)
        assert len(result) == 1
        assert result[0].suggested_collection == "Tech"


# ---------------------------------------------------------------------------
# _edit_collections_interactive (helper, semi-public)
# ---------------------------------------------------------------------------

class TestEditCollectionsInteractive:
    def _collections(self) -> dict[str, list[CollectionSuggestion]]:
        suggestions = _make_suggestions()
        return {
            "Development": [s for s in suggestions if s.suggested_collection == "Development"],
            "Design": [s for s in suggestions if s.suggested_collection == "Design"],
        }

    def test_done_immediately_returns_flat_list(self) -> None:
        suggestions = _make_suggestions()
        collections = self._collections()
        with patch("builtins.input", return_value="done"):
            result = _edit_collections_interactive(suggestions, collections)
        assert isinstance(result, list)
        assert len(result) == len(suggestions)

    def test_returns_collection_suggestions(self) -> None:
        suggestions = _make_suggestions()
        collections = self._collections()
        with patch("builtins.input", return_value="done"):
            result = _edit_collections_interactive(suggestions, collections)
        assert all(isinstance(s, CollectionSuggestion) for s in result)
