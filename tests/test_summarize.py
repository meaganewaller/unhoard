from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
import responses

from unhoard import summarize as summarize_module
from unhoard.summarize import (
    ANTHROPIC_API_URL,
    content_hash,
    context_hash,
    fetch_article_text,
    parse_summary_response,
    summarize,
)


def _last_request_body() -> dict[str, Any]:
    body = responses.calls[-1].request.body
    assert isinstance(body, (str, bytes))
    return json.loads(body)


class TestContextBlock:
    def test_empty_context_returns_empty_string(self) -> None:
        assert summarize_module._context_block("") == ""

    def test_nonempty_context_is_wrapped_and_stripped(self) -> None:
        block = summarize_module._context_block("  likes old-web nostalgia sites  ")
        assert "likes old-web nostalgia sites" in block
        assert block.startswith("\n")


class TestCollectionsBlock:
    def test_none_tells_model_to_always_answer_none(self) -> None:
        assert "always answer Collection: none" in summarize_module._collections_block(None)

    def test_empty_list_is_treated_like_none(self) -> None:
        assert "always answer Collection: none" in summarize_module._collections_block([])

    def test_lists_each_collection_name_as_a_bullet(self) -> None:
        block = summarize_module._collections_block(["Reading", "Recipes"])
        assert "- Reading" in block
        assert "- Recipes" in block


class TestParseSummaryResponse:
    def test_parses_all_fields(self) -> None:
        text = (
            "Summary: A neat article about foo.\n"
            "Action: Read good primer\n"
            "Tags: foo, bar\n"
            "Collection: Reading\n"
        )
        result = parse_summary_response(text)
        assert result["summary"] == "A neat article about foo."
        assert result["action"] == "Read good primer"
        assert result["tags"] == ["foo", "bar"]
        assert result["collection"] == "Reading"

    def test_tags_none_becomes_empty_list(self) -> None:
        assert parse_summary_response("Tags: none\n")["tags"] == []

    def test_collection_none_becomes_none(self) -> None:
        assert parse_summary_response("Collection: none\n")["collection"] is None

    def test_blank_and_colon_less_lines_are_skipped(self) -> None:
        result = parse_summary_response("\nnot a real line\nSummary: ok\n")
        assert result["summary"] == "ok"

    def test_unrecognized_key_is_ignored(self) -> None:
        result = parse_summary_response("Foo: bar\n")
        assert result == {"summary": "", "action": "", "tags": [], "collection": None}

    def test_never_raises_on_garbage_input(self) -> None:
        result = parse_summary_response(":::\n:\n   :   \n")
        assert result["summary"] == ""

    def test_tags_are_trimmed_of_surrounding_whitespace(self) -> None:
        result = parse_summary_response("Tags:  foo ,  bar  \n")
        assert result["tags"] == ["foo", "bar"]


class TestContextHash:
    def test_deterministic(self) -> None:
        assert context_hash("same") == context_hash("same")

    def test_differs_for_different_input(self) -> None:
        assert context_hash("a") != context_hash("b")

    def test_is_16_chars(self) -> None:
        assert len(context_hash("anything")) == 16


class TestContentHash:
    def test_deterministic(self) -> None:
        assert content_hash("same text") == content_hash("same text")

    def test_differs_for_different_input(self) -> None:
        assert content_hash("a") != content_hash("b")

    def test_is_16_chars(self) -> None:
        assert len(content_hash("anything")) == 16


