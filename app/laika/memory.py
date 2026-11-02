"""Organic Knowledge store (PLAN Phase 3, DECISIONS D14): what Laika remembers about the user.

SQLite is the fast store. Episodes (her diary gist of each stretch of conversation), facts
(atomic, with validity dates and evidence), entities (people, topics) with co-mention edges,
intentions (open loops to follow up), and rounds (a message plus her reply, for exact wording).
Facts, episodes and rounds are indexed for full-text search (FTS5) and vector search (sqlite-vec),
all through one `items` table whose ids are the rowids of both indexes.

Strength follows ACT-R base-level activation over the times a memory was actually used (mentioned
again or used in a reply), so trivia fades from ranking while important things stay pinned; nothing
fades out of storage. Changed facts are superseded, never deleted; only "forget" deletes.
"""
import hashlib
import json
import math
import re
import sqlite3
import time

import sqlite_vec

DIM = 1024
DAY = 86400.0
STOPWORDS = set("""the and but that this those these with from just really very what what's which when where
there then than were been have has had did does your yours about into over after before also ever even still
some thing like they them their would could should will shall can can't don't didn't i'm you're it's that's
here how why who whom whose not are was is be am being for you she him her his its our out all any one""".split())
SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    aliases TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    note_path TEXT,
    episodes INTEGER NOT NULL DEFAULT 0,
    importance REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    a INTEGER NOT NULL,
    b INTEGER NOT NULL,
    weight REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    start_msg INTEGER NOT NULL,
    end_msg INTEGER NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    lang TEXT,
    title TEXT NOT NULL,
    narrative TEXT NOT NULL,
    mood TEXT,
    arousal REAL NOT NULL DEFAULT 0,
    self_rel REAL NOT NULL DEFAULT 0,
    future_rel REAL NOT NULL DEFAULT 0,
    entity_ids TEXT NOT NULL DEFAULT '[]',
    note_path TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'identity',
    importance REAL NOT NULL DEFAULT 0.5,
    arousal REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.8,
    sensitive INTEGER NOT NULL DEFAULT 0,
    no_mention INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at REAL NOT NULL,
    superseded_by INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT NOT NULL DEFAULT 'extracted',
    evidence TEXT NOT NULL DEFAULT '[]',
    episode_id INTEGER,
    n_uses INTEGER NOT NULL DEFAULT 1,
    first_ts REAL NOT NULL,
    recent_ts TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS facts_entity ON facts(entity_id);
