"""SB5 (docs/PLAN-second-brain.md): screenshots and photos.

- A screenshot with text: Tesseract reads it, it becomes a saved item with format `image` whose text is
  searchable, and the notice says the text was read.
- A picture without text: kept as a saved item, no model call to file it, and the notice asks what it is.
- A photo sent with "para después: …" becomes a `for_later` item with the caption as its note, and the
  caption arriving as a chat message doesn't save a second item.
- The original comes back with an image Content-Type.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_sb5
"""
import asyncio
import os
import pathlib
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

import pypdfium2 as pdfium  # noqa: E402

from laika import library  # noqa: E402
from laika.api_brain import png  # noqa: E402
from laika.areas import Areas  # noqa: E402
from laika.brain import Brain  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_brain import CFG, FakeLLM  # noqa: E402
from tests.test_consolidate import FakeEmbedder  # noqa: E402
from tests.test_library import FakeEngine, make_pdf  # noqa: E402


def screenshot(lines):
    pdf = pdfium.PdfDocument(make_pdf([lines]))
    bm = pdf[0].render(scale=2, rev_byteorder=True)
    data = png(bm.width, bm.height, bm.buffer, bm.stride)
    pdf.close()
    return data


def blank(w=200, h=120):
    return png(w, h, b"\xff" * (w * 3 * h), w * 3)


async def main():
    db = DB(":memory:")
    mem = Memory(db)
    areas = Areas(CFG, db)
    vault = Vault(TMP / "vault", "Dasha")
    engine = FakeEngine()
    llm = FakeLLM()
    lib = library.Library({**CFG, "library": {"chunk_tokens": 200, "overlap": 0}}, db, FakeEmbedder(), vault, engine)
    brain = Brain(CFG, db, llm, mem, FakeEmbedder(), vault, engine, areas, lib)
    lib.on_document.append(brain.on_document)

    # a screenshot with text
    shot = screenshot(["The capacitive moisture sensor costs four dollars.", "Order it before Friday."])
    d, _ = await lib.add(shot, "Screenshot_2026-11-02.png", "web", "web")
    await asyncio.gather(*lib.tasks)
    await lib.run()
    item = brain.by_doc(d["id"])
    text = " ".join(c["text"] for c in lib.chunks(d["id"])).lower()
    assert item["format"] == "image" and "moisture" in text and "friday" in text, (item, text)
    assert lib.document(d["id"])["status"] == "ready" and "📚 I saved the picture and read its text" in engine.said()
    assert not any(t.startswith("📚 Reading") for t in engine.said()), "no 'Reading…' for a picture"

    # a picture without text: kept, not filed by the model, and she asks
    calls = len(llm.prompts)
    d2, _ = await lib.add(blank(), "IMG_0001.jpg.png", "telegram", "telegram")
    await asyncio.gather(*lib.tasks)
    await lib.run()
    item2 = brain.by_doc(d2["id"])
    assert lib.document(d2["id"])["status"] == "ready" and not lib.chunks(d2["id"]) and item2["format"] == "image"
    assert any("no text I can read" in t for t in engine.said())
    await brain.run()
    assert brain.get(item2["id"])["filed"] and len(llm.prompts) == calls + 1, "only the screenshot went to the model"

    # a photo with a caption: for later, caption as note, no second item from the caption message
    n = len(brain.items(status="all"))
    d3, _ = await lib.add(blank(240, 90), "foto-77.jpg", "telegram", "telegram",
                          meta={"caption": "para después: la receta de la abuela"})
    item3 = brain.by_doc(d3["id"])
    assert item3["kind"] == "for_later" and item3["note"] == "para después: la receta de la abuela" and \
        item3["title"] == "para después: la receta de la abuela", item3
    notes = await brain.before_turn([db.add_message("user", "telegram", "para después: la receta de la abuela")])
    assert len(brain.items(status="all")) == n + 1 and notes and "sent a file" in notes[0], notes
    # "comment": a "📎" message from him starts a turn, and her note has what's in it (no second save)
    class Recv(FakeEngine):
        def __init__(self):
            super().__init__()
            self.got = []

        async def receive(self, channel, text, meta=None):
            self.got.append((channel, text, meta))
            return db.add_message("user", channel, text, meta=meta)

    brain.engine = Recv()
    await brain.comment_on(item["id"])
    channel, text, meta = brain.engine.got[0]
    assert channel == "web" and text.startswith("📎 ") and meta == {"attachment": {"item_id": item["id"]}}
    msg = db.get_message(db.conn.execute("SELECT MAX(id) FROM messages").fetchone()[0])
    n = len(brain.items(status="all"))
    notes = await brain.before_turn([msg])
    assert len(notes) == 1 and "he just sent you a picture" in notes[0] and "moisture" in notes[0].lower(), notes
    assert len(brain.items(status="all")) == n
    await brain.comment_on(item2["id"])
    notes = await brain.before_turn([db.get_message(db.conn.execute("SELECT MAX(id) FROM messages").fetchone()[0])])
    assert "you can't see pictures" in notes[0], notes
    print("ok sb5 (screenshot OCR, picture without text, caption → kind and note, no double save, comment)")


def test_sb5():
    asyncio.run(main())


if __name__ == "__main__":
    test_sb5()
