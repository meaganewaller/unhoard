"""Fetch article text and summarize it via the Claude API -- only called for
items in the 'stale' bucket, and only if a summary isn't already cached for
the current content."""
from __future__ import annotations

import hashlib
import sys
from types import ModuleType
from typing import Optional, TypedDict

import requests

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura: Optional[ModuleType] = None

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

PROMPT_TEMPLATE = """You're helping someone triage a long-forgotten reading list item \
they saved a while ago and never got to. Given the article text below, respond with \
exactly these lines and nothing else:

Summary: <one or two plain sentences on what this actually is/argues/covers>
Action: <one of Read / Skim / Archive / Delete, then a 4-8 word reason>
Tags: <comma-separated tags that describe this item, or "none">
Collection: <the single best-fitting collection from the list below, or "none">
{context_block}{collections_block}
Article title: {title}

Article text (may be truncated):
{content}
"""


class SummaryResult(TypedDict, total=False):
    """All fields are optional: summarize() returns {} when no summary could
    be produced at all, and callers already read every field via .get()."""
    summary: str
    action: str
    tags: list[str]
    collection: Optional[str]
    raw: str


def _context_block(context: str) -> str:
    if not context:
        return ""
    return (
        "\nContext about the person triaging this -- weigh it before recommending "
        f"Delete or Archive:\n{context.strip()}\n"
    )


def _collections_block(collection_names: Optional[list[str]]) -> str:
    if not collection_names:
        return "\nThere are no existing collections to choose from -- always answer Collection: none.\n"
    names = "\n".join(f"- {name}" for name in collection_names)
    return (
        "\nExisting collections to pick Collection from (pick the single best fit, "
        f"or 'none' if nothing really fits -- don't invent a new one):\n{names}\n"
    )


def parse_summary_response(text: str) -> SummaryResult:
    """Best-effort line-based parse of the AI response into structured fields.
    Never raises -- an unrecognized or missing line just leaves that field at
    its empty default, callers should tolerate a partially-parsed response."""
    result: SummaryResult = {"summary": "", "action": "", "tags": [], "collection": None}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        prefix, _, rest = line.partition(":")
        rest = rest.strip()
        key = prefix.strip().lower()
        if key == "summary":
            result["summary"] = rest
        elif key == "action":
            result["action"] = rest
        elif key == "tags" and rest.lower() != "none":
            result["tags"] = [t.strip() for t in rest.split(",") if t.strip()]
        elif key == "collection" and rest.lower() != "none":
            result["collection"] = rest
    return result


def context_hash(context: str) -> str:
    """Cheap local hash of the user's context setting, so a cached summary can be
    invalidated when the context changes without re-fetching the article."""
    return hashlib.sha256(context.encode("utf-8", errors="ignore")).hexdigest()[:16]


def fetch_article_text(url: str, max_chars: int = 6000) -> str:
    if not trafilatura or not url:
        return ""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded) or ""
        return text[:max_chars]
    except Exception as e:  # noqa: BLE001 -- best-effort, never fatal
        print(f"[summarize] couldn't fetch {url}: {e}", file=sys.stderr)
        return ""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def summarize(
    title: str,
    url: str,
    api_key: str,
    model: str,
    context: str = "",
    collection_names: Optional[list[str]] = None,
    max_tokens: int = 300,
) -> tuple[SummaryResult, str]:
    """Returns (parsed, content_hash). parsed is {} if a summary couldn't be produced --
    caller should fall back to the raw excerpt in that case. parsed['raw'] holds the full
    response text for display; 'tags'/'collection' are pulled out for the apply-on-demand
    write-back flow."""
    article_text = fetch_article_text(url)
    if not article_text:
        return {}, ""
    chash = content_hash(article_text)

    if not api_key:
        return {}, chash

    prompt = PROMPT_TEMPLATE.format(
        title=title,
        content=article_text,
        context_block=_context_block(context),
        collections_block=_collections_block(collection_names),
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
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        raw = "\n".join(text_blocks).strip()
        parsed = parse_summary_response(raw)
        parsed["raw"] = raw
        return parsed, chash
    except Exception as e:  # noqa: BLE001 -- summarization failures shouldn't break the digest
        print(f"[summarize] AI summary failed for {url}: {e}", file=sys.stderr)
        return {}, chash

