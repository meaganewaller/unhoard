"""Unit tests for unhoard.analyze — collection suggestion via LLM.

All tests mock the Anthropic HTTP endpoint so no real API calls are made.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from unhoard.analyze import _parse_collection_suggestions, suggest_collections
from unhoard.schema import Item
from unhoard.types import CollectionSuggestion


def _make_item(source_id: str, title: str, url: str = "https://example.com") -> Item:
    return Item(source="test", source_id=source_id, title=title, url=url)


# ---------------------------------------------------------------------------
# _parse_collection_suggestions
# ---------------------------------------------------------------------------

class TestParseCollectionSuggestions:
    def _items(self) -> list[Item]:
        return [
            _make_item("1", "Python async patterns"),
            _make_item("2", "CSS Grid tutorial"),
        ]

    def test_parses_two_suggestions(self) -> None:
        text = (
            "Item ID: 1\n"
            "Title: Python async patterns\n"
            "Suggested Collection: Development\n"
            "Confidence: high\n"
            "Conflict: none\n"
            "Reasoning: Core Python development content\n"
            "\n"
            "Item ID: 2\n"
            "Title: CSS Grid tutorial\n"
            "Suggested Collection: Design\n"
            "Confidence: high\n"
            "Conflict: none\n"
            "Reasoning: Frontend design and layout\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert len(suggestions) == 2
        assert suggestions[0].suggested_collection == "Development"
        assert suggestions[1].suggested_collection == "Design"

    def test_confidence_values_preserved(self) -> None:
        text = (
            "Item ID: 1\n"
            "Suggested Collection: Development\n"
            "Confidence: medium\n"
            "Conflict: none\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert suggestions[0].confidence == "medium"

    def test_conflict_none_becomes_none(self) -> None:
        text = (
            "Item ID: 1\n"
            "Suggested Collection: Development\n"
            "Confidence: high\n"
            "Conflict: none\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert suggestions[0].conflict is None

    def test_conflict_value_is_preserved(self) -> None:
        text = (
            "Item ID: 1\n"
            "Suggested Collection: Development\n"
            "Confidence: medium\n"
            "Conflict: Gaming\n"
            "Reasoning: Could fit either\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert suggestions[0].conflict == "Gaming"

    def test_reasoning_is_captured(self) -> None:
        text = (
            "Item ID: 1\n"
            "Suggested Collection: Development\n"
            "Confidence: high\n"
            "Conflict: none\n"
            "Reasoning: Really good Python content\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert suggestions[0].reasoning == "Really good Python content"

    def test_item_id_not_in_items_is_skipped(self) -> None:
        """An Item ID that has no matching Item should not produce a suggestion."""
        text = (
            "Item ID: 99\n"
            "Suggested Collection: Nowhere\n"
            "Confidence: high\n"
            "Conflict: none\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert len(suggestions) == 0

    def test_missing_collection_defaults_to_uncategorized(self) -> None:
        text = (
            "Item ID: 1\n"
            "Confidence: high\n"
            "Conflict: none\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert suggestions[0].suggested_collection == "Uncategorized"

    def test_item_title_is_filled_from_item(self) -> None:
        text = (
            "Item ID: 1\n"
            "Suggested Collection: Development\n"
            "Confidence: high\n"
            "Conflict: none\n"
        )
        suggestions = _parse_collection_suggestions(text, self._items())
        assert suggestions[0].item_title == "Python async patterns"

    def test_empty_response_returns_empty_list(self) -> None:
        suggestions = _parse_collection_suggestions("", self._items())
        assert suggestions == []

    def test_garbage_response_does_not_raise(self) -> None:
        suggestions = _parse_collection_suggestions("::::\nnot parseable at all", self._items())
        assert isinstance(suggestions, list)


# ---------------------------------------------------------------------------
# suggest_collections
# ---------------------------------------------------------------------------

class TestSuggestCollections:
    def _items(self) -> list[Item]:
        return [
            _make_item("1", "Python async patterns"),
            _make_item("2", "CSS Grid tutorial"),
        ]

    @patch("unhoard.analyze.requests.post")
    def test_returns_suggestions(self, mock_post: MagicMock) -> None:
        """suggest_collections returns CollectionSuggestion objects."""
        response_text = (
            "Item ID: 1\n"
            "Title: Python async patterns\n"
            "Suggested Collection: Development\n"
            "Confidence: high\n"
            "Conflict: none\n"
            "Reasoning: Core Python development content\n"
            "\n"
            "Item ID: 2\n"
            "Title: CSS Grid tutorial\n"
            "Suggested Collection: Design\n"
            "Confidence: high\n"
            "Conflict: none\n"
            "Reasoning: Frontend design and layout\n"
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": response_text}]
        }
        mock_post.return_value = mock_resp

        suggestions = suggest_collections(self._items())

        assert len(suggestions) == 2
        assert all(isinstance(s, CollectionSuggestion) for s in suggestions)
        assert all(hasattr(s, "item_id") for s in suggestions)
        assert all(hasattr(s, "suggested_collection") for s in suggestions)

    @patch("unhoard.analyze.requests.post")
    def test_confidence_values_are_valid(self, mock_post: MagicMock) -> None:
        response_text = (
            "Item ID: 1\n"
            "Suggested Collection: Development\n"
            "Confidence: high\n"
            "Conflict: none\n"
            "\n"
            "Item ID: 2\n"
            "Suggested Collection: Design\n"
            "Confidence: low\n"
            "Conflict: none\n"
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": response_text}]
        }
        mock_post.return_value = mock_resp

        suggestions = suggest_collections(self._items())

        assert all(s.confidence in {"high", "medium", "low"} for s in suggestions)

    @patch("unhoard.analyze.requests.post")
    def test_handles_conflicts(self, mock_post: MagicMock) -> None:
        """Items fitting multiple collections should have conflict populated."""
        items = [
            _make_item("3", "Game Development Guide"),
            _make_item("4", "Web Design Patterns"),
        ]
        response_text = (
            "Item ID: 3\n"
            "Title: Game Development Guide\n"
            "Suggested Collection: Development\n"
            "Confidence: medium\n"
            "Conflict: Gaming\n"
            "Reasoning: Could fit either Development or Gaming\n"
            "\n"
            "Item ID: 4\n"
            "Title: Web Design Patterns\n"
            "Suggested Collection: Design\n"
            "Confidence: high\n"
            "Conflict: Development\n"
            "Reasoning: Front-end design, could be in Development too\n"
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": response_text}]
        }
        mock_post.return_value = mock_resp

        suggestions = suggest_collections(items)

        assert suggestions[0].conflict == "Gaming"
        assert suggestions[1].conflict == "Development"

    def test_empty_items_returns_empty_list(self) -> None:
        """No items should return empty without hitting the API."""
        result = suggest_collections([])
        assert result == []

    @patch("unhoard.analyze.requests.post")
    def test_limit_truncates_items(self, mock_post: MagicMock) -> None:
        """limit parameter controls max items passed to LLM."""
        many_items = [_make_item(str(i), f"Item {i}") for i in range(10)]
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"content": [{"type": "text", "text": ""}]}
        mock_post.return_value = mock_resp

        suggest_collections(many_items, limit=3)

        # The prompt sent to the API should only mention 3 items
        call_args = mock_post.call_args
        prompt = call_args.kwargs["json"]["messages"][0]["content"]
        # Items 3..9 should NOT appear in the prompt
        assert "Item 4" not in prompt
        assert "Item 5" not in prompt

    @patch("unhoard.analyze.requests.post")
    def test_api_error_returns_empty_list(self, mock_post: MagicMock) -> None:
        """HTTP errors from the API should be swallowed and return []."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500 Internal Server Error")
        mock_post.return_value = mock_resp

        result = suggest_collections(self._items())

        assert result == []

    @patch("unhoard.analyze.requests.post")
    def test_connection_error_returns_empty_list(self, mock_post: MagicMock) -> None:
        """Network failures should be swallowed and return []."""
        mock_post.side_effect = Exception("Connection refused")

        result = suggest_collections(self._items())

        assert result == []

    @patch("unhoard.analyze.requests.post")
    def test_prompt_includes_item_titles(self, mock_post: MagicMock) -> None:
        """The LLM prompt should include item titles."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"content": [{"type": "text", "text": ""}]}
        mock_post.return_value = mock_resp

        suggest_collections(self._items())

        call_args = mock_post.call_args
        prompt = call_args.kwargs["json"]["messages"][0]["content"]
        assert "Python async patterns" in prompt
        assert "CSS Grid tutorial" in prompt

    @patch("unhoard.analyze.requests.post")
    def test_uses_correct_model(self, mock_post: MagicMock) -> None:
        """Should call the configured Claude model."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"content": [{"type": "text", "text": ""}]}
        mock_post.return_value = mock_resp

        suggest_collections(self._items())

        call_args = mock_post.call_args
        model = call_args.kwargs["json"]["model"]
        assert "claude" in model

    @patch("unhoard.analyze.requests.post")
    def test_item_ids_use_source_id(self, mock_post: MagicMock) -> None:
        """CollectionSuggestion.item_id should reflect the item's source_id."""
        response_text = (
            "Item ID: 42\n"
            "Suggested Collection: Development\n"
            "Confidence: high\n"
            "Conflict: none\n"
        )
        items = [_make_item("42", "Some Article")]
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": response_text}]
        }
        mock_post.return_value = mock_resp

        suggestions = suggest_collections(items)

        assert suggestions[0].item_id == 42
