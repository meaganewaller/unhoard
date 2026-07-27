# `unhoard`

Turns your reading-list backlog (Raindrop, browser bookmarks, or any JSON export) into a small daily digest, instead of a pile you eventually declare bankruptcy on.

## How It Works

An item keeps showing up in the digest until you either run `mark ... --done`, or handle it in the source app itself (archive it, move it out of Raindrop's Unsorted, delete the bookmark). The next `sync` notices it's gone from the source and closes it out automatically. Nothing is ever deleted by this tool. It only reads.

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
unhoard mark <key> --snoze 14    # hide for 2 weeks
```

`digest` always writes both `digest-YYYY-MM-DD.md` and `latest.md` (same content) so cronjobs / scripts can always read a stable filename.

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
