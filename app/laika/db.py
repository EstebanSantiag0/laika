"""SQLite storage (WAL). One connection, used from the event loop thread only.

Calls are short (single-user app), so they run synchronously inside async code.
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'companion', 'correction', 'system')),
    channel TEXT NOT NULL,
    text TEXT NOT NULL,
    lang TEXT,
    audio_path TEXT,
    batch_id INTEGER,
    status TEXT NOT NULL DEFAULT 'sent',
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS messages_batch ON messages(batch_id);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    opened_ts REAL NOT NULL,
    closed_ts REAL,
    status TEXT NOT NULL DEFAULT 'open',
    channel TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    run_after REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(batches)")}
        if "raw" not in cols:  # W2: her reply exactly as generated, replayed in later prompts
            self.conn.execute("ALTER TABLE batches ADD COLUMN raw TEXT")

    # settings -----------------------------------------------------------
    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # messages -----------------------------------------------------------
    def add_message(self, role, channel, text, lang=None, batch_id=None, status="sent", meta=None,
                    audio_path=None):
        cur = self.conn.execute(
            "INSERT INTO messages(ts, role, channel, text, lang, batch_id, status, meta, audio_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), role, channel, text, lang, batch_id, status,
             json.dumps(meta or {}, ensure_ascii=False), audio_path),
        )
        return self.get_message(cur.lastrowid)

    def get_message(self, message_id):
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def set_meta(self, message_id, **fields):
        msg = self.get_message(message_id)
        if msg:
            self.update_message(message_id, meta={**msg["meta"], **fields})

    def update_message(self, message_id, **fields):
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE messages SET {cols} WHERE id = ?", (*fields.values(), message_id))

    def history(self, before_id=None, limit=50):
        """Newest-last page of messages for the chat UI."""
        if before_id:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        else:
            rows = self.conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        return [_message(r) for r in reversed(rows.fetchall())]

    def conversation_since(self, start_id):
        """User and companion messages from start_id on, oldest first (corrections excluded)."""
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE id >= ? AND role IN ('user', 'companion') ORDER BY id", (start_id,))
        return [_message(r) for r in rows.fetchall()]

    # batches ------------------------------------------------------------
    def open_batch(self, channel):
        cur = self.conn.execute("INSERT INTO batches(opened_ts, channel) VALUES (?, ?)", (time.time(), channel))
        return cur.lastrowid

    def close_batch(self, batch_id, status, raw=None):
        self.conn.execute("UPDATE batches SET closed_ts = ?, status = ?, raw = ? WHERE id = ?",
                          (time.time(), status, raw, batch_id))

    def batch_raws(self, batch_ids):
        """{batch_id: reply as generated} for finished batches (interrupted ones replay their bubbles)."""
        ids = [i for i in batch_ids if i]
        if not ids:
            return {}
        rows = self.conn.execute(f"SELECT id, raw FROM batches WHERE status = 'done' AND raw IS NOT NULL "
                                 f"AND id IN ({','.join('?' * len(ids))})", ids)
        return {r["id"]: r["raw"] for r in rows}


def _message(row):
    m = dict(row)
    m["meta"] = json.loads(m["meta"] or "{}")
    return m
