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

import json
import os
import sys
import time
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
BATCH_API_URL = "https://api.anthropic.com/v1/messages/batches"
DEFAULT_MODEL = "claude-sonnet-5"

# Batches bill every token at 50%. analyze is a good fit -- it is not
# latency-sensitive, and chunking already produced the natural unit of work.
# Most batches finish well inside an hour; the API's own ceiling is 24h.
_BATCH_POLL_INTERVAL = 30.0
_BATCH_TIMEOUT = 24 * 60 * 60.0

# Items sampled to derive the collection taxonomy before a batch run. The
# synchronous path threads each chunk's collection names into the next prompt
# so the taxonomy converges; a batch submits every chunk at once, so there is
# no such feedback and the collections would fragment into near-duplicates.
# One small up-front call fixes the taxonomy that all chunks then classify
# against, which is both cheaper and more consistent than the sync carry-over.
_TAXONOMY_SAMPLE_SIZE = 150

# Items per request. The whole corpus used to go in a single call against a flat
# max_tokens=8192, which truncated the response after ~130 items -- every item
# beyond that was paid for on the way in and never came back. Chunking keeps
# each request's demanded output comfortably inside its own budget.
_CHUNK_SIZE = 100

# Output budget per item, sized for the compact one-line format below (~15
# tokens) with headroom for long collection names. Output bills at 5x input,
# so this is the number that actually drives cost.
_OUTPUT_TOKENS_PER_ITEM = 24
_MIN_OUTPUT_TOKENS = 512


def _max_tokens_for(item_count: int) -> int:
    """Size the output budget to the request instead of a flat 8192."""
    return max(_MIN_OUTPUT_TOKENS, item_count * _OUTPUT_TOKENS_PER_ITEM)


_PROMPT_TEMPLATE = """\
You are helping to organize a personal bookmark collection. \
Analyze the items below and assign each one to a collection.

Use 8–15 collections overall to group this content. \
Each collection should have a clear semantic purpose and be one level deep \
(e.g. "Development" or "Design").
{existing_block}
Items ({count}):
{items_block}

Output exactly one line per item and nothing else -- no preamble, no blank \
lines, no explanation:
<item id> | <collection name> | high|medium|low | <alternative collection, or none>
"""

_EXISTING_COLLECTIONS_BLOCK = """
Collections already chosen for earlier items -- reuse these names exactly \
wherever they fit, and only introduce a new collection when none of them work:
{names}
"""


def _existing_block(names: list[str]) -> str:
    if not names:
        return ""
    return _EXISTING_COLLECTIONS_BLOCK.format(
        names="\n".join(f"- {name}" for name in sorted(names))
    )


def _items_block(items: list[Item]) -> str:
    return "\n".join(
        f"- ID: {item.source_id} | Title: {item.title} | URL: {item.url}"
        + (f" | Note: {item.excerpt}" if item.excerpt else "")
        for item in items
    )