CREATE TABLE IF NOT EXISTS intentions (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    entity_id INTEGER,
    due_from TEXT,
    due_to TEXT,
    cue_terms TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    asked_at REAL,
    created REAL NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY,
    msg_from INTEGER NOT NULL,
    msg_to INTEGER NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tombstones (
    id INTEGER PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS injections (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    batch_id INTEGER,
    reason TEXT,
    items TEXT NOT NULL,
    tokens INTEGER,
    used TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    UNIQUE (kind, ref_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, tokenize='porter unicode61');
"""
VECTORS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS items_vec USING vec0(embedding float[{DIM}]);
CREATE VIRTUAL TABLE IF NOT EXISTS tombstones_vec USING vec0(embedding float[{DIM}]);
"""
_WORDS = re.compile(r"\w+", re.U)


def load_vec(conn):
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def base_level(n, first_ts, recent, now, d=0.5):
    """ACT-R base-level activation B = ln Σ t^-d over the uses of a memory (ages in days, 1-hour floor).

    Petrov (2006) hybrid: the newest uses (`recent`) count exactly; the n - k older ones are assumed
    evenly spread between the first use and the oldest recent one. One use: +1.6 after an hour, 0
    after a day, -1.7 after a month, -2.95 after a year.
    """
    floor = 1 / 24
    ages = sorted(max((now - t) / DAY, floor) for t in recent)
    total = sum(a ** -d for a in ages)
    k = len(ages)
    if n > k:
        life = max((now - first_ts) / DAY, floor)
        tk = ages[-1] if ages else floor
        if life > tk:
            total += (n - k) * (life ** (1 - d) - tk ** (1 - d)) / ((1 - d) * (life - tk))
        else:
            total += (n - k) * life ** -d
    return math.log(total) if total > 0 else -10.0


def digest(text):
    return hashlib.sha256(" ".join(_WORDS.findall(text.lower())).encode()).hexdigest()


def _polarity(value):
    return value if value in ("like", "dislike") else None


def _row(r):
    d = dict(r)
    for key in ("aliases", "entity_ids", "evidence", "recent_ts", "cue_terms", "items", "used"):
        if key in d and isinstance(d[key], str):
            d[key] = json.loads(d[key])
    return d


class Memory:
    def __init__(self, db):
        self.conn = db.conn
        load_vec(self.conn)
        self.conn.executescript(SCHEMA + VECTORS)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "episode_id" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN episode_id INTEGER")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(facts)")}
        if "time_scope" not in cols:  # W8.3: permanent | stage | dated | archived
            self.conn.execute("ALTER TABLE facts ADD COLUMN time_scope TEXT NOT NULL DEFAULT 'permanent'")
        if "review_after" not in cols:  # W8.3: a "stage" fact is checked again after this day
            self.conn.execute("ALTER TABLE facts ADD COLUMN review_after TEXT")
        if "polarity" not in cols:  # SB1 (D22): likes and dislikes are facts with a polarity
            self.conn.execute("ALTER TABLE facts ADD COLUMN polarity TEXT")
        # SB7b (D23): edits from the app need updated_at (stale-edit check) and a 30-day trash
        for table, col in (("facts", "updated REAL"), ("entities", "trashed_at REAL"), ("episodes", "trashed_at REAL"),
                           ("episodes", "updated REAL")):
            if col.split()[0] not in {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

    # index --------------------------------------------------------------------------------
    def _index(self, kind, ref_id, text, embedding):
        cur = self.conn.execute("INSERT INTO items(kind, ref_id) VALUES (?, ?) ON CONFLICT(kind, ref_id) DO UPDATE "
                                "SET kind = excluded.kind RETURNING id", (kind, ref_id))
        item = cur.fetchone()[0]
        self.conn.execute("DELETE FROM items_fts WHERE rowid = ?", (item,))
        self.conn.execute("INSERT INTO items_fts(rowid, text) VALUES (?, ?)", (item, text))
        if embedding is not None:
            self.conn.execute("DELETE FROM items_vec WHERE rowid = ?", (item,))
            self.conn.execute("INSERT INTO items_vec(rowid, embedding) VALUES (?, ?)",
                              (item, sqlite_vec.serialize_float32(embedding)))
        return item

    def _unindex(self, kind, ref_id):
        row = self.conn.execute("SELECT id FROM items WHERE kind = ? AND ref_id = ?", (kind, ref_id)).fetchone()
        if row:
            for table in ("items_fts", "items_vec"):
                self.conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (row[0],))
            self.conn.execute("DELETE FROM items WHERE id = ?", (row[0],))

    def item(self, item_id):
        row = self.conn.execute("SELECT kind, ref_id FROM items WHERE id = ?", (item_id,)).fetchone()
        return (row["kind"], row["ref_id"]) if row else None

    def search_vec(self, embedding, k=20, kinds=None):
        """[(kind, ref_id, cosine)] nearest first. Vectors are normalized, so L2 ranks like cosine."""
        rows = self.conn.execute("SELECT rowid, distance FROM items_vec WHERE embedding MATCH ? AND k = ? "
                                 "ORDER BY distance", (sqlite_vec.serialize_float32(embedding), k * 3 if kinds else k))
        out = []
        for r in rows:
            if (it := self.item(r["rowid"])) and (not kinds or it[0] in kinds):
                out.append((*it, 1 - r["distance"] ** 2 / 2))
        return out[:k]

    def search_text(self, text, k=20, kinds=None):
        """[(kind, ref_id, bm25)] best first (bm25 is negative; lower is better)."""
        words = {w for w in _WORDS.findall(text.lower()) if len(w) > 2 and w not in STOPWORDS}
        if not words:
            return []
        query = " OR ".join(f'"{w}"' for w in words)
        rows = self.conn.execute("SELECT rowid, bm25(items_fts) AS score FROM items_fts WHERE items_fts MATCH ? "
                                 "ORDER BY score LIMIT ?", (query, k * 3 if kinds else k))
        out = []
        for r in rows:
            if (it := self.item(r["rowid"])) and (not kinds or it[0] in kinds):
                out.append((*it, r["score"]))
        return out[:k]

    # entities -------------------------------------------------------------------------------
    def entity(self, name, kind="topic", aliases=()):
        """Find an entity by name or alias (any script), or create it."""
        now = time.time()
        row = self.conn.execute("SELECT * FROM entities WHERE name = ?", (name,)).fetchone()
        if row is None:
            for r in self.conn.execute("SELECT * FROM entities"):
                if name.lower() in (a.lower() for a in json.loads(r["aliases"])):
                    row = r
                    break
        if row is None:
            cur = self.conn.execute("INSERT INTO entities(kind, name, aliases, created, updated) VALUES (?, ?, ?, ?, ?)",
                                    (kind, name, json.dumps(sorted(set(aliases)), ensure_ascii=False), now, now))
            return self.get_entity(cur.lastrowid)
        known = set(json.loads(row["aliases"]))
        if set(aliases) - known - {row["name"]}:
            self.conn.execute("UPDATE entities SET aliases = ?, updated = ? WHERE id = ?",
                              (json.dumps(sorted(known | set(aliases)), ensure_ascii=False), now, row["id"]))
        return self.get_entity(row["id"])

    def get_entity(self, entity_id):
        row = self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return _row(row) if row else None

    def entities(self, include_trashed=False):
        q = "SELECT * FROM entities" + ("" if include_trashed else " WHERE trashed_at IS NULL") + " ORDER BY name"
        return [_row(r) for r in self.conn.execute(q)]

    def link(self, entity_ids):
        """Strengthen co-mention edges between entities that appear together."""
        ids = sorted(set(entity_ids))
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                self.conn.execute("INSERT INTO edges(a, b, weight) VALUES (?, ?, 1) "
                                  "ON CONFLICT(a, b) DO UPDATE SET weight = weight + 1", (a, b))

    def neighbours(self, entity_id):
        """Co-mentioned entities with their weights. SB7b: a `related` link he hid doesn't count, and a link he
        drew between two entities counts as the strongest one."""
        rows = self.conn.execute("SELECT b AS other, weight FROM edges WHERE a = ? UNION ALL "
                                 "SELECT a AS other, weight FROM edges WHERE b = ?", (entity_id, entity_id))
        out = {r["other"]: r["weight"] for r in rows}
        me = f"entity:{entity_id}"
        try:
            links = self.conn.execute("SELECT source, target, kind, action FROM graph_links WHERE (source = ? OR target = ?) "
                                      "AND trashed_at IS NULL AND kind IN ('related', 'link')", (me, me)).fetchall()
        except sqlite3.OperationalError:  # no second brain tables (tests of older parts)
            return out
        top = max(out.values(), default=1)
        for r in links:
            other = r["target"] if r["source"] == me else r["source"]
            if not other.startswith("entity:"):
                continue
            oid = int(other.split(":", 1)[1])
            if r["action"] == "hide" and r["kind"] == "related":
                out.pop(oid, None)
            elif r["action"] == "add" and r["kind"] == "link":
                out[oid] = max(out.get(oid, 0), top)
        return out

    # episodes -------------------------------------------------------------------------------
    def add_episode(self, ep, embedding=None, facts_text=""):
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO episodes(start_msg, end_msg, start_ts, end_ts, lang, title, narrative, mood, arousal, "
            "self_rel, future_rel, entity_ids, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ep["start_msg"], ep["end_msg"], ep["start_ts"], ep["end_ts"], ep.get("lang"), ep["title"],
             ep["narrative"], ep.get("mood"), ep.get("arousal", 0), ep.get("self_rel", 0), ep.get("future_rel", 0),
             json.dumps(ep.get("entity_ids", [])), now))
        episode_id = cur.lastrowid
        self.conn.execute("UPDATE messages SET episode_id = ? WHERE id BETWEEN ? AND ?",
                          (episode_id, ep["start_msg"], ep["end_msg"]))
        for e in set(ep.get("entity_ids", [])):
            self.conn.execute("UPDATE entities SET episodes = episodes + 1, updated = ? WHERE id = ?", (now, e))
        self.link(ep.get("entity_ids", []))
        # fact-augmented key: the narrative plus the facts drawn from it (LongMemEval)
        self._index("episode", episode_id, f"{ep['title']}. {ep['narrative']} {facts_text}".strip(), embedding)
        return episode_id

    def get_episode(self, episode_id):
        row = self.conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return _row(row) if row else None

    def episodes_between(self, start_ts, end_ts):
        return [_row(r) for r in self.conn.execute(
            "SELECT * FROM episodes WHERE start_ts >= ? AND start_ts < ? AND trashed_at IS NULL ORDER BY start_ts",
            (start_ts, end_ts))]

    def last_episode(self):
        row = self.conn.execute("SELECT * FROM episodes ORDER BY end_ts DESC LIMIT 1").fetchone()
        return _row(row) if row else None

    def last_consolidated_msg(self):
        return self.conn.execute("SELECT COALESCE(MAX(end_msg), 0) FROM episodes").fetchone()[0]

    # facts ----------------------------------------------------------------------------------
    def add_fact(self, fact, embedding=None):
        now = time.time()
        status = fact.get("status") or ("tentative" if fact.get("confidence", 0.8) < 0.6 else "active")
        cur = self.conn.execute(
            "INSERT INTO facts(entity_id, text, category, importance, arousal, confidence, sensitive, valid_from, "
            "valid_to, recorded_at, status, origin, evidence, episode_id, n_uses, first_ts, recent_ts, time_scope, "
            "review_after, polarity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (fact.get("entity_id"), fact["text"], fact.get("category", "identity"), fact.get("importance", 0.5),
             fact.get("arousal", 0), fact.get("confidence", 0.8), int(bool(fact.get("sensitive"))),
             fact.get("valid_from"), fact.get("valid_to"), now, status, fact.get("origin", "extracted"),
             json.dumps(fact.get("evidence", [])), fact.get("episode_id"), now, json.dumps([now]),
             fact.get("time_scope") or "permanent", fact.get("review_after"), _polarity(fact.get("polarity"))))
        self._index("fact", cur.lastrowid, fact["text"], embedding)
        return cur.lastrowid

    def get_fact(self, fact_id):
        row = self.conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return _row(row) if row else None

    def facts(self, entity_id=None, statuses=("active",)):
        q, args = f"SELECT * FROM facts WHERE status IN ({','.join('?' * len(statuses))})", list(statuses)
        if entity_id is not None:
            q, args = q + " AND entity_id = ?", args + [entity_id]
        return [_row(r) for r in self.conn.execute(q + " ORDER BY id", args)]

    def update_fact(self, fact_id, embedding=None, **fields):
        if "evidence" in fields:
            fields["evidence"] = json.dumps(fields["evidence"])
        fields.setdefault("updated", time.time())
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE facts SET {cols} WHERE id = ?", (*fields.values(), fact_id))
        if "text" in fields:
            self._index("fact", fact_id, fields["text"], embedding)

    def supersede(self, old_id, new_fact, embedding=None, valid_to=None):
        """A fact changed: the old one stays as history ("used to…"), the new one takes over."""
        new_id = self.add_fact(new_fact, embedding)
        self.conn.execute("UPDATE facts SET status = 'superseded', superseded_by = ?, valid_to = COALESCE(?, valid_to, "
                          "?) WHERE id = ?", (new_id, valid_to, new_fact.get("valid_from"), old_id))
        return new_id

    def use(self, kind, ref_id, now=None):
        """Strengthen a memory that was mentioned again or used in a reply (ACT-R: one more use)."""
        if kind != "fact":
            return
        now = now or time.time()
        row = self.conn.execute("SELECT recent_ts FROM facts WHERE id = ?", (ref_id,)).fetchone()
        if row:
            recent = (json.loads(row["recent_ts"]) + [now])[-3:]
            self.conn.execute("UPDATE facts SET n_uses = n_uses + 1, recent_ts = ? WHERE id = ?",
                              (json.dumps(recent), ref_id))

    def activation(self, fact, now=None):
        d = 0.5 - 0.2 * fact.get("arousal", 0)  # emotional memories fade more slowly
        return base_level(fact["n_uses"], fact["first_ts"], fact["recent_ts"], now or time.time(), d)

    # intentions -------------------------------------------------------------------------------
    def add_intention(self, it):
        cur = self.conn.execute(
            "INSERT INTO intentions(text, entity_id, due_from, due_to, cue_terms, created, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (it["text"], it.get("entity_id"), it.get("due_from"), it.get("due_to"),
             json.dumps(it.get("cue_terms", []), ensure_ascii=False), time.time(), json.dumps(it.get("evidence", []))))
        return cur.lastrowid

    def intentions(self, statuses=("open",)):
        return [_row(r) for r in self.conn.execute(
            f"SELECT * FROM intentions WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY due_from",
            list(statuses))]

    def set_intention(self, intention_id, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE intentions SET {cols} WHERE id = ?", (*fields.values(), intention_id))

    # rounds -------------------------------------------------------------------------------------
    def add_round(self, msg_from, msg_to, ts, text, embedding=None):
        cur = self.conn.execute("INSERT INTO rounds(msg_from, msg_to, ts, text) VALUES (?, ?, ?, ?)",
                                (msg_from, msg_to, ts, text))
        self._index("round", cur.lastrowid, text, embedding)
        return cur.lastrowid

    def archive_round(self, round_id):
        """Out of the search indexes, kept in the table (verbatim fades, gist stays)."""
        self.conn.execute("UPDATE rounds SET archived = 1 WHERE id = ?", (round_id,))
        self._unindex("round", round_id)

    # forgetting -----------------------------------------------------------------------------------
    def forget(self, fact_ids=(), episode_ids=(), round_ids=(), texts=(), embeddings=()):
        """The user asked her to forget: delete for real, and keep only a tombstone (a digest and a
        vector, never the text) so the same thing isn't learned again."""
        for fid in fact_ids:
            self._unindex("fact", fid)
            self.conn.execute("DELETE FROM facts WHERE id = ?", (fid,))
        for eid in episode_ids:
            self._unindex("episode", eid)
            self.conn.execute("DELETE FROM episodes WHERE id = ?", (eid,))
            self.conn.execute("UPDATE messages SET episode_id = NULL WHERE episode_id = ?", (eid,))
        for rid in round_ids:
            self._unindex("round", rid)
            self.conn.execute("DELETE FROM rounds WHERE id = ?", (rid,))
        for text, emb in zip(texts, embeddings or [None] * len(texts)):
            cur = self.conn.execute("INSERT OR IGNORE INTO tombstones(digest, created) VALUES (?, ?)",
                                    (digest(text), time.time()))
            if emb is not None and cur.rowcount == 1:
                self.conn.execute("INSERT INTO tombstones_vec(rowid, embedding) VALUES (?, ?)",
                                  (cur.lastrowid, sqlite_vec.serialize_float32(emb)))

    def is_forgotten(self, text, embedding=None, threshold=0.85):
        if self.conn.execute("SELECT 1 FROM tombstones WHERE digest = ?", (digest(text),)).fetchone():
            return True
        if embedding is None:
            return False
        row = self.conn.execute("SELECT distance FROM tombstones_vec WHERE embedding MATCH ? AND k = 1",
                                (sqlite_vec.serialize_float32(embedding),)).fetchone()
        return bool(row) and 1 - row["distance"] ** 2 / 2 >= threshold

    # backup ------------------------------------------------------------------------------------------
    def backup(self, dest_dir, keep=14):
        """Online copy of the whole database (messages too) into the vault, rotated."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / time.strftime("laika-%Y-%m-%d.db")
        tmp = target.with_suffix(".tmp")
        out = sqlite3.connect(tmp)
        try:
            self.conn.backup(out)
        finally:
            out.close()
        tmp.replace(target)
        for old in sorted(dest_dir.glob("laika-*.db"))[:-keep]:
            old.unlink()
        return target
