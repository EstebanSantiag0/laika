"""Laika draws on his library (PLAN P4.2, DECISIONS D18): book passages in the turn's note, /books, /reading.

Before she answers, his batch is checked for a reason to look in the books:
- he names a book (its title, a distinctive title word, or the author's surname): search that book;
- he uses a cue ("in the book", "en el libro", "в книге", "书里", "chapter 3"): search the book he's
  reading, or all of them;
- he is reading a book (`current_book`): search it, but only a match that clearly stands out counts.
Candidates come from the vector search (every chunk of the books in scope, so it's known how much a
match stands out there) and the lemma keyword index, merged by reciprocal rank fusion. Only matches
over the similarity bar get in: at most 2 passages, about 350 tokens together, cut from their chunk
around the sentences that share his words, and cited as `From «Title», ch. N "heading": «…»`. Her own
chapter and book summaries (P4.3) are searched too: a chapter question gets that chapter's summary first.
A passage offered once stays in the replayed history, so it isn't offered again in the visible window.
Asking about the library itself ("what books do I have?") adds the list of books to the note.
"""
import json
import logging
import re
import time

from . import lang, words
from .config import personal
from .extract import estimate_tokens
from .library import _join, sentences, split_long
from .recall import FILLER, recently_offered

log = logging.getLogger(__name__)

CUES = re.compile(
    r"\b(?:the|this|that|your|my|his|her) (?:book|novel|story|stories|text|chapter|author|ending)\b|"
    r"\b(?:in|from) (?:the|this|that) (?:book|novel|story|text|chapter)\b|\bchapter\b|\bch\.\s*\d|"
    r"\b(?:el|este|ese|mi|tu|su) (?:libro|cuento|relato|texto|cap[ií]tulo|autor|final)\b|"
    r"\ben (?:el|este|ese) (?:libro|cuento|texto|cap[ií]tulo)\b|\bcap[ií]tulo\b|\bnovela\b|"
    r"\bв (?:книге|рассказе|романе|тексте|главе|повести)\b|\bглав[аеуы]\b|\bкниг[аеиу]\b|\bрассказ[аеу]?\b|"
    r"书里|书中|这本书|那本书|小说|故事里|第[一二三四五六七八九十百\d]+章|这一章|作者", re.I)
LIBRARY_CUES = re.compile(r"\b(?:my|the|your|our) library\b|\bwhat books\b|\bbiblioteca\b|\bqu[eé] libros\b|"
                          r"библиотек|какие (?:у меня )?книги|书库|图书馆|哪些书", re.I)
_GENERIC_TITLE_WORDS = {"book", "books", "story", "stories", "tales", "novel", "chapter", "collected", "complete",
                        "works", "volume", "part", "other", "selected", "libro", "cuentos", "relatos", "obras",
                        "рассказы", "повести", "сочинения", "избранное", "книга", "собрание"}
_ORDINALS = {w: n for n, ws in enumerate([
    "first primero primer primera первая первый первой первую",
    "second segundo segunda вторая второй вторую",
    "third tercero tercer tercera третья третий третьей третью",
    "fourth cuarto cuarta четвертая четвёртая четвертый четвёртый четвертой четвёртой",
    "fifth quinto quinta пятая пятый пятой пятую",
    "sixth sexto sexta шестая шестой шестую",
    "seventh séptimo septimo séptima septima седьмая седьмой седьмую",
    "eighth octavo octava восьмая восьмой восьмую",
    "ninth noveno novena девятая девятый девятой девятую",
    "tenth décimo decimo décima decima десятая десятый десятой десятую",
    "last último ultimo última ultima последняя последний последней последнюю"], 1) for w in ws.split()}
_ZH_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CHAPTER_NUM = re.compile(r"(?:chapter|ch\.|cap[ií]tulo|cap\.|глав[аеуы]|гл\.)\s*"
                          r"(\d{1,3}|(?-i:[IVXLC]{2,6}|[IVXLC](?=\s*(?:$|[.,:;!?)]))))(?!\w)|"
                          r"第([一二两三四五六七八九十\d]{1,3})章", re.I)
