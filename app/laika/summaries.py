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

import sqlite_vec

from . import words
from .config import personal
from .extract import estimate_tokens
from .library import chapter_label, sentences

log = logging.getLogger(__name__)

PIECE_TOKENS = 5000  # per call: the background slot has ~10k tokens of context
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
                summary = await self.summarize(text, what, doc["title"], by, doc["lang"] or "en")
                await self.store(doc, s, summary)
            secs = self.lib.sections(doc["id"])
            notes = [f'Chapter {s["ord"] + 1}' + (f' ({s["title"]})' if s["title"] else "") + f": {s['summary']}"
                     for s in secs if s["summary"]]
            book = await self._merged(notes, personal(BOOK, self.cfg).format(title=doc["title"], by=by)) if notes else ""
            await self.store(doc, None, book)
            self.lib.write_note(self.lib.document(doc["id"]))
            log.info("library: summaries for document %s «%s» done in %.0f s", doc["id"], doc["title"], time.time() - t0)

    async def summarize(self, text, what, title, by, code):
        """~120 words for a chapter: one call, or pieces of ≤ PIECE_TOKENS merged into one."""
        pieces = _pieces(text, code)
        if len(pieces) == 1:
            return await self._call(personal(CHAPTER, self.cfg).format(what=f"this chapter ({what})", title=title, by=by, words=120),
                                    pieces[0])
        notes = [await self._call(personal(CHAPTER, self.cfg).format(what=f"part {i} of {len(pieces)} of {what}", title=title, by=by,
                                                 words=80), p) for i, p in enumerate(pieces, 1)]
        return await self._merged(notes, personal(MERGE, self.cfg).format(what=what, title=title, by=by, words=120))

    async def _merged(self, notes, system):
        """Merge notes in one call, or in rounds when they don't fit together."""
        while len(notes) > 2 and sum(estimate_tokens(n) for n in notes) > PIECE_TOKENS:
            groups, cur = [], []
            for n in notes:  # at least two notes per group, so every round shrinks the list
                if len(cur) >= 2 and sum(estimate_tokens(x) for x in cur) + estimate_tokens(n) > PIECE_TOKENS:
                    groups.append(cur)
                    cur = []
                cur.append(n)
            groups.append(cur)
            notes = [await self._call(system, "\n\n".join(g)) for g in groups]
        return await self._call(system, "\n\n".join(notes))

    async def _call(self, system, text):
        out = await self.llm.chat([{"role": "system", "content": system}, {"role": "user", "content": text}],
                                  self.slot, temperature=0.3, max_tokens=400)
        return _plain(out)

    async def store(self, doc, section, summary):
        """Save a summary (section None: the whole book) and index it as a `summary` chunk. An empty one is
        saved too (so it isn't retried every minute) but not indexed."""
        c = self.lib.conn
        if not summary:
            if section:
                c.execute("UPDATE lib.sections SET summary = '', summary_ts = ? WHERE id = ?", (time.time(), section["id"]))
            else:
                c.execute("UPDATE lib.documents SET summary = '', summary_ts = ? WHERE id = ?", (time.time(), doc["id"]))
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


def _plain(text):
    text = re.sub(r"[*#`_]+", "", text or "")
    return re.sub(r"\s+", " ", text).strip()
