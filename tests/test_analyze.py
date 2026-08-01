"""Unit tests for unhoard.analyze — collection suggestion via LLM.

All tests mock the Anthropic HTTP endpoint so no real API calls are made.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from unhoard.analyze import (
    _parse_collection_suggestions,
    _parse_tag_suggestions,
    suggest_collections,
    suggest_tags,
)
from unhoard.schema import Item
from unhoard.types import CollectionSuggestion, TagSuggestion


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


# ---------------------------------------------------------------------------
# _parse_tag_suggestions
# ---------------------------------------------------------------------------

class TestParseTagSuggestions:
    def _items(self) -> list[Item]:
        return [
            _make_item("1", "Python async patterns"),
            _make_item("2", "CSS Grid tutorial"),
        ]

    def test_parses_two_suggestions(self) -> None:
        text = (
            "Item ID: 1\n"
            "Use-Case Tags: reference, learning\n"
            "Status Tags: reviewed\n"
            "Reasoning: Core learning reference\n"
            "\n"
            "Item ID: 2\n"
            "Use-Case Tags: learning\n"
            "Status Tags: needs-refinement\n"
            "Reasoning: Design tutorial worth revisiting\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert len(suggestions) == 2
        assert suggestions[0].use_case_tags == ["reference", "learning"]
        assert suggestions[0].status_tags == ["reviewed"]
        assert suggestions[1].use_case_tags == ["learning"]
        assert suggestions[1].status_tags == ["needs-refinement"]

    def test_item_title_filled_from_item(self) -> None:
        text = (
            "Item ID: 1\n"
            "Use-Case Tags: tool\n"
            "Status Tags: wip\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert suggestions[0].item_title == "Python async patterns"

    def test_reasoning_captured(self) -> None:
        text = (
            "Item ID: 1\n"
            "Use-Case Tags: inspiration\n"
            "Status Tags: archived\n"
            "Reasoning: Old but insightful read\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert suggestions[0].reasoning == "Old but insightful read"

    def test_unknown_id_skipped(self) -> None:
        text = (
            "Item ID: 99\n"
            "Use-Case Tags: bookmark\n"
            "Status Tags: reviewed\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert len(suggestions) == 0

    def test_empty_response_returns_empty_list(self) -> None:
        assert _parse_tag_suggestions("", self._items()) == []

    def test_multiple_use_case_tags(self) -> None:
        text = (
            "Item ID: 1\n"
            "Use-Case Tags: reference, tool, project\n"
            "Status Tags: reviewed\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert "reference" in suggestions[0].use_case_tags
        assert "tool" in suggestions[0].use_case_tags
        assert "project" in suggestions[0].use_case_tags

    def test_multiple_status_tags(self) -> None:
        text = (
            "Item ID: 1\n"
            "Use-Case Tags: learning\n"
            "Status Tags: wip, needs-refinement\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert "wip" in suggestions[0].status_tags
        assert "needs-refinement" in suggestions[0].status_tags

    def test_invalid_use_case_tags_filtered(self) -> None:
        """Tags not in the allowed set should be silently dropped."""
        text = (
            "Item ID: 1\n"
            "Use-Case Tags: reference, nonsense-tag, learning\n"
            "Status Tags: reviewed\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert "nonsense-tag" not in suggestions[0].use_case_tags
        assert suggestions[0].use_case_tags == ["reference", "learning"]

    def test_invalid_status_tags_filtered(self) -> None:
        text = (
            "Item ID: 1\n"
            "Use-Case Tags: tool\n"
            "Status Tags: reviewed, invalid-status\n"
        )
        suggestions = _parse_tag_suggestions(text, self._items())
        assert "invalid-status" not in suggestions[0].status_tags
        assert suggestions[0].status_tags == ["reviewed"]

    def test_item_id_integer_resolution(self) -> None:
        text = (
            "Item ID: 42\n"
            "Use-Case Tags: bookmark\n"
            "Status Tags: archived\n"
        )
        items = [_make_item("42", "Special Article")]
        suggestions = _parse_tag_suggestions(text, items)
        assert suggestions[0].item_id == 42


# ---------------------------------------------------------------------------
# suggest_tags
# ---------------------------------------------------------------------------

class TestSuggestTags:
    def _items(self) -> list[Item]:
        return [
            _make_item("1", "Python async patterns"),
            _make_item("2", "CSS Grid tutorial"),
        ]

    def _collections(self) -> dict[int, str]:
        return {1: "Development", 2: "Design"}

    @patch("unhoard.analyze.requests.post")
    def test_suggest_tags_returns_suggestions(self, mock_post: MagicMock) -> None:
        """Happy path: suggest_tags returns TagSuggestion objects for each item."""
        response_text = (
            "Item ID: 1\n"
            "Use-Case Tags: reference, learning\n"
            "Status Tags: reviewed\n"
            "Reasoning: Core Python reference content\n"
            "\n"
            "Item ID: 2\n"
            "Use-Case Tags: learning, inspiration\n"
            "Status Tags: needs-refinement\n"
            "Reasoning: CSS design learning resource\n"
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": response_text}]
        }
        mock_post.return_value = mock_resp

        suggestions = suggest_tags(self._items(), self._collections())

        assert len(suggestions) == 2
        assert all(isinstance(s, TagSuggestion) for s in suggestions)
        assert all(hasattr(s, "item_id") for s in suggestions)
        assert all(hasattr(s, "use_case_tags") for s in suggestions)
        assert all(hasattr(s, "status_tags") for s in suggestions)

    def test_empty_items_returns_empty_list(self) -> None:
        result = suggest_tags([], {})
        assert result == []

    @patch("unhoard.analyze.requests.post")
    def test_api_error_returns_empty_list(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        mock_post.return_value = mock_resp

        result = suggest_tags(self._items(), self._collections())
        assert result == []

    @patch("unhoard.analyze.requests.post")
    def test_connection_error_returns_empty_list(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = Exception("Connection refused")

        result = suggest_tags(self._items(), self._collections())
        assert result == []

    @patch("unhoard.analyze.requests.post")
    def test_groups_by_collection_for_batching(self, mock_post: MagicMock) -> None:
        """Items in the same collection should be sent in one LLM call."""
        items = [
            _make_item("1", "Python async patterns"),
            _make_item("2", "Python type hints"),
            _make_item("3", "CSS Grid tutorial"),
        ]
        collections = {1: "Development", 2: "Development", 3: "Design"}

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"content": [{"type": "text", "text": ""}]}
        mock_post.return_value = mock_resp

        suggest_tags(items, collections)

        # Two collections -> two LLM calls
        assert mock_post.call_count == 2

    @patch("unhoard.analyze.requests.post")
    def test_prompt_includes_collection_name(self, mock_post: MagicMock) -> None:
        """The LLM prompt should mention the collection name for context."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"content": [{"type": "text", "text": ""}]}
        mock_post.return_value = mock_resp

        suggest_tags(self._items(), self._collections())

        call_args_list = mock_post.call_args_list
        prompts = [
            c.kwargs["json"]["messages"][0]["content"] for c in call_args_list
        ]
        all_prompts = "\n".join(prompts)
        assert "Development" in all_prompts or "Design" in all_prompts

    @patch("unhoard.analyze.requests.post")
    def test_uses_correct_model(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"content": [{"type": "text", "text": ""}]}
        mock_post.return_value = mock_resp

        suggest_tags(self._items(), self._collections())

        for call in mock_post.call_args_list:
            model = call.kwargs["json"]["model"]
            assert "claude" in model

    @patch("unhoard.analyze.requests.post")
    def test_items_without_collection_assignment_still_processed(
        self, mock_post: MagicMock
    ) -> None:
        """Items with no entry in collections dict go into an 'Uncategorized' batch."""
        items = [_make_item("5", "Orphan Item")]
        collections: dict[int, str] = {}  # nothing mapped

        response_text = (
            "Item ID: 5\n"
            "Use-Case Tags: bookmark\n"
            "Status Tags: reviewed\n"
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": response_text}]
        }
        mock_post.return_value = mock_resp

        suggestions = suggest_tags(items, collections)

        # Should still get a suggestion for item 5
        assert any(s.item_id == 5 for s in suggestions)
