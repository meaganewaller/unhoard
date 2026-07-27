"""Fetch article text and summarize it via the Claude API -- only called for
items in the 'stale' bucket, and only if a summary isn't already cached for
the current content."""
from __future__ import annotations

import hashlib
import sys

import requests

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

PROMPT_TEMPLATE = """You're helping someone triage a long-forgotten reading list item \
they saved a while ago and never got to. Given the article text below, respond with \
exactly two short lines, nothing else:

Summary: <one or two plain sentences on what this actually is/argues/covers>
Action: <one of Read / Skim / Archive / Delete, then a 4-8 word reason>
{context_block}
Article title: {title}

Article text (may be truncated):
{content}
"""


def _context_block(context: str) -> str:
    if not context:
        return ""
    return (
        "\nContext about the person triaging this -- weigh it before recommending "
        f"Delete or Archive:\n{context.strip()}\n"
    )


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
    title: str, url: str, api_key: str, model: str, context: str = "", max_tokens: int = 300
) -> tuple[str, str]:
    """Returns (summary_text, content_hash). summary_text is '' if it couldn't be produced --
    caller should fall back to the raw excerpt in that case."""
    article_text = fetch_article_text(url)
    if not article_text:
        return "", ""
    chash = content_hash(article_text)

    if not api_key:
        return "", chash

    prompt = PROMPT_TEMPLATE.format(title=title, content=article_text, context_block=_context_block(context))
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
        return "\n".join(text_blocks).strip(), chash
    except Exception as e:  # noqa: BLE001 -- summarization failures shouldn't break the digest
        print(f"[summarize] AI summary failed for {url}: {e}", file=sys.stderr)
        return "", chash