_CHAPTER_WORD = re.compile(r"(\w+)\s+(?:chapter|cap[ií]tulo|глав[аеуы]|главой)|"
                           r"(?:chapter|cap[ií]tulo|глав[аеуы])\s+(\w+)", re.I)
_CHAPTER_WORDS = re.compile(r"\b(?:chapter|cap[ií]tulo|глава|part|parte|часть|contents|índice|содержание)\b|第.{1,3}章",
                            re.I)
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}

# Calibrated on three test books with the live embedder (BENCHMARKS P4.2); see scripts/library_eval.py.
COS_ASKED = 0.40     # he named the book or asked about one: the best match only needs to be on topic...
Z_ASKED = 1.0        # ...and above the book's average
COS_READING = 0.48   # he's only reading it: the match must be clearly about it...
Z_READING = 1.5      # ...and stand out from the rest of the book (the cosine does most of the work)
RRF_K = 60
MAX_PASSAGES = 2
PASSAGE_TOKENS = 350  # both passages together
LIST_MAX = 12


def chapter_number(text):
    """The chapter he refers to ("chapter 3", "capítulo III", "третья глава", "第三章", "the last chapter"),
    as 1-based number (-1 for the last), or None."""
    if m := _CHAPTER_NUM.search(text):
        if n := m[1]:
            return int(n) if n.isdigit() else _roman(n.lower())
        return int(m[2]) if m[2].isdigit() else _zh_number(m[2])
    for m in _CHAPTER_WORD.finditer(text):
        for w in (m[1], m[2]):
            if w and (k := _ORDINALS.get(w.lower())):
                return -1 if k == 11 else k
    return None


def _roman(s):
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN.get(a, 0)
        total += -v if _ROMAN.get(b, 0) > v else v
    return total or None


def _zh_number(s):
    if s == "十":
        return 10
    if "十" in s:
        tens, _, ones = s.partition("十")
        return _ZH_NUM.get(tens, 1) * 10 + _ZH_NUM.get(ones, 0)
    return _ZH_NUM.get(s)


def name_keys(doc, section_titles=()):
    """Ways he might name a document: the full title, distinctive title words, the author's surname, and
    the titles of stories in a collection (short section titles that aren't "Chapter 3")."""
    meta = json.loads(doc.get("meta") or "{}")
    keys = set()
    for title in {(t or "").split(" — ")[0] for t in section_titles}:  # "Каштанка — Глава первая" → "Каштанка"
        t = title.lower().strip(" «»\"'.")
        if len(t) >= 5 and len(t.split()) <= 4 and not _CHAPTER_WORDS.search(t) and t not in _GENERIC_TITLE_WORDS:
            keys.add(t)
    for title in filter(None, (doc["title"], meta.get("full_title"))):
        t = title.lower().strip(" «»\"'")
        if len(t) >= 3 and t not in _GENERIC_TITLE_WORDS:
            keys.add(t)
        latin_or_cyr = re.findall(r"[^\W\d_]{5,}", t)
        if len(latin_or_cyr) >= 2:
            keys |= {w for w in latin_or_cyr
                     if w not in _GENERIC_TITLE_WORDS and w not in words._QUERY_STOP and not _is_han(w)}
    if doc.get("author"):
        surname = doc["author"].split()[-1].lower()
        if len(surname) >= 4 and not _is_han(surname):
            keys.add(surname)
        if _is_han(doc["author"]) and len(doc["author"]) >= 2:
            keys.add(doc["author"])
    return keys


def _is_han(s):
    return any("一" <= ch <= "鿿" for ch in s)


def mentions(text, key):
    """Whole-word match; Russian keys also match their case forms (Чехова, Каштанку)."""
    if _is_han(key):
        return key in text
    if re.search(r"[а-яё]$", key):
        stem = key[:-1] if key[-1] in "аяоеыийь" else key
        return re.search(rf"(?<!\w){re.escape(stem)}[а-яё]{{0,3}}(?!\w)", text, re.I) is not None
    return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text, re.I) is not None


