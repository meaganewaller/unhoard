from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from typing import Optional

import questionary
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .adapters.base import WritebackAdapter
from .config import load_config, write_default_config, CONFIG_PATH
from .digest import build_digest
from .sources import build_adapters, find_adapter_for_source
from .state import StateStore
from .summarize import fetch_article_text
from .ui import console, err_console, print_error, print_success, print_warning


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:80] or "untitled"


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _find_single_item(store: StateStore, key: str) -> Optional[sqlite3.Row]:
    """Looks up exactly one item by key or key fragment. On an ambiguous match,
    offers an interactive picker in a real terminal; scripted/piped/cron usage
    (no tty) keeps the old behavior of printing the list and returning None --
    there's no one there to answer a prompt. Returns None (callers should
    return 1) on no match, a cancelled picker, or a non-interactive ambiguous
    match."""
    matches = store.find_by_prefix(key)
    if not matches:
        matches = store.conn.execute("SELECT * FROM items WHERE key LIKE ?", (f"%{key}%",)).fetchall()
    if not matches:
        print_error(f"no item found matching '{escape(key)}'.")
        return None
    if len(matches) > 1:
        if _is_interactive():
            choices = [questionary.Choice(title=f"{m['key']}  {m['title']}", value=m) for m in matches[:20]]
            return questionary.select(f"Multiple items match '{key}' -- pick one:", choices=choices).ask()
        print_warning(f"multiple items match '{escape(key)}' -- be more specific:")
        for m in matches[:20]:
            err_console.print(f"  [cyan]{escape(m['key'])}[/cyan]  {escape(m['title'])}")
        return None
    return matches[0]


def cmd_init(args: argparse.Namespace) -> int:
    path = write_default_config(force=args.force)
    print_success(f"Config written to {escape(str(path))}")

    next_steps = (
        "1. export RAINDROP_TOKEN=...   (https://app.raindrop.io/settings/integrations)\n"
        "2. export ANTHROPIC_API_KEY=... (optional -- enables AI summaries for stale items)\n"
        f"3. Add {escape('[[sources]]')} tables to the config if you want Chrome/Safari/JSON sources too.\n"
        "4. unhoard sync\n"
        "5. unhoard digest"
    )
    console.print(Panel(next_steps, title="Next steps", border_style="cyan", expand=False))

    console.print(
        f"\n[dim]{escape(str(path))}[/dim] also has commented-out examples worth a look:\n"
        "  - [bold]context[/bold]: tells the summarizer about your own projects, so it stops recommending\n"
        "    Delete on stale items you actually want (e.g. old-web reference material)\n"
        "  - [bold]unhoarded_tag[/bold] / [bold]unhoarded_collection_id[/bold]: how `mark <key> --unhoarded` "
        "writes back\n"
        "    to Raindrop (tag and/or collection move)\n"
    )
    console.print(
        "[bold]For cron[/bold] (every morning at 7am):\n"
        "  0 7 * * * /usr/bin/env unhoard sync && unhoard digest"
    )
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    try:
        adapters = build_adapters(cfg, args.source)
    except ValueError as e:
        print_error(escape(str(e)))
        return 1

    if not adapters:
        print_error(
            "no sources configured. Set RAINDROP_TOKEN for the simplest setup, "
            f"or add {escape('[[sources]]')} to your config (see `unhoard init`), "
            "or pass --source chrome / --source safari / --source json:<path>."
        )
        return 1

    total = 0
    for label, adapter in adapters:
        console.print(f"Syncing [bold]{escape(label)}[/bold]...")
        try:
            items = list(adapter.fetch())
        except Exception as e:  # noqa: BLE001
            err_console.print(f"  [red]failed:[/red] {escape(str(e))}")
            continue
        if not items:
            console.print("  [dim]0 items[/dim]")
            continue
        seen_keys = store.upsert_items(items)
        actual_sources = {it.source for it in items}
        closed = sum(store.mark_missing_as_done(src, seen_keys) for src in actual_sources)
        console.print(f"  [bold]{len(items)}[/bold] items ({closed} closed -- handled outside the tool since last sync)")
        total += len(items)
    print_success(f"Sync complete: {total} items processed across {len(adapters)} source(s).")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    markdown, filename = build_digest(cfg, store)
    out_path = cfg.output_dir / filename
    out_path.write_text(markdown)
    (cfg.output_dir / "latest.md").write_text(markdown)
    print_success(f"Digest written to {escape(str(out_path))}")
    if args.print:
        console.print()
        console.print(Markdown(markdown))
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    row = _find_single_item(store, args.key)
    if row is None:
        return 1

    if args.unhoarded:
        if not args.note:
            print_warning(
                "--unhoarded without --note -- 'properly sourced' is half the "
                "definition, consider recording where/how this was used"
            )
        store.mark_unhoarded(row["key"], note=args.note or "")
        print_success(f"Unhoarded: {escape(row['title'])}")

        adapter = find_adapter_for_source(cfg, row["source"])
        if adapter is None or not isinstance(adapter, WritebackAdapter):
            print_warning(f"no write-back support for source '{escape(row['source'])}' -- local state only")
        else:
            try:
                adapter.mark_unhoarded(row["source_id"], note=args.note or None)
                console.print(f"  [dim]also marked unhoarded in {escape(row['source'])}[/dim]")
            except Exception as e:  # noqa: BLE001 -- write-back is best-effort; local state already saved
                print_warning(f"couldn't write back to {escape(row['source'])}: {escape(str(e))}")
    elif args.done:
        store.mark_done(row["key"], reason="marked done via CLI")
        print_success(f"Done: {escape(row['title'])}")
    elif args.snooze is not None:
        until = date.today() + timedelta(days=args.snooze)
        store.mark_snoozed(row["key"], until)
        print_success(f"Snoozed until {until.isoformat()}: {escape(row['title'])}")
    else:
        print_error("specify --done, --unhoarded, or --snooze N")
        return 1
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    row = _find_single_item(store, args.key)
    if row is None:
        return 1

    want_tags = args.all or args.tags
    want_collection = args.all or args.collection
    want_summary = args.all or args.summary
    if not (want_tags or want_collection or want_summary):
        print_error("specify --tags, --collection, --summary, or --all")
        return 1

    adapter = find_adapter_for_source(cfg, row["source"])
    if adapter is None or not isinstance(adapter, WritebackAdapter):
        print_error(f"no write-back support for source '{escape(row['source'])}' -- nothing to apply.")
        return 1

    tags: Optional[list[str]] = None
    if want_tags:
        try:
            suggested_tags = json.loads(row["suggested_tags"] or "[]")
        except json.JSONDecodeError:
            suggested_tags = []
        if suggested_tags:
            tags = suggested_tags
        else:
            print_warning("no suggested tags for this item -- run `unhoard digest` first.")

    collection_id: Optional[int] = None
    collection_title: Optional[str] = None
    if want_collection:
        suggested_collection = row["suggested_collection"] or ""
        if not suggested_collection:
            print_warning("no suggested collection for this item -- run `unhoard digest` first.")
        elif not hasattr(adapter, "list_collections"):
            print_warning(f"'{escape(row['source'])}' can't list collections -- skipping collection.")
        else:
            match = next(
                (c for c in adapter.list_collections() if c["title"].lower() == suggested_collection.lower()),
                None,
            )
            if match:
                collection_id, collection_title = match["id"], match["title"]
            else:
                print_warning(f"suggested collection '{escape(suggested_collection)}' no longer exists -- skipping.")

    note = row["summary"] or None if want_summary else None
    if want_summary and note is None:
        print_warning("no AI summary for this item -- run `unhoard digest` first.")

    if tags is None and collection_id is None and note is None:
        print_error("nothing to apply.")
        return 1

    try:
        adapter.apply_updates(row["source_id"], tags=tags, collection_id=collection_id, note=note)
    except Exception as e:  # noqa: BLE001 -- report, don't crash
        print_error(f"couldn't apply to {escape(row['source'])}: {escape(str(e))}")
        return 1

    store.mark_applied(row["key"], tags=tags is not None, collection=collection_id is not None, summary=note is not None)

    applied: list[str] = []
    if tags is not None:
        applied.append(f"tags ({escape(', '.join(tags))})")
    if collection_id is not None:
        assert collection_title is not None, "set together with collection_id above"
        applied.append(f"collection ({escape(collection_title)})")
    if note is not None:
        applied.append("summary as note")
    print_success(f"Applied to {escape(row['title'])}: {', '.join(applied)}")
    return 0


