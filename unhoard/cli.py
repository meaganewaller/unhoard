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
from .analyze import suggest_collections, suggest_tags
from .config import load_config, write_default_config, CONFIG_PATH
from .digest import build_digest, ensure_ai_suggestions, needs_fresh_summary
from .review import review_collections_interactive, review_tags_interactive
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
            elif hasattr(adapter, "create_collection"):
                # Collection doesn't exist, but adapter supports creation -- create it now
                try:
                    collection_id = adapter.create_collection(suggested_collection)
                    collection_title = suggested_collection
                    console.print(f"Created collection '{escape(suggested_collection)}'")
                except Exception as e:  # noqa: BLE001 -- report and skip collection
                    print_warning(f"couldn't create collection '{escape(suggested_collection)}': {escape(str(e))}")
            else:
                print_warning(f"suggested collection '{escape(suggested_collection)}' doesn't exist and source doesn't support creation -- skipping.")

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


def cmd_apply_all(args: argparse.Namespace) -> int:
    """Batch version of `apply`: chews through up to `--limit` active items
    (oldest first), generating an AI summary/tags/collection for any that
    don't have one yet -- exactly like the stale digest bucket does -- and
    pushing whichever of tags/collection/summary are requested and not
    already applied. Unlike `apply`, this never touches a key: it picks its
    own targets, which is the point -- it's for chewing through backlog
    on demand instead of one `apply <key>` at a time."""
    cfg = load_config()
    store = StateStore(cfg.state_db_path)

    want_tags = args.all or args.tags
    want_collection = args.all or args.collection
    want_summary = args.all or args.summary
    if not (want_tags or want_collection or want_summary):
        print_error("specify --tags, --collection, --summary, or --all")
        return 1

    candidates = sorted(store.active_items(), key=lambda r: r["created_at"])
    collections_cache: dict[str, list[dict]] = {}

    applied_count = 0
    skipped_count = 0
    for row in candidates:
        if applied_count >= args.limit:
            break

        needs_tags = want_tags and not row["tags_applied_at"]
        needs_collection = want_collection and not row["collection_applied_at"]
        needs_summary = want_summary and not row["summary_applied_at"]
        if not (needs_tags or needs_collection or needs_summary):
            skipped_count += 1
            continue

        adapter = find_adapter_for_source(cfg, row["source"])
        if adapter is None or not isinstance(adapter, WritebackAdapter):
            skipped_count += 1
            continue

        # Only worth fetching real collections when we might resolve a suggested
        # one to an id, or ground a fresh AI suggestion in them -- a --tags-only
        # batch with cached suggestions has no use for them at all.
        will_generate = cfg.anthropic_enabled and needs_fresh_summary(cfg, row)
        collections: list[dict] = []
        if needs_collection or will_generate:
            if row["source"] not in collections_cache:
                collections_cache[row["source"]] = (
                    adapter.list_collections() if hasattr(adapter, "list_collections") else []
                )
            collections = collections_cache[row["source"]]
        collection_names = [c["title"] for c in collections]

        summary_text, suggested_tags, suggested_collection = ensure_ai_suggestions(
            cfg, store, row, collection_names
        )

        tags: Optional[list[str]] = suggested_tags if needs_tags and suggested_tags else None

        collection_id: Optional[int] = None
        collection_title: Optional[str] = None
        if needs_collection and suggested_collection:
            match = next((c for c in collections if c["title"].lower() == suggested_collection.lower()), None)
            if match:
                collection_id, collection_title = match["id"], match["title"]
            elif hasattr(adapter, "create_collection"):
                # Collection doesn't exist, but adapter supports creation -- create it now
                try:
                    collection_id = adapter.create_collection(suggested_collection)
                    collection_title = suggested_collection
                    # Add to in-memory cache so subsequent items in this batch find it
                    collections.append({"id": collection_id, "title": suggested_collection})
                    collections_cache[row["source"]] = collections
                    console.print(f"  [dim]created collection '{escape(suggested_collection)}'[/dim]")
                except Exception as e:  # noqa: BLE001 -- report and skip collection, keep processing
                    print_warning(f"couldn't create collection '{escape(suggested_collection)}': {escape(str(e))}")

        note = summary_text if needs_summary and summary_text else None

        if tags is None and collection_id is None and note is None:
            skipped_count += 1
            continue

        try:
            adapter.apply_updates(row["source_id"], tags=tags, collection_id=collection_id, note=note)
        except Exception as e:  # noqa: BLE001 -- report and keep going, one bad item shouldn't stop the batch
            print_warning(f"couldn't apply to {escape(row['title'])}: {escape(str(e))}")
            skipped_count += 1
            continue

        store.mark_applied(row["key"], tags=tags is not None, collection=collection_id is not None, summary=note is not None)

        applied: list[str] = []
        if tags is not None:
            applied.append(f"tags ({escape(', '.join(tags))})")
        if collection_id is not None:
            assert collection_title is not None, "set together with collection_id above"
            applied.append(f"collection ({escape(collection_title)})")
        if note is not None:
            applied.append("summary as note")
        console.print(f"  [green]applied[/green] {escape(row['title'])}: {', '.join(applied)}")
        applied_count += 1

    print_success(f"Applied to {applied_count} item(s); {skipped_count} skipped (nothing to do or no write-back support).")
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

    processing_table = Table(title="By processing state")
    processing_table.add_column("State")
    processing_table.add_column("Count", justify="right")
    for state, count in s["by_processing_state"].items():
        processing_table.add_row(state, str(count))
    console.print(processing_table)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Analyze untagged items and suggest collections and tags.

    Pipeline: fetch → suggest collections → review → suggest tags → review → store.
    Exits early if the user cancels either review step.
    """
    cfg = load_config()
    store = StateStore(cfg.state_db_path)

    # Step 1: Fetch untagged items.
    console.print(f"Fetching up to [bold]{args.items}[/bold] untagged items...")
    items = store.fetch_untagged_items(limit=args.items)
    if not items:
        print_warning("No untagged items found — nothing to analyze.")
        return 0
    console.print(f"  Found [bold]{len(items)}[/bold] items.")

    # Step 2: LLM collection clustering.
    console.print("Suggesting collections via LLM...")
    if args.batch:
        console.print(
            "[dim]  Batch mode: 50% of standard token rates, but the run blocks "
            "until the batch finishes (usually minutes, occasionally hours).[/dim]"
        )
    collection_suggestions = suggest_collections(
        items, limit=args.items, api_key=cfg.anthropic_api_key, model=cfg.model,
        batch=args.batch,
    )
    if not collection_suggestions:
        print_warning("LLM returned no collection suggestions — check ANTHROPIC_API_KEY.")
        return 1
    console.print(f"  Got [bold]{len(collection_suggestions)}[/bold] collection suggestion(s).")

    # Persist immediately, before offering review. These cost real money to
    # generate, and review is where a run is most likely to end early -- a
    # cancel, a Ctrl-C, or piped stdin hitting EOFError. Storing first means
    # ending the run early costs nothing to redo: the suggestions are on disk,
    # `apply-all` can push them, and the next `analyze` skips these items
    # instead of re-sending the whole corpus at full price.
    store.bulk_store_suggestions(items, collection_suggestions, [])

    # Step 3: User review of collections (skip when --auto-apply).
    if args.auto_apply:
        reviewed_collections = collection_suggestions
    else:
        reviewed_collections = review_collections_interactive(collection_suggestions)
        if not reviewed_collections:
            console.print(
                "[dim]Review canceled — the collection suggestions were already "
                "saved, so re-running won't re-query the model.[/dim]"
            )
            return 0

    # Step 4: Build the collection map (item_id -> collection name) for tag grouping.
    collections_map: dict[int, str] = {
        cs.item_id: cs.suggested_collection for cs in reviewed_collections
    }

    # Step 5: LLM tag suggestion.
    console.print("Suggesting tags via LLM...")
    # fast_model, not model: assigning from a fixed 10-word vocabulary is
    # classification, not the judgment call that picking the taxonomy was.
    tag_suggestions = suggest_tags(
        items, collections_map, api_key=cfg.anthropic_api_key, model=cfg.fast_model,
        batch=args.batch,
    )
    if not tag_suggestions:
        print_warning("LLM returned no tag suggestions.")
    else:
        console.print(f"  Got [bold]{len(tag_suggestions)}[/bold] tag suggestion(s).")
        # Same reasoning as the collection suggestions above -- bank the paid-for
        # result before the second place the run can end early. Also re-stores the
        # reviewed collections so any edits from step 3 land even if tag review
        # is cancelled.
        store.bulk_store_suggestions(items, reviewed_collections, tag_suggestions)

    # Step 6: User review of tags (skip when --auto-apply).
    if args.auto_apply:
        reviewed_tags = tag_suggestions
    else:
        reviewed_tags = review_tags_interactive(
            tag_suggestions, by_collection=True, collections=collections_map
        )
        if not reviewed_tags:
            console.print(
                "[dim]Tag review cancelled — the suggestions were already saved, "
                "so re-running won't re-query the model.[/dim]"
            )
            return 0

    # Step 7: Persist to DB.
    console.print("Saving suggestions to local state...")
    store.bulk_store_suggestions(items, reviewed_collections, reviewed_tags)
    print_success(
        f"Stored {len(reviewed_collections)} collection suggestion(s) "
        f"and {len(reviewed_tags)} tag suggestion(s)."
    )

    # Step 8: Offer to sync to Raindrop.
    if _is_interactive():
        try:
            answer = input("Sync suggestions to Raindrop now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer == "y":
            console.print("[dim]Tip: run `unhoard apply-all --all` to push suggestions to Raindrop.[/dim]")

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unhoard", description="Daily triage digest for your reading backlog.")
    sub = p.add_subparsers(dest="command")

    init_p = sub.add_parser(
        "init",
        help=f"Write a starter config to {CONFIG_PATH}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  unhoard init                    Write default config to {CONFIG_PATH}
  unhoard init --force            Overwrite existing config

Next steps after init:
  1. Set RAINDROP_TOKEN (https://app.raindrop.io/settings/integrations)
  2. Set ANTHROPIC_API_KEY (optional, for AI summaries)
  3. unhoard sync                 Fetch items from your sources
  4. unhoard digest               Generate today's digest
        """.strip()
    )
    init_p.add_argument("--force", action="store_true", help="Overwrite existing config")
    init_p.set_defaults(func=cmd_init)

    sync_p = sub.add_parser(
        "sync",
        help="Pull latest items from configured sources into local state",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard sync                           Sync from sources in your config
  unhoard sync --source chrome           Ad-hoc sync from Chrome only
  unhoard sync --source chrome --source safari
                                         Sync from multiple ad-hoc sources

Related commands:
  unhoard digest                         Generate digest after syncing
  unhoard stats                          View item counts by source
        """.strip()
    )
    sync_p.add_argument(
        "--source", action="append",
        help="Ad-hoc source instead of config.toml, e.g. --source chrome --source json:/path.json "
             "(repeatable)",
    )
    sync_p.set_defaults(func=cmd_sync)

    digest_p = sub.add_parser(
        "digest",
        help="Generate today's digest markdown file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard digest                 Write digest to output directory
  unhoard digest --print         Also display digest on stdout

Digest contains:
  - Recently added items (newest first)
  - Stale items (haven't been touched in 30+ days)
  - AI-generated summaries and tag suggestions

Related commands:
  unhoard analyze                Interactive review of suggestions
  unhoard apply <key> --summary  Push digest suggestions to source
  unhoard apply-all --all        Batch push suggestions for multiple items
        """.strip()
    )
    digest_p.add_argument("--print", action="store_true", help="Also print the digest to stdout")
    digest_p.set_defaults(func=cmd_digest)

    mark_p = sub.add_parser(
        "mark",
        help="Mark an item done, unhoarded, or snoozed by its key (or fragment)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard mark raindrop:12345 --done
                                 Mark as handled (closed out)
  unhoard mark "react pattern" --unhoarded
                                 Mark as synthesized and sourced elsewhere
  unhoard mark abc123 --unhoarded --note "Used in project X"
                                 Mark with provenance note
  unhoard mark def456 --snooze 14
                                 Hide for 14 days, reappear later

Item key:
  Use full key (e.g. raindrop:12345) or a unique fragment ("react", "patterns")
  The tool will auto-complete or show options if ambiguous.

Related commands:
  unhoard synthesize <key>       Extract article text before marking
  unhoard stats                  View current item counts
        """.strip()
    )
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
        "apply",
        help="Push AI-suggested tags/collection/summary to the source, on demand",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard apply raindrop:12345 --all
                                 Apply tags, collection, and summary
  unhoard apply abc123 --tags    Apply only suggested tags
  unhoard apply def456 --collection --summary
                                 Apply collection and summary, skip tags

Requires:
  - Item must have AI suggestions (run 'unhoard digest' first)
  - Source must support write-back (Raindrop works, others may not)

Related commands:
  unhoard digest                 Generate suggestions for items
  unhoard analyze                Interactive review before applying
  unhoard apply-all --all        Batch apply to multiple items
        """.strip()
    )
    apply_p.add_argument("key", help="Item key, e.g. raindrop:12345 (a unique fragment also works)")
    apply_p.add_argument("--tags", action="store_true", help="Apply the suggested tags")
    apply_p.add_argument("--collection", action="store_true", help="Move to the suggested collection")
    apply_p.add_argument("--summary", action="store_true", help="Write the AI summary into the source's note field")
    apply_p.add_argument("--all", action="store_true", help="Apply tags, collection, and summary")
    apply_p.set_defaults(func=cmd_apply)

    apply_all_p = sub.add_parser(
        "apply-all",
        help="Batch-generate and push suggestions for up to N unacted items",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard apply-all --all        Apply tags, collection, summary to 10 items
  unhoard apply-all --all --limit 25
                                 Apply to 25 items instead of default 10
  unhoard apply-all --tags       Apply only tags (generate if missing)
  unhoard apply-all --summary --limit 5
                                 Generate and apply summaries to 5 items

Behavior:
  - Processes oldest items first (by creation date)
  - Generates AI suggestions for items that don't have them yet
  - Skips items that have already been acted on for that dimension
  - Continues on errors (one failure doesn't stop the batch)

Related commands:
  unhoard apply <key> --all      Apply to a single item by key
  unhoard digest                 See what suggestions are available
  unhoard analyze                Interactive review before batch applying
        """.strip()
    )
    apply_all_p.add_argument(
        "--limit", type=int, default=10, metavar="N",
        help="Max number of items to act on in one run (default: 10)",
    )
    apply_all_p.add_argument("--tags", action="store_true", help="Apply suggested tags")
    apply_all_p.add_argument("--collection", action="store_true", help="Move to the suggested collection")
    apply_all_p.add_argument("--summary", action="store_true", help="Write the AI summary into the source's note field")
    apply_all_p.add_argument("--all", action="store_true", help="Apply tags, collection, and summary")
    apply_all_p.set_defaults(func=cmd_apply_all)

    synthesize_p = sub.add_parser(
        "synthesize",
        help="Extract full article text into a standalone markdown note",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard synthesize raindrop:12345
                                 Synthesize article into markdown note
  unhoard synthesize "react tutorial"
                                 Find item by fragment and synthesize
  unhoard synthesize abc123 --force
                                 Overwrite existing note for this item

Output:
  Markdown files are written to: <output_dir>/synthesized/
  Each file includes YAML frontmatter (title, url, tags, etc) plus:
    - AI-generated summary (if available)
    - Full article text

Workflow:
  1. unhoard digest               See which items have full text available
  2. unhoard synthesize <key>     Extract article to markdown
  3. unhoard mark <key> --unhoarded --note "location"
                                 Mark as handled and note where you stored it

Related commands:
  unhoard digest                 Check synthesis status and summaries
  unhoard mark <key> --unhoarded
                                 Mark as handled after writing about it
        """.strip()
    )
    synthesize_p.add_argument("key", help="Item key, e.g. raindrop:12345 (a unique fragment also works)")
    synthesize_p.add_argument("--force", action="store_true", help="Overwrite an existing note for this item")
    synthesize_p.set_defaults(func=cmd_synthesize)

    stats_p = sub.add_parser(
        "stats",
        help="Show item counts by status, source, and processing state",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard stats                  Display all counts

Tables:
  By status                       active, done, unhoarded
  Active items by source          where items came from (Raindrop, Chrome, etc)
  By processing state             new, awaiting_suggestions, ready_to_review, etc

Related commands:
  unhoard digest                 See items and suggestions
  unhoard sync                   Update stats by syncing from sources
        """.strip()
    )
    stats_p.set_defaults(func=cmd_stats)

    analyze_p = sub.add_parser(
        "analyze",
        help="Analyze untagged items and suggest collections and tags via LLM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  unhoard analyze                Review and tag up to 200 items interactively
  unhoard analyze --items 50     Review and tag up to 50 items
  unhoard analyze --items 1000   Work through a larger slice of the backlog
  unhoard analyze --auto-apply   Skip review and auto-apply all suggestions
  unhoard analyze --items 1414 --batch --auto-apply
                                 Whole backlog at half price, unattended

Workflow:
  1. Fetches untagged items
  2. Suggests collections via LLM (in chunks, so every item gets an answer)
  3. Saves those suggestions, then you review and adjust them
  4. Suggests tags (grouped by collection) via LLM
  5. Saves those too, then you review and adjust them
  6. Persists the reviewed result (optionally syncs to Raindrop)

Cost:
  Each run queries the model once per chunk of items, so --items is the main
  cost lever. --batch halves the token rate when you can wait. Suggestions are
  saved as soon as they arrive, so cancelling a review never throws away work
  you've already paid for, and already-analyzed items are skipped next run.
  Tagging uses the cheaper `fast_model`; only the collection taxonomy uses
  `model`. Both are set in ~/.config/unhoard/config.toml.

Requires:
  - ANTHROPIC_API_KEY set (for LLM suggestions)

Related commands:
  unhoard digest                 Generate and review suggestions (lighter weight)
  unhoard apply-all --tags       Apply stored suggestions without re-analyzing
  unhoard apply <key> --all      Apply suggestions to a single item
        """.strip()
    )
    analyze_p.add_argument(
        "--items", type=int, default=200, metavar="N",
        help="Max items to analyze (default: 200)",
    )
    analyze_p.add_argument(
        "--auto-apply", action="store_true",
        help="Skip interactive review and auto-apply all suggestions",
    )
    analyze_p.add_argument(
        "--batch", action="store_true",
        help="Use the Message Batches API: 50%% of standard token rates, but "
             "the run blocks until the batch finishes (usually minutes, "
             "occasionally hours). Best paired with --auto-apply.",
    )
    analyze_p.set_defaults(func=cmd_analyze)

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

