"""Collection analysis via LLM.

Uses the Anthropic Messages API (via ``requests``, matching the project's
existing HTTP pattern in ``summarize.py``) to cluster bookmark items into
suggested collections.

Public API
----------
suggest_collections(items, limit) -> list[CollectionSuggestion]
_parse_collection_suggestions(text, items) -> list[CollectionSuggestion]  (semi-public for testing)
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import requests

from unhoard.schema import Item
from unhoard.types import CollectionSuggestion

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"
_SAMPLE_SIZE = 50  # items included in the prompt body (rest counted only)

_PROMPT_TEMPLATE = """\
You are helping to organize a personal bookmark collection. \
Analyze the items below and suggest a collection structure.

Suggest 8–15 collections that would naturally group this content. \
Each collection should have a clear semantic purpose and be one level deep \
(e.g. "Development" or "Design").

Items ({count} total, showing up to {sample_size}):
{items_block}

For each item listed above, output an assignment using this exact format \
(one blank line between entries):

Item ID: <source_id>
Title: <title>
Suggested Collection: <collection_name>
Confidence: high|medium|low
Conflict: <alternative_collection_name_if_ambiguous, or "none">
Reasoning: <one or two sentence explanation>
"""


def suggest_collections(
    items: list[Item], limit: int = 1504
) -> list[CollectionSuggestion]:
    """Use the LLM to cluster items and suggest collection assignments.

    Args:
        items: List of items to analyze.
        limit: Maximum number of items to process.

    Returns:
        List of CollectionSuggestion objects, one per recognized item. Returns
        an empty list if ``items`` is empty, the API call fails, or the
        response cannot be parsed.
    """
    if not items:
        return []

    working = items[:limit]
    sample = working[:_SAMPLE_SIZE]

    items_block = "\n".join(
        f"- ID: {item.source_id} | Title: {item.title} | URL: {item.url}"
        + (f" | Note: {item.excerpt}" if item.excerpt else "")
        for item in sample
    )

    prompt = _PROMPT_TEMPLATE.format(
        count=len(working),
        sample_size=_SAMPLE_SIZE,
        items_block=items_block,
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    model = os.environ.get("UNHOARD_MODEL", DEFAULT_MODEL)

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        text_blocks = [
            b["text"] for b in data.get("content", []) if b.get("type") == "text"
        ]
        response_text = "\n".join(text_blocks).strip()
    except Exception as exc:  # noqa: BLE001 — never fatal
        print(f"[analyze] LLM call failed: {exc}", file=sys.stderr)
        return []

    return _parse_collection_suggestions(response_text, working)


def _parse_collection_suggestions(
    response_text: str, items: list[Item]
) -> list[CollectionSuggestion]:
    """Parse the LLM response text into CollectionSuggestion objects.

    Scans line by line for ``Item ID:`` blocks. Unknown IDs (those that don't
    correspond to any item in ``items``) are silently skipped. Unrecognized
    lines within a block are ignored so the parser degrades gracefully on
    unexpected model output.

    Args:
        response_text: Raw text returned by the LLM.
        items: The same item list that was sent to the LLM (used to look up
            title and validate IDs).

    Returns:
        List of CollectionSuggestion objects in the order they appear in the
        response. Empty list if nothing parseable is found.
    """
    # Build a fast lookup by source_id (string) so we don't do O(n²) scans.
    items_by_id: dict[str, Item] = {item.source_id: item for item in items}

    suggestions: list[CollectionSuggestion] = []

    # State for the current block being parsed
    current_source_id: Optional[str] = None
    current_collection: Optional[str] = None
    current_confidence: str = "medium"
    current_conflict: Optional[str] = None
    current_reasoning: Optional[str] = None

    def _flush() -> None:
        """Emit a suggestion for the current block if the ID is valid."""
        if current_source_id is None:
            return
        item = items_by_id.get(current_source_id)
        if item is None:
            return
        # Resolve item_id: use int(source_id) when numeric, else fallback to hash.
        try:
            item_id = int(current_source_id)
        except ValueError:
            item_id = abs(hash(current_source_id)) % (10**9)

        suggestions.append(
            CollectionSuggestion(
                item_id=item_id,
                item_title=item.title,
                suggested_collection=current_collection or "Uncategorized",
                confidence=current_confidence,
                conflict=(
                    None
                    if current_conflict is None
                    or current_conflict.lower() == "none"
                    else current_conflict
                ),
                reasoning=current_reasoning,
            )
        )

    for raw_line in response_text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue

        prefix, _, rest = line.partition(":")
        key = prefix.strip().lower()
        value = rest.strip()

        if key == "item id":
            _flush()
            # Reset state for new block
            current_source_id = value
            current_collection = None
            current_confidence = "medium"
            current_conflict = None
            current_reasoning = None
        elif key == "suggested collection":
            current_collection = value
        elif key == "confidence":
            cleaned = value.lower()
            if cleaned in {"high", "medium", "low"}:
                current_confidence = cleaned
        elif key == "conflict":
            current_conflict = value
        elif key == "reasoning":
            # Use partition on original line to capture colons within the value.
            _, _, rest_full = raw_line.strip().partition(":")
            current_reasoning = rest_full.strip()

    # Flush the final block
    _flush()

    return suggestions
