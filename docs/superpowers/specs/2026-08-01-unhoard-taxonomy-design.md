# Unhoard Hybrid Taxonomy Design

**Date:** 2026-08-01  
**Status:** Design approved  
**Scope:** Batch analysis and suggestion of collections + tags for 1504 untagged items

## Problem Statement

User has 1561 items in Raindrop, but:
- 1504 (96%) are untagged and unorganized
- Current 27 collections are flat, overlapping, and unclear in purpose
- No good way to discover content, understand what's been collected, or feed collections to agents for synthesis
- Manual per-item tagging is too slow; need batch analysis + human review workflow

## Solution Overview

**Hybrid collection + tag strategy:**
- **Collections** (semantic domains): 8-15 top-level collections, max one level deep. Represent "how you think about knowledge."
- **Tags** (use cases + status): Cross-cutting attributes for filtering, search, and agent access.

**Workflow:**
1. Analyze 1504 untagged items with LLM
2. Suggest collection structure (cluster + map items to collections)
3. Suggest tags per item (use-case + status tags)
4. CLI review interface: user reviews, refines, approves
5. Apply suggestions back to Raindrop

## Data Model

### Collections

- **Scope:** Top-level or one level deep (e.g., `Development`, `Development/Backend`)
- **Count:** ~8-15 collections across all 1561 items
- **Semantics:** Stable domain-based grouping (e.g., Development, Design, Writing, Crafts, Food, Career, Learning)
- **Update frequency:** Infrequent; represent fundamental knowledge domains
- **Storage:** Raindrop collection ID + name in `Item.collection` field

**Expected structure** (examples from current 27):
- Development (aggregate: Software Development 256 + Game Development 11 + Developer Environments 46 + maybe Graphic Design 101 = ~414 items)
- Design (Graphic Design 101 + Sozai & Pixels 144 = ~245)
- Crafts (Crafting 38 + Knitting 18 + Crochet 186 = ~242)
- Writing (Writing 28 + related)
- Food (Food & Drink 22 + Recipe Sources 22 + Baking 29 = ~73)
- Career (Career 68)
- Learning (Learning & Hobbies 73)
- Web/Tools (Web Princess 51 + Scripts, Widgets 56 + Agents & More 41 = ~148)
- Personal (Kids & Parenting 23 + Life & Home 11 + ADHD 1 + Shopping 230 = ~265)
- Reference (Old Web References 14 + Archived Sites 18 = ~32)
- Art/Creative (Art 47 + PKM 26 = ~73)
- Minecraft (14) — keep if distinct, or fold into Gaming/Development

### Tags

**Two categories:**

1. **Use-case tags** (describes why item is valuable):
   - `reference` — factual/documentation resource, look-up material
   - `learning` — educational, tutorial, how-to
   - `inspiration` — ideas, aesthetics, examples to draw from
   - `project` — actionable, something to do/build
   - `tool` — software, service, utility
   - `bookmark` — casual save, low-intent

2. **Status tags** (lifecycle/review state):
   - `wip` — work in progress, actively using
   - `archived` — old, kept for reference but not current
   - `reviewed` — user has examined and confirmed relevance
   - `needs-refinement` — collection/tag assignment uncertain, needs user review

**Storage:** JSON list in `Item.tags` field (existing); batch insert via `apply_updates()`

**Constraints:**
- No forced tag on every item (tags are optional)
- Items can have multiple tags (multiple use-cases)
- Tags are case-insensitive, dash-separated (`work-in-progress` not `wip`)

## Analysis & Suggestion Engine

### Analysis Phase

**Input:** 1504 untagged items + 72 already-tagged items (for context)

**Steps:**
1. Fetch all items from DB (title, description/note, URL, tags, collection if present)
2. For untagged items, extract content summary (LLM or heuristic)
3. Cluster items by semantic similarity (LLM-driven grouping)
4. Suggest collection assignments for each cluster
5. Suggest use-case + status tags for each item

### LLM Prompts

**Collection suggestion prompt:**
```
Analyze these {n} items and suggest a collection structure.
Suggest 8-15 collections that would naturally group this content.
Each collection should:
- Have a clear semantic purpose
- Contain 30-200+ items
- Be one level deep (e.g., "Development" or "Development/Backend")

Items:
{items_summary}

Output format:
Collection: {name}
  Description: {purpose}
  Expected item count: {estimate}
  Sample topics: {topics}

Then for each item, assign to a collection:
Item: {title}
Collection: {collection_name}
Confidence: high|medium|low
Conflict: {if this could fit multiple collections}
```

**Tag suggestion prompt:**
```
For these items in collection "{collection}", suggest use-case and status tags.

Use-case tags: reference, learning, inspiration, project, tool, bookmark
Status tags: wip, archived, reviewed, needs-refinement

Items:
{items_with_titles}

Output format:
Item: {title}
Use-case tags: {tags} (or "none")
Status tags: {tags} (or "none")
Reasoning: {1-2 sentence}
```

### Suggestion Output

Stored in DB with same structure as current suggestions:
- `suggested_collection` (collection name, not ID)
- `suggested_tags` (JSON list of tags)
- `suggestion_created_at` (timestamp)

Allows user to review before applying, and preserves original data until approved.

## CLI Review Interface

