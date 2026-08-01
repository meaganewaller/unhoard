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