class TestFetchArticleText:
    def test_no_trafilatura_module_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "trafilatura", None)
        assert fetch_article_text("https://example.com") == ""

    def test_empty_url_short_circuits_without_calling_trafilatura(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = MagicMock()
        monkeypatch.setattr(summarize_module, "trafilatura", fake)
        assert fetch_article_text("") == ""
        fake.fetch_url.assert_not_called()

    def test_download_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = MagicMock()
        fake.fetch_url.return_value = None
        monkeypatch.setattr(summarize_module, "trafilatura", fake)
        assert fetch_article_text("https://example.com") == ""

    def test_extract_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = MagicMock()
        fake.fetch_url.return_value = "<html></html>"
        fake.extract.return_value = None
        monkeypatch.setattr(summarize_module, "trafilatura", fake)
        assert fetch_article_text("https://example.com") == ""

    def test_returns_extracted_text_truncated_to_max_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = MagicMock()
        fake.fetch_url.return_value = "<html></html>"
        fake.extract.return_value = "x" * 100
        monkeypatch.setattr(summarize_module, "trafilatura", fake)
        assert fetch_article_text("https://example.com", max_chars=10) == "x" * 10

    def test_exception_is_swallowed_and_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = MagicMock()
        fake.fetch_url.side_effect = RuntimeError("network boom")
        monkeypatch.setattr(summarize_module, "trafilatura", fake)
        assert fetch_article_text("https://example.com") == ""


class TestSummarize:
    def test_no_article_text_returns_empty_result_and_empty_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "")

        result, chash = summarize("Title", "https://x", api_key="key", model="m")

        assert result == {}
        assert chash == ""

    def test_no_api_key_skips_the_api_call_but_still_hashes_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "article body")

        result, chash = summarize("Title", "https://x", api_key="", model="m")

        assert result == {}
        assert chash == content_hash("article body")

    @responses.activate
    def test_success_parses_response_and_keeps_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "article body")
        raw = "Summary: ok\nAction: Read now\nTags: none\nCollection: none"
        responses.add(
            responses.POST, ANTHROPIC_API_URL,
            json={"content": [{"type": "text", "text": raw}]}, status=200,
        )

        result, chash = summarize("Title", "https://x", api_key="key", model="claude-sonnet-5")

        assert result["summary"] == "ok"
        assert result["action"] == "Read now"
        assert result["raw"] == raw
        assert chash == content_hash("article body")

    @responses.activate
    def test_joins_multiple_text_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "article body")
        responses.add(
            responses.POST, ANTHROPIC_API_URL,
            json={"content": [
                {"type": "text", "text": "Summary: part one"},
                {"type": "other", "text": "should be ignored"},
                {"type": "text", "text": "Action: Read"},
            ]},
            status=200,
        )

        result, _ = summarize("Title", "https://x", api_key="key", model="m")

        assert result["raw"] == "Summary: part one\nAction: Read"
        assert "should be ignored" not in result["raw"]

    @responses.activate
    def test_prompt_includes_title_content_context_and_collections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "article body")
        responses.add(responses.POST, ANTHROPIC_API_URL, json={"content": []}, status=200)

        summarize(
            "My Title", "https://x", api_key="key", model="m",
            context="likes old web stuff", collection_names=["Reading"], max_tokens=42,
        )

        body = _last_request_body()
        assert body["model"] == "m"
        assert body["max_tokens"] == 42
        prompt = body["messages"][0]["content"]
        assert "My Title" in prompt
        assert "article body" in prompt
        assert "likes old web stuff" in prompt
        assert "- Reading" in prompt

    @responses.activate
    def test_sends_expected_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "article body")
        responses.add(responses.POST, ANTHROPIC_API_URL, json={"content": []}, status=200)

        summarize("Title", "https://x", api_key="secret-key", model="m")

        headers = responses.calls[-1].request.headers
        assert headers["x-api-key"] == "secret-key"
        assert headers["anthropic-version"] == "2023-06-01"

    @responses.activate
    def test_http_error_is_swallowed_and_returns_empty_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "article body")
        responses.add(responses.POST, ANTHROPIC_API_URL, json={}, status=500)

        result, chash = summarize("Title", "https://x", api_key="key", model="m")

        assert result == {}
        assert chash == content_hash("article body")

    def test_connection_error_is_swallowed_and_returns_empty_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(summarize_module, "fetch_article_text", lambda url, **_: "article body")

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise summarize_module.requests.exceptions.ConnectionError("no network")

        monkeypatch.setattr(summarize_module.requests, "post", _boom)

        result, chash = summarize("Title", "https://x", api_key="key", model="m")

        assert result == {}
        assert chash == content_hash("article body")
