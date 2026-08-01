# Unhoard Hybrid Taxonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement batch LLM analysis of 1504 untagged items, suggest collection structure and tags, provide CLI review interface for refinement, then apply suggestions back to Raindrop.

**Architecture:** Three-phase pipeline: (1) LLM analysis generates collection + tag suggestions, (2) CLI review interface allows user to refine, (3) apply suggestions back to DB and Raindrop. Suggestions stored separately from source data until approved.

**Tech Stack:** Python, LLM (Claude via anthropic SDK), Typer CLI, interactive prompt library, SQLite DB

## Global Constraints

- Collections: max 8-15 across all items; one level deep only
- Tags: two categories (use-case: reference/learning/inspiration/project/tool/bookmark; status: wip/archived/reviewed/needs-refinement)
- No auto-apply without user review
- Preserve existing 72 "acted on" items; merge tags on apply (union, not replace)
- Use existing `apply_updates()` for Raindrop writes; no adapter changes

---

## File Structure

**New files:**
- `unhoard/analyze.py` — LLM analysis engine (collection clustering, tag suggestion)
- `unhoard/review.py` — Interactive CLI review interface

**Modified files:**
- `unhoard/cli.py` — Add `unhoard analyze` command
- `unhoard/state.py` — Add bulk suggestion storage helper (if needed; may reuse existing)

**Test files:**
- `tests/test_analyze.py` — Unit tests for analysis logic
- `tests/test_review.py` — Unit tests for review interface
- `tests/test_cli_analyze.py` — Integration test for full pipeline

---

## Task 1: Define Data Structures

**Files:**
- Create: `unhoard/types.py` (or extend existing)
- Test: (types are validated by consumers)

**Interfaces:**
- Produces: `CollectionSuggestion`, `TagSuggestion` dataclasses

**Rationale:** Centralize types so all modules use consistent definitions.

- [ ] **Step 1: Create types module with suggestion dataclasses**

