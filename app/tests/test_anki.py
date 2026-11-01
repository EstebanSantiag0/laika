"""Anki export (P4.6): the deck opens as an Anki collection, words keep stable GUIDs across exports, the
book sentence marks the word in whatever form it takes, and /anki posts a message plus the file.

Needs genanki (in the image since P4.4). Run:
docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_anki
"""
import asyncio
import os
import pathlib
import sqlite3
import tempfile
import zipfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import library  # noqa: E402
from laika.anki import AnkiExport, export, mark  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.persona import Persona  # noqa: E402
from laika.vault import Vault  # noqa: E402
from laika.vocab import Vocab  # noqa: E402
from tests.test_engine import CFG  # noqa: E402
from tests.test_library import FakeEmbedder, FakeEngine  # noqa: E402
from tests.test_vocab import RU, FakeLLM  # noqa: E402


def read_deck(path):
    with zipfile.ZipFile(path) as z:
        (TMP / "c.anki2").write_bytes(z.read("collection.anki2"))
    c = sqlite3.connect(TMP / "c.anki2")
    return {guid: flds.split("\x1f") for guid, flds in c.execute("SELECT guid, flds FROM notes")}, \
        c.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


async def _flow():
    assert mark("Собака искала хозяина.", "хозяин", "ru") == "Собака искала <b>хозяина</b>."
    assert mark("Собака искала хозяина.", "хозяин", "ru", bold=False) == "Собака искала ＿＿＿＿＿＿."
    assert mark("我生平没有见过这样的天空。", "天空", "zh") == "我生平没有见过这样的<b>天空</b>。"
    db = DB(":memory:")
    Memory(db)
    engine = FakeEngine()
    lib = library.Library({"library": {"chunk_tokens": 200, "overlap": 20, "embed_batch": 8}}, db, FakeEmbedder(),
                          Vault(TMP / "vault"), engine)
    db.set_setting("level_ru", "A1")
    v = Vocab({**CFG, "llm": {"background_slot": 1}}, db, lib, Persona(CFG, db, TMP / "vault"), FakeLLM())
    doc, _ = await lib.add(RU.encode(), "dog.txt", "web", "web")
    await asyncio.gather(*lib.tasks)
    await lib.run()
    doc = lib.document(doc["id"])
    a = AnkiExport(db, lib, v, engine)
    assert (await a.command("web", str(doc["id"]))).startswith("🃏 No words from «dog» yet")
    await v.run()
    path, n = export(v, doc)
    notes, n_cards = read_deck(path)
    assert n == len(notes) > 3 and n_cards == 2 * n, (n, len(notes), n_cards)
    host = next(f for f in notes.values() if f[0] == "хозяин")
    assert "<b>хозяин" in host[2] and "＿" in host[3] and host[4] == "es-хозяин · <i>en-хозяин</i>", host
    assert "«dog»" in host[5]
    v._upsert("ru", "хозяин", gloss="dueño")
    again, _ = read_deck(export(v, doc)[0])
    assert set(again) == set(notes), "the same words keep their GUIDs (Anki updates instead of duplicating)"
    assert next(f for f in again.values() if f[0] == "хозяин")[4].startswith("dueño")
    v._upsert("ru", "хозяин", status="known")
    assert len(read_deck(export(v, doc)[0])[0]) == n - 1, "a known word leaves the deck"
    assert await a.command("web", str(doc["id"])) is None
    msg, file = engine.events[-2], engine.events[-1]
    assert msg["message"]["meta"]["download"] == f"/api/library/{doc['id']}/anki" and file["type"] == "file"
    assert file["filename"] == "Laika - dog.apkg" and pathlib.Path(file["path"]).exists()
    assert (await a.command("web", "99")).startswith("🃏 /anki <n>")
    print("ok anki (deck, stable GUIDs, marked sentences, command)")


def test_anki():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_anki()
    print("all anki tests passed")