def _chunked(items: list[Item], size: int) -> list[list[Item]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


_TAXONOMY_PROMPT_TEMPLATE = """\
You are helping to organize a personal bookmark collection. \
Below is a sample of {count} items from it.

Propose 8–15 collections that would naturally group this content. \
Each collection should have a clear semantic purpose and be one level deep \
(e.g. "Development" or "Design"). They must cover the whole library, not just \
the sample.

Sample items:
{items_block}

Output one collection name per line and nothing else -- no numbering, no \
preamble, no explanation.
"""


def derive_taxonomy(
    items: list[Item],
    sample_size: int = _TAXONOMY_SAMPLE_SIZE,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> list[str]:
    """Ask for the collection names once, from a sample, before classifying.

    Returns [] if the call fails, which callers treat as "no taxonomy to seed
    with" rather than an error -- classification still works, it just has less
    to anchor on.
    """
    if not items:
        return []
    api_key, model = _resolve_credentials(api_key, model)
    sample = items[:sample_size]
    prompt = _TAXONOMY_PROMPT_TEMPLATE.format(
        count=len(sample), items_block=_items_block(sample)
    )
    try:
        text = _call_llm(prompt, 1024, api_key, model)
    except Exception as exc:  # noqa: BLE001 — seeding is best-effort
        print(f"[analyze] taxonomy call failed: {exc}", file=sys.stderr)
        return []

    names: list[str] = []
    for line in (text or "").splitlines():
        name = line.strip().lstrip("-*0123456789. ").strip()
        if name and name not in names:
            names.append(name)
    return names


def _run_batch(
    prompts: dict[str, tuple[str, int]],
    api_key: str,
    model: str,
    poll_interval: float,
    timeout: float,
) -> dict[str, str]:
    """Submit one batch, wait for it, and return custom_id -> response text.

    Results come back in arbitrary order, so everything is keyed by custom_id
    rather than position. A request that errored, expired, or was canceled is
    simply absent from the result, which callers treat the same as a failed
    chunk on the synchronous path.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    submit = requests.post(
        BATCH_API_URL,
        headers=headers,
        json={
            "requests": [
                {
                    "custom_id": custom_id,
                    "params": {
                        "model": model,
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                }
                for custom_id, (prompt, max_tokens) in prompts.items()
            ]
        },
        timeout=120,
    )
    submit.raise_for_status()
    batch_id = submit.json()["id"]
    print(
        f"[analyze] submitted batch {batch_id} ({len(prompts)} requests) "
        f"at 50% of standard token rates; waiting for it to finish",
        file=sys.stderr,
    )

    deadline = time.monotonic() + timeout
    while True:
        status_resp = requests.get(f"{BATCH_API_URL}/{batch_id}", headers=headers, timeout=60)
        status_resp.raise_for_status()
        if status_resp.json().get("processing_status") == "ended":
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"batch {batch_id} did not finish within {timeout:.0f}s; "
                f"it may still complete -- results stay available for 29 days"
            )
        time.sleep(poll_interval)

    results_resp = requests.get(f"{BATCH_API_URL}/{batch_id}/results", headers=headers, timeout=300)
    results_resp.raise_for_status()

    out: dict[str, str] = {}
    for line in results_resp.text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = record.get("result", {})
        if result.get("type") != "succeeded":
            print(
                f"[analyze] batch request {record.get('custom_id')} "
                f"{result.get('type', 'failed')}",
                file=sys.stderr,
            )
            continue
        blocks = result.get("message", {}).get("content", [])
        out[record["custom_id"]] = "\n".join(
            b["text"] for b in blocks if b.get("type") == "text"
        ).strip()
    return out


def _resolve_credentials(
    api_key: Optional[str], model: Optional[str]
) -> tuple[str, str]:
    """Prefer explicitly-passed values, falling back to the environment.

    Callers should pass resolved ``Config`` values. The environment fallback
    exists only so direct library use still works without a Config; it must
    never override an argument, which is what made ``model`` in config.toml
    silently ineffective on the most expensive code path.
    """
    resolved_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    resolved_model = model or os.environ.get("UNHOARD_MODEL") or DEFAULT_MODEL
    return resolved_key, resolved_model


def _call_llm(prompt: str, max_tokens: int, api_key: str, model: str) -> Optional[str]:
    """Single Messages API call. Returns None on any failure -- callers treat a
    failed chunk as "no suggestions for these items" and keep the rest."""
    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(text_blocks).strip()


def suggest_collections(
    items: list[Item],
    limit: int = 200,
    chunk_size: int = _CHUNK_SIZE,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    batch: bool = False,
    poll_interval: float = _BATCH_POLL_INTERVAL,
    timeout: float = _BATCH_TIMEOUT,
) -> list[CollectionSuggestion]:
    """Use the LLM to cluster items and suggest collection assignments.

    Items are processed in chunks so every item that is paid for on the way in
    actually gets an assignment back. Collection names chosen in earlier chunks
    are fed into later ones, so the taxonomy converges instead of fragmenting
    into per-chunk near-duplicates.

    Args:
        items: List of items to analyze.
        limit: Maximum number of items to process.
        chunk_size: Items per API request.
        api_key: Anthropic API key. Callers should pass the resolved
            ``Config.anthropic_api_key``; the environment is only a fallback.
        model: Model id. Callers should pass a resolved ``Config`` value --
            reading the environment here made ``model`` in config.toml a no-op.
        batch: Submit the chunks through the Message Batches API, which bills
            every token at 50%. Trades latency for cost: the call blocks until
            the batch finishes, which is usually minutes but can be hours.
        poll_interval: Seconds between batch status checks.
        timeout: Seconds to wait for a batch before giving up.

    Returns:
        List of CollectionSuggestion objects, one per recognized item. A chunk
        whose request fails contributes nothing but does not discard the chunks
        that succeeded.
    """
    if not items:
        return []

    working = items[:limit]
    api_key, model = _resolve_credentials(api_key, model)

    if batch:
        return _suggest_collections_batched(
            working, chunk_size, api_key, model, poll_interval, timeout
        )

    suggestions: list[CollectionSuggestion] = []
    seen_collections: list[str] = []

    for chunk in _chunked(working, chunk_size):
        prompt = _PROMPT_TEMPLATE.format(
            count=len(chunk),
            items_block=_items_block(chunk),
            existing_block=_existing_block(seen_collections),
        )
        try:
            response_text = _call_llm(prompt, _max_tokens_for(len(chunk)), api_key, model)
        except Exception as exc:  # noqa: BLE001 — one bad chunk shouldn't lose the rest
            print(f"[analyze] LLM call failed: {exc}", file=sys.stderr)
            continue

        chunk_suggestions = _parse_collection_suggestions(response_text or "", chunk)
        suggestions.extend(chunk_suggestions)
        for s in chunk_suggestions:
            if s.suggested_collection not in seen_collections:
                seen_collections.append(s.suggested_collection)

    return suggestions


def _suggest_collections_batched(
    working: list[Item],
    chunk_size: int,
    api_key: str,
    model: str,
    poll_interval: float,
    timeout: float,
) -> list[CollectionSuggestion]:
    """Derive the taxonomy once, then classify every chunk against it in one batch.

    The synchronous path converges the taxonomy by feeding each chunk's
    collection names into the next prompt. A batch has no such feedback -- every
    chunk is submitted at once -- so the taxonomy is fixed up front instead.
    """
    taxonomy = derive_taxonomy(working, api_key=api_key, model=model)

    chunks = _chunked(working, chunk_size)
    prompts = {
        f"chunk-{i}": (
            _PROMPT_TEMPLATE.format(
                count=len(chunk),
                items_block=_items_block(chunk),
                existing_block=_existing_block(taxonomy),
            ),
            _max_tokens_for(len(chunk)),
        )
        for i, chunk in enumerate(chunks)
    }

    try:
        results = _run_batch(prompts, api_key, model, poll_interval, timeout)
    except Exception as exc:  # noqa: BLE001 — never fatal
        print(f"[analyze] batch failed: {exc}", file=sys.stderr)
        return []

    # Key by custom_id, never by position: the Batches API returns results in
    # arbitrary order, so zipping them against the chunk list would silently
    # assign one chunk's collections to another chunk's items.
    suggestions: list[CollectionSuggestion] = []
    for i, chunk in enumerate(chunks):
        text = results.get(f"chunk-{i}")
        if text is None:
            continue
        suggestions.extend(_parse_collection_suggestions(text, chunk))
    return suggestions


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

    def _compact(line: str) -> bool:
        """Parse `<id> | <collection> | <confidence> | <conflict>`.

        Returns True if the line was consumed. A line only counts as compact
        when its first field is an id we actually sent, which keeps prose and
        legacy `Key: value` lines from being misread as data.
        """
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or parts[0] not in items_by_id:
            return False
        item = items_by_id[parts[0]]
        confidence = parts[2].lower() if len(parts) > 2 else "medium"
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        conflict = parts[3] if len(parts) > 3 else ""
        suggestions.append(
            CollectionSuggestion(
                item_id=_stable_id(parts[0]),
                item_title=item.title,
                suggested_collection=parts[1] or "Uncategorized",
                confidence=confidence,
                conflict=None if not conflict or conflict.lower() == "none" else conflict,
            )
        )
        return True

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
        if not line:
            continue
        if _compact(line):
            continue
        if ":" not in line:
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

Output exactly one line per item and nothing else -- no preamble, no blank \
lines, no explanation:
<item id> | <comma-separated use-case tags, or none> | <comma-separated status tags, or none>
"""


def suggest_tags(
    items: list[Item],
    collections: dict[int, str],
    chunk_size: int = _CHUNK_SIZE,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    batch: bool = False,
    poll_interval: float = _BATCH_POLL_INTERVAL,
    timeout: float = _BATCH_TIMEOUT,
) -> list[TagSuggestion]:
    """Use the LLM to suggest use-case and status tags for items.

    Groups items by their suggested collection so each batch shares semantic
    context, then sends one LLM request per collection group.

    Args:
        items: List of items to tag.
        collections: Mapping of item_id -> suggested_collection_name.  Items
            absent from this dict are placed in an "Uncategorized" batch.
        batch: Submit all groups through the Message Batches API at 50% of
            standard token rates, trading latency for cost.
        poll_interval: Seconds between batch status checks.
        timeout: Seconds to wait for a batch before giving up.

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

    api_key, model = _resolve_credentials(api_key, model)

    if batch:
        return _suggest_tags_batched(
            groups, chunk_size, api_key, model, poll_interval, timeout
        )

    all_suggestions: list[TagSuggestion] = []

    for collection_name, group_items in groups.items():
        # Chunk rather than truncate. This previously sent only the first 50
        # items of a group while claiming the whole group was still tagged --
        # everything past that was never in the prompt, so it never came back.
        for chunk in _chunked(group_items, chunk_size):
            prompt = _TAG_PROMPT_TEMPLATE.format(
                collection=collection_name,
                items_block=_items_block(chunk),
            )
            try:
                response_text = _call_llm(
                    prompt, _max_tokens_for(len(chunk)), api_key, model
                )
            except Exception as exc:  # noqa: BLE001 — one bad chunk shouldn't lose the rest
                print(
                    f"[analyze] LLM tag call failed ({collection_name}): {exc}",
                    file=sys.stderr,
                )
                continue

            all_suggestions.extend(_parse_tag_suggestions(response_text or "", chunk))

    return all_suggestions


def _suggest_tags_batched(
    groups: dict[str, list[Item]],
    chunk_size: int,
    api_key: str,
    model: str,
    poll_interval: float,
    timeout: float,
) -> list[TagSuggestion]:
    """Every collection group's chunks go out as one batch.

    Unlike collection assignment, tagging needs no taxonomy pass: the tag
    vocabulary is fixed in the prompt, so the groups are already independent.
    """
    prompts: dict[str, tuple[str, int]] = {}
    chunk_items: dict[str, list[Item]] = {}
    for g, (collection_name, group_items) in enumerate(groups.items()):
        for c, chunk in enumerate(_chunked(group_items, chunk_size)):
            custom_id = f"group-{g}-chunk-{c}"
            prompts[custom_id] = (
                _TAG_PROMPT_TEMPLATE.format(
                    collection=collection_name, items_block=_items_block(chunk)
                ),
                _max_tokens_for(len(chunk)),
            )
            chunk_items[custom_id] = chunk

    if not prompts:
        return []

    try:
        results = _run_batch(prompts, api_key, model, poll_interval, timeout)
    except Exception as exc:  # noqa: BLE001 — never fatal
        print(f"[analyze] batch tag call failed: {exc}", file=sys.stderr)
        return []

    suggestions: list[TagSuggestion] = []
    for custom_id, chunk in chunk_items.items():
        text = results.get(custom_id)
        if text is not None:
            suggestions.extend(_parse_tag_suggestions(text, chunk))
    return suggestions


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

    def _compact(line: str) -> bool:
        """Parse `<id> | <use-case tags> | <status tags>`. See the collection
        parser's twin for why the id must match something we sent."""
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or parts[0] not in items_by_id:
            return False
        item = items_by_id[parts[0]]

        def _tags(raw: str, allowed: frozenset[str]) -> list[str]:
            if not raw or raw.lower() == "none":
                return []
            return [t.strip() for t in raw.split(",") if t.strip() in allowed]

        suggestions.append(
            TagSuggestion(
                item_id=_stable_id(parts[0]),
                item_title=item.title,
                use_case_tags=_tags(parts[1], _VALID_USE_CASE_TAGS),
                status_tags=_tags(parts[2] if len(parts) > 2 else "", _VALID_STATUS_TAGS),
            )
        )
        return True

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
        if not line:
            continue
        if _compact(line):
            continue
        if ":" not in line:
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
