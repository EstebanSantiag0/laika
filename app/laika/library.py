"""Laika's library (PLAN Phase 4, DECISIONS D18): books, files and long pasted texts she can draw on.

Storage is `data/library.db`, attached to the main connection as `lib`. It can be rebuilt from the
originals in `data/library/originals/`, so it stays out of the nightly backup.
Ingestion (P4.1):
1. A file arrives (PWA 📎, Telegram document, `vault/Library/_inbox/`, or a pasted text over ~3,000
   tokens). It is deduplicated by SHA-256 and its original is kept.
2. extract.py turns it into sections. Each sentence is tokenized once (words.py) in a worker thread:
   the lemmas feed the keyword index, and per-section word counts (`doc_words`) feed P4.4's difficulty.
3. The sections are cut into ~600-token chunks of whole sentences with ~80 tokens of overlap, which go
   into the keyword index right away.
4. The background job embeds a few chunks at a time while the user is away (jobs.py), so a query
   embedding for recall never waits long behind a book. When the last chunk is embedded, the book
   is `ready`: a vault note `Library/<Title>.md` and a 📚 message.
"""
import asyncio
import hashlib
import json
import logging
import re
import time
from collections import Counter

import sqlite_vec

from . import config, extract, words
from .consolidate import note_name
from .extract import estimate_tokens
from .memory import DIM

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lib.documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- ids are never reused (/reading <n>, P4.2)
    sha256 TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    lang TEXT,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    added_ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    tokens INTEGER NOT NULL DEFAULT 0,
    note_path TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS lib.sections (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    title TEXT,
    summary TEXT,
    summary_ts REAL
);
CREATE INDEX IF NOT EXISTS lib.sections_doc ON sections(doc_id, ord);
CREATE TABLE IF NOT EXISTS lib.chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL,
    section_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    text TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    embedded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS lib.chunks_doc ON chunks(doc_id, ord);
CREATE INDEX IF NOT EXISTS lib.chunks_todo ON chunks(embedded, doc_id, ord);
CREATE TABLE IF NOT EXISTS lib.doc_words (
    doc_id INTEGER NOT NULL,
    section_id INTEGER NOT NULL,
    lang TEXT NOT NULL,
    lemma TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (section_id, lang, lemma)
);
CREATE INDEX IF NOT EXISTS lib.doc_words_doc ON doc_words(doc_id, lang, lemma);
CREATE VIRTUAL TABLE IF NOT EXISTS lib.chunks_fts USING fts5(search_text, tokenize='unicode61');
"""
VECTORS = f"CREATE VIRTUAL TABLE IF NOT EXISTS lib.chunks_vec USING vec0(embedding float[{DIM}]);"
_SENTENCE = re.compile(r"[^.!?…。！？\n]*(?:[.!?…。！？]+[\"'»”’)）」』]*|\n|$)")
INBOX = "Library/_inbox"
INBOX_FAILED = "Library/_inbox/_failed"
INBOX_SETTLE_S = 10
INBOX_MAX = 300 * 2**20
SKIP_SUFFIXES = (".part", ".tmp", ".crdownload", "~", ".sync-conflict")
_SCRIPT_LANG = {"zh": "zh", "ru": "ru", "num": None}


def sentences(text):
    """Sentences and lines of a section, whitespace trimmed, empty ones dropped."""
    return [s.strip() for s in _SENTENCE.findall(text) if s.strip()]


def split_long(sentence, limit):
    """A sentence over `limit` tokens is cut into pieces at spaces (or anywhere, for CJK)."""
    if estimate_tokens(sentence) <= limit:
        return [sentence]
    ratio = len(sentence) / estimate_tokens(sentence)
    size, out = max(1, int(limit * ratio)), []
    while sentence:
        piece = sentence[:size]
        if len(sentence) > size and (cut := piece.rfind(" ")) > size // 2:
            piece = piece[:cut]
        out.append(piece.strip())
        sentence = sentence[len(piece):].strip()
    return [p for p in out if p]


def chunk(units, size, overlap):
    """Pack sentence units [(text, tokens, …)] into chunks of ≤ `size` tokens; each chunk after the first
    starts with the previous chunk's last sentences, ≤ `overlap` tokens of them. Returns lists of units."""
    chunks, cur, total = [], [], 0
    for u in units:
        if cur and total + u[1] > size:
            chunks.append(cur)
            keep, kept = [], 0
            for prev in reversed(cur):
                if kept + prev[1] > overlap:
                    break
                keep.insert(0, prev)
                kept += prev[1]
            while keep and kept + u[1] > size:
                kept -= keep.pop(0)[1]
            cur, total = keep, kept
        cur.append(u)
        total += u[1]
    if cur:
        chunks.append(cur)
    return chunks