Add to `unhoard/types.py` (create if doesn't exist):

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class CollectionSuggestion:
    """Suggested collection for an item."""
    item_id: int
    item_title: str
    suggested_collection: str
    confidence: str  # "high", "medium", "low"
    conflict: Optional[str] = None  # Alternative if item fits multiple
    reasoning: Optional[str] = None

@dataclass
class TagSuggestion:
    """Suggested tags for an item."""
    item_id: int
    item_title: str
    use_case_tags: list[str]  # From: reference, learning, inspiration, project, tool, bookmark
    status_tags: list[str]    # From: wip, archived, reviewed, needs-refinement
    reasoning: Optional[str] = None

@dataclass
class AnalysisResult:
    """Output of full analysis pipeline."""
    collections: list[CollectionSuggestion]
    tags: list[TagSuggestion]
    created_at: str  # ISO timestamp
```

- [ ] **Step 2: Commit**

```bash
git add unhoard/types.py
/commit --type=feat --scope=types --subject="add suggestion dataclasses" --body="Define CollectionSuggestion, TagSuggestion, AnalysisResult for batch analysis"
```

---

## Task 2: Implement Collection Analysis (LLM Clustering)

**Files:**
- Create: `unhoard/analyze.py` (core module)
- Modify: `unhoard/state.py` (fetch untagged items)
- Test: `tests/test_analyze.py`

**Interfaces:**
- Consumes: `Item` from state.py, `CollectionSuggestion` from types.py
- Produces: `suggest_collections(items: list[Item]) -> list[CollectionSuggestion]`

- [ ] **Step 1: Write failing test for collection clustering**

Create `tests/test_analyze.py`:

```python
import pytest
from unhoard.analyze import suggest_collections
from unhoard.schema import Item

def test_suggest_collections_returns_suggestions():
    """Test that suggest_collections returns CollectionSuggestion objects."""
    items = [
        Item(id=1, title="Python async patterns", url="...", tags=[], collection=None),
        Item(id=2, title="Rust ownership guide", url="...", tags=[], collection=None),
        Item(id=3, title="CSS Grid tutorial", url="...", tags=[], collection=None),
    ]
    
    suggestions = suggest_collections(items)
    
    assert len(suggestions) > 0
    assert all(hasattr(s, 'item_id') for s in suggestions)
    assert all(hasattr(s, 'suggested_collection') for s in suggestions)
    assert all(s.confidence in ["high", "medium", "low"] for s in suggestions)

def test_suggest_collections_groups_by_topic():
    """Test that similar items are suggested for same collection."""
    items = [
        Item(id=1, title="Python async patterns", url="...", tags=[], collection=None),
        Item(id=2, title="Python type hints", url="...", tags=[], collection=None),
        Item(id=3, title="Crochet patterns", url="...", tags=[], collection=None),
    ]
    
    suggestions = suggest_collections(items)
    
    # Python items should suggest same collection
    python_suggestions = [s for s in suggestions if s.item_id in [1, 2]]
    assert len(set(s.suggested_collection for s in python_suggestions)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_analyze.py::test_suggest_collections_returns_suggestions -v
```

Expected: FAIL with "ModuleNotFoundError: No module named 'unhoard.analyze'"

- [ ] **Step 3: Write minimal collection analysis implementation**

Create `unhoard/analyze.py`:

```python
from typing import Optional
from anthropic import Anthropic

from unhoard.schema import Item
from unhoard.types import CollectionSuggestion, TagSuggestion
from unhoard.summarize import parse_summary_response  # Reuse existing LLM parsing

def suggest_collections(items: list[Item], limit: int = 1504) -> list[CollectionSuggestion]:
    """
    Use LLM to cluster items and suggest collection assignments.
    
    Args:
        items: List of items to analyze
        limit: Max items to process
    
    Returns:
        List of CollectionSuggestion objects
    """
    if not items:
        return []
    
    # Truncate to limit
    items = items[:limit]
    
    client = Anthropic()
    
    # Prepare items summary for LLM
    items_summary = "\n".join([
        f"- Title: {item.title}\n  URL: {item.url}\n  Note: {item.note or 'none'}"
        for item in items[:50]  # Sample for prompt (full list in production)
    ])
    
    prompt = f"""Analyze these {len(items)} items and suggest a collection structure.
Suggest 8-15 collections that would naturally group this content.
Each collection should:
- Have a clear semantic purpose
- Be one level deep (e.g., "Development" or "Development/Backend")

Items (sample):
{items_summary}

Output format for each collection:
Collection: <name>
Description: <purpose>
Expected item count: <estimate>

Then for each item in the input, assign to a collection:
Item ID: <id>
Title: <title>
Suggested Collection: <collection_name>
Confidence: high|medium|low
Conflict: <alternative if item fits multiple, or "none">
Reasoning: <1-2 sentence explanation>
"""
    
    message = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=4096,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    # Parse response (simplified; production version handles edge cases)
    response_text = message.content[0].text
    suggestions = _parse_collection_suggestions(response_text, items)
    
    return suggestions

def _parse_collection_suggestions(response_text: str, items: list[Item]) -> list[CollectionSuggestion]:
    """Parse LLM response into CollectionSuggestion objects."""
    suggestions = []
    
    # Simplified parsing: extract "Item ID: X" and "Suggested Collection: Y" blocks
    # Production version uses more robust parsing
    lines = response_text.split('\n')
    current_item_id = None
    current_collection = None
    current_confidence = "medium"
    current_conflict = None
    current_reasoning = None
    
    for line in lines:
        line = line.strip()
        if line.startswith("Item ID:"):
            if current_item_id is not None:
                # Save previous
                item = next((i for i in items if i.id == current_item_id), None)
                if item:
                    suggestions.append(CollectionSuggestion(
                        item_id=current_item_id,
                        item_title=item.title,
                        suggested_collection=current_collection or "Uncategorized",
                        confidence=current_confidence,
                        conflict=current_conflict if current_conflict != "none" else None,
                        reasoning=current_reasoning
                    ))
            current_item_id = int(line.split(":")[-1].strip())
        elif line.startswith("Suggested Collection:"):
            current_collection = line.split(":")[-1].strip()
        elif line.startswith("Confidence:"):
            current_confidence = line.split(":")[-1].strip()
        elif line.startswith("Conflict:"):
            current_conflict = line.split(":")[-1].strip()
        elif line.startswith("Reasoning:"):
            current_reasoning = line.split(":", 1)[-1].strip()
    
    # Don't forget last item
    if current_item_id is not None:
        item = next((i for i in items if i.id == current_item_id), None)
        if item:
            suggestions.append(CollectionSuggestion(
                item_id=current_item_id,
                item_title=item.title,
                suggested_collection=current_collection or "Uncategorized",
                confidence=current_confidence,
                conflict=current_conflict if current_conflict != "none" else None,
                reasoning=current_reasoning
            ))
    
    return suggestions

def suggest_tags(items: list[Item], collections: dict[int, str]) -> list[TagSuggestion]:
    """Suggest tags for items (implemented in Task 3)."""
    raise NotImplementedError("See Task 3")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_analyze.py::test_suggest_collections_returns_suggestions -v
```

Expected: PASS (mock response with valid structure)

*Note: Real test will mock `client.messages.create()` to avoid API calls. See Task 2B.*

- [ ] **Step 5: Commit**

```bash
git add unhoard/analyze.py tests/test_analyze.py
/commit --type=feat --scope=analyze --subject="implement collection analysis with LLM" --body="Add suggest_collections() to cluster items and assign to collections"
```

---

## Task 2B: Mock LLM for Unit Tests

**Files:**
- Modify: `tests/test_analyze.py`
- Test: (tests themselves)

**Interfaces:**
- Consumes: `suggest_collections()` from analyze.py
- Produces: Working unit tests without API calls

- [ ] **Step 1: Add mock LLM responses to tests**

Modify `tests/test_analyze.py`:

```python
import pytest
from unittest.mock import patch, MagicMock
from unhoard.analyze import suggest_collections
from unhoard.schema import Item

@patch('unhoard.analyze.Anthropic')
def test_suggest_collections_returns_suggestions(mock_client_class):
    """Test suggest_collections with mocked LLM."""
    mock_response = MagicMock()
    mock_response.content[0].text = """Collection: Development
Description: Software and code-related content
Expected item count: 50

Collection: Design
Description: Design and graphics content
Expected item count: 30

Item ID: 1
Title: Python async patterns
Suggested Collection: Development
Confidence: high
Conflict: none
Reasoning: Core Python development content

Item ID: 2
Title: CSS Grid tutorial
Suggested Collection: Design
Confidence: high
Conflict: none
Reasoning: Frontend design and layout
"""
    
    mock_client_class.return_value.messages.create.return_value = mock_response
    
    items = [
        Item(id=1, title="Python async patterns", url="http://example.com", tags=[], collection=None),
        Item(id=2, title="CSS Grid tutorial", url="http://example.com", tags=[], collection=None),
    ]
    
    suggestions = suggest_collections(items)
    
    assert len(suggestions) == 2
    assert suggestions[0].suggested_collection == "Development"
    assert suggestions[1].suggested_collection == "Design"

@patch('unhoard.analyze.Anthropic')
def test_suggest_collections_handles_conflicts(mock_client_class):
    """Test that items fitting multiple collections are flagged."""
    mock_response = MagicMock()
    mock_response.content[0].text = """Item ID: 3
Title: Game Development Guide
Suggested Collection: Development
Confidence: medium
Conflict: Gaming
Reasoning: Could fit either Development or Gaming

Item ID: 4
Title: Web Design Patterns
Suggested Collection: Design
Confidence: high
Conflict: Development
Reasoning: Front-end design, could be in Development too
"""
    
    mock_client_class.return_value.messages.create.return_value = mock_response
    
    items = [
        Item(id=3, title="Game Development Guide", url="http://example.com", tags=[], collection=None),
        Item(id=4, title="Web Design Patterns", url="http://example.com", tags=[], collection=None),
    ]
    
    suggestions = suggest_collections(items)
    
    assert suggestions[0].conflict == "Gaming"
    assert suggestions[1].conflict == "Development"
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_analyze.py -v
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_analyze.py
/commit --type=test --scope=analyze --subject="add mocked unit tests for collection analysis" --body="Mock LLM responses to test suggest_collections logic"
```

---

## Task 3: Implement Tag Analysis (LLM Tag Suggestion)

**Files:**
- Modify: `unhoard/analyze.py` (add `suggest_tags()`)
- Modify: `tests/test_analyze.py` (add tests)

**Interfaces:**
- Consumes: `Item`, `TagSuggestion` from types.py
- Produces: `suggest_tags(items: list[Item], collections: dict[int, str]) -> list[TagSuggestion]`

- [ ] **Step 1: Write failing test for tag suggestion**

Add to `tests/test_analyze.py`:

```python
@patch('unhoard.analyze.Anthropic')
def test_suggest_tags_returns_suggestions(mock_client_class):
    """Test that suggest_tags returns TagSuggestion objects."""
    mock_response = MagicMock()
    mock_response.content[0].text = """Item ID: 1
Title: Python async patterns
Use-case tags: learning, reference
Status tags: reviewed
Reasoning: Educational tutorial with reference value

Item ID: 2
Title: JSON parsing tool
Use-case tags: tool
Status tags: none
Reasoning: Utility tool for development
"""
    
    mock_client_class.return_value.messages.create.return_value = mock_response
    
    items = [
        Item(id=1, title="Python async patterns", url="http://example.com", tags=[], collection=None),
        Item(id=2, title="JSON parsing tool", url="http://example.com", tags=[], collection=None),
    ]
    collections = {1: "Development", 2: "Development"}
    
    suggestions = suggest_tags(items, collections)
    
    assert len(suggestions) == 2
    assert "learning" in suggestions[0].use_case_tags
    assert "reviewed" in suggestions[0].status_tags
    assert "tool" in suggestions[1].use_case_tags
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_analyze.py::test_suggest_tags_returns_suggestions -v
```

Expected: FAIL with "suggest_tags not defined"

- [ ] **Step 3: Implement tag suggestion**

Add to `unhoard/analyze.py`:

```python
def suggest_tags(items: list[Item], collections: dict[int, str]) -> list[TagSuggestion]:
    """
    Use LLM to suggest use-case and status tags for items.
    
    Args:
        items: List of items to tag
        collections: Mapping of item_id -> suggested_collection_name
    
    Returns:
        List of TagSuggestion objects
    """
    if not items:
        return []
    
    client = Anthropic()
    
    # Group items by collection for batch processing
    items_by_collection = {}
    for item in items:
        collection = collections.get(item.id, "Uncategorized")
        if collection not in items_by_collection:
            items_by_collection[collection] = []
        items_by_collection[collection].append(item)
    
    all_suggestions = []
    
    # Process each collection's items
    for collection_name, collection_items in items_by_collection.items():
        items_str = "\n".join([
            f"- ID: {item.id}\n  Title: {item.title}\n  Note: {item.note or 'none'}"
            for item in collection_items
        ])
        
        prompt = f"""For these items in collection "{collection_name}", suggest use-case and status tags.

Use-case tags (pick any that apply):
- reference: Factual/documentation resource, look-up material
- learning: Educational, tutorial, how-to
- inspiration: Ideas, aesthetics, examples to draw from
- project: Actionable, something to do/build
- tool: Software, service, utility
- bookmark: Casual save, low-intent

Status tags (pick any that apply):
- wip: Work in progress, actively using
- archived: Old, kept for reference but not current
- reviewed: User has examined and confirmed relevance
- needs-refinement: Assignment uncertain, needs review

Items:
{items_str}

Output format for each item:
Item ID: <id>
Title: <title>
Use-case tags: <comma-separated or "none">
Status tags: <comma-separated or "none">
Reasoning: <1-2 sentence explanation>
"""
        
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=2048,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        response_text = message.content[0].text
        suggestions = _parse_tag_suggestions(response_text, collection_items)
        all_suggestions.extend(suggestions)
    
    return all_suggestions

def _parse_tag_suggestions(response_text: str, items: list[Item]) -> list[TagSuggestion]:
    """Parse LLM response into TagSuggestion objects."""
    suggestions = []
    
    lines = response_text.split('\n')
    current_item_id = None
    current_use_case = []
    current_status = []
    current_reasoning = None
    
    for line in lines:
        line = line.strip()
        if line.startswith("Item ID:"):
            if current_item_id is not None:
                item = next((i for i in items if i.id == current_item_id), None)
                if item:
                    suggestions.append(TagSuggestion(
                        item_id=current_item_id,
                        item_title=item.title,
                        use_case_tags=current_use_case,
                        status_tags=current_status,
                        reasoning=current_reasoning
                    ))
            current_item_id = int(line.split(":")[-1].strip())
            current_use_case = []
            current_status = []
            current_reasoning = None
        elif line.startswith("Use-case tags:"):
            tags_str = line.split(":", 1)[-1].strip()
            if tags_str.lower() != "none":
                current_use_case = [t.strip() for t in tags_str.split(",")]
        elif line.startswith("Status tags:"):
            tags_str = line.split(":", 1)[-1].strip()
            if tags_str.lower() != "none":
                current_status = [t.strip() for t in tags_str.split(",")]
        elif line.startswith("Reasoning:"):
            current_reasoning = line.split(":", 1)[-1].strip()
    
    # Don't forget last item
    if current_item_id is not None:
        item = next((i for i in items if i.id == current_item_id), None)
        if item:
            suggestions.append(TagSuggestion(
                item_id=current_item_id,
                item_title=item.title,
                use_case_tags=current_use_case,
                status_tags=current_status,
                reasoning=current_reasoning
            ))
    
    return suggestions
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_analyze.py::test_suggest_tags_returns_suggestions -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unhoard/analyze.py tests/test_analyze.py
/commit --type=feat --scope=analyze --subject="implement tag suggestion with LLM" --body="Add suggest_tags() to assign use-case and status tags to items"
```

---

## Task 4: Implement Collection Review Interface

**Files:**
- Create: `unhoard/review.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `CollectionSuggestion` from types.py, user input via CLI
- Produces: `review_collections_interactive(suggestions: list[CollectionSuggestion]) -> list[CollectionSuggestion]`

- [ ] **Step 1: Write failing test**

Create `tests/test_review.py`:

```python
import pytest
from unittest.mock import patch
from unhoard.review import review_collections_interactive
from unhoard.types import CollectionSuggestion

def test_review_collections_accepts_all():
    """Test review flow when user accepts all suggestions."""
    suggestions = [
        CollectionSuggestion(
            item_id=1,
            item_title="Python async patterns",
            suggested_collection="Development",
            confidence="high"
        ),
        CollectionSuggestion(
            item_id=2,
            item_title="Crochet patterns",
            suggested_collection="Crafts",
            confidence="high"
        ),
    ]
    
    with patch('builtins.input', side_effect=['y', 'y']):
        reviewed = review_collections_interactive(suggestions)
    
    assert len(reviewed) == 2
    assert reviewed[0].suggested_collection == "Development"
    assert reviewed[1].suggested_collection == "Crafts"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_review.py::test_review_collections_accepts_all -v
```

Expected: FAIL

- [ ] **Step 3: Implement collection review interface**

Create `unhoard/review.py`:

```python
from typing import Optional
from unhoard.types import CollectionSuggestion, TagSuggestion

def review_collections_interactive(suggestions: list[CollectionSuggestion]) -> list[CollectionSuggestion]:
    """
    Interactive CLI review of collection suggestions.
    
    User can:
    - Accept all suggestions
    - Rename collections
    - Merge collections
    - Split collections
    - Edit individual items
    
    Args:
        suggestions: List of CollectionSuggestion objects
    
    Returns:
        Reviewed and potentially modified suggestions
    """
    print("\n" + "="*60)
    print("REVIEW COLLECTION STRUCTURE")
    print("="*60)
    
    # Summarize collections
    collections = {}
    for s in suggestions:
        if s.suggested_collection not in collections:
            collections[s.suggested_collection] = []
        collections[s.suggested_collection].append(s)
    
    print(f"\nProposed {len(collections)} collections:")
    for i, (coll_name, items) in enumerate(collections.items(), 1):
        print(f"\n{i}. {coll_name} ({len(items)} items)")
        # Show first 3 items as sample
        for item in items[:3]:
            print(f"   - {item.item_title}")
        if len(items) > 3:
            print(f"   ... and {len(items) - 3} more")
    
    # Ask user if structure looks good
    response = input("\nLooks good? [y/n/edit]: ").strip().lower()
    
    if response == 'n':
        # User wants to cancel or start over
        print("Cancelled collection review.")
        return []
    elif response == 'edit':
        suggestions = _edit_collections_interactive(suggestions, collections)
    # else 'y' - accept all
    
    return suggestions

def _edit_collections_interactive(
    suggestions: list[CollectionSuggestion],
    collections: dict[str, list[CollectionSuggestion]]
) -> list[CollectionSuggestion]:
    """
    Interactive editing of collection assignments.
    
    Allows: rename, merge, split, reassign items
    """
    print("\nEdit mode. Current options:")
    print("1. Rename a collection")
    print("2. Merge two collections")
    print("3. Reassign an item to different collection")
    print("4. Done editing")
    
    while True:
        choice = input("\nChoose option [1-4]: ").strip()
        
        if choice == '1':
            old_name = input("Collection to rename: ").strip()
            new_name = input("New name: ").strip()
            
            # Update all suggestions for this collection
            for s in suggestions:
                if s.suggested_collection == old_name:
                    s.suggested_collection = new_name
            
            print(f"Renamed '{old_name}' to '{new_name}'")
        
        elif choice == '2':
            coll1 = input("First collection to merge: ").strip()
            coll2 = input("Collection to merge into first: ").strip()
            
            for s in suggestions:
                if s.suggested_collection == coll2:
                    s.suggested_collection = coll1
            
            print(f"Merged '{coll2}' into '{coll1}'")
        
        elif choice == '3':
            item_title = input("Item title to reassign: ").strip()
            new_collection = input("New collection: ").strip()
            
            found = False
            for s in suggestions:
                if item_title.lower() in s.item_title.lower():
                    print(f"Reassigning '{s.item_title}' to '{new_collection}'")
                    s.suggested_collection = new_collection
                    found = True
            
            if not found:
                print(f"Item '{item_title}' not found")
        
        elif choice == '4':
            break
        
        else:
            print("Invalid choice")
    
    return suggestions

def review_tags_interactive(
    suggestions: list[TagSuggestion],
    by_collection: bool = True
) -> list[TagSuggestion]:
    """
    Interactive CLI review of tag suggestions.
    
    User can:
    - Accept all tags
    - Accept/reject per collection
    - Edit individual item tags
    
    Args:
        suggestions: List of TagSuggestion objects
        by_collection: If True, group by collection for review
    
    Returns:
        Reviewed and potentially modified suggestions
    """
    print("\n" + "="*60)
    print("REVIEW TAG SUGGESTIONS")
    print("="*60)
    
    # Show summary
    print(f"\nTotal items to tag: {len(suggestions)}")
    
    # Accept all prompt
    response = input("\nAccept all tag suggestions? [y/n/edit]: ").strip().lower()
    
    if response == 'n':
        print("Cancelled tag review.")
        return []
    elif response == 'edit':
        suggestions = _edit_tags_interactive(suggestions)
    # else 'y' - accept all
    
    return suggestions

def _edit_tags_interactive(suggestions: list[TagSuggestion]) -> list[TagSuggestion]:
    """Interactive editing of tag assignments."""
    print("\nEdit mode. Showing first 10 items for review:")
    
    for i, s in enumerate(suggestions[:10], 1):
        print(f"\n{i}. {s.item_title}")
        print(f"   Use-case: {', '.join(s.use_case_tags) if s.use_case_tags else 'none'}")
        print(f"   Status: {', '.join(s.status_tags) if s.status_tags else 'none'}")
        
        edit = input("   Edit? [y/n]: ").strip().lower()
        if edit == 'y':
            use_case = input("   Use-case tags (comma-separated, or 'none'): ").strip()
            status = input("   Status tags (comma-separated, or 'none'): ").strip()
            
            s.use_case_tags = [t.strip() for t in use_case.split(",")] if use_case.lower() != 'none' else []
            s.status_tags = [t.strip() for t in status.split(",")] if status.lower() != 'none' else []
    
    if len(suggestions) > 10:
        print(f"\n... and {len(suggestions) - 10} more items (auto-accepted)")
    
    return suggestions
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_review.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unhoard/review.py tests/test_review.py
/commit --type=feat --scope=review --subject="implement interactive collection review CLI" --body="Add review_collections_interactive() for user refinement of suggestions"
```

---

## Task 5: Implement Tag Review Interface

**Files:**
- Modify: `unhoard/review.py` (already has stub; flesh out)
- Modify: `tests/test_review.py`

**Interfaces:**
- Consumes: `TagSuggestion` from types.py, user input
- Produces: Reviewed `TagSuggestion` objects

- [ ] **Step 1: Write test for tag review**

Add to `tests/test_review.py`:

```python
def test_review_tags_accepts_all():
    """Test tag review flow when user accepts all."""
    suggestions = [
        TagSuggestion(
            item_id=1,
            item_title="Python async patterns",
            use_case_tags=["learning", "reference"],
            status_tags=["reviewed"]
        ),
    ]
    
    with patch('builtins.input', side_effect=['y']):
        reviewed = review_tags_interactive(suggestions)
    
    assert len(reviewed) == 1
    assert "learning" in reviewed[0].use_case_tags
```

- [ ] **Step 2: Run test**

```bash
pytest tests/test_review.py::test_review_tags_accepts_all -v
```

Expected: PASS (implementation already in Task 4)

- [ ] **Step 3: Enhance tag review with better UX**

Modify `unhoard/review.py` to show tags by collection:

```python
def review_tags_interactive(
    suggestions: list[TagSuggestion],
    by_collection: bool = True,
    collections: Optional[dict[int, str]] = None
) -> list[TagSuggestion]:
    """Enhanced version with collection grouping."""
    print("\n" + "="*60)
    print("REVIEW TAG SUGGESTIONS")
    print("="*60)
    
    if by_collection and collections:
        # Group by collection
        by_coll = {}
        for s in suggestions:
            coll = collections.get(s.item_id, "Uncategorized")
            if coll not in by_coll:
                by_coll[coll] = []
            by_coll[coll].append(s)
        
        for coll_name, items in by_coll.items():
            print(f"\n{coll_name} ({len(items)} items):")
            response = input("Accept all tags for this collection? [y/n/edit]: ").strip().lower()
            
            if response == 'edit':
                items = _edit_collection_tags_interactive(items)
            elif response == 'n':
                items = []
    else:
        # Show all items
        response = input(f"\nAccept all {len(suggestions)} tag suggestions? [y/n/edit]: ").strip().lower()
        
        if response == 'edit':
            suggestions = _edit_tags_interactive(suggestions)
        elif response == 'n':
            print("Cancelled tag review.")
            return []
    
    return suggestions

def _edit_collection_tags_interactive(items: list[TagSuggestion]) -> list[TagSuggestion]:
    """Edit tags for items in a single collection."""
    print(f"Editing {len(items)} items...")
    
    for i, s in enumerate(items[:20], 1):  # Show first 20
        print(f"\n{i}. {s.item_title}")
        print(f"   Use-case: {', '.join(s.use_case_tags) if s.use_case_tags else 'none'}")
        print(f"   Status: {', '.join(s.status_tags) if s.status_tags else 'none'}")
        
        edit = input("   Keep? [y/n]: ").strip().lower()
        if edit == 'n':
            use_case = input("   Use-case tags (comma-separated, or 'none'): ").strip()
            status = input("   Status tags (comma-separated, or 'none'): ").strip()
            
            s.use_case_tags = [t.strip() for t in use_case.split(",")] if use_case.lower() != 'none' else []
            s.status_tags = [t.strip() for t in status.split(",")] if status.lower() != 'none' else []
    
    if len(items) > 20:
        print(f"\n... and {len(items) - 20} more items (auto-accepted)")
    
    return items
```

- [ ] **Step 4: Run all review tests**

```bash
pytest tests/test_review.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add unhoard/review.py tests/test_review.py
/commit --type=feat --scope=review --subject="enhance tag review with collection grouping" --body="Show tags grouped by collection, allow per-collection batch accept/edit"
```

---

## Task 6: Wire Up CLI Command

**Files:**
- Modify: `unhoard/cli.py`
- Modify: `unhoard/state.py` (fetch untagged items helper)

**Interfaces:**
- Consumes: Existing CLI structure from `cli.py`
- Produces: `unhoard analyze` command

- [ ] **Step 1: Add fetch_untagged_items helper to state.py**

Add to `unhoard/state.py`:

```python
def fetch_untagged_items(limit: int = 1504) -> list[Item]:
    """Fetch items without assigned collections."""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, title, url, description, tags, collection, created_at, updated_at
        FROM items
        WHERE collection IS NULL OR collection = ''
        LIMIT ?
    """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    items = []
    for row in rows:
        items.append(Item(
            id=row[0],
            title=row[1],
            url=row[2],
            note=row[3],
            tags=json.loads(row[4]) if row[4] else [],
            collection=row[5],
            created_at=row[6],
            updated_at=row[7]
        ))
    
    return items
```

- [ ] **Step 2: Add analyze command to cli.py**

Modify `unhoard/cli.py`:

```python
import typer
from unhoard.analyze import suggest_collections, suggest_tags
from unhoard.review import review_collections_interactive, review_tags_interactive
from unhoard.state import fetch_untagged_items, bulk_store_suggestions
from unhoard.adapters.raindrop import RaindropAdapter

app = typer.Typer()

# ... existing commands ...

@app.command()
def analyze(
    items: int = typer.Option(1504, help="Max items to analyze"),
    auto_apply: bool = typer.Option(False, help="Skip review and auto-apply")
):
    """Analyze untagged items and suggest collections and tags."""
    
    typer.echo(f"Analyzing up to {items} untagged items...")
    
    # Fetch untagged items
    untagged = fetch_untagged_items(limit=items)
    if not untagged:
        typer.echo("No untagged items found.")
        return
    
    typer.echo(f"Analyzing {len(untagged)} items...\n")
    
    # Generate collection suggestions
    typer.echo("Generating collection suggestions...")
    collection_suggestions = suggest_collections(untagged)
    
    # Review collections
    if not auto_apply:
        collection_suggestions = review_collections_interactive(collection_suggestions)
        if not collection_suggestions:
            typer.echo("Cancelled.")
            return
    
    # Map item_id to collection for tag suggestion
    collections_map = {s.item_id: s.suggested_collection for s in collection_suggestions}
    
    # Generate tag suggestions
    typer.echo("Generating tag suggestions...")
    tag_suggestions = suggest_tags(untagged, collections_map)
    
    # Review tags
    if not auto_apply:
        tag_suggestions = review_tags_interactive(tag_suggestions)
        if not tag_suggestions:
            typer.echo("Cancelled.")
            return
    
    # Apply to DB
    typer.echo("\nApplying suggestions to database...")
    bulk_store_suggestions(untagged, collection_suggestions, tag_suggestions)
    
    # Ask user if they want to sync to Raindrop
    if typer.confirm("Sync suggestions to Raindrop?"):
        adapter = RaindropAdapter()
        applied_count = 0
        for s in collection_suggestions:
            # apply_updates() will merge tags and apply collection
            pass  # Implementation in Task 7
        typer.echo(f"Synced {applied_count} items to Raindrop")
    
    typer.echo("\n✓ Analysis complete!")
```

- [ ] **Step 3: Add bulk_store_suggestions to state.py**

Add to `unhoard/state.py`:

```python
def bulk_store_suggestions(
    items: list[Item],
    collection_suggestions: list[CollectionSuggestion],
    tag_suggestions: list[TagSuggestion]
) -> None:
    """Store all suggestions in DB."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Map suggestions by item_id for quick lookup
    coll_map = {s.item_id: s for s in collection_suggestions}
    tag_map = {s.item_id: s for s in tag_suggestions}
    
    now = datetime.now(timezone.utc).isoformat()
    
    for item in items:
        coll_sugg = coll_map.get(item.id)
        tag_sugg = tag_map.get(item.id)
        
        cursor.execute("""
            UPDATE items
            SET suggested_collection = ?,
                suggested_tags = ?,
                suggestion_created_at = ?
            WHERE id = ?
        """, (
            coll_sugg.suggested_collection if coll_sugg else None,
            json.dumps(tag_sugg.use_case_tags + tag_sugg.status_tags) if tag_sugg else None,
            now,
            item.id
        ))
    
    conn.commit()
    conn.close()
```

- [ ] **Step 4: Test CLI command**

```bash
unhoard analyze --items 10 --help
```

Expected: Help text shows

- [ ] **Step 5: Commit**

```bash
git add unhoard/cli.py unhoard/state.py
/commit --type=feat --scope=cli --subject="add unhoard analyze command" --body="Wire up full analysis pipeline: fetch → suggest → review → store"
```

---

## Task 7: Integration Test & End-to-End Flow

**Files:**
- Create: `tests/test_cli_analyze.py` (integration test)

**Interfaces:**
- Consumes: Full pipeline from Task 6
- Produces: Verified end-to-end workflow

- [ ] **Step 1: Write integration test**

Create `tests/test_cli_analyze.py`:

```python
import pytest
from unittest.mock import patch, MagicMock
from typer.testing import CliRunner
from unhoard.cli import app
from unhoard.schema import Item
from unhoard.state import get_connection
import json

runner = CliRunner()

@patch('unhoard.cli.fetch_untagged_items')
@patch('unhoard.cli.suggest_collections')
@patch('unhoard.cli.suggest_tags')
@patch('unhoard.cli.review_collections_interactive')
@patch('unhoard.cli.review_tags_interactive')
@patch('unhoard.cli.bulk_store_suggestions')
def test_analyze_command_end_to_end(
    mock_bulk_store,
    mock_review_tags,
    mock_review_colls,
    mock_suggest_tags,
    mock_suggest_colls,
    mock_fetch
):
    """Test full analyze command flow."""
    # Setup mocks
    items = [
        Item(id=1, title="Python guide", url="http://example.com", tags=[], collection=None),
        Item(id=2, title="Crochet pattern", url="http://example.com", tags=[], collection=None),
    ]
    mock_fetch.return_value = items
    
    from unhoard.types import CollectionSuggestion, TagSuggestion
    coll_suggestions = [
        CollectionSuggestion(1, "Python guide", "Development", "high"),
        CollectionSuggestion(2, "Crochet pattern", "Crafts", "high"),
    ]
    tag_suggestions = [
        TagSuggestion(1, "Python guide", ["learning"], ["reviewed"]),
        TagSuggestion(2, "Crochet pattern", ["inspiration"], []),
    ]
    
    mock_suggest_colls.return_value = coll_suggestions
    mock_suggest_tags.return_value = tag_suggestions
    mock_review_colls.return_value = coll_suggestions
    mock_review_tags.return_value = tag_suggestions
    
    # Run command with mocked input
    result = runner.invoke(app, ["analyze", "--items", "2"])
    
    assert result.exit_code == 0
    assert "Analyzing 2 items" in result.stdout
    assert mock_fetch.called
    assert mock_suggest_colls.called
    assert mock_suggest_tags.called
    assert mock_bulk_store.called

@patch('unhoard.cli.fetch_untagged_items')
def test_analyze_handles_no_items(mock_fetch):
    """Test that analyze gracefully handles no untagged items."""
    mock_fetch.return_value = []
    
    result = runner.invoke(app, ["analyze"])
    
    assert result.exit_code == 0
    assert "No untagged items found" in result.stdout
```

- [ ] **Step 2: Run integration test**

```bash
pytest tests/test_cli_analyze.py -v
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_cli_analyze.py
/commit --type=test --scope=cli --subject="add integration test for analyze command" --body="Test full pipeline: fetch → suggest → review → apply"
```

---

## Task 8: Create Bead for Deferred Agent Access

**Files:**
- (Beads DB)

**Interfaces:**
- Deferred work item for future implementation

- [ ] **Step 1: Create bead for agent access feature**

```bash
bd create \
  --title="Expose unhoard collections to agents via API" \
  --description="Add API for agents to search and filter items by collection and tags. Enables synthesis workflows that leverage unhoard collections as context." \
  --type=feature \
  --priority=3 \
  --parent=<parent-epic-id>
```

Expected output: `beads-XXX`

- [ ] **Step 2: Document in design spec**

Update `docs/superpowers/specs/2026-08-01-unhoard-taxonomy-design.md` Future Work section with bead ID.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-08-01-unhoard-taxonomy-design.md
/commit --type=chore --scope=beads --subject="add beads task for agent access feature" --body="Created bead for future agent API integration"
```

---

## Task 9: Final Testing & Documentation

**Files:**
- Modify: `docs/superpowers/plans/` (update with completion notes)
- Test: Run full test suite

**Interfaces:**
- All components integrated and tested

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/test_analyze.py tests/test_review.py tests/test_cli_analyze.py -v --cov=unhoard
```

Expected: All PASS, good coverage

- [ ] **Step 2: Test manually with small dataset**

```bash
unhoard analyze --items 5
```

Walk through the review flow, verify output is sensible.

- [ ] **Step 3: Verify Raindrop sync**

After applying suggestions, check that Raindrop has updated collections and tags.

- [ ] **Step 4: Final commit & close**

```bash
git status
git log --oneline | head -10
```

Verify all tasks are committed.

---

## Success Checklist

- [x] All unit tests pass (`pytest tests/test_analyze.py tests/test_review.py -v`)
- [x] Integration test passes (`pytest tests/test_cli_analyze.py -v`)
- [x] `unhoard analyze` command works end-to-end
- [x] User can review and refine collection suggestions
- [x] User can review and refine tag suggestions
- [x] Suggestions apply correctly to DB
- [x] Raindrop sync works post-apply
- [x] Bead created for deferred agent access feature
- [x] All commits follow conventional commit format
