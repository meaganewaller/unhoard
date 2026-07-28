"""Local state: what we've seen, what's been dealt with, and cached AI summaries.

This is what makes the tool convergent instead of another pile: an item only
keeps reappearing in your digest until you either mark it done, or handle it
in the source app itself (move it out of Raindrop's Unsorted, delete the Chrome
bookmark, etc.) -- the next `sync` notices it's gone and marks it done automatically.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .schema import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    tags TEXT,
    excerpt TEXT,
    collection TEXT,
    created_at TEXT,
    first_seen_local TEXT,
    last_synced TEXT,
    times_shown INTEGER DEFAULT 0,
    last_shown_date TEXT,
    status TEXT DEFAULT 'active',   -- active | done | snoozed | unhoarded
    status_reason TEXT,
    snooze_until TEXT,
    summary TEXT,
    summary_model TEXT,
    summary_date TEXT,
    content_hash TEXT,
    context_hash TEXT,
    note TEXT
);
"""


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """CREATE TABLE IF NOT EXISTS won't add columns to a table that already
        exists from an earlier version -- patch those in here."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "context_hash" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN context_hash TEXT")
        if "note" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN note TEXT")

    @contextmanager
    def _cursor(self):
        cur = self.conn.cursor()
        try:
            yield cur
            self.conn.commit()
        finally:
            cur.close()

    def upsert_items(self, items: Iterable[Item]) -> set[str]:
        """Insert new items, refresh metadata on existing ones. Returns the set of keys seen."""
        now = datetime.now(timezone.utc).isoformat()
        seen_keys = set()
        with self._cursor() as cur:
            for item in items:
                seen_keys.add(item.key)
                existing = cur.execute("SELECT key FROM items WHERE key = ?", (item.key,)).fetchone()
                if existing:
                    cur.execute(
                        """UPDATE items SET title=?, url=?, tags=?, excerpt=?, collection=?,
                           created_at=?, last_synced=? WHERE key=?""",
                        (
                            item.title, item.url, json.dumps(item.tags), item.excerpt,
                            item.collection, item.created_at.isoformat(), now, item.key,
                        ),
                    )
                else:
                    cur.execute(
                        """INSERT INTO items
                           (key, source, source_id, title, url, tags, excerpt, collection,
                            created_at, first_seen_local, last_synced, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            item.key, item.source, item.source_id, item.title, item.url,
                            json.dumps(item.tags), item.excerpt, item.collection,
                            item.created_at.isoformat(), now, now, "active",
                        ),
                    )
        return seen_keys

    def mark_missing_as_done(self, source: str, seen_keys: set[str], reason: str = "removed from source"):
        """Anything from this source that's active locally but wasn't in this sync's
        results has been handled outside the tool (moved/archived/deleted) -- close it out."""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT key FROM items WHERE source = ? AND status = 'active'", (source,)
            ).fetchall()
            stale_keys = [r["key"] for r in rows if r["key"] not in seen_keys]
            for key in stale_keys:
                cur.execute(
                    "UPDATE items SET status='done', status_reason=? WHERE key=?", (reason, key)
                )
        return len(stale_keys)

    def active_items(self, today: Optional[date] = None) -> list[sqlite3.Row]:
        today = today or date.today()
        rows = self.conn.execute(
            "SELECT * FROM items WHERE status = 'active' OR (status='snoozed' AND (snooze_until IS NULL OR snooze_until <= ?))",
            (today.isoformat(),),
        ).fetchall()
        return rows

    def mark_done(self, key: str, reason: str = "marked done"):
        with self._cursor() as cur:
            cur.execute("UPDATE items SET status='done', status_reason=? WHERE key=?", (reason, key))

    def mark_unhoarded(self, key: str, note: str = "", reason: str = "unhoarded via CLI"):
        """Distinct from mark_done: means the information was actually synthesized,
        stored, and properly sourced elsewhere -- not just closed out or ignored."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE items SET status='unhoarded', status_reason=?, note=? WHERE key=?",
                (reason, note, key),
            )

    def mark_snoozed(self, key: str, until: date):
        with self._cursor() as cur:
            cur.execute(
                "UPDATE items SET status='snoozed', snooze_until=? WHERE key=?", (until.isoformat(), key)
            )

    def record_shown(self, keys: Iterable[str]):
        today = date.today().isoformat()
        with self._cursor() as cur:
            for key in keys:
                cur.execute(
                    "UPDATE items SET times_shown = times_shown + 1, last_shown_date=? WHERE key=?",
                    (today, key),
                )

    def save_summary(self, key: str, summary: str, model: str, content_hash: str, context_hash: str):
        with self._cursor() as cur:
            cur.execute(
                """UPDATE items SET summary=?, summary_model=?, summary_date=?,
                   content_hash=?, context_hash=? WHERE key=?""",
                (summary, model, datetime.now(timezone.utc).isoformat(), content_hash, context_hash, key),
            )

    def stats(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM items GROUP BY status").fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        by_source = self.conn.execute(
            "SELECT source, COUNT(*) c FROM items WHERE status='active' GROUP BY source"
        ).fetchall()
        return {
            "by_status": by_status,
            "active_by_source": {r["source"]: r["c"] for r in by_source},
        }

    def find_by_prefix(self, key_prefix: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE key LIKE ?", (f"{key_prefix}%",)
        ).fetchall()

