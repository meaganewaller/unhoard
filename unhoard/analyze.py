"""Collection and tag analysis via LLM.

Uses the Anthropic Messages API (via ``requests``, matching the project's
existing HTTP pattern in ``summarize.py``) to cluster bookmark items into
suggested collections and assign use-case / status tags.

Public API
----------
suggest_collections(items, limit) -> list[CollectionSuggestion]
suggest_tags(items, collections) -> list[TagSuggestion]
_parse_collection_suggestions(text, items) -> list[CollectionSuggestion]  (semi-public for testing)
_parse_tag_suggestions(text, items) -> list[TagSuggestion]  (semi-public for testing)
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Optional

import requests

from unhoard.schema import Item, stable_item_id
from unhoard.types import CollectionSuggestion, TagSuggestion

# Canonical definition lives in schema.py so state.py can share it -- the two
# must agree or suggestions for non-numeric source_ids get dropped on persist.
# Re-exported under the original private name for existing callers and tests.
_stable_id = stable_item_id


_VALID_USE_CASE_TAGS = frozenset(
    {"reference", "learning", "inspiration", "project", "tool", "bookmark"}
)
_VALID_STATUS_TAGS = frozenset({"wip", "archived", "reviewed", "needs-refinement"})

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

    items_block = "\n".join(
        f"- ID: {item.source_id} | Title: {item.title} | URL: {item.url}"
        + (f" | Note: {item.excerpt}" if item.excerpt else "")
        for item in working
    )

    prompt = _PROMPT_TEMPLATE.format(
        count=len(working),
        sample_size=len(working),
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
        # Resolve item_id: use int(source_id) when numeric, else stable MD5-based hash.
        item_id = _stable_id(current_source_id)

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


# ---------------------------------------------------------------------------
# Tag suggestion
# ---------------------------------------------------------------------------

_TAG_PROMPT_TEMPLATE = """\
You are helping to tag a personal bookmark collection. \
Analyze the items below (all from the "{collection}" collection) and assign tags.

Use-case tags (apply any that fit): reference, learning, inspiration, project, tool, bookmark
Status tags (apply any that fit): wip, archived, reviewed, needs-refinement

Items:
{items_block}

For each item listed above, output an assignment using this exact format \
(one blank line between entries):

Item ID: <source_id>
Use-Case Tags: <comma-separated list from allowed use-case tags>
Status Tags: <comma-separated list from allowed status tags>
Reasoning: <one sentence explanation>
"""


def suggest_tags(
    items: list[Item], collections: dict[int, str]
) -> list[TagSuggestion]:
    """Use the LLM to suggest use-case and status tags for items.

    Groups items by their suggested collection so each batch shares semantic
    context, then sends one LLM request per collection group.

    Args:
        items: List of items to tag.
        collections: Mapping of item_id -> suggested_collection_name.  Items
            absent from this dict are placed in an "Uncategorized" batch.

    Returns:
        List of TagSuggestion objects, one per recognized item.  Returns an
        empty list if ``items`` is empty, all API calls fail, or nothing can
        be parsed.
    """
    if not items:
        return []

    # Group items by their suggested collection name.
    groups: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        item_id = _stable_id(item.source_id)
        collection_name = collections.get(item_id, "Uncategorized")
        groups[collection_name].append(item)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    model = os.environ.get("UNHOARD_MODEL", DEFAULT_MODEL)

    all_suggestions: list[TagSuggestion] = []

    for collection_name, group_items in groups.items():
        # Cap the items sent per-group to avoid exceeding the context window.
        # The full group is still tagged — items beyond _SAMPLE_SIZE reuse the
        # same collection context so the LLM guidance stays coherent.
        prompt_items = group_items[:_SAMPLE_SIZE]
        items_block = "\n".join(
            f"- ID: {item.source_id} | Title: {item.title} | URL: {item.url}"
            + (f" | Note: {item.excerpt}" if item.excerpt else "")
            for item in prompt_items
        )

        prompt = _TAG_PROMPT_TEMPLATE.format(
            collection=collection_name,
            items_block=items_block,
        )

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
            print(f"[analyze] LLM tag call failed ({collection_name}): {exc}", file=sys.stderr)
            continue

        all_suggestions.extend(_parse_tag_suggestions(response_text, group_items))

    return all_suggestions


def _parse_tag_suggestions(
    response_text: str, items: list[Item]
) -> list[TagSuggestion]:
    """Parse the LLM response text into TagSuggestion objects.

    Scans line by line for ``Item ID:`` blocks.  Unknown IDs and tags not in
    the allowed sets are silently filtered.

    Args:
        response_text: Raw text returned by the LLM.
        items: The same item list that was sent to the LLM (used to look up
            title and validate IDs).

    Returns:
        List of TagSuggestion objects in the order they appear in the
        response.  Empty list if nothing parseable is found.
    """
    items_by_id: dict[str, Item] = {item.source_id: item for item in items}

    suggestions: list[TagSuggestion] = []

    current_source_id: Optional[str] = None
    current_use_case_tags: list[str] = []
    current_status_tags: list[str] = []
    current_reasoning: Optional[str] = None

    def _flush() -> None:
        if current_source_id is None:
            return
        item = items_by_id.get(current_source_id)
        if item is None:
            return
        item_id = _stable_id(current_source_id)

        suggestions.append(
            TagSuggestion(
                item_id=item_id,
                item_title=item.title,
                use_case_tags=list(current_use_case_tags),
                status_tags=list(current_status_tags),
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
            current_source_id = value
            current_use_case_tags = []
            current_status_tags = []
            current_reasoning = None
        elif key == "use-case tags":
            raw_tags = [t.strip() for t in value.split(",") if t.strip()]
            current_use_case_tags = [t for t in raw_tags if t in _VALID_USE_CASE_TAGS]
        elif key == "status tags":
            raw_tags = [t.strip() for t in value.split(",") if t.strip()]
            current_status_tags = [t for t in raw_tags if t in _VALID_STATUS_TAGS]
        elif key == "reasoning":
            _, _, rest_full = raw_line.strip().partition(":")
            current_reasoning = rest_full.strip()

    _flush()

    return suggestions
