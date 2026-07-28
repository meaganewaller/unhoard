# `unhoard`

Turns your reading-list backlog (Raindrop, browser bookmarks, or any JSON export) into a small daily digest, instead of a pile you eventually declare bankruptcy on.

## How It Works

An item keeps showing up in the digest until you either run `mark ... --done`, or handle it in the source app itself (archive it, move it out of Raindrop's Unsorted, delete the bookmark). The next `sync` notices it's gone from the source and closes it out automatically. unhoard never deletes anything on its own -- but `mark <key> --unhoarded` can optionally write a small marker (a tag and/or collection move) back to a source that supports it, and only when you explicitly ask for it. See "What 'Unhoarded' Means" below.

## Install

After cloning this repo:

```bash
brew tap meaganewaller/unhoard
brew install unhoard
```

## Dev / Local Install Instead

```
git repo clone git@github.com:meaganewaller/unhoard
cd unhoard
pip install -e
```

## Setup

```bash
unhoard init                  # writes ~/.config/unhoard/config.toml
export RAINDROP_TOKEN=...     # <https://app.raindrop.io/settings/integrations> -> For Developers -> Create test token
export ANTHROPIC_API_KEY=...  # optional -- enables AI summaries for stale items
```

With just `RAINDROP_TOKEN` set and no `[[sources]]` in the config, it defaults to Raindrop's "Unsorted" collection (id: `0`).

### Adding More Sources

Edit `~/.config/unhoard/config.toml`:

```toml
[[sources]]
type = "raindrop"
collection_id = 0        # 0 = Unsorted, -1 = All

[[sources]]
type = "chrome"           # reads Chrome's local Bookmarks + Reading List JSON directly
# profile_dir = "~/Library/Application Support/Google/Chrome/Profile 1"  # only if not Default

[[sources]]
type = "safari"           # reads ~/Library/Safari/Bookmarks.plist (macOS only)

[[sources]]
type = "json"
name = "pocket"
source_path = "~/exports/pocket-export.json"
# records_path = "data.items"          # if the list is nested
# field_map = { title = "name", url = "link", created_at = "saved_at" }
```

`type = "json"` is the escape hatch: point it at any JSON file or URL and it'll guess common field names (`title`, `name`, `url`, `link`, `created`, `date`, etc.); override it with `field_map` if it guesses wrong.

You can also skip the config file entirely for one-off syncs:


```bash
unhoard sync --source chrone --source safari --source json:/path/export.json
```

## Daily Use

```bash
unhoard sync                     # pull latest from all configured sources
unhoard digest                   # write today's digest to ~/unhoard-digests/
unhoard stats                    # quick counts
unhoard mark <key> --done        # e.g., unhoard mark raindrop:123456 --done
unhoard mark <key> --unhoarded --note "..."  # see "What 'Unhoarded' Means" below
unhoard mark <key> --snoze 14    # hide for 2 weeks
unhoard apply <key> --all        # push suggested tags/collection + AI summary to the source
unhoard synthesize <key>         # pull the full article text into a standalone note to work from
```

`digest` always writes both `digest-YYYY-MM-DD.md` and `latest.md` (same content) so cronjobs / scripts can always read a stable filename.

`synthesize` writes to `<output_dir>/synthesized/<slug>.md` -- frontmatter (title/url/source/tags) plus the AI summary (if cached) and the full fetched article text, real material to actually work from instead of just a link. It won't overwrite a note you've already started editing unless you pass `--force`. This is distinct from `--unhoarded`: synthesize just pulls the raw material in, `--unhoarded --note` is the separate, later step of recording that you actually used it somewhere.

## CLI Experience

Output across `sync`/`mark`/`apply`/`stats`/`init` is color-coded (green success, yellow warning, red error) via [rich](https://github.com/Textualize/rich); `stats` renders as tables. `digest --print` renders the digest as formatted Markdown in the terminal instead of dumping raw source -- the saved `digest-YYYY-MM-DD.md`/`latest.md` files are untouched, still plain markdown for cron/scripts/Claude Code to read.

When `digest` actually needs to call the AI for one or more stale items, you'll see a progress bar; a fully-cached digest (nothing to summarize) skips it entirely and renders instantly, same as before.

If a `mark`/`apply` key fragment matches more than one item, you get an interactive picker (via [questionary](https://github.com/tmbo/questionary)) to choose which one -- but only in a real terminal. Piped output, scripts, and cron jobs (no tty attached) get the old behavior: the match list printed to stderr and a non-zero exit, so nothing hangs waiting on input that'll never come.

All of this degrades gracefully when piped or redirected -- rich detects non-terminal output and drops color/table-drawing in favor of plain readable text automatically.

## What "Unhoarded" Means

`--done` just means "stop showing me this" -- you might have read it, ignored it, or decided you don't care. `--unhoarded` means something stronger: you actually synthesized and stored the information somewhere, and properly sourced/cited it back to the original. An old-web tutorial you folded into a retrospective page with a link back is unhoarded; one you skimmed and closed is just done.

```bash
unhoard mark raindrop:123456 --unhoarded --note "folded into the CSS-tricks retrospective page, linked back to original"
```

`--note` is where the "properly sourced" half lives -- record where/how you used it. Skipping it still works, but you'll get a warning: an unhoarded item without a note is only half-unhoarded.

For sources that support it (currently just Raindrop), marking an item unhoarded also writes a tag back to the item in the source app itself -- `unhoarded` by default, configurable via `unhoarded_tag` -- and, if you've set `unhoarded_collection_id`, moves it to that collection too. Existing tags are preserved, not overwritten. Sources without write-back support (Chrome, Safari, generic JSON) still get the local `--unhoarded` state; you'll just see a note that there's no write-back for that source.

Write-back is best-effort: if it fails (network, auth, whatever) or isn't supported, the local mark still succeeds -- local state is unhoard's source of truth, the source-app marker is enrichment on top.

## Cronjobs

```cron
0 7 * * * /usr/bin/env unhoard sync && /usr/bin/env unhoard digest
```

## Claude Code

Since the digest is just a Markdown file at `~/unhoard-digets/latest.md`, wiring it in Claude Code is as simple as a slash command or hook that reads that file, no special integration needed. For example, a `.claude/commands/triage.md` that says "read `~/unhoard-digests/latest.md` and help me decide on the top 3 stale items" works as-is.

## How the "Stale" Bucket Works

Items older than `stale_days` (default 30) get their article text fetched and summarized by Claude, with a suggested action (Read/Skim/Archive/Delete). Summaries are cached by content hash, so rerunning `digest` doesn't re-summarize or re-spend tokens on items you've already seen. Items in the `new` and `aging` buckets are metadata-only (title/tags/excerpt).

### Giving It Context

The summarizer only sees the article text by default, so it'll happily recommend Delete on something that's "outdated" in isolation but that you actually want -- e.g. an archived tutorial you're deliberately collecting for an old-web-style project. Set `context` (in `config.toml` or `UNHOARD_CONTEXT`) to a few sentences about what you're doing with your backlog, and it's included in the summarization prompt so the Action recommendation accounts for it:

```toml
context = """
I collect and restore old-web / early-internet style sites: archived tutorials,
GeoCities-era design patterns, old CSS tricks. These often read as "outdated"
but are reference material I actively reuse -- don't recommend Delete for
nostalgic/archival web content like this.
"""
```

Changing `context` invalidates cached summaries for stale items (compared cheaply, no re-fetch needed unless it actually changed), so the next `digest` re-evaluates them under the new rule instead of showing the old cached recommendation forever.

### Suggested Tags & Collection

Alongside Summary/Action, stale items also get a suggested set of tags and a suggested destination collection, shown in the digest like:

```
*suggested -- tags: geocities, blinkie | collection: Old Web Archive (apply with `unhoard apply <key>`)*
```

The collection suggestion is grounded in your real Raindrop collections (fetched once per digest run, only when something actually needs summarizing) -- the model picks from your existing collections or says "none," it doesn't invent new ones. Sources without collections (Chrome, Safari, generic JSON) just don't get a collection suggestion.

Nothing here writes anything on its own -- suggestions just sit in the digest until you run:

```bash
unhoard apply <key> --tags          # merge the suggested tags into the item (Raindrop only, doesn't replace existing tags)
unhoard apply <key> --collection    # move the item to the suggested collection
unhoard apply <key> --summary       # write the AI summary into the source's note field
unhoard apply <key> --all           # all three at once
```

Once applied, that suggestion drops out of future digests -- it's tracked locally so you don't keep getting nagged about something you already pushed. If the suggested collection no longer exists by the time you apply (renamed/deleted), that part is skipped with a message rather than erroring.

Note: `mark --unhoarded --note "..."` and `apply --summary` both write to the same Raindrop note field -- there's only one per item, so whichever you run last wins.

## Config Reference

All of these can be set as environment variables (`UNHOARD_*`) or in `config.toml`

| Setting | Default | Meaning |
| --- | --- | --- |
| `aging_days` | 7 | Below this = "New" |
| `stale_days` | 30 | Above this = "Stale" (gets AI summary) |
| `max_new` / `max_aging` / `max_stale` | 6 / 6 / 8 | Cap per bucket per digest |
| `model` | `claude-sonnet-5` | Model used for stale-item summaries |
| `output_dir` | `~/unhoard-digests/` | Where digest files land |
| `context` | _(empty)_ | Free-text notes on your projects/interests, given to the summarizer |
| `unhoarded_tag` | `unhoarded` | Tag written to a Raindrop item on `mark --unhoarded` |
| `unhoarded_collection_id` | _(unset)_ | If set, also moves the item to this Raindrop collection on `mark --unhoarded` |