def word_lang(pos, doc_lang):
    """Language of a token from words.tokens(): its script, or the document's language for Latin words."""
    if pos in ("ru", "en", "es"):
        return pos if pos != "en" or doc_lang != "es" else "es"
    return _SCRIPT_LANG.get(pos, "zh")


def prepare(doc, size, overlap):
    """Everything heavy, done in a worker thread: chunks with their search text, and word counts.
    Returns [(section title, [(chunk text, tokens, search text)], Counter{(lang, lemma): n})]."""
    code = doc.lang or "en"
    out = []
    for title, text in doc.sections:
        units, counts = [], Counter()

        def count(toks):
            for _, lemma, kind, pos in toks:
                if kind == words.WORD and (lg := word_lang(pos, code)):
                    counts[(lg, lemma.lower())] += 1

        count(words.tokens(title or "", code))  # he reads the chapter title too
        for sentence in sentences(text):
            for piece in split_long(sentence, size):
                toks = words.tokens(piece, code)
                units.append((piece, estimate_tokens(piece), " ".join(t[1].lower() for t in toks)))
                count(toks)
        chunks = []
        for group in chunk(units, size, overlap):
            body = _join([u[0] for u in group], code)
            chunks.append((body, sum(u[1] for u in group), " ".join(u[2] for u in group)))
        out.append((title, chunks, counts))
    return out


def _join(parts, code):
    return ("" if code == "zh" else " ").join(parts)


def chapter_label(doc, section_ord, section_title):
    """'Title — Author — ch. 3 "Heading"': put in front of each chunk's embedded and indexed text."""
    head = [doc["title"]] + ([doc["author"]] if doc["author"] else [])
    ch = f"ch. {section_ord + 1}" + (f' "{section_title}"' if section_title else "")
    return " — ".join(head + [ch])


