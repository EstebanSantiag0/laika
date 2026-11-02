"""Chapter and book summaries (PLAN P4.3, DECISIONS D18: RAPTOR-lite), written while the user is away.

For every chapter of a ready book, the background slot writes a ~120-word summary in English, like
the vault notes. A chapter that doesn't fit in one call (~5,000 tokens) is summarized in pieces that
are then merged. When all chapters are done, the book gets a ~200-word summary built from them.
Summaries are indexed as chunks of kind `summary` (embedded and keyword-searchable), so "what is
this book about?" finds the book layer and "what happens in chapter 3?" the chapter layer, and they go
into the book's vault note. Each chapter is saved as soon as it's done, so a cancelled run (he came
back) resumes from the next one; the book he's reading goes first.
"""
import logging
import re
import time

import httpx
import sqlite_vec

from . import lang, words
from .config import personal
from .extract import estimate_tokens
from .library import FAIL_AFTER, chapter_label, sentences
from .retry import is_transient

log = logging.getLogger(__name__)

PIECE_TOKENS = 5000  # per call: the background slot has ~10k tokens of context
MAX_WORDS = 300      # longer than this, the model rambled (targets are 80-200 words)
MIN_WORDS = 40       # a summary of a long text (LONG_SOURCE words or more) shorter than this is broken
LONG_SOURCE = 1000
_ECHO = re.compile(r"keep notes on the books|^summari[sz]e\b|^(here is|here's) (a|the) summary", re.I)
CHAPTER = """You keep notes on the books in {user}'s library, so you can talk with him about them later. \
Summarize {what} of «{title}»{by} in English, in about {words} words: what happens or what it argues, who \
is in it, and anything striking. Keep the names as the text spells them. Plain prose, no lists or \
markdown, and nothing that isn't in the text."""
MERGE = """You keep notes on the books in {user}'s library. Below are notes on consecutive parts of {what} \
of «{title}»{by}. Merge them into one summary in English of about {words} words, in order. Plain prose, \
no lists or markdown, and nothing that isn't in the notes."""
BOOK = """You keep notes on the books in {user}'s library. From the chapter notes below, write a summary \
of the whole of «{title}»{by} in English, in about 200 words: what kind of book it is, what happens or \
what it argues, the main people, and how it ends if the notes say. Plain prose, no lists or markdown, \
and nothing that isn't in the notes."""


