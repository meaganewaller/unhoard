# Task 6 Report: Wire Up CLI Command

**Status:** Complete

## Changes Made

### `unhoard/state.py`

Added two new methods to `StateStore`:

- **`fetch_untagged_items(limit: int = 1504) -> list[Item]`**
  Queries for active items where `suggested_collection` is NULL or empty, ordered oldest-first, up to `limit` rows. Returns proper `Item` dataclass instances (not raw `sqlite3.Row`).

- **`bulk_store_suggestions(items, collection_suggestions, tag_suggestions) -> None`**
  Resolves `CollectionSuggestion` and `TagSuggestion` objects back to DB rows by matching `item_id` (int) to `source_id` using the same numeric/hash resolution as `analyze.py`. Writes `suggested_collection` and `suggested_tags` (JSON array) in a single cursor context per call.

Also added the import: `from .types import CollectionSuggestion, TagSuggestion`.

### `unhoard/cli.py`

Added imports for `suggest_collections`, `suggest_tags`, `review_collections_interactive`, and `review_tags_interactive`.

Added **`cmd_analyze(args)`** implementing the full pipeline:

1. `store.fetch_untagged_items(limit=args.items)` — fetch candidates
2. `suggest_collections(items)` — LLM clustering
3. `review_collections_interactive(suggestions)` — user review (skipped with `--auto-apply`)
4. Build `collections_map: dict[int, str]` for tag grouping
5. `suggest_tags(items, collections_map)` — LLM tagging
6. `review_tags_interactive(suggestions, by_collection=True, collections=collections_map)` — user review (skipped with `--auto-apply`)
7. `store.bulk_store_suggestions(...)` — persist
8. Prompt to sync to Raindrop (interactive terminals only; prints `apply-all` tip if user says yes)

Registered the subcommand in `build_parser()`:
```
unhoard analyze [--items N] [--auto-apply]
```

## Verification

- `python -m unhoard.cli analyze --help` — shows command with both options
- `python -m pytest -q` — 275/275 passed, no regressions
