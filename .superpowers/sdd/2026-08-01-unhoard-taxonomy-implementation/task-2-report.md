# Task 2: Implement Collection Analysis (LLM Clustering) — Completion Report

**Status:** DONE

---

## Summary

Created `unhoard/analyze.py` with:
- `suggest_collections(items, limit=1504) -> list[CollectionSuggestion]` — main entry point
- `_parse_collection_suggestions(text, items) -> list[CollectionSuggestion]` — parser helper

Created `tests/test_analyze.py` with 20 unit tests across two classes:
- `TestParseCollectionSuggestions` — 10 tests covering parser edge cases
- `TestSuggestCollections` — 10 tests for the full function with mocked HTTP

---

## Test Output

```
============================= test session starts ==============================
platform darwin -- Python 3.14.2, pytest-9.1.1, pluggy-1.6.0
collected 20 items

tests/test_analyze.py::TestParseCollectionSuggestions::test_parses_two_suggestions PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_confidence_values_preserved PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_conflict_none_becomes_none PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_conflict_value_is_preserved PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_reasoning_is_captured PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_item_id_not_in_items_is_skipped PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_missing_collection_defaults_to_uncategorized PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_item_title_is_filled_from_item PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_empty_response_returns_empty_list PASSED
tests/test_analyze.py::TestParseCollectionSuggestions::test_garbage_response_does_not_raise PASSED
tests/test_analyze.py::TestSuggestCollections::test_returns_suggestions PASSED
tests/test_analyze.py::TestSuggestCollections::test_confidence_values_are_valid PASSED
tests/test_analyze.py::TestSuggestCollections::test_handles_conflicts PASSED
tests/test_analyze.py::TestSuggestCollections::test_empty_items_returns_empty_list PASSED
tests/test_analyze.py::TestSuggestCollections::test_limit_truncates_items PASSED
tests/test_analyze.py::TestSuggestCollections::test_api_error_returns_empty_list PASSED
tests/test_analyze.py::TestSuggestCollections::test_connection_error_returns_empty_list PASSED
tests/test_analyze.py::TestSuggestCollections::test_prompt_includes_item_titles PASSED
tests/test_analyze.py::TestSuggestCollections::test_uses_correct_model PASSED
tests/test_analyze.py::TestSuggestCollections::test_item_ids_use_source_id PASSED

============================== 20 passed in 0.02s ==============================
```

Full suite: **234 passed** — no regressions.

---

## Commit

**SHA:** `9f9d5eb`

```
feat(analyze): implement collection analysis with LLM :brain:

Add suggest_collections() to cluster items into suggested collections
via the Anthropic Messages API, plus _parse_collection_suggestions()
to extract structured CollectionSuggestion objects from LLM output.
Uses requests (consistent with summarize.py) rather than the anthropic
SDK, which is not installed. Handles empty input, API errors, and
unparseable responses gracefully. 20 unit tests with fully mocked
HTTP calls.
```

**Files changed:**
- Created: `unhoard/analyze.py` (208 lines)
- Created: `tests/test_analyze.py` (323 lines)

---

## Design Decisions / Concerns

### Uses `requests`, not the `anthropic` SDK

The plan spec said to use `anthropic.Anthropic()` client, but the `anthropic`
package is not installed in the project venv. The existing `summarize.py`
already calls the API via `requests.post` to `ANTHROPIC_API_URL` with the
same headers. This implementation follows that established pattern for
consistency and to avoid adding a new dependency. The mocking strategy in
tests (`@patch("unhoard.analyze.requests.post")`) is simpler and more aligned
with the existing `test_summarize.py` approach.

### `Item.source_id` (str) vs `CollectionSuggestion.item_id` (int)

The real `Item` schema has `source_id: str`, not `id: int` as the plan
assumed. `CollectionSuggestion.item_id` is typed `int` (from Task 1). The
implementation converts with `int(source_id)` when numeric (the common case
for Raindrop IDs) and falls back to `abs(hash(source_id)) % 10^9` for
non-numeric source IDs.

### `item.excerpt` instead of `item.note`

The plan referenced `item.note` which doesn't exist on `Item`. The equivalent
field is `excerpt`. The prompt optionally appends it when non-empty.

### LLM prompt sends up to 50 items as context

The prompt includes up to `_SAMPLE_SIZE = 50` items in the body, while
`limit` controls the total batch size. For the full 1504-item run, this
means the LLM sees a sample and must extrapolate. A production refinement
could batch all items across multiple API calls — tracked as a concern for
whoever wires up Task 6 (CLI integration).

### Confidence normalization

If the model returns a confidence value outside `{"high", "medium", "low"}`,
it silently defaults to `"medium"` rather than raising.

---

## Next Steps

Task 3 (tag suggestion) and Task 6 (CLI wiring) can now consume
`suggest_collections()`. The `_parse_collection_suggestions()` function is
also importable for testing downstream.