def excerpt(text, terms, code, budget):
    """The part of a chunk worth quoting: the sentence that shares most of his words (else the middle one),
    grown forwards and backwards with whole sentences up to `budget` tokens. '…' marks a cut."""
    if estimate_tokens(text) <= budget:
        return text.strip()
    sents = [p for s in sentences(text) for p in split_long(s, budget)]
    if not sents:
        return ""
    wanted = set(terms)
    hits = [len(wanted & {t[1].lower() for t in words.tokens(s, code)}) for s in sents] if wanted else [0] * len(sents)
    best = max(range(len(sents)), key=lambda i: (hits[i], -abs(i - len(sents) // 2)))
    lo = hi = best
    used = estimate_tokens(sents[best])
    grew = True
    while grew:
        grew = False
        if hi + 1 < len(sents) and used + (t := estimate_tokens(sents[hi + 1])) <= budget:
            hi, used, grew = hi + 1, used + t, True
        if lo > 0 and used + (t := estimate_tokens(sents[lo - 1])) <= budget:
            lo, used, grew = lo - 1, used + t, True
    body = _join(sents[lo:hi + 1], code)
    return ("…" if lo > 0 else "") + body + ("…" if hi < len(sents) - 1 else "")


def citation(doc, chunk, passage, reading=False, cfg=None):
    head = f"«{doc['title']}»" + (f" by {doc['author']}" if doc["author"] else "")
    if chunk["s_ord"] is None:
        where = "your notes on the whole book"
    else:
        where = f"ch. {chunk['s_ord'] + 1}" + (f' "{chunk["s_title"]}"' if chunk["s_title"] else "")
        where = f"your notes on {where}" if chunk["kind"] == "summary" else where
    passage = re.sub(r"\s+", " ", passage).replace("[", "(").replace("]", ")")
    lead = personal("from his library" + (" (the book he is reading)" if reading else ""), cfg)
    return f"{lead}, {head}, {where}: «{passage}»"


class Books:
    def __init__(self, cfg, db, library, embedder, persona):
        self.cfg, self.db, self.lib, self.embedder, self.persona = cfg, db, library, embedder, persona

    # the book he's reading -----------------------------------------------------------------------
    def current(self):
        cur = self.db.get_setting("current_book") or {}
        doc = self.lib.document(cur.get("id")) if cur.get("id") else None
        return doc if doc and doc["status"] in ("ready", "indexing") else None

    def current_chapter(self):
        return (self.db.get_setting("current_book") or {}).get("chapter")

    def section_for(self, doc_id, number):
        """The section he means by chapter `number`: a title that says so ("Chapter 3", "Глава третья",
        "第三章", "III."), else the number-th section; -1 is the last one."""
        secs = self.lib.sections(doc_id)
        if not secs:
            return None
        if number == -1:
            return secs[-1]
        for s in secs:
            title = s["title"] or ""
            if chapter_number(title) == number or re.match(rf"^\s*(?:{number}|{_to_roman(number)})\b[.:)]?",
                                                           title, re.I):
                return s
        return secs[number - 1] if 0 < number <= len(secs) else None

    # the turn ----------------------------------------------------------------------------------------
    async def before_turn(self, batch, batch_id=None):
        text = "\n".join(m["text"] for m in batch)
        docs = self.lib.searchable()
        notes = []
        if LIBRARY_CUES.search(text):
            notes.append(self.list_note())
        if not docs or FILLER.match(text.strip()):
            return notes
        t0 = time.monotonic()
        scope, reason, sections = self.scope(text, docs)
        if not scope:
            return notes
        try:
            query = await self.embedder.one(text, query=True)  # the same text as recall: shared embedding
        except Exception as e:
            log.warning("library recall skipped: %s", e)
            return notes
        code = lang.detect(text) or self.persona.practice_lang()
        picked = self.pick(text, query, code, scope, reason, sections)
        if not picked:
            log.info("library: %s, nothing clears the bar (%.0f ms)", reason, (time.monotonic() - t0) * 1000)
            return notes
        current = self.current()
        summaries = [p for p in picked if p["chunk"]["kind"] == "summary"]  # short, always whole (P4.3)
        passages = len(picked) - len(summaries)
        budget = (PASSAGE_TOKENS - sum(p["chunk"]["tokens"] for p in summaries)) // max(1, passages)
        terms = words.query_terms(text, code)
        for p in picked:
            doc = self.lib.document(p["chunk"]["doc_id"])
            passage = p["chunk"]["text"] if p["chunk"]["kind"] == "summary" else \
                excerpt(p["chunk"]["text"], terms, doc["lang"] or code, max(80, budget))
            notes.append(citation(doc, p["chunk"], passage, reading=bool(current and current["id"] == doc["id"]),
                                    cfg=self.cfg))
        self.db.conn.execute(
            "INSERT INTO injections(ts, batch_id, reason, items, tokens) VALUES (?, ?, ?, ?, ?)",
            (time.time(), batch_id, f"book:{reason}",
             json.dumps([["chunk", p["chunk"]["id"], round(p["cos"], 3)] for p in picked]),
             sum(estimate_tokens(n) for n in notes)))
        log.info("library: %s → %s (%.0f ms)", reason, ", ".join(f"chunk {p['chunk']['id']} cos {p['cos']:.2f} "
                                                                   f"z {p['z']:.1f}" for p in picked),
                 (time.monotonic() - t0) * 1000)
        return notes

    def scope(self, text, docs):
        """(documents to search, why, sections or None). Empty when there's no reason to look."""
        named = [d for d in docs if any(mentions(text, k) for k in
                                        name_keys(d, [x["title"] for x in self.lib.sections(d["id"])]))]
        current = self.current()
        number = chapter_number(text)
        if named:
            target = named[0] if len(named) == 1 else None
        else:
            target = current or (docs[0] if len(docs) == 1 else None)
        sections = None
        if number is not None and target and (sec := self.section_for(target["id"], number)):
            sections = [sec["id"]]
        if named:
            return named, "named", sections
        if CUES.search(text) or number is not None:
            return ([current] if current else docs), "cue", sections
        if current:
            return [current], "reading", None
        return [], None, None

    def pick(self, text, query, code, scope, reason, sections=None):
        doc_ids = [d["id"] for d in scope]
        cos = self.lib.similarities(query, doc_ids, sections)
        if not cos:
            return []
        values = list(cos.values())
        mean = sum(values) / len(values)
        sd = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 or 1.0
        by_vec = sorted(cos, key=cos.get, reverse=True)[:20]
        by_text = [cid for cid, _ in self.lib.search_text(words.query_terms(text, code), doc_ids, 20, sections)]
        fused = {}
        for ranking in (by_vec, by_text):
            for rank, cid in enumerate(ranking):
                fused[cid] = fused.get(cid, 0.0) + 1 / (RRF_K + rank + 1)
        cos_min, z_min = (COS_READING, Z_READING) if reason == "reading" else (COS_ASKED, Z_ASKED)
        if sections:  # he named the chapter: anything from it is on topic
            cos_min, z_min = 0.0, -99
        recent = {i for k, i in recently_offered(self.db) if k == "chunk"}
        picked = []
        order = sorted(fused, key=fused.get, reverse=True)
        if sections:  # "what happens in chapter 3?": her notes on that chapter first (small-to-big, P4.3)
            notes = [cid for cid in cos if self.lib.chunk_row(cid)["kind"] == "summary"]
            order = notes + [cid for cid in order if cid not in notes]
        for cid in order:
            c = cos.get(cid)
            if c is None or cid in recent or c < cos_min or (c - mean) / sd < z_min:
                continue
            chunk = self.lib.chunk_row(cid)
            if chunk["kind"] == "text" and any(p["chunk"]["doc_id"] == chunk["doc_id"] and p["chunk"]["kind"] == "text"
                                               and abs(p["chunk"]["ord"] - chunk["ord"]) <= 1 for p in picked):
                continue  # its neighbour shares the overlap: take something else
            picked.append({"chunk": chunk, "cos": c, "z": (c - mean) / sd, "rrf": fused[cid]})
            if len(picked) == MAX_PASSAGES:
                break
        return picked

    # the library itself --------------------------------------------------------------------------------
    def list_note(self):
        docs = self.lib.searchable()
        if not docs:
            return personal("his library is empty for now (he can send books with 📎 or in Telegram)", self.cfg)
        current = self.current()
        items = [f"«{d['title']}»" + (f" by {d['author']}" if d["author"] else "")
                 + f" ({lang.NAMES.get(d['lang'], 'unknown language')})"
                 + (personal(" - he is reading this one", self.cfg) if current and current["id"] == d["id"] else "")
                 for d in docs[-LIST_MAX:]]
        return personal("books in his library: ", self.cfg) + "; ".join(items)

    # commands ------------------------------------------------------------------------------------------
    async def cmd_books(self, channel, arg):
        docs = [d for d in self.lib.documents() if not d["status"].startswith("failed")]
        if not docs:
            return "📚 Your library is empty. Send a book with 📎 (or as a file in Telegram)"
        current = self.current()
        lines = ["📚 Your library:"]
        for d in docs:
            n = len(self.lib.sections(d["id"]))
            bits = [lang.NAMES.get(d["lang"], "?"), f"{n} chapter{'s' if n != 1 else ''}"]
            if d["status"] == "indexing":
                bits.append(f"indexing {self.lib.progress(d['id']):.0%}")
            elif d["status"] == "queued":
                bits = ["reading it…"]
            bits += self.extra_info(d)
            mark = " ← reading" if current and current["id"] == d["id"] else ""
            lines.append(f"{d['id']}. «{d['title']}»" + (f" — {d['author']}" if d["author"] else "")
                         + " · " + " · ".join(bits) + mark)
        lines.append("/reading <n> [chapter] sets the book you're reading")
        return "\n".join(lines)

    def extra_info(self, doc):
        """More per-book info for /books (difficulty, from P4.4)."""
        return []

    async def cmd_reading(self, channel, arg):
        parts = arg.split()
        if not parts:
            doc = self.current()
            if not doc:
                return "📖 You're not reading anything in the library. /books lists them, /reading <n> picks one"
            return "📖 Reading " + self._reading_label(doc, self.current_chapter())
        if parts[0].lower() in ("off", "none", "stop", "0", "-"):
            self.db.set_setting("current_book", None)
            return "📖 OK, no current book"
        if not parts[0].isdigit() or not (doc := self.lib.document(int(parts[0]))) or doc["status"].startswith("failed"):
            return "📖 /reading <n> [chapter], with n from /books"
        chapter = None
        if len(parts) > 1:
            number = int(parts[1]) if parts[1].isdigit() else chapter_number("chapter " + parts[1])
            sec = self.section_for(doc["id"], number) if number else None
            if not sec:
                return f"📖 «{doc['title']}» has {len(self.lib.sections(doc['id']))} chapters"
            chapter = sec["ord"]
        self.db.set_setting("current_book", {"id": doc["id"], "chapter": chapter})
        return "📖 Reading " + self._reading_label(doc, chapter)

    def _reading_label(self, doc, chapter):
        label = f"«{doc['title']}»"
        if chapter is not None:
            secs = self.lib.sections(doc["id"])
            if 0 <= chapter < len(secs):
                label += f", ch. {chapter + 1}" + (f" «{secs[chapter]['title']}»" if secs[chapter]["title"] else "")
        return label


def _to_roman(n):
    out = ""
    for v, s in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out, n = out + s, n - v
    return out or "0"
