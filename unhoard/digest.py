from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

from rich.markup import escape
from rich.progress import track

from .adapters.base import WritebackAdapter
from .config import Config
from .sources import find_adapter_for_source
from .state import StateStore, acted_on_flags, processing_state
from .summarize import context_hash as compute_context_hash
from .summarize import summarize as ai_summarize
from .ui import console, print_warning


def _age_days(created_at_iso: str) -> int:
    created = datetime.fromisoformat(created_at_iso)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).days


def build_digest(cfg: Config, store: StateStore) -> tuple[str, str]:
    """Returns (markdown_text, output_filename)."""
    rows = list(store.active_items())

    buckets: dict[str, list[tuple[int, sqlite3.Row]]] = {"new": [], "aging": [], "stale": []}
    for row in rows:
        age = _age_days(row["created_at"])
        if age <= cfg.aging_days:
            buckets["new"].append((age, row))
        elif age <= cfg.stale_days:
            buckets["aging"].append((age, row))
        else:
            buckets["stale"].append((age, row))

    for bucket in buckets.values():
        bucket.sort(key=lambda t: -t[0])  # oldest first

    # Split the stale bucket by whether it still needs unhoard's own attention.
    # Once something's been acted on (tagged/summarized/collected, or already
    # synthesized) it doesn't need re-analysis -- keep it out of the
    # max_stale-capped "needs analysis" list so that cap is spent on genuinely
    # untouched backlog, not on re-showing items that are just waiting on you
    # to `apply`/`synthesize`. This is how the backlog actually moves instead
    # of the same handful of already-done items crowding out new ones forever.
    stale_needs_analysis = [t for t in buckets["stale"] if needs_fresh_summary(cfg, t[1])]
    stale_ready = [t for t in buckets["stale"] if not needs_fresh_summary(cfg, t[1])]

    selected = {
        "ready": stale_ready,
        "stale": stale_needs_analysis[: cfg.max_stale],
        "aging": buckets["aging"][: cfg.max_aging],
        "new": buckets["new"][: cfg.max_new],
    }

    shown_keys: list[str] = []
    lines: list[str] = []
    today = date.today()
    total_active = len(rows)
    lines.append(f"# Reading backlog digest -- {today.isoformat()}\n")
    lines.append(
        f"_{total_active} active items across your sources. "
        f"Showing {sum(len(v) for v in selected.values())} today "
        f"({len(buckets['stale'])} stale total ({len(stale_ready)} ready to finish, "
        f"{len(stale_needs_analysis)} still need a look), "
        f"{len(buckets['aging'])} aging, {len(buckets['new'])} new)._\n"
    )
    lines.append(
        "> To close an item out: `unhoard mark <key> --done`, "
        "`unhoard mark <key> --snooze 14`, or just handle it in the "
        "source app (archive/delete/move it) -- it'll drop off after the next `sync`.\n"
    )

    if selected["ready"]:
        lines.append("## \u2705 Ready to finish (already tagged/summarized/collected)\n")
        for age, row in selected["ready"]:
            lines.append(_render_stale_item(cfg, store, row, age, []))
            shown_keys.append(row["key"])

    if selected["stale"]:
        lines.append("## \U0001f578\ufe0f Stale backlog (the whole point)\n")
        needs_fresh = cfg.anthropic_enabled
        collection_names = _fetch_collection_names(cfg) if needs_fresh else []
        # Only show a progress bar when there's actual summarization work to do --
        # a fully-cached digest renders instantly and a progress bar would be noise.
        stale_iter = (
            track(selected["stale"], description="Summarizing stale items...", console=console)
            if needs_fresh else selected["stale"]
        )
        for age, row in stale_iter:
            lines.append(_render_stale_item(cfg, store, row, age, collection_names))
            shown_keys.append(row["key"])

    if selected["aging"]:
        lines.append("## \u23f3 Aging\n")
        for age, row in selected["aging"]:
            lines.append(_render_metadata_item(row, age))
            shown_keys.append(row["key"])

    if selected["new"]:
        lines.append("## \U0001f195 New\n")
        for age, row in selected["new"]:
            lines.append(_render_metadata_item(row, age))
            shown_keys.append(row["key"])

    if not shown_keys:
        lines.append("\nNothing active right now -- inbox zero. Go outside. \u2600\ufe0f\n")

    store.record_shown(shown_keys)
    markdown = "\n".join(lines)
    filename = f"digest-{today.isoformat()}.md"
    return markdown, filename


