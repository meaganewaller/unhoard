"""Interactive CLI for reviewing and refining collection and tag suggestions.

Public API
----------
review_collections_interactive(suggestions) -> list[CollectionSuggestion]
_edit_collections_interactive(suggestions, collections) -> list[CollectionSuggestion]
review_tags_interactive(suggestions, by_collection, collections) -> list[TagSuggestion]
_edit_collection_tags_interactive(items) -> list[TagSuggestion]
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from unhoard.types import CollectionSuggestion, TagSuggestion

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


# ---------------------------------------------------------------------------
# Tag review
# ---------------------------------------------------------------------------

_TAG_SAMPLE_SIZE = 20  # max items shown per collection for detailed review


def review_tags_interactive(
    suggestions: list[TagSuggestion],
    by_collection: bool = True,
    collections: Optional[dict[int, str]] = None,
) -> list[TagSuggestion]:
    """Interactive CLI review of tag suggestions.

    User can:
    - Accept all tags
    - Accept/reject per collection
    - Edit individual item tags

    Returns:
        Reviewed and potentially modified suggestions.
    """
    if not suggestions:
        return []

    print("\n=== REVIEW TAG SUGGESTIONS ===\n")

    if not by_collection:
        # Flat review — single prompt for the whole batch
        answer = input(f"Accept all {len(suggestions)} tag suggestions? [y/n/edit]: ").strip().lower()
        if answer == "y":
            return list(suggestions)
        if answer == "n":
            return []
        if answer == "edit":
            return _edit_collection_tags_interactive(suggestions)
        # Unrecognized — treat as accept
        return list(suggestions)

    # Group by collection_id when collections map is provided, otherwise treat as
    # a single unnamed group so the function still works without a map.
    if collections:
        groups: dict[str, list[TagSuggestion]] = defaultdict(list)
        for s in suggestions:
            # TagSuggestion doesn't carry a collection_id; look up by item_id if
            # a mapping is provided, otherwise fall into a single "All items" group.
            group_name = collections.get(s.item_id, "All items")
            groups[group_name].append(s)
    else:
        groups = {"All items": list(suggestions)}

    result: list[TagSuggestion] = []
    for group_name, items in groups.items():
        print(f"\nCollection: {group_name} ({len(items)} items)")
        _print_tag_sample(items[:_TAG_SAMPLE_SIZE])

        answer = input(f"Accept all tags for '{group_name}'? [y/n/edit]: ").strip().lower()
        if answer == "y":
            result.extend(items)
        elif answer == "n":
            # Drop this group's items (user rejected them)
            pass
        elif answer == "edit":
            # Show first 20 for interactive editing; auto-accept the rest
            edited = _edit_collection_tags_interactive(items[:_TAG_SAMPLE_SIZE])
            result.extend(edited)
            result.extend(items[_TAG_SAMPLE_SIZE:])
        else:
            # Unrecognized response — accept as-is
            result.extend(items)

    return result


def _edit_collection_tags_interactive(items: list[TagSuggestion]) -> list[TagSuggestion]:
    """Edit tags for items in a single collection.

    For each item the user is shown the title and current tags, then asked:
    - 'y'  — keep as-is
    - 'n'  — drop the item from results
    - 'e'  — enter new use_case_tags and status_tags (comma-separated)

    Returns the reviewed (and possibly edited/pruned) list.
    """
    result: list[TagSuggestion] = []
    for item in items:
        use_case_str = ", ".join(item.use_case_tags)
        status_str = ", ".join(item.status_tags)
        print(f"\n  [{item.item_id}] {item.item_title}")
        print(f"       use-case: {use_case_str}")
        print(f"       status  : {status_str}")
        answer = input("  Keep? [y/n/e]: ").strip().lower()

        if answer == "n":
            # Drop this item
            continue
        elif answer == "e":
            new_use_case_raw = input("  New use-case tags (comma-separated): ").strip()
            new_status_raw = input("  New status tags (comma-separated): ").strip()
            item.use_case_tags = [t.strip() for t in new_use_case_raw.split(",") if t.strip()]
            item.status_tags = [t.strip() for t in new_status_raw.split(",") if t.strip()]
            result.append(item)
        else:
            # 'y' or anything else — keep as-is
            result.append(item)

    return result


def _print_tag_sample(items: list[TagSuggestion]) -> None:
    """Print a compact summary of tag suggestions."""
    for item in items:
        use_case_str = ", ".join(item.use_case_tags) or "(none)"
        status_str = ", ".join(item.status_tags) or "(none)"
        print(f"  [{item.item_id}] {item.item_title}")
        print(f"       use-case: {use_case_str}  |  status: {status_str}")
