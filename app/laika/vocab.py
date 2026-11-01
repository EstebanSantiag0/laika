"""His vocabulary and how hard each book is for him (PLAN P4.4, DECISIONS D18). No manual marking needed.

What he knows starts from his level per language, then follows the evidence:
- the starting point: Chinese HSK n → the HSK 3.0 words of levels 1..n (`data/hsk30.tsv`); Russian and
  English → the most frequent lemmas for his CEFR level (wordfreq: A1 700 … C2 25,000);
- his own messages: a word he uses without a correction is known (one Laika wove in for him becomes
  learning first, then known the next time); a word the tutor gives him in a correction is learning;
- `vocab` rows (laika.db, backed up nightly) hold everything that differs from the starting point.
A book's difficulty is its coverage: the share of its counted words (names and numbers left out) he
knows, per book and per chapter. About 98% is comfortable reading, 95% the minimum (Hu & Nation 2000;
Schmitt et al. 2011), and "learn N words" counts the most frequent unknown ones needed to reach 98%.
Chinese words jieba keeps whole (看书) count as known when they split into words he knows (看 + 书).

At night the background slot glosses each book's most frequent unknown words (Spanish and English,
a reading, and a sentence from the book), for P4.5 (woven into chat) and P4.6 (Anki).

P4.5: while he reads a book in the language he's practising, at most one turn in about four gets one
of its glossed words in the note, for Laika to use once if it fits (the chapter he's on first). Each
word is offered at most 3 times; her using it counts as an exposure, and his using it later moves it
to learning, then known. It's a hint in the note, never a quiz; the texting style stays in charge.
"""
import functools
import logging
import pathlib
import re
import time

from pypinyin import Style, pinyin

from . import lang, words
from .config import personal
from .library import sentences, word_lang

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab (
    lang TEXT NOT NULL,
    lemma TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',      -- new | learning | known | ignored
    source TEXT,                             -- his_msg | correction | book:<id> | woven
    seen_in_his_msgs INTEGER NOT NULL DEFAULT 0,
    exposures INTEGER NOT NULL DEFAULT 0,    -- times Laika used it after it was offered to her (P4.5)
    gloss TEXT,                              -- Spanish meaning
    gloss_en TEXT,
    reading TEXT,                            -- pinyin, or the Russian word with its stress mark
    example TEXT,
    example_doc INTEGER,
    tries INTEGER NOT NULL DEFAULT 0,        -- times it was offered for weaving (P4.5)
    updated_ts REAL,
    PRIMARY KEY (lang, lemma)
);
"""
VOCAB_LANGS = ("en", "ru", "zh")
CEFR_LEMMAS = {"A1": 700, "A2": 1500, "B1": 3000, "B2": 5000, "C1": 9000, "C2": 25000}
CEFR_HSK = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
COMFORTABLE, MINIMUM = 0.98, 0.95
GLOSS_PER_BOOK = 60      # unknown words glossed per book (most frequent first)
GLOSS_BATCH = 15
GLOSS_ZIPF_MIN = 3.0     # useful words first: below this they're rare (Zipf 3 ≈ once per million words)
GLOSS_SCHEMA = {
    "type": "object",
    "properties": {"words": {"type": "array", "items": {
        "type": "object",
        "properties": {"word": {"type": "string"}, "es": {"type": "string"}, "en": {"type": "string"},
                       "stressed": {"type": "string"}},
        "required": ["word", "es", "en"]}}},
    "required": ["words"],
}
GLOSS_PROMPT = """You write vocabulary cards for {user}, a native Spanish speaker learning {lang}. For each \
{lang} word below (in its dictionary form), give its most common meaning as it is used in the example \
sentence: "es" in Spanish and "en" in English, each 1 to 4 words.{stress} Answer for every word, in order."""
RU_STRESS = (' "stressed" is the Russian word with an acute accent (U+0301) on the stressed vowel, '
             'for example соба́ка (leave it out for one-syllable words).')
_ACUTE = "\u0301"
WEAVE_EVERY = 4  # P4.5: at most one turn in about four
WEAVE_TRIES = 3
WEAVE_NOTE = "a word from «{title}» he hasn't learned yet, for you to use once only if it fits naturally: {word} ({meaning})"
NOTES_EVERY_S = 20 * 3600  # difficulty in the vault notes follows what he learns, refreshed about daily


def level_size(code, level):
    """How many words the starting point counts as known: an HSK level for Chinese, else a lemma count."""
    m = re.search(r"HSK\s*([1-9])", level or "", re.I)
    cefr = re.search(r"\b([ABC][12])\b", (level or "").upper())
    if code == "zh":
        return int(m.group(1)) if m else CEFR_HSK.get(cefr.group(1), 1) if cefr else 1
    return CEFR_LEMMAS.get(cefr.group(1), 3000) if cefr else 3000


@functools.cache
def hsk_levels():
    """{word: HSK 3.0 level}."""
    out = {}
    for line in (pathlib.Path(__file__).with_name("data") / "hsk30.tsv").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            word, level = line.split("\t")
            out.setdefault(word, int(level))
    return out


@functools.lru_cache(maxsize=8)
def baseline(code, size):
    """The words counted as known before any evidence: HSK words up to level `size` (zh), or the `size`
    most frequent lemmas (wordfreq forms lemmatized like the books are)."""
    if code == "zh":
        return frozenset(w for w, lv in hsk_levels().items() if lv <= size)
    from wordfreq import top_n_list
    known = set()
    for form in top_n_list(code, size * 4):
        for _, lemma, kind, _ in words.tokens(form, code):
            if kind == words.WORD:
                known.add(lemma.lower())
        if len(known) >= size:
            break
    return frozenset(known)


@functools.lru_cache(maxsize=50_000)
def zipf(code, lemma):
    from wordfreq import zipf_frequency
    return zipf_frequency(lemma, code)


def reading_of(code, lemma):
    if code == "zh":
        return " ".join(s[0] for s in pinyin(lemma, style=Style.TONE))
    return None


def _plain_word(word):
    """A word as the lemmas store it: no stress mark, ё as е, lowercase."""
    return (word or "").replace(_ACUTE, "").replace("ё", "е").strip().lower()


def percent(coverage):
    """Rounded down, so 97.7% doesn't show as "98%" next to "with effort"."""
    return f"{int(coverage * 100)}%"


