"""Recall from books (P4.2) with a fake embedder: chapter references, book names, excerpts, when the
library is searched (named, cue, reading) and when it isn't, citations, once-only passages, /books, /reading.

The similarity bars are calibrated for the real embedder (BENCHMARKS P4.2); the bag-of-words fake gets
its own, so this tests the logic only.
Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_books
"""
import asyncio
import os
import pathlib
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import books, library  # noqa: E402
from laika.books import Books, chapter_number, excerpt, mentions, name_keys  # noqa: E402
from laika.conversation import Engine  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.persona import Persona, prompt_tokens  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_consolidate import FakeEmbedder as QueryEmbedder  # noqa: E402
from tests.test_engine import CFG  # noqa: E402
from tests.test_library import FakeEmbedder, FakeEngine  # noqa: E402

BOOK = """# The Lighthouse Keeper

## Chapter 1: The Storm
The storm came from the north at night. Waves broke over the rocks and the lamp shook in its glass.
Old Tomas climbed the stairs again and again to keep the flame alive. His hands were cold and wet.
By morning the sea was calm, and gulls sat on the railing as if nothing had happened.

## Chapter 2: The Letter
A boat brought a letter from his daughter Marta in the city. She wrote that she was going to marry a
baker named Pavel, and that she wanted her father at the wedding in spring.
Tomas read the letter by the window many times. He had not left the island in eleven years.

## Chapter 3: The Ship
In April a big ship lost its way in the fog. Tomas rang the bell for hours until the captain heard it
and turned away from the reef. The sailors waved their caps when the fog lifted.
"""


def test_parsing():
    assert chapter_number("what happens in chapter 3?") == 3
    assert chapter_number("¿qué pasa en el capítulo IV?") == 4
    assert chapter_number("в третьей главе он уходит") == 3
    assert chapter_number("第三章很有意思") == 3 and chapter_number("第十二章") == 12
    assert chapter_number("the last chapter made me cry") == -1
    assert chapter_number("in that chapter I cried") is None, "the English 'I' is not chapter one"
    assert chapter_number("Chapter XII. The End") == 12 and chapter_number("I like the book") is None
    chekhov = {"title": "Рассказы", "author": "Антон Чехов", "meta": "{}"}
    keys = name_keys(chekhov, ["Смерть чиновника", "Каштанка — Глава первая. Дурное поведение", "Глава вторая"])
    assert "рассказы" not in keys, "a generic title is not a name"
    assert "каштанка" in keys and "чехов" in keys and not any("глава" in k for k in keys), keys
    assert mentions("ты читала Чехова?", "чехов") and mentions("мне жаль Каштанку", "каштанка")
    assert not mentions("чеховский стиль", "чехов"), "only case endings, not other words"
    alice = name_keys({"title": "Alice's Adventures in Wonderland", "author": "Lewis Carroll", "meta": "{}"})
    assert {"wonderland", "carroll"} <= alice and mentions("I love Wonderland", "wonderland")
    assert mentions("我在读鲁迅的书", "鲁迅")
    long = " ".join(f"Sentence number {i} is here." for i in range(60)) + " The golden key opened the door. " + \
        " ".join(f"More filler {i} now." for i in range(60))
    cut = excerpt(long, ["golden", "key"], "en", 60)
    assert "golden key" in cut and cut.startswith("…") and cut.endswith("…") and len(cut) < 400, cut
    msg = {"text": "hi", "meta": {"note": "x" * 700}}
    assert prompt_tokens(msg) > 200, "the replayed note counts toward the history budget"
    print("ok parsing (chapters, names, excerpts, note sizes)")


async def _flow():
    books.COS_ASKED, books.Z_ASKED, books.COS_READING, books.Z_READING = 0.15, 0.0, 0.30, 1.0  # fake embedder
    db = DB(":memory:")
    Memory(db)
    engine = FakeEngine()
    lib = library.Library({"library": {"chunk_tokens": 60, "overlap": 10, "embed_batch": 8}}, db, FakeEmbedder(),
                          Vault(TMP / "vault"), engine)
    persona = Persona(CFG, db, TMP / "vault")
    b = Books(CFG, db, lib, QueryEmbedder(), persona)
    say = lambda t: db.add_message("user", "web", t)  # noqa: E731

    assert await b.before_turn([say("what books do I have in my library?")]) == [
        "their library is empty for now (they can send books with 📎 or in Telegram)"]
    doc, _ = await lib.add(BOOK.encode(), "lighthouse.md", "web", "web")
    await asyncio.gather(*lib.tasks)
    await lib.run()
    doc = lib.document(doc["id"])
    assert doc["status"] == "ready" and b.current()["id"] == doc["id"], "the book they sent is the one they're reading"

    assert await b.before_turn([say("haha")]) == []
    assert await b.before_turn([say("I made pasta for dinner tonight")]) == [], "unrelated: nothing"
    notes = await b.before_turn([say("Tomas rang the bell in the fog for the ship")], batch_id=1)
    assert len(notes) >= 1 and notes[0].startswith("from their library (the book they are reading), «The Lighthouse Keeper"), notes
    assert 'ch. 3 "Chapter 3: The Ship"' in notes[0] and "bell" in notes[0], notes
    again = await b.before_turn([say("Tomas rang the bell in the fog for the ship")], batch_id=2)
    assert not any("rang the bell" in n for n in again), "a passage is offered once in the visible window"

    db.set_setting("current_book", None)
    assert await b.before_turn([say("Marta wrote a letter about the wedding")]) == [], "no book, no cue, no name"
    named = await b.before_turn([say("In The Lighthouse Keeper, why did Marta write the letter about the wedding?")])
    assert named and "Marta" in named[0] and "ch. 2" in named[0], named
    chapter = await b.before_turn([say("what happens in the first chapter?")])
    assert chapter and "Chapter 1: The Storm" in chapter[0], chapter
    listed = await b.before_turn([say("what books are in my library?")])
    assert listed[0] == "books in their library: «The Lighthouse Keeper» (English)", listed
    log = db.conn.execute("SELECT reason, items FROM injections ORDER BY id").fetchall()
    assert [r["reason"] for r in log] == ["book:reading", "book:named", "book:cue"], [tuple(r) for r in log]

    # commands
    out = await b.cmd_books("web", "")
    assert out.startswith("📚 Your library:\n1. «The Lighthouse Keeper» · English · 3 chapters"), out
    assert await b.cmd_reading("web", "") == "📖 You're not reading anything in the library. /books lists them, " \
                                             "/reading <n> picks one"
    assert await b.cmd_reading("web", "1 2") == "📖 Reading «The Lighthouse Keeper», ch. 2 «Chapter 2: The Letter»"
    assert b.current_chapter() == 1 and "← reading" in await b.cmd_books("web", "")
    assert await b.cmd_reading("web", "1 9") == "📖 «The Lighthouse Keeper» has 3 chapters"
    assert (await b.cmd_reading("web", "7")).startswith("📖 /reading <n>")
    assert await b.cmd_reading("web", "off") == "📖 OK, no current book" and b.current() is None

    # the engine runs registered commands and posts their reply
    eng = Engine(CFG, db, None, persona)
    eng.channels.append(type("Channel", (), {"deliver": staticmethod(engine.publish)})())
    eng.commands["books"] = b.cmd_books
    assert await eng.receive("web", "/Books") is None and engine.said()[-1].startswith("📚 Your library:")
    print("ok library recall (gating, citations, once only, chapters, list, commands)")


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_parsing()
    test_flow()
    print("all book tests passed")
