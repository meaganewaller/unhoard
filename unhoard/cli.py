from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from .config import load_config, write_default_config, CONFIG_PATH
from .digest import build_digest
from .sources import build_adapters, find_adapter_for_source
from .state import StateStore


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
    matches = store.find_by_prefix(args.key)
    if not matches:
        matches = store.conn.execute(
            "SELECT * FROM items WHERE key LIKE ?", (f"%{args.key}%",)
        ).fetchall()
    if not matches:
        print(f"No item found matching '{args.key}'.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"Multiple items match '{args.key}' -- be more specific:", file=sys.stderr)
        for m in matches[:20]:
            print(f"  {m['key']}  {m['title']}", file=sys.stderr)
        return 1

    row = matches[0]
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

    stats_p = sub.add_parser("stats", help="Show counts by status and source")
    stats_p.set_defaults(func=cmd_stats)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

