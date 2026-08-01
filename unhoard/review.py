"""Interactive CLI for reviewing and refining collection suggestions.

Public API
----------
review_collections_interactive(suggestions) -> list[CollectionSuggestion]
_edit_collections_interactive(suggestions, collections) -> list[CollectionSuggestion]
"""
from __future__ import annotations

from collections import defaultdict

from unhoard.types import CollectionSuggestion

_SAMPLE_SIZE = 3  # max items to show per collection in the summary


def review_collections_interactive(
    suggestions: list[CollectionSuggestion],
) -> list[CollectionSuggestion]:
    """Interactive CLI review of collection suggestions.

    User can:
    - Accept all suggestions
    - Rename collections
    - Merge collections
    - Reassign individual items
    - Cancel (returns empty list)

    Returns:
        Reviewed and potentially modified suggestions.
    """
    if not suggestions:
        return []

    # Group by collection name for display and editing.
    # We keep a mutable working copy so edits don't affect the original list
    # until the user accepts.
    working: list[CollectionSuggestion] = list(suggestions)

    while True:
        collections: dict[str, list[CollectionSuggestion]] = defaultdict(list)
        for s in working:
            collections[s.suggested_collection].append(s)

        _print_summary(collections)
        answer = input("Looks good? [y/n/edit]: ").strip().lower()

        if answer == "y":
            return working
        if answer == "n":
            return []
        if answer == "edit":
            working = _edit_collections_interactive(working, collections)
            # Continue loop to show updated summary and re-prompt


def _edit_collections_interactive(
    suggestions: list[CollectionSuggestion],
    collections: dict[str, list[CollectionSuggestion]],
) -> list[CollectionSuggestion]:
    """Interactive editing of collection assignments.

    Menu options: rename, merge, reassign, done.
    """
    while True:
        print("\nEdit options: rename, merge, reassign, done")
        action = input("Action: ").strip().lower()

        if action == "done":
            break

        elif action == "rename":
            old_name = input("Collection to rename: ").strip()
            new_name = input("New name: ").strip()
            if old_name in collections:
                items = collections.pop(old_name)
                for item in items:
                    item.suggested_collection = new_name
                collections[new_name].extend(items)

        elif action == "merge":
            source = input("Collection to merge (source): ").strip()
            target = input("Merge into (target): ").strip()
            if source in collections:
                items = collections.pop(source)
                for item in items:
                    item.suggested_collection = target
                collections[target].extend(items)

        elif action == "reassign":
            item_id_str = input("Item ID to reassign: ").strip()
            new_collection = input("New collection name: ").strip()
            try:
                item_id = int(item_id_str)
            except ValueError:
                print(f"Invalid item ID: {item_id_str!r}")
                continue
            _reassign_item(collections, item_id, new_collection)

        else:
            # Unknown action — ignore and continue
            pass

    return _flatten(collections)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _print_summary(collections: dict[str, list[CollectionSuggestion]]) -> None:
    """Print a human-readable summary of collection structure."""
    print("\n=== REVIEW COLLECTION STRUCTURE ===")
    for name, items in sorted(collections.items()):
        sample = [s.item_title for s in items[:_SAMPLE_SIZE]]
        extra = len(items) - len(sample)
        sample_str = ", ".join(sample)
        if extra > 0:
            sample_str += f" (+{extra} more)"
        print(f"  {name} ({len(items)} items): {sample_str}")
    print()


def _flatten(
    collections: dict[str, list[CollectionSuggestion]],
) -> list[CollectionSuggestion]:
    """Flatten the collections dict back to a flat list preserving order."""
    result: list[CollectionSuggestion] = []
    for items in collections.values():
        result.extend(items)
    return result


def _reassign_item(
    collections: dict[str, list[CollectionSuggestion]],
    item_id: int,
    new_collection: str,
) -> None:
    """Move a single item to a new collection, removing it from its current one."""
    for name, items in list(collections.items()):
        for item in items:
            if item.item_id == item_id:
                items.remove(item)
                if not items:
                    del collections[name]
                item.suggested_collection = new_collection
                collections[new_collection].append(item)
                return
