"""Anki decks from his books (PLAN P4.6): `/anki <n>` in either channel, or GET /api/library/{id}/anki.

One note per glossed word he hasn't learned yet (P4.4), with two cards:
- Recognize: the word (with its pinyin or stress mark) and the book's sentence with the word in bold →
  its meaning in Spanish and English, and where it's from;
- Recall: the sentence with the word blanked out and its meaning as a hint → the word.
Note GUIDs come from the language and the word, and the deck id from the book, so exporting again
updates his existing cards instead of adding copies.
"""
import hashlib
import html
import re

from . import config, words

MODEL_ID = 1729413001  # fixed forever: changing it would make Anki treat every card as new
CSS = """.card { font-family: system-ui, sans-serif; font-size: 22px; text-align: center; color: #17201d; background: #fff; }
.word { font-size: 40px; margin: 12px 0 4px; }
.reading { color: #0f766e; font-size: 20px; }
.sentence { font-size: 20px; margin-top: 16px; line-height: 1.5; }
.meaning { font-size: 24px; margin-top: 12px; }
.source { color: #66736e; font-size: 14px; margin-top: 18px; }
.nightMode .card { color: #e6ece9; background: #141d1a; }"""


def _model():
    import genanki
    return genanki.Model(
        MODEL_ID, "Laika vocabulary",
        fields=[{"name": "Word"}, {"name": "Reading"}, {"name": "Sentence"}, {"name": "Blank"},
                {"name": "Meaning"}, {"name": "Source"}],
        templates=[
            {"name": "Recognize",
             "qfmt": '<div class="word">{{Word}}</div><div class="reading">{{Reading}}</div>'
                     '{{#Sentence}}<div class="sentence">{{Sentence}}</div>{{/Sentence}}',
             "afmt": '{{FrontSide}}<hr id="answer"><div class="meaning">{{Meaning}}</div>'
                     '<div class="source">{{Source}}</div>'},
            {"name": "Recall",
             "qfmt": '{{#Blank}}<div class="sentence">{{Blank}}</div>{{/Blank}}<div class="meaning">{{Meaning}}</div>',
             "afmt": '{{FrontSide}}<hr id="answer"><div class="word">{{Word}}</div><div class="reading">{{Reading}}</div>'
                     '<div class="source">{{Source}}</div>'}],
        css=CSS)


def deck_id(doc):
    return int(hashlib.sha256(f"laika-book-{doc['sha256']}".encode()).hexdigest()[:8], 16) | 1 << 30


def mark(sentence, lemma, code, bold=True):
    """The sentence with the word (whatever form it takes there) in bold, or blanked out."""
    if not sentence:
        return ""
    found = next((surface for surface, lem, _, _ in words.tokens(sentence, code) if lem.lower() == lemma), None)
    found = found or (lemma if lemma in sentence else None)
    esc = html.escape(sentence)
    if not found:
        return esc
    target = html.escape(found)
    repl = f"<b>{target}</b>" if bold else "＿" * max(2, min(len(found), 6))
    if code == "zh":
        return esc.replace(target, repl, 1)
    return re.sub(rf"(?<!\w){re.escape(target)}(?!\w)", repl, esc, count=1)


def cards(vocab, doc):
    """Glossed words of this book he hasn't learned yet, most frequent in the book first."""
    rows = [r for r in vocab.rows(doc["lang"]).values()
            if r["example_doc"] == doc["id"] and r["gloss"] and r["gloss"] != "?" and r["status"] in ("new", "learning")]
    freq = {lemma: n for lemma, n in vocab.db.conn.execute(
        "SELECT lemma, SUM(count) FROM lib.doc_words WHERE doc_id = ? AND lang = ? GROUP BY lemma",
        (doc["id"], doc["lang"]))}
    return sorted(rows, key=lambda r: -freq.get(r["lemma"], 0))


def export(vocab, doc):
    """Write the deck for a book; returns (path, number of words), or (None, 0) when there is nothing yet."""
    import genanki
    rows = cards(vocab, doc)
    if not rows:
        return None, 0
    model = _model()
    deck = genanki.Deck(deck_id(doc), f"Laika::{doc['title']}")
    for r in rows:
        meaning = html.escape(r["gloss"]) + (f" · <i>{html.escape(r['gloss_en'])}</i>" if r["gloss_en"] else "")
        source = f"«{html.escape(doc['title'])}»" + (f" — {html.escape(doc['author'])}" if doc["author"] else "")
        deck.add_note(genanki.Note(
            model=model, guid=genanki.guid_for("laika", doc["lang"], r["lemma"]),
            fields=[html.escape(r["lemma"]), html.escape(r["reading"] or ""),
                    mark(r["example"], r["lemma"], doc["lang"]), mark(r["example"], r["lemma"], doc["lang"], bold=False),
                    meaning, source],
            tags=["laika", doc["lang"]]))
    out = config.DATA_DIR / "library" / "anki"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{doc['id']}.apkg"
    genanki.Package(deck).write_to_file(str(path))
    return path, len(rows)


def filename(doc):
    base = re.sub(r"[^\w\- ]+", "", doc["title"]).strip()[:60] or f"book {doc['id']}"
    return f"Laika - {base}.apkg"


class AnkiExport:
    """The `/anki <n>` command: a 🃏 message with a download link (PWA) and the file itself (Telegram)."""

    def __init__(self, db, library, vocab, engine):
        self.db, self.lib, self.vocab, self.engine = db, library, vocab, engine

    async def command(self, channel, arg):
        current = (self.db.get_setting("current_book") or {}).get("id")
        n = int(arg) if arg.strip().isdigit() else current
        doc = self.lib.document(n) if n else None
        if not doc or doc["status"] != "ready":
            return "🃏 /anki <n>, with n from /books (a book that's in the library)"
        if doc["lang"] not in ("en", "ru", "zh"):
            return f"🃏 «{doc['title']}» is in a language you aren't learning, so it has no word list"
        path, count = export(self.vocab, doc)
        if not path:
            return f"🃏 No words from «{doc['title']}» yet: I pick and explain them while you're away"
        name = filename(doc)
        msg = self.db.add_message("system", channel, f"🃏 Anki deck for «{doc['title']}»: {count} words",
                                  meta={"download": f"/api/library/{doc['id']}/anki", "filename": name})
        await self.engine.publish({"type": "message", "message": msg, "origin": channel})
        await self.engine.publish({"type": "file", "path": str(path), "filename": name, "message": msg,
                                   "origin": channel})
        return None
