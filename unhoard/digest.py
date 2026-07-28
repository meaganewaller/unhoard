from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone

from .config import Config
from .sources import find_adapter_for_source
from .state import StateStore
from .summarize import context_hash as compute_context_hash
from .summarize import summarize as ai_summarize


def _age_days(created_at_iso: str) -> int:
    created = datetime.fromisoformat(created_at_iso)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).days


def build_digest(cfg: Config, store: StateStore) -> tuple[str, str]:
    """Returns (markdown_text, output_filename)."""
    rows = list(store.active_items())

    buckets = {"new": [], "aging": [], "stale": []}
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

    selected = {
        "new": buckets["new"][: cfg.max_new],
        "aging": buckets["aging"][: cfg.max_aging],
        "stale": buckets["stale"][: cfg.max_stale],
    }

    shown_keys = []
    lines = []
    today = date.today()
    total_active = len(rows)
    lines.append(f"# Reading backlog digest -- {today.isoformat()}\n")
    lines.append(
        f"_{total_active} active items across your sources. "
        f"Showing {sum(len(v) for v in selected.values())} today "
        f"({len(buckets['stale'])} stale total, {len(buckets['aging'])} aging, {len(buckets['new'])} new)._\n"
    )
    lines.append(
        "> To close an item out: `unhoard mark <key> --done`, "
        "`unhoard mark <key> --snooze 14`, or just handle it in the "
        "source app (archive/delete/move it) -- it'll drop off after the next `sync`.\n"
    )

    if selected["stale"]:
        lines.append("## \U0001f578\ufe0f Stale backlog (the whole point)\n")
        needs_fresh = cfg.anthropic_enabled and any(_needs_fresh_summary(cfg, row) for _, row in selected["stale"])
        collection_names = _fetch_collection_names(cfg) if needs_fresh else []
        for age, row in selected["stale"]:
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


def _tags_str(row) -> str:
    try:
        tags = json.loads(row["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    return ", ".join(tags) if tags else ""


def _render_metadata_item(row, age: int) -> str:
    tags = _tags_str(row)
    tag_part = f" `{tags}`" if tags else ""
    excerpt = f"\n  > {row['excerpt']}" if row["excerpt"] else ""
    return (
        f"- **[{row['title']}]({row['url']})** -- {age}d old, via `{row['source']}`{tag_part}"
        f"{excerpt}\n  <sub>key: `{row['key']}`</sub>"
    )


def _fetch_collection_names(cfg: Config) -> list:
    """Best-effort fetch of real collection names to ground AI collection
    suggestions in -- returns [] if no write-back-capable adapter is
    configured or the call fails, in which case the summarizer just won't
    suggest a collection."""
    adapter = find_adapter_for_source(cfg, "raindrop")
    if adapter is None or not hasattr(adapter, "list_collections"):
        return []
    try:
        return [c["title"] for c in adapter.list_collections()]
    except Exception as e:  # noqa: BLE001 -- grounding data is best-effort, never fatal
        print(f"[digest] couldn't fetch collections for suggestions: {e}", file=sys.stderr)
        return []


def _load_suggested_tags(raw) -> list:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _suggestion_line(row, tags: list, collection: str) -> str:
    """Only mentions suggestions that haven't already been pushed via
    `unhoard apply` -- once applied, nagging about it again is just noise."""
    parts = []
    if tags and not row["tags_applied_at"]:
        parts.append(f"tags: {', '.join(tags)}")
    if collection and not row["collection_applied_at"]:
        parts.append(f"collection: {collection}")
    if not parts:
        return ""
    return f"  <sub>suggested -- {' | '.join(parts)} (apply with `unhoard apply <key>`)</sub>\n"


def _needs_fresh_summary(cfg: Config, row) -> bool:
    """A cached summary was generated under a different `context` setting -- re-run
    it so newly-added preservation rules actually apply to items already summarized."""
    summary_text = row["summary"] or ""
    if not summary_text:
        return True
    current_ctx_hash = compute_context_hash(cfg.context)
    return (row["context_hash"] or "") != current_ctx_hash


def _render_stale_item(cfg: Config, store: StateStore, row, age: int, collection_names: list) -> str:
    summary_text = row["summary"] or ""
    current_ctx_hash = compute_context_hash(cfg.context)
    need_summary = _needs_fresh_summary(cfg, row)
    suggested_tags = _load_suggested_tags(row["suggested_tags"])
    suggested_collection = row["suggested_collection"] or ""

    if need_summary and cfg.anthropic_enabled:
        parsed, chash = ai_summarize(
            row["title"], row["url"], cfg.anthropic_api_key, cfg.model,
            cfg.context, collection_names, cfg.max_tokens_summary,
        )
        if parsed.get("raw"):
            # Display/store just Summary+Action -- tags/collection are surfaced via
            # the dedicated suggestion line below, not duplicated in the body text.
            if parsed.get("summary") or parsed.get("action"):
                summary_text = f"Summary: {parsed.get('summary', '')}\nAction: {parsed.get('action', '')}"
            else:
                summary_text = parsed["raw"]
            suggested_tags = parsed.get("tags", [])
            suggested_collection = parsed.get("collection") or ""
            store.save_summary(
                row["key"], summary_text, cfg.model, chash, current_ctx_hash,
                json.dumps(suggested_tags), suggested_collection,
            )

    tags = _tags_str(row)
    tag_part = f" `{tags}`" if tags else ""
    body = summary_text if summary_text else (row["excerpt"] or "_(no summary or excerpt available)_")
    note = "" if summary_text else " <sub>(AI summary unavailable -- showing saved excerpt)</sub>"
    return (
        f"- **[{row['title']}]({row['url']})** -- {age}d old, via `{row['source']}`{tag_part}\n"
        f"  > {body}{note}\n"
        f"{_suggestion_line(row, suggested_tags, suggested_collection)}"
        f"  <sub>key: `{row['key']}`</sub>"
    )