**Command:** `unhoard analyze [--items COUNT] [--auto-apply]`

**Flow:**

```
$ unhoard analyze
Analyzing 1504 untagged items...
[Progress: 25%, 50%, 75%, 100%]
Analysis complete: 12 collections suggested, 1504 tags suggested.

Review collection structure? [y/n]: y

PROPOSED COLLECTIONS (12):
  1. Development (414 items)
     Sample: "Python async patterns", "Rust ownership guide", "Electron app..."
  2. Design (245 items)
     Sample: "CSS Grid layouts", "Color theory", "Figma plugins"...
  [etc.]

Looks good? [y/n/edit]: edit
  Rename "Development" to "Engineering"? [y/n]: n
  Merge "Web/Tools" and "Development"? [y/n]: n
  Split any collections? [y/n]: n
Collection structure approved.

Review tags? [y/n]: y

SUGGESTED TAGS BY COLLECTION:

Development (414 items):
  [Table format]
  Title                          | Use-case        | Status    | Certainty
  "Python async patterns"        | learning        | reviewed  | high
  "Rust ownership guide"         | reference       | reviewed  | high
  "Electron app boilerplate"     | tool            | reviewed  | high
  [...]
  
  Accept all for this collection? [y/n/edit]: y

[Next collection...]

All tags reviewed. Apply suggestions to Raindrop? [y/n]: y
Applying...
  Assigned 1504 items to collections
  Added 1847 tags
  Updated 72 existing items (merged tags)
Applied successfully.
```

**Edit modes:**
- Collection rename/merge/split
- Per-item tag accept/reject/modify
- Bulk tag operations (e.g., "mark all in Development/Backend as reviewed")

## Implementation Architecture

### New Files

**`unhoard/analyze.py`** — Core analysis logic
```python
def analyze_untagged_items(limit=None) -> dict[str, Any]:
    """Fetch untagged items, generate suggestions."""
    
def suggest_collections(items: list[Item]) -> list[CollectionSuggestion]:
    """LLM-driven collection clustering."""
    
def suggest_tags(items: list[Item], collections: dict) -> list[TagSuggestion]:
    """LLM-driven tag assignment."""
    
def store_suggestions(suggestions: list[Suggestion]) -> None:
    """Write to DB."""
```

**`unhoard/review.py`** — CLI review interface
```python
def review_collections_interactive(suggestions: CollectionSuggestions) -> CollectionSuggestions:
    """Interactive review of collection structure."""
    
def review_tags_interactive(suggestions: TagSuggestions, by_collection=True) -> TagSuggestions:
    """Interactive review and refinement of tag suggestions."""
    
def present_results(applied: ApplyResult) -> None:
    """Summary of what was applied."""
```

### Modified Files

**`unhoard/cli.py`** — Add command
```bash
unhoard analyze [--items COUNT] [--auto-apply]
```

**`unhoard/state.py`** — May need helper to bulk-update suggestions
- New method: `bulk_store_suggestions(items, collection_suggests, tag_suggestions)` 
- Existing `apply_updates()` reused

**`unhoard/adapters/raindrop.py`** — No changes needed
- Existing `apply_updates()` handles writes

### Integration with Existing Workflow

- `unhoard digest` (LLM per-item suggestions) — unchanged
- `unhoard analyze` (batch LLM suggestions for structure) — new
- `unhoard apply` (apply reviewed suggestions) — works on both
- Current 72 "acted on" items: preserved; tags merged on apply (union, not replace)

## Error Handling & Edge Cases

**LLM failures:**
- If LLM can't cluster items, present them ungrouped for manual review
- If LLM confidence is low, flag with "needs-refinement" status tag

**Conflicts:**
- Item could fit multiple collections → user reviews, picks one, tag notes alternative
- Item has existing tags → merge with suggested tags (union)

**Large scale:**
- 1504 items analyzed in batches to avoid token limits
- Progress indicator shown during LLM calls
- Can resume if interrupted (suggestions stored incrementally)

## Testing Strategy

**Unit tests:**
- Collection clustering logic (mock LLM responses)
- Tag suggestion parsing
- Review interface input handling

**Integration tests:**
- End-to-end: analyze → review → apply
- Verify collections and tags written to DB correctly
- Verify Raindrop sync works post-apply

**Manual testing:**
- User reviews and applies suggestions (this design, per user approval)

## Success Criteria

- [ ] User can analyze 1504 items and get sensible collection + tag suggestions
- [ ] Review interface is fast and intuitive (< 5 min to review)
- [ ] Suggestions can be refined before apply
- [ ] Applied tags and collections appear in Raindrop
- [ ] User can then browse/filter collections easily (UI deferred; manual verification ok for now)

## Future Work (Deferred)

1. **Agent access** — expose unhoard collections + search to Claude agents (unhoard-nrw)
2. **Browser UI** — web interface for browsing and filtering collections
3. **Incremental analysis** — analyze new items as they're added
4. **Tag refinement** — post-apply feedback loop (user rates suggestions, improves model)

## Assumptions & Constraints

- Raindrop API supports collection hierarchy; we use flat for now, can nest later
- ~1500-2000 items is manageable in single analysis pass (can batch if needed)
- User has 30 min to review and refine suggestions (batch, not real-time)
- No auto-apply without user review (conservative default)