class Library:
    def __init__(self, cfg, db, embedder, vault, engine):
        self.cfg, self.db, self.embedder, self.vault, self.engine = cfg, db, embedder, vault, engine
        self.lcfg = cfg.get("library", {})
        self.conn = db.conn
        self.dir = config.DATA_DIR / "library"
        self.originals = self.dir / "originals"
        self.originals.mkdir(parents=True, exist_ok=True)
        path = config.DATA_DIR / "library.db"
        if not any(r["name"] == "lib" for r in self.conn.execute("PRAGMA database_list")):
            self.conn.execute("ATTACH DATABASE ? AS lib", (str(path),))
        self.conn.execute("PRAGMA lib.journal_mode=WAL")
        self.conn.executescript(SCHEMA + VECTORS)
        cols = {r["name"] for r in self.conn.execute("PRAGMA lib.table_info(documents)")}
        if "summary" not in cols:  # P4.3: the book's summary (chapter summaries are in `sections`)
            self.conn.execute("ALTER TABLE lib.documents ADD COLUMN summary TEXT")
            self.conn.execute("ALTER TABLE lib.documents ADD COLUMN summary_ts REAL")
        self.lock = asyncio.Lock()  # one extraction at a time (they're CPU-heavy)
        self.tasks = set()

    # documents ---------------------------------------------------------------------------------
    def document(self, doc_id):
        row = self.conn.execute("SELECT * FROM lib.documents WHERE id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def documents(self, statuses=None):
        rows = self.conn.execute("SELECT * FROM lib.documents ORDER BY id").fetchall()
        return [dict(r) for r in rows if not statuses or r["status"] in statuses]

    def sections(self, doc_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM lib.sections WHERE doc_id = ? ORDER BY ord",
                                                   (doc_id,))]

    def chunks(self, doc_id):
        return [dict(r) for r in self.conn.execute("SELECT * FROM lib.chunks WHERE doc_id = ? ORDER BY ord",
                                                   (doc_id,))]

    def chunk_row(self, chunk_id):
        """A chunk with its section's ord and title (None for the book's summary, P4.3)."""
        row = self.conn.execute("SELECT c.*, s.ord AS s_ord, s.title AS s_title FROM lib.chunks c "
                                "LEFT JOIN lib.sections s ON s.id = c.section_id WHERE c.id = ?", (chunk_id,)).fetchone()
        return dict(row) if row else None

    def section_text(self, section_id):
        """A section's text rebuilt from its chunks, without the overlap repeated at each chunk's start."""
        out = []
        for (text,) in self.conn.execute("SELECT text FROM lib.chunks WHERE section_id = ? AND kind = 'text' "
                                         "ORDER BY ord", (section_id,)):
            sents = sentences(text)
            if out:
                k = min(len(sents), len(out))
                while k and out[-k:] != sents[:k]:
                    k -= 1
                sents = sents[k:]
            out += sents
        return "\n".join(out)

    def progress(self, doc_id):
        """Share of a document's chunks already embedded (1.0 when ready)."""
        n, done = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(embedded), 0) FROM lib.chunks WHERE doc_id = ?",
                                    (doc_id,)).fetchone()
        return done / n if n else 0.0

    # search (P4.2) -------------------------------------------------------------------------------------
    def searchable(self):
        """Documents she can draw on: ready ones, and ones still embedding (their keyword index is complete)."""
        return self.documents(("ready", "indexing"))

    def similarities(self, query, doc_ids, section_ids=None):
        """{chunk id: cosine} for every embedded chunk (text and summaries) of these documents (or sections). Computed in SQLite
        (a few thousand chunks take milliseconds), so the caller can also see how much a match stands out."""
        if not doc_ids:
            return {}
        where, args = _in("c.doc_id", doc_ids)
        if section_ids:
            w2, a2 = _in("c.section_id", section_ids)
            where, args = f"{where} AND {w2}", args + a2
        rows = self.conn.execute(
            f"SELECT c.id, 1 - vec_distance_cosine(v.embedding, ?) FROM lib.chunks c "
            f"JOIN lib.chunks_vec v ON v.rowid = c.id WHERE c.embedded = 1 AND {where}",
            (sqlite_vec.serialize_float32(query), *args))
        return dict(rows.fetchall())

    def search_text(self, terms, doc_ids, k=20, section_ids=None):
        """[(chunk id, bm25)] best first (bm25 is negative; lower is better) for lemmas from words.query_terms()."""
        if not terms or not doc_ids:
            return []
        where, args = _in("c.doc_id", doc_ids)
        if section_ids:
            w2, a2 = _in("c.section_id", section_ids)
            where, args = f"{where} AND {w2}", args + a2
        query = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
        rows = self.conn.execute(
            f"SELECT chunks_fts.rowid, bm25(chunks_fts) AS score FROM lib.chunks_fts "
            f"JOIN lib.chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ? AND {where} "
            f"ORDER BY score LIMIT ?", (query, *args, k))
        return [tuple(r) for r in rows]

    # intake --------------------------------------------------------------------------------------
    async def add(self, data, filename, source, channel, quiet=False):
        """Take a file in. Returns (document or None, reply): None with a reply when it's refused or a
        duplicate. Extraction and indexing continue in the background; their messages go to `channel`."""
        if extract.kind_of(filename) is None:
            reply = "📚 I can't read that kind of file yet (EPUB, PDF, FB2, DOCX, TXT, MD and HTML work)"
            await self.say(channel, reply)
            return None, reply
        sha = hashlib.sha256(data).hexdigest()
        old = self.conn.execute("SELECT * FROM lib.documents WHERE sha256 = ?", (sha,)).fetchone()
        if old and not old["status"].startswith("failed"):
            reply = f"📚 «{old['title']}» is already in the library"
            await self.say(channel, reply)
            return dict(old), reply
        if old:
            self._delete(old["id"])
        (self.originals / f"{sha}.{extract.kind_of(filename)}").write_bytes(data)
        title = extract._clean_title(re.sub(r"\.[A-Za-z0-9]{1,5}$", "", filename)) or "Untitled"
        cur = self.conn.execute(
            "INSERT INTO lib.documents(sha256, title, kind, source, added_ts, meta) VALUES (?, ?, ?, ?, ?, ?)",
            (sha, title, extract.kind_of(filename), source, time.time(),
             _json({"filename": filename, "kind": extract.kind_of(filename), "channel": channel, "quiet": quiet})))
        doc_id = cur.lastrowid
        if not quiet:  # P4.2: the book he just sent is the one he's reading (/reading changes it; pastes don't)
            self.db.set_setting("current_book", {"id": doc_id, "chapter": None})
        self._spawn(self.ingest(doc_id))
        return self.document(doc_id), None

    async def add_text(self, text, channel):
        """A long pasted text: saved as a .txt document. Returns its title."""
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "Pasted text")
        title = first[:60].rstrip(" .,:;") + ("…" if len(first) > 60 else "")
        stamp = time.strftime("%Y-%m-%d %H-%M")
        doc, _ = await self.add(text.encode("utf-8"), f"{note_name(title)} ({stamp}).txt", "paste", channel,
                                quiet=True)
        return doc["title"] if doc else title

    async def shorten_paste(self, channel, text):
        """Engine hook for long messages: the stored message keeps the start and a 📄 line, the whole text
        goes into the library. Returns the text to keep (unchanged when it's short)."""
        if estimate_tokens(text) < self.lcfg.get("paste_tokens", 3000):
            return text
        n_words = len(text.split()) + extract.lang.counts(text)[0] // 2
        title = await self.add_text(text, channel)
        keep = text[:self.lcfg.get("paste_keep_chars", 1500)].rstrip()
        return f"{keep}…\n\n📄 (pasted text, about {n_words} words: saved in the library as «{title}»)"

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def resume(self):
        """At startup: extraction that a restart interrupted starts again from the original."""
        for doc in self.documents(("queued",)):
            self._spawn(self.ingest(doc["id"]))

    def original(self, doc):
        return self.originals / f"{doc['sha256']}.{_loads(doc['meta']).get('kind', doc['kind'])}"

    async def ingest(self, doc_id):
        async with self.lock:
            doc = self.document(doc_id)
            if not doc or doc["status"] != "queued":
                return
            meta = _loads(doc["meta"])
            channel = meta.get("channel", "web")
            try:
                data = self.original(doc).read_bytes()
                parsed = await asyncio.to_thread(extract.extract, data, meta.get("filename", doc["title"]))
                prepared = await asyncio.to_thread(prepare, parsed, self.lcfg.get("chunk_tokens", 600),
                                                   self.lcfg.get("overlap", 80))
            except extract.Unreadable as e:
                self._fail(doc_id, str(e))
                await self.say(channel, f"📚 I couldn't add «{doc['title']}»: {e}")
                return
            except Exception as e:
                log.exception("library: extraction of document %s failed", doc_id)
                self._fail(doc_id, f"{type(e).__name__}: {e}")
                await self.say(channel, f"📚 I couldn't read «{doc['title']}» (something went wrong on my side)")
                return
            if not self.document(doc_id):  # deleted meanwhile
                return
            self._store(doc_id, parsed, prepared, meta)
        n = len(prepared)
        log.info("library: document %s «%s» extracted: %s sections, %s chunks", doc_id, parsed.title, n,
                 sum(len(p[1]) for p in prepared))
        if not meta.get("quiet"):
            await self.say(channel, f"📚 Reading «{parsed.title}»… ({_chapters(n)})")

    def _store(self, doc_id, parsed, prepared, meta):
        """Sections, chunks (keyword-indexed now, embedded later) and word counts, in one transaction."""
        c = self.conn
        doc = {"title": parsed.title, "author": parsed.author}
        total, n_chunks = 0, 0
        c.execute("BEGIN")
        try:
            for ord_, (title, chunks, counts) in enumerate(prepared):
                sid = c.execute("INSERT INTO lib.sections(doc_id, ord, title) VALUES (?, ?, ?)",
                                (doc_id, ord_, title)).lastrowid
                label = chapter_label(doc, ord_, title)
                label_search = words.search_text(label, parsed.lang or "en")
                for k, (body, toks, search) in enumerate(chunks):
                    cid = c.execute("INSERT INTO lib.chunks(doc_id, section_id, ord, text, tokens) VALUES (?, ?, ?, ?, ?)",
                                    (doc_id, sid, n_chunks, body, toks)).lastrowid
                    n_chunks += 1
                    c.execute("INSERT INTO lib.chunks_fts(rowid, search_text) VALUES (?, ?)",
                              (cid, f"{label_search} {search}"))
                    total += toks
                c.executemany("INSERT INTO lib.doc_words(doc_id, section_id, lang, lemma, count) VALUES (?, ?, ?, ?, ?)",
                              [(doc_id, sid, lg, lemma, n) for (lg, lemma), n in counts.items()])
            meta = {**meta, **parsed.meta}
            c.execute("UPDATE lib.documents SET title = ?, author = ?, lang = ?, kind = ?, status = 'indexing', "
                      "tokens = ?, meta = ? WHERE id = ?",
                      (parsed.title, parsed.author, parsed.lang, parsed.kind, total, _json(meta), doc_id))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def _fail(self, doc_id, reason):
        self.conn.execute("UPDATE lib.documents SET status = ? WHERE id = ?", (f"failed: {reason}"[:300], doc_id))

    def _delete(self, doc_id):
        c = self.conn
        ids = [r[0] for r in c.execute("SELECT id FROM lib.chunks WHERE doc_id = ?", (doc_id,))]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            c.execute(f"DELETE FROM lib.chunks_fts WHERE rowid IN ({marks})", part)
            c.execute(f"DELETE FROM lib.chunks_vec WHERE rowid IN ({marks})", part)
        for table in ("chunks", "sections", "doc_words"):
            c.execute(f"DELETE FROM lib.{table} WHERE doc_id = ?", (doc_id,))
        c.execute("DELETE FROM lib.documents WHERE id = ?", (doc_id,))

    # background embedding (jobs.py) ----------------------------------------------------------------
    def pending(self):
        return self.conn.execute("SELECT 1 FROM lib.documents WHERE status = 'indexing' LIMIT 1").fetchone() is not None

    async def run(self):
        """Embed waiting chunks a few at a time; finish each document whose chunks are all embedded.
        Cancelling between or during batches loses nothing: a batch is written only after its embedding."""
        batch = self.lcfg.get("embed_batch", 4)
        while True:
            rows = self.conn.execute(
                "SELECT c.id, c.doc_id, c.section_id, c.text, s.ord AS s_ord, s.title AS s_title FROM lib.chunks c "
                "JOIN lib.sections s ON s.id = c.section_id JOIN lib.documents d ON d.id = c.doc_id "
                "WHERE c.embedded = 0 AND d.status = 'indexing' ORDER BY c.doc_id, c.ord LIMIT ?", (batch,)).fetchall()
            if not rows:
                for doc in self.documents(("indexing",)):
                    await self.finish(doc["id"])
                return
            docs = {r["doc_id"]: self.document(r["doc_id"]) for r in rows}
            texts = [chapter_label(docs[r["doc_id"]], r["s_ord"], r["s_title"]) + "\n" + r["text"] for r in rows]
            vecs = await self.embedder.embed(texts)
            for r, v in zip(rows, vecs):
                self.conn.execute("DELETE FROM lib.chunks_vec WHERE rowid = ?", (r["id"],))
                self.conn.execute("INSERT INTO lib.chunks_vec(rowid, embedding) VALUES (?, ?)",
                                  (r["id"], sqlite_vec.serialize_float32(v)))
                self.conn.execute("UPDATE lib.chunks SET embedded = 1 WHERE id = ?", (r["id"],))
            for doc_id in docs:
                left = self.conn.execute("SELECT 1 FROM lib.chunks WHERE doc_id = ? AND embedded = 0 LIMIT 1",
                                         (doc_id,)).fetchone()
                if not left:
                    await self.finish(doc_id)

    async def finish(self, doc_id):
        doc = self.document(doc_id)
        if not doc or doc["status"] != "indexing":
            return
        rel = self.write_note(doc)
        self.conn.execute("UPDATE lib.documents SET status = 'ready', note_path = ? WHERE id = ?", (rel, doc_id))
        n = len(self.sections(doc_id))
        log.info("library: document %s «%s» ready", doc_id, doc["title"])
        await self.say(_loads(doc["meta"]).get("channel", "web"), f"📚 «{doc['title']}» is in the library ({_chapters(n)})")

    def write_note(self, doc):
        """Vault note `Library/<Title>.md`: what the book is, its summary and its chapters with theirs (P4.3)."""
        rel = doc["note_path"] or self._note_rel(doc)
        meta = _loads(doc["meta"])
        lines = [f"- Author: {doc['author']}" if doc["author"] else None,
                 f"- Language: {extract.lang.NAMES.get(doc['lang'], doc['lang'] or 'unknown')}"
                 + (" (traditional characters, read as simplified)" if meta.get("traditional") else ""),
                 f"- Format: {doc['kind'].upper()}, about {doc['tokens']:,} tokens",
                 f"- Added: {time.strftime('%Y-%m-%d', time.localtime(doc['added_ts']))} from {doc['source']}"
                 + (f" ({meta['filename']})" if meta.get("filename") and doc["source"] != "paste" else ""),
                 *self.note_extra(doc)]
        if doc.get("summary"):
            lines += ["", "## Summary", doc["summary"]]
        lines += ["", "## Chapters"]
        for s in self.sections(doc["id"]):
            lines.append(f"{s['ord'] + 1}. {s['title'] or '(untitled)'}")
            if s["summary"]:
                lines.append(f"    {s['summary']}")
        self.vault.write(rel, "\n".join(ln for ln in lines if ln is not None), title=doc["title"], kind="book")
        return rel

    def note_extra(self, doc):
        """More lines for the note's header (difficulty, P4.4)."""
        return []

    def _note_rel(self, doc):
        base = note_name(doc["title"])[:80] or "Untitled"
        taken = {r[0] for r in self.conn.execute("SELECT note_path FROM lib.documents WHERE note_path IS NOT NULL")}
        rel, n = f"Library/{base}.md", 2
        while rel in taken or (self.vault.path(rel).exists() and self.vault.section(rel) is None):
            rel, n = f"Library/{base} ({n}).md", n + 1
        return rel

    # vault inbox ---------------------------------------------------------------------------------------
    async def scan_inbox(self):
        """Files he dropped into `Library/_inbox/` (synced from his PC or phone): settled ones are taken in.
        The original moves into the library; files that can't be read go to `_inbox/_failed/`."""
        inbox = self.vault.path(INBOX)
        inbox.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for path in sorted(inbox.iterdir()):
            name = path.name
            if not path.is_file() or name.startswith(".") or name.lower().endswith(SKIP_SUFFIXES):
                continue
            stat = path.stat()
            if now - stat.st_mtime < INBOX_SETTLE_S:
                continue
            channel = self.last_channel()
            if stat.st_size > INBOX_MAX or extract.kind_of(name) is None:
                why = "it's over 300 MB" if stat.st_size > INBOX_MAX else "I can't read that kind of file yet"
                self._park(path)
                await self.say(channel, f"📚 I couldn't add {name}: {why} (moved to {INBOX_FAILED})")
                continue
            doc, _ = await self.add(await asyncio.to_thread(path.read_bytes), name, "inbox", channel)
            if doc:
                path.unlink(missing_ok=True)
            else:
                self._park(path)

    def _park(self, path):
        failed = self.vault.path(INBOX_FAILED)
        failed.mkdir(parents=True, exist_ok=True)
        path.replace(failed / path.name)

    def last_channel(self):
        row = self.conn.execute("SELECT channel FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT 1").fetchone()
        return row["channel"] if row else "web"

    # messages ------------------------------------------------------------------------------------------
    async def say(self, channel, text):
        msg = self.db.add_message("system", channel, text)
        await self.engine.publish({"type": "message", "message": msg, "origin": channel})


def _in(column, values):
    values = list(values)
    return f"{column} IN ({','.join('?' * len(values))})", values


def _chapters(n):
    return f"{n} chapter{'s' if n != 1 else ''}"


def _json(d):
    return json.dumps(d, ensure_ascii=False)


def _loads(s):
    return json.loads(s or "{}")
