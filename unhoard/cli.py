from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from .config import load_config, write_default_config, CONFIG_PATH
from .digest import build_digest
from .sources import build_adapters, find_adapter_for_source
from .state import StateStore


def _find_single_item(store: StateStore, key: str):
    """Looks up exactly one item by key or key fragment. Prints an error and
    returns None on no match or an ambiguous match -- callers should return 1."""
    matches = store.find_by_prefix(key)
    if not matches:
        matches = store.conn.execute("SELECT * FROM items WHERE key LIKE ?", (f"%{key}%",)).fetchall()
    if not matches:
        print(f"No item found matching '{key}'.", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(f"Multiple items match '{key}' -- be more specific:", file=sys.stderr)
        for m in matches[:20]:
            print(f"  {m['key']}  {m['title']}", file=sys.stderr)
        return None
    return matches[0]


def cmd_init(args) -> int:
    path = write_default_config(force=args.force)
    print(f"Config written to {path}")
    print(
        "\nNext steps:\n"
        "  1. export RAINDROP_TOKEN=...   (https://app.raindrop.io/settings/integrations)\n"
        "  2. export ANTHROPIC_API_KEY=... (optional -- enables AI summaries for stale items)\n"
        "  3. Add [[sources]] tables to the config if you want Chrome/Safari/JSON sources too.\n"
        "  4. unhoard sync\n"
        "  5. unhoard digest\n\n"
        f"{path} also has commented-out examples worth a look:\n"
        "  - context: tells the summarizer about your own projects, so it stops recommending\n"
        "    Delete on stale items you actually want (e.g. old-web reference material)\n"
        "  - unhoarded_tag / unhoarded_collection_id: how `mark <key> --unhoarded` writes back\n"
        "    to Raindrop (tag and/or collection move)\n\n"
        "For cron (every morning at 7am):\n"
        "  0 7 * * * /usr/bin/env unhoard sync && unhoard digest\n"
    )
    return 0


def cmd_sync(args) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    try:
        adapters = build_adapters(cfg, args.source)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not adapters:
        print(
            "No sources configured. Set RAINDROP_TOKEN for the simplest setup, "
            "or add [[sources]] to your config (see `unhoard init`), "
            "or pass --source chrome / --source safari / --source json:<path>.",
            file=sys.stderr,
        )
        return 1

    total = 0
    for label, adapter in adapters:
        print(f"Syncing {label}...")
        try:
            items = list(adapter.fetch())
        except Exception as e:  # noqa: BLE001
            print(f"  failed: {e}", file=sys.stderr)
            continue
        if not items:
            print("  0 items")
            continue
        seen_keys = store.upsert_items(items)
        actual_sources = {it.source for it in items}
        closed = sum(store.mark_missing_as_done(src, seen_keys) for src in actual_sources)
        print(f"  {len(items)} items ({closed} closed -- handled outside the tool since last sync)")
        total += len(items)
    print(f"\nSync complete: {total} items processed across {len(adapters)} source(s).")
    return 0


def cmd_digest(args) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    markdown, filename = build_digest(cfg, store)
    out_path = cfg.output_dir / filename
    out_path.write_text(markdown)
    (cfg.output_dir / "latest.md").write_text(markdown)
    print(f"Digest written to {out_path}")
    if args.print:
        print("\n" + markdown)
    return 0


def cmd_mark(args) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    row = _find_single_item(store, args.key)
    if row is None:
        return 1

    if args.unhoarded:
        if not args.note:
            print(
                "warning: --unhoarded without --note -- 'properly sourced' is half the "
                "definition, consider recording where/how this was used",
                file=sys.stderr,
            )
        store.mark_unhoarded(row["key"], note=args.note or "")
        print(f"Unhoarded: {row['title']}")

        adapter = find_adapter_for_source(cfg, row["source"])
        if adapter is None or not hasattr(adapter, "mark_unhoarded"):
            print(f"  (no write-back support for source '{row['source']}' -- local state only)", file=sys.stderr)
        else:
            try:
                adapter.mark_unhoarded(row["source_id"], note=args.note or None)
                print(f"  also marked unhoarded in {row['source']}")
            except Exception as e:  # noqa: BLE001 -- write-back is best-effort; local state already saved
                print(f"  warning: couldn't write back to {row['source']}: {e}", file=sys.stderr)
    elif args.done:
        store.mark_done(row["key"], reason="marked done via CLI")
        print(f"Done: {row['title']}")
    elif args.snooze is not None:
        until = date.today() + timedelta(days=args.snooze)
        store.mark_snoozed(row["key"], until)
        print(f"Snoozed until {until.isoformat()}: {row['title']}")
    else:
        print("Specify --done, --unhoarded, or --snooze N", file=sys.stderr)
        return 1
    return 0


def cmd_apply(args) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    row = _find_single_item(store, args.key)
    if row is None:
        return 1

    want_tags = args.all or args.tags
    want_collection = args.all or args.collection
    want_summary = args.all or args.summary
    if not (want_tags or want_collection or want_summary):
        print("Specify --tags, --collection, --summary, or --all", file=sys.stderr)
        return 1

    adapter = find_adapter_for_source(cfg, row["source"])
    if adapter is None or not hasattr(adapter, "apply_updates"):
        print(f"No write-back support for source '{row['source']}' -- nothing to apply.", file=sys.stderr)
        return 1

    tags = None
    if want_tags:
        try:
            suggested_tags = json.loads(row["suggested_tags"] or "[]")
        except json.JSONDecodeError:
            suggested_tags = []
        if suggested_tags:
            tags = suggested_tags
        else:
            print("No suggested tags for this item -- run `unhoard digest` first.", file=sys.stderr)

    collection_id = None
    collection_title = None
    if want_collection:
        suggested_collection = row["suggested_collection"] or ""
        if not suggested_collection:
            print("No suggested collection for this item -- run `unhoard digest` first.", file=sys.stderr)
        elif not hasattr(adapter, "list_collections"):
            print(f"'{row['source']}' can't list collections -- skipping collection.", file=sys.stderr)
        else:
            match = next(
                (c for c in adapter.list_collections() if c["title"].lower() == suggested_collection.lower()),
                None,
            )
            if match:
                collection_id, collection_title = match["id"], match["title"]
            else:
                print(
                    f"Suggested collection '{suggested_collection}' no longer exists -- skipping.",
                    file=sys.stderr,
                )

    note = row["summary"] or None if want_summary else None
    if want_summary and note is None:
        print("No AI summary for this item -- run `unhoard digest` first.", file=sys.stderr)

    if tags is None and collection_id is None and note is None:
        print("Nothing to apply.", file=sys.stderr)
        return 1

    try:
        adapter.apply_updates(row["source_id"], tags=tags, collection_id=collection_id, note=note)
    except Exception as e:  # noqa: BLE001 -- report, don't crash
        print(f"error: couldn't apply to {row['source']}: {e}", file=sys.stderr)
        return 1

    store.mark_applied(row["key"], tags=tags is not None, collection=collection_id is not None, summary=note is not None)

    applied = []
    if tags is not None:
        applied.append(f"tags ({', '.join(tags)})")
    if collection_id is not None:
        applied.append(f"collection ({collection_title})")
    if note is not None:
        applied.append("summary as note")
    print(f"Applied to {row['title']}: {', '.join(applied)}")
    return 0


def cmd_stats(args) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    s = store.stats()
    print("By status:")
    for status, count in s["by_status"].items():
        print(f"  {status}: {count}")
    print("Active items by source:")
    for source, count in s["active_by_source"].items():
        print(f"  {source}: {count}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unhoard", description="Daily triage digest for your reading backlog.")
    sub = p.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help=f"Write a starter config to {CONFIG_PATH}")
    init_p.add_argument("--force", action="store_true", help="Overwrite existing config")
    init_p.set_defaults(func=cmd_init)

    sync_p = sub.add_parser("sync", help="Pull latest items from configured sources into local state")
    sync_p.add_argument(
        "--source", action="append",
        help="Ad-hoc source instead of config.toml, e.g. --source chrome --source json:/path.json "
             "(repeatable)",
    )
    sync_p.set_defaults(func=cmd_sync)

    digest_p = sub.add_parser("digest", help="Generate today's digest markdown file")
    digest_p.add_argument("--print", action="store_true", help="Also print the digest to stdout")
    digest_p.set_defaults(func=cmd_digest)

    mark_p = sub.add_parser("mark", help="Mark an item done, unhoarded, or snoozed by its key (or a fragment of it)")
    mark_p.add_argument("key", help="Item key, e.g. raindrop:12345 (a unique fragment also works)")
    mark_p.add_argument("--done", action="store_true", help="Mark as handled (closed out, not necessarily used)")
    mark_p.add_argument(
        "--unhoarded", action="store_true",
        help="Mark as unhoarded: synthesized, stored, and properly sourced elsewhere",
    )
    mark_p.add_argument(
        "--note", type=str,
        help="Provenance note for --unhoarded: where/how this was used",
    )
    mark_p.add_argument("--snooze", type=int, metavar="DAYS", help="Hide for N days")
    mark_p.set_defaults(func=cmd_mark)

    apply_p = sub.add_parser(
        "apply", help="Push AI-suggested tags/collection or the AI summary to the source, on demand"
    )
    apply_p.add_argument("key", help="Item key, e.g. raindrop:12345 (a unique fragment also works)")
    apply_p.add_argument("--tags", action="store_true", help="Apply the suggested tags")
    apply_p.add_argument("--collection", action="store_true", help="Move to the suggested collection")
    apply_p.add_argument("--summary", action="store_true", help="Write the AI summary into the source's note field")
    apply_p.add_argument("--all", action="store_true", help="Apply tags, collection, and summary")
    apply_p.set_defaults(func=cmd_apply)

    stats_p = sub.add_parser("stats", help="Show counts by status and source")
    stats_p.set_defaults(func=cmd_stats)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