def _tags_str(row: sqlite3.Row) -> str:
    try:
        tags = json.loads(row["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    return ", ".join(tags) if tags else ""


def _processing_badge(row: sqlite3.Row) -> str:
    """Marks how far unhoard's own pipeline has gotten with this item, so an
    acted-on/synthesized item reads differently at a glance from one still
    waiting on a look -- independent of the age-based bucket it's shown in."""
    state = processing_state(row)
    if state == "synced":
        return ""
    if state == "synthesized":
        return " `✨ synthesized`"
    done = "+".join(name for name, on in acted_on_flags(row).items() if on)
    return f" `✅ {done}`"


def _render_metadata_item(row: sqlite3.Row, age: int) -> str:
    tags = _tags_str(row)
    tag_part = f" `{tags}`" if tags else ""
    lines = [f"- **[{row['title']}]({row['url']})** -- {age}d old, via `{row['source']}`{tag_part}{_processing_badge(row)}"]
    if row["excerpt"]:
        lines += ["", f"  > {row['excerpt']}"]
    lines += ["", f"  *key: `{row['key']}`*"]
    return "\n".join(lines)


def _fetch_collection_names(cfg: Config) -> list[str]:
    """Best-effort fetch of real collection names to ground AI collection
    suggestions in -- returns [] if no write-back-capable adapter is
    configured or the call fails, in which case the summarizer just won't
    suggest a collection."""
    adapter = find_adapter_for_source(cfg, "raindrop")
    if adapter is None or not isinstance(adapter, WritebackAdapter):
        return []
    try:
        return [c["title"] for c in adapter.list_collections()]
    except Exception as e:  # noqa: BLE001 -- grounding data is best-effort, never fatal
        print_warning(f"couldn't fetch collections for suggestions: {escape(str(e))}")
        return []


def _load_suggested_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _suggestion_line(row: sqlite3.Row, tags: list[str], collection: str) -> str:
    """Only mentions suggestions that haven't already been pushed via
    `unhoard apply` -- once applied, nagging about it again is just noise."""
    parts = []
    if tags and not row["tags_applied_at"]:
        parts.append(f"tags: {', '.join(tags)}")
    if collection and not row["collection_applied_at"]:
        parts.append(f"collection: {collection}")
    if not parts:
        return ""
    return f"*suggested -- {' | '.join(parts)} (apply with `unhoard apply <key>`)*"


def needs_fresh_summary(cfg: Config, row: sqlite3.Row) -> bool:
    """A cached summary was generated under a different `context` setting -- re-run
    it so newly-added preservation rules actually apply to items already summarized.
    A synthesized item never needs a fresh one: the standalone note is already
    written, so an automatic re-summarize here wouldn't reach it anyway --
    `unhoard synthesize --force` is the explicit way to redo that."""
    if row["synthesized_at"]:
        return False
    summary_text = row["summary"] or ""
    if not summary_text:
        return True
    current_ctx_hash = compute_context_hash(cfg.context)
    return (row["context_hash"] or "") != current_ctx_hash


def ensure_ai_suggestions(
    cfg: Config, store: StateStore, row: sqlite3.Row, collection_names: list[str]
) -> tuple[str, list[str], str]:
    """Generates and caches an AI summary/suggested tags/suggested collection for
    `row` if it doesn't already have a current one (see `needs_fresh_summary`);
    otherwise just returns what's cached. Shared by the stale-item digest render
    and `unhoard apply-all`, which both need the same generate-if-missing step
    before they have anything to show or push."""
    summary_text = row["summary"] or ""
    suggested_tags = _load_suggested_tags(row["suggested_tags"])
    suggested_collection = row["suggested_collection"] or ""

    if not (needs_fresh_summary(cfg, row) and cfg.anthropic_enabled):
        return summary_text, suggested_tags, suggested_collection

    current_ctx_hash = compute_context_hash(cfg.context)
    # fast_model: a two-sentence summary plus an action label is closer to
    # classification than to reasoning. Note this only affects summaries
    # generated from here on -- needs_fresh_summary() keys off the context
    # hash, not the model, so switching models does not invalidate and re-bill
    # every summary already cached.
    parsed, chash = ai_summarize(
        row["title"], row["url"], cfg.anthropic_api_key, cfg.fast_model,
        cfg.context, collection_names, cfg.max_tokens_summary,
    )
    if parsed.get("raw"):
        # Display/store just Summary+Action -- tags/collection are surfaced via
        # the dedicated suggestion line below, not duplicated in the body text.
        # Kept on one line: markdown blockquotes only get a real line break with
        # a blank line between them, which would be a lot of vertical space here.
        summary_part, action_part = parsed.get("summary", ""), parsed.get("action", "")
        if summary_part or action_part:
            summary_text = summary_part
            if action_part:
                summary_text = f"{summary_text} **Action:** {action_part}".strip()
        else:
            summary_text = parsed["raw"]
        suggested_tags = parsed.get("tags", [])
        suggested_collection = parsed.get("collection") or ""
        store.save_summary(
            row["key"], summary_text, cfg.model, chash, current_ctx_hash,
            json.dumps(suggested_tags), suggested_collection,
        )
    return summary_text, suggested_tags, suggested_collection


def _render_stale_item(
    cfg: Config, store: StateStore, row: sqlite3.Row, age: int, collection_names: list[str]
) -> str:
    summary_text, suggested_tags, suggested_collection = ensure_ai_suggestions(cfg, store, row, collection_names)

    tags = _tags_str(row)
    tag_part = f" `{tags}`" if tags else ""
    body = summary_text if summary_text else (row["excerpt"] or "_(no summary or excerpt available)_")
    note = "" if summary_text else " *(AI summary unavailable -- showing saved excerpt)*"
    suggestion = _suggestion_line(row, suggested_tags, suggested_collection)

    lines = [
        f"- **[{row['title']}]({row['url']})** -- {age}d old, via `{row['source']}`{tag_part}{_processing_badge(row)}",
        "",
        f"  > {body}{note}",
    ]
    if suggestion:
        lines += ["", f"  {suggestion}"]
    lines += ["", f"  *key: `{row['key']}`*"]
    return "\n".join(lines)