def label(coverage):
    return "comfortable" if coverage >= COMFORTABLE else "with effort" if coverage >= MINIMUM else "hard"


class Vocab:
    def __init__(self, cfg, db, library, persona, llm=None):
        self.cfg, self.db, self.lib, self.persona, self.llm = cfg, db, library, persona, llm
        db.conn.executescript(SCHEMA)
        self.slot = cfg["llm"]["background_slot"]

    # what he knows -------------------------------------------------------------------------------------
    def rows(self, code):
        return {r["lemma"]: dict(r) for r in self.db.conn.execute("SELECT * FROM vocab WHERE lang = ?", (code,))}

    def known_set(self, code):
        """(baseline known, {lemma: status} from evidence): the inputs of `knows`."""
        base = baseline(code, level_size(code, self.persona.level(code)))
        return base, {lemma: r["status"] for lemma, r in self.rows(code).items()}

    @staticmethod
    def knows(lemma, base, status, code):
        s = status.get(lemma)
        if s in ("known", "ignored"):
            return True
        if s in ("new", "learning"):
            return False
        if lemma in base:
            return True
        if code == "zh" and len(lemma) > 1:  # 看书 = 看 + 书: greedy longest known pieces
            i = 0
            while i < len(lemma):
                for j in range(min(len(lemma), i + 4), i, -1):
                    piece = lemma[i:j]
                    if (piece in base and status.get(piece) not in ("new", "learning")) or \
                            status.get(piece) == "known":
                        i = j
                        break
                else:
                    return False
            return True
        return False

    # difficulty ----------------------------------------------------------------------------------------
    def coverage(self, doc):
        """{"book": {...}, "sections": {section id: {...}}} with coverage, known/counted tokens and how many
        words to learn for 98%; None for languages he doesn't learn (Spanish, unknown)."""
        code = doc["lang"]
        if code not in VOCAB_LANGS:
            return None
        base, status = self.known_set(code)
        per_sec, unknown = {}, {}
        for sid, lemma, n in self.db.conn.execute(
                "SELECT section_id, lemma, count FROM lib.doc_words WHERE doc_id = ? AND lang = ?", (doc["id"], code)):
            sec = per_sec.setdefault(sid, [0, 0])
            sec[1] += n
            if self.knows(lemma, base, status, code):
                sec[0] += n
            else:
                unknown[lemma] = unknown.get(lemma, 0) + n
        known = sum(s[0] for s in per_sec.values())
        total = sum(s[1] for s in per_sec.values())
        if not total:
            return None
        need, have = 0, known
        for _, n in sorted(unknown.items(), key=lambda kv: kv[1], reverse=True):
            if have / total >= COMFORTABLE:
                break
            have += n
            need += 1
        return {"book": {"coverage": known / total, "known": known, "total": total, "learn_for_98": need,
                         "unknown_lemmas": len(unknown)},
                "sections": {sid: {"coverage": k / t if t else 1.0, "known": k, "total": t}
                             for sid, (k, t) in per_sec.items()}}

    def difficulty_line(self, doc):
        cov = self.coverage(doc)
        if not cov:
            return None
        b = cov["book"]
        line = f"{percent(b['coverage'])} known ({label(b['coverage'])})"
        if b["learn_for_98"]:
            line += f", learn {b['learn_for_98']} word{'s' if b['learn_for_98'] != 1 else ''} for 98%"
        return line

    def unknown_words(self, doc, section_id=None, limit=GLOSS_PER_BOOK, zipf_min=GLOSS_ZIPF_MIN):
        """The most frequent words of a book (or chapter) he doesn't know yet: [(lemma, count)]."""
        code = doc["lang"]
        base, status = self.known_set(code)
        sql = "SELECT lemma, SUM(count) AS n FROM lib.doc_words WHERE doc_id = ? AND lang = ?"
        args = [doc["id"], code]
        if section_id is not None:
            sql += " AND section_id = ?"
            args.append(section_id)
        out = []
        for lemma, n in self.db.conn.execute(sql + " GROUP BY lemma ORDER BY n DESC", args):
            if status.get(lemma) == "ignored" or self.knows(lemma, base, status, code):
                continue
            if len(lemma) < 2 and code != "zh" or zipf(code, lemma) < zipf_min:
                continue
            out.append((lemma, n))
            if len(out) >= limit:
                break
        return out

    # evidence from his messages ------------------------------------------------------------------------
    def from_his_messages(self, texts, code, fixes=None):
        """Words he used in the practice language: known when uncorrected (learning first, if Laika wove it
        in for him); words a correction gave him are learning. `fixes` None means no tutor pass ran."""
        if code not in VOCAB_LANGS:
            return
        base, status = self.known_set(code)
        corrected, offered = set(), set()
        for f in fixes or []:
            before = self._lemmas(f.get("original", ""), code)
            corrected |= before
            if f.get("type") in ("vocabulary", "spelling", "register"):
                offered |= self._lemmas(f.get("better", ""), code) - before
        used = set()
        for t in texts:
            used |= self._lemmas(t, code)
        now = time.time()
        rows = self.rows(code)
        for lemma in used - corrected:
            row = rows.get(lemma)
            if row is None:
                if lemma in base:
                    continue  # nothing new to record
                self._upsert(code, lemma, status="known", source="his_msg", seen=1, ts=now)
            elif row["status"] in ("new", "learning"):
                new = "learning" if row["status"] == "new" and row["exposures"] else "known"
                self._upsert(code, lemma, status=new, seen=row["seen_in_his_msgs"] + 1, ts=now)
            else:
                self._upsert(code, lemma, seen=row["seen_in_his_msgs"] + 1, ts=now)
        for lemma in offered:
            row = rows.get(lemma)
            if not row or row["status"] == "new":
                self._upsert(code, lemma, status="learning", source=(row or {}).get("source") or "correction", ts=now)

    def _lemmas(self, text, code):
        return {lemma.lower() for _, lemma, kind, pos in words.tokens(text, code)
                if kind == words.WORD and word_lang(pos, code) == code}

    def _upsert(self, code, lemma, ts=None, seen=None, **fields):
        cols = {"status": fields.get("status"), "source": fields.get("source"), "seen_in_his_msgs": seen,
                **{k: v for k, v in fields.items() if k not in ("status", "source")}}
        cols = {k: v for k, v in cols.items() if v is not None}
        cols["updated_ts"] = ts or time.time()
        self.db.conn.execute("INSERT OR IGNORE INTO vocab(lang, lemma, status) VALUES (?, ?, ?)",
                             (code, lemma, fields.get("status") or "new"))
        self.db.conn.execute(f"UPDATE vocab SET {', '.join(f'{k} = ?' for k in cols)} WHERE lang = ? AND lemma = ?",
                             (*cols.values(), code, lemma))

    # glosses (background job) --------------------------------------------------------------------------
    def gloss_todo(self):
        """[(doc, [lemma])] still to gloss; the book he's reading first, its current chapter's words first."""
        if self.llm is None:
            return []
        cur = self.db.get_setting("current_book") or {}
        out = []
        docs = sorted(self.lib.documents(("ready",)), key=lambda d: (d["id"] != cur.get("id"), d["id"]))
        for doc in docs:
            if doc["lang"] not in VOCAB_LANGS:
                continue
            rows = self.rows(doc["lang"])
            words_ = []
            if doc["id"] == cur.get("id") and cur.get("chapter") is not None:
                secs = self.lib.sections(doc["id"])
                if 0 <= cur["chapter"] < len(secs):
                    words_ += [w for w, _ in self.unknown_words(doc, secs[cur["chapter"]]["id"], limit=25)]
            words_ += [w for w, _ in self.unknown_words(doc) if w not in words_]
            todo = [w for w in words_ if not (rows.get(w) or {}).get("gloss")]
            if todo:
                out.append((doc, todo))
        return out

    def notes_due(self):
        return time.time() - self.db.get_setting("vocab_notes_ts", 0) > NOTES_EVERY_S and self.lib.documents(("ready",))

    def pending(self):
        return bool(self.notes_due() or self.gloss_todo())

    async def run(self):
        """Gloss a batch at a time; each batch is saved when it's done (a cancel loses at most one). Then, once
        a day, the book notes get his current difficulty and word lists."""
        for doc, todo in self.gloss_todo():
            code = doc["lang"]
            for i in range(0, len(todo), GLOSS_BATCH):
                batch = todo[i:i + GLOSS_BATCH]
                examples = {w: self.example(doc, w) for w in batch}
                listing = "\n".join(f"{w} (example: {examples[w] or '-'})" for w in batch)
                system = personal(GLOSS_PROMPT, self.cfg).format(lang=lang.NAMES[code], stress=RU_STRESS if code == "ru" else "")
                result = await self.llm.chat([{"role": "system", "content": system},
                                              {"role": "user", "content": listing}],
                                             self.slot, json_schema=GLOSS_SCHEMA, temperature=0.2, max_tokens=1200)
                answers = result.get("words", [])
                got = {_plain_word(g.get("word")): g for g in answers}
                for k, w in enumerate(batch):
                    # the model often writes the stress into "word" itself (бе́дный); else match by position
                    g = got.get(w) or (answers[k] if len(answers) == len(batch) else {})
                    stressed = next((x.strip() for x in (g.get("stressed"), g.get("word"))
                                     if x and _ACUTE in x and _plain_word(x) == w), None)
                    reading = reading_of(code, w) or stressed
                    self._upsert(code, w, source=f"book:{doc['id']}", gloss=(g.get("es") or "").strip()[:80] or "?",
                                 gloss_en=(g.get("en") or "").strip()[:80] or None, reading=reading,
                                 example=examples[w], example_doc=doc["id"])
            log.info("vocab: glossed %d words of document %s «%s»", len(todo), doc["id"], doc["title"])
            self.lib.write_note(self.lib.document(doc["id"]))
        if self.notes_due():
            for doc in self.lib.documents(("ready",)):
                self.lib.write_note(doc)
            self.db.set_setting("vocab_notes_ts", time.time())

    def example(self, doc, lemma, max_tokens=40):
        """A short sentence of the book that uses the word (any form of it)."""
        rows = self.db.conn.execute(
            "SELECT c.text FROM lib.chunks_fts f JOIN lib.chunks c ON c.id = f.rowid "
            "WHERE chunks_fts MATCH ? AND c.doc_id = ? AND c.kind = 'text' LIMIT 8",
            ('"' + lemma.replace('"', "") + '"', doc["id"])).fetchall()
        best = None
        for (text,) in rows:
            for s in sentences(text):
                if lemma in {t[1].lower() for t in words.tokens(s, doc["lang"])}:
                    size = len(s)
                    if 12 <= size and (best is None or abs(size - 80) < abs(len(best) - 80)):
                        best = s
        return best[:240] if best else None

    def note_lines(self, doc):
        """Lines for the book's vault note."""
        cov = self.coverage(doc)
        if not cov:
            return []
        lines = [f"- Difficulty for you: {self.difficulty_line(doc)}"]
        hard = [(s, cov["sections"].get(s["id"])) for s in self.lib.sections(doc["id"])]
        hard = [(s, c) for s, c in hard if c and c["total"] >= 50]
        if len(hard) > 1:
            easiest = max(hard, key=lambda sc: sc[1]["coverage"])
            hardest = min(hard, key=lambda sc: sc[1]["coverage"])
            lines.append(f"- Easiest chapter: {easiest[0]['ord'] + 1} ({percent(easiest[1]['coverage'])}); hardest: "
                         f"{hardest[0]['ord'] + 1} ({percent(hardest[1]['coverage'])})")
        glossed = [r for r in self.rows(doc["lang"]).values()
                   if r["example_doc"] == doc["id"] and r["gloss"] and r["status"] in ("new", "learning")]
        if glossed:
            glossed.sort(key=lambda r: r["lemma"])
            lines += ["", "## Words to learn"] + [
                f"- {r['lemma']}" + (f" [{r['reading']}]" if r["reading"] else "") + f" — {r['gloss']}"
                + (f" / {r['gloss_en']}" if r["gloss_en"] else "") + (" (learning)" if r["status"] == "learning" else "")
                for r in glossed[:80]]
        return lines

    # weaving words in (P4.5) ---------------------------------------------------------------------------
    def reading_now(self):
        """(book, chapter section id or None) when he reads a ready book in the language he practises."""
        cur = self.db.get_setting("current_book") or {}
        doc = self.lib.document(cur.get("id")) if cur.get("id") else None
        if not doc or doc["status"] != "ready" or doc["lang"] != self.persona.practice_lang():
            return None, None
        secs = self.lib.sections(doc["id"])
        ch = cur.get("chapter")
        return doc, (secs[ch]["id"] if ch is not None and 0 <= ch < len(secs) else None)

    def weave_target(self, doc, section_id=None):
        rows = self.rows(doc["lang"])
        candidates = (self.unknown_words(doc, section_id, limit=40) if section_id is not None else []) + \
            self.unknown_words(doc, limit=GLOSS_PER_BOOK)
        for lemma, _ in candidates:
            r = rows.get(lemma)
            if r and r["gloss"] and r["status"] == "new" and not r["exposures"] and r["tries"] < WEAVE_TRIES:
                return r
        return None

    async def before_turn(self, batch, batch_id=None):
        doc, section_id = self.reading_now()
        if doc is None or doc["lang"] not in VOCAB_LANGS:
            return []
        turns = self.db.get_setting("weave_turns", WEAVE_EVERY - 1) + 1
        if turns < WEAVE_EVERY or not (target := self.weave_target(doc, section_id)):
            self.db.set_setting("weave_turns", turns)
            return []
        self.db.set_setting("weave_turns", 0)
        self.db.set_setting("weave_offer", {"batch": batch_id, "lang": doc["lang"], "lemma": target["lemma"]})
        self._upsert(doc["lang"], target["lemma"], tries=target["tries"] + 1)
        meaning = target["gloss_en"] or target["gloss"]
        log.info("vocab: offered «%s» from document %s (try %d)", target["lemma"], doc["id"], target["tries"] + 1)
        return [personal(WEAVE_NOTE, self.cfg).format(title=doc["title"], word=target["lemma"], meaning=meaning)]

    async def after_turn(self, batch_id, batch, bubbles, origin):
        """Her reply used the offered word (any form of it): one exposure."""
        offer = self.db.get_setting("weave_offer")
        if not offer or offer.get("batch") != batch_id:
            return
        self.db.set_setting("weave_offer", None)
        said = set()
        for b in bubbles:
            said |= self._lemmas(b["text"], offer["lang"])
        if offer["lemma"] in said or (offer["lang"] == "zh" and any(offer["lemma"] in b["text"] for b in bubbles)):
            row = self.rows(offer["lang"]).get(offer["lemma"]) or {}
            self._upsert(offer["lang"], offer["lemma"], exposures=row.get("exposures", 0) + 1)
            log.info("vocab: Laika used «%s»", offer["lemma"])