def cmd_synthesize(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    row = _find_single_item(store, args.key)
    if row is None:
        return 1

    article_text = fetch_article_text(row["url"])
    if not article_text:
        print_error("couldn't fetch article text for this item -- nothing to synthesize.")
        return 1

    synth_dir = cfg.output_dir / "synthesized"
    synth_dir.mkdir(parents=True, exist_ok=True)
    out_path = synth_dir / f"{_slugify(row['title'])}.md"

    if out_path.exists() and not args.force:
        print_warning(f"{escape(str(out_path))} already exists -- skipping (use --force to overwrite).")
        return 1

    try:
        tags = json.loads(row["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []

    frontmatter = "\n".join([
        "---",
        f"title: {json.dumps(row['title'])}",
        f"url: {json.dumps(row['url'])}",
        f"source: {json.dumps(row['source'])}",
        f"key: {json.dumps(row['key'])}",
        f"tags: {json.dumps(tags)}",
        f"synthesized_at: {date.today().isoformat()}",
        "---",
    ])

    body_parts = [f"# {row['title']}"]
    if row["summary"]:
        body_parts += ["## Summary", row["summary"]]
    body_parts += ["## Full Text", article_text]

    out_path.write_text(frontmatter + "\n\n" + "\n\n".join(body_parts) + "\n")
    store.mark_synthesized(row["key"])
    print_success(f"Synthesized note written to {escape(str(out_path))}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = StateStore(cfg.state_db_path)
    s = store.stats()

    status_table = Table(title="By status")
    status_table.add_column("Status")
    status_table.add_column("Count", justify="right")
    for status, count in s["by_status"].items():
        status_table.add_row(status, str(count))
    console.print(status_table)

    source_table = Table(title="Active items by source")
    source_table.add_column("Source")
    source_table.add_column("Count", justify="right")
    for source, count in s["active_by_source"].items():
        source_table.add_row(source, str(count))
    console.print(source_table)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unhoard", description="Daily triage digest for your reading backlog.")
    sub = p.add_subparsers(dest="command")

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

    synthesize_p = sub.add_parser(
        "synthesize", help="Pull the full article text into a standalone markdown note for your own writing"
    )
    synthesize_p.add_argument("key", help="Item key, e.g. raindrop:12345 (a unique fragment also works)")
    synthesize_p.add_argument("--force", action="store_true", help="Overwrite an existing note for this item")
    synthesize_p.set_defaults(func=cmd_synthesize)

    stats_p = sub.add_parser("stats", help="Show counts by status and source")
    stats_p.set_defaults(func=cmd_stats)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