class Summaries:
    def __init__(self, cfg, db, llm, library, embedder):
        self.cfg, self.db, self.llm, self.lib, self.embedder = cfg, db, llm, library, embedder
        self.slot = cfg["llm"]["background_slot"]

    def todo(self):
        """Ready documents with chapters or a book summary still to write; the one he's reading first."""
        current = (self.db.get_setting("current_book") or {}).get("id")
        out = []
        for doc in self.lib.documents(("ready",)):
            missing = self.lib.conn.execute(
                "SELECT 1 FROM lib.sections s WHERE s.doc_id = ? AND s.summary IS NULL AND EXISTS "
                "(SELECT 1 FROM lib.chunks c WHERE c.section_id = s.id AND c.kind = 'text') LIMIT 1",
                (doc["id"],)).fetchone()
            if missing or doc.get("summary") is None:
                out.append(doc)
        return sorted(out, key=lambda d: (d["id"] != current, d["id"]))

    def pending(self):
        return bool(self.todo())

    async def run(self):
        for doc in self.todo():
            t0 = time.time()
            by = f" by {doc['author']}" if doc["author"] else ""
            secs = self.lib.sections(doc["id"])
            for s in secs:
                if s["summary"] is not None:
                    continue
                text = self.lib.section_text(s["id"])
                if not text.strip():
                    continue
                what = f'chapter {s["ord"] + 1}' + (f' ("{s["title"]}")' if s["title"] else "")
                try:
                    summary = await self.checked(lambda t=0.3: self.summarize(text, what, doc["title"], by,
                                                                              doc["lang"] or "en", t), text)
                except httpx.HTTPError as e:
                    if is_transient(e):
                        raise
                    self.failed(doc, s, e)
                    continue
                await self.store(doc, s, summary, None if summary else "invalid summary twice")
            secs = self.lib.sections(doc["id"])
            if any(x["summary"] is None and self.lib.section_text(x["id"]).strip() for x in secs):
                continue  # a chapter failed and is tried again on the next run; the book summary waits for it
            notes = [f'Chapter {s["ord"] + 1}' + (f' ({s["title"]})' if s["title"] else "") + f": {s['summary']}"
                     for s in secs if s["summary"]]
            source = "\n".join(notes)
            try:
                book = await self.checked(lambda t=0.3: self._merged(notes, personal(BOOK, self.cfg).format(
                    title=doc["title"], by=by), t), source) if notes else ""
            except httpx.HTTPError as e:
                if is_transient(e):
                    raise
                self.failed(doc, None, e)
                continue
            await self.store(doc, None, book, None if book or not notes else "invalid summary twice")
            self.lib.write_note(self.lib.document(doc["id"]))
            log.info("library: summaries for document %s «%s» done in %.0f s", doc["id"], doc["title"], time.time() - t0)

    async def checked(self, make, source):
        """Run a summary call; if the result isn't a usable summary, try once more at a low temperature.
        Returns '' when both are unusable (SB0/P5)."""
        out = await make()
        if valid(out, source):
            return out
        log.warning("library: unusable summary (%d words), retrying at a low temperature", len(out.split()))
        out = await make(0.1)
        return out if valid(out, source) else ""

    def failed(self, doc, section, exc):
        """A summary call failed for this input (not because the server is down): count it, and give up
        after FAIL_AFTER attempts, so the other chapters and books go on."""
        err = f"{type(exc).__name__}: {exc}"[:300]
        table, rid = ("sections", section["id"]) if section else ("documents", doc["id"])
        c = self.lib.conn
        c.execute(f"UPDATE lib.{table} SET summary_attempts = summary_attempts + 1, summary_error = ? WHERE id = ?",
                  (err, rid))
        n = c.execute(f"SELECT summary_attempts FROM lib.{table} WHERE id = ?", (rid,)).fetchone()[0]
        if n >= FAIL_AFTER:
            c.execute(f"UPDATE lib.{table} SET summary = '', summary_ts = ? WHERE id = ?", (time.time(), rid))
        log.warning("library: summary of %s %s failed (%d/%d): %s", table, rid, n, FAIL_AFTER, err)

    async def summarize(self, text, what, title, by, code, temperature=0.3):
        """~120 words for a chapter: one call, or pieces of ≤ PIECE_TOKENS merged into one."""
        pieces = _pieces(text, code)
        if len(pieces) == 1:
            return await self._call(personal(CHAPTER, self.cfg).format(what=f"this chapter ({what})", title=title, by=by, words=120),
                                    pieces[0], temperature)
        notes = [await self._call(personal(CHAPTER, self.cfg).format(what=f"part {i} of {len(pieces)} of {what}", title=title, by=by,
                                                 words=80), p, temperature) for i, p in enumerate(pieces, 1)]
        return await self._merged(notes, personal(MERGE, self.cfg).format(what=what, title=title, by=by, words=120),
                                  temperature)

    async def _merged(self, notes, system, temperature=0.3):
        """Merge notes in one call, or in rounds when they don't fit together."""
        while len(notes) > 2 and sum(estimate_tokens(n) for n in notes) > PIECE_TOKENS:
            groups, cur = [], []
            for n in notes:  # at least two notes per group, so every round shrinks the list
                if len(cur) >= 2 and sum(estimate_tokens(x) for x in cur) + estimate_tokens(n) > PIECE_TOKENS:
                    groups.append(cur)
                    cur = []
                cur.append(n)
            groups.append(cur)
            notes = [await self._call(system, "\n\n".join(g), temperature) for g in groups]
        return await self._call(system, "\n\n".join(notes), temperature)

    async def _call(self, system, text, temperature=0.3):
        out = await self.llm.chat([{"role": "system", "content": system}, {"role": "user", "content": text}],
                                  self.slot, temperature=temperature, max_tokens=400)
        return _plain(out)

    async def store(self, doc, section, summary, error=None):
        """Save a summary (section None: the whole book) and index it as a `summary` chunk. An empty one is
        saved too (so it isn't retried every minute) but not indexed; `error` marks it as failed."""
        c = self.lib.conn
        if not summary:
            if section:
                c.execute("UPDATE lib.sections SET summary = '', summary_ts = ?, summary_error = ? WHERE id = ?",
                          (time.time(), error, section["id"]))
            else:
                c.execute("UPDATE lib.documents SET summary = '', summary_ts = ?, summary_error = ? WHERE id = ?",
                          (time.time(), error, doc["id"]))
            return
        label = chapter_label(doc, section["ord"], section["title"]) if section else \
            " — ".join([doc["title"]] + ([doc["author"]] if doc["author"] else []))
        label += " (summary)"
        vec = (await self.embedder.embed([label + "\n" + summary]))[0]
        sid = section["id"] if section else 0
        for (old,) in c.execute("SELECT id FROM lib.chunks WHERE doc_id = ? AND section_id = ? AND kind = 'summary'",
                                (doc["id"], sid)).fetchall():
            c.execute("DELETE FROM lib.chunks_fts WHERE rowid = ?", (old,))
            c.execute("DELETE FROM lib.chunks_vec WHERE rowid = ?", (old,))
            c.execute("DELETE FROM lib.chunks WHERE id = ?", (old,))
        cid = c.execute("INSERT INTO lib.chunks(doc_id, section_id, ord, kind, text, tokens, embedded) "
                        "VALUES (?, ?, -1, 'summary', ?, ?, 1)",
                        (doc["id"], sid, summary, estimate_tokens(summary))).lastrowid
        c.execute("INSERT INTO lib.chunks_fts(rowid, search_text) VALUES (?, ?)",
                  (cid, words.search_text(label + " " + summary, "en")))
        c.execute("INSERT INTO lib.chunks_vec(rowid, embedding) VALUES (?, ?)", (cid, sqlite_vec.serialize_float32(vec)))
        if section:
            c.execute("UPDATE lib.sections SET summary = ?, summary_ts = ? WHERE id = ?", (summary, time.time(), sid))
        else:
            c.execute("UPDATE lib.documents SET summary = ?, summary_ts = ? WHERE id = ?", (summary, time.time(), doc["id"]))


def _pieces(text, code):
    """Whole sentences packed into pieces of ≤ PIECE_TOKENS tokens."""
    if estimate_tokens(text) <= PIECE_TOKENS:
        return [text]
    out, cur, size = [], [], 0
    for s in sentences(text):
        t = estimate_tokens(s)
        if cur and size + t > PIECE_TOKENS:
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += t
    if cur:
        out.append(cur)
    sep = "" if code == "zh" else " "
    return [sep.join(p) for p in out]


def valid(summary, source):
    """A usable summary: some words (at least MIN_WORDS for a long source), not rambling past MAX_WORDS,
    a finished last sentence (not cut by max_tokens), in English (no Cyrillic or Chinese prose), and not an
    echo of the instructions."""
    summary = (summary or "").strip()
    n = len(summary.split())
    if not n or n > MAX_WORDS:
        return False
    if n < MIN_WORDS and len(source.split()) >= LONG_SOURCE:
        return False
    if not re.search(r"[.!?…][\"'»”’)]*$", summary):
        return False
    if lang.detect(summary) in ("ru", "zh") or _ECHO.search(summary):
        return False
    return True


def _plain(text):
    text = re.sub(r"[*#`_]+", "", text or "")
    return re.sub(r"\s+", " ", text).strip()
