"""Chapter and book summaries (P4.3) with a fake model: chapter text without the chunk overlap, long chapters
in pieces, the book summary, indexing as `summary` chunks, resuming after a cancel, the vault note, and the
chapter summary coming first when they ask about a chapter.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_summaries
"""
import asyncio
import os
import pathlib
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import books, library, summaries  # noqa: E402
from laika.books import Books  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.persona import Persona  # noqa: E402
from laika.summaries import Summaries  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_books import BOOK  # noqa: E402
from tests.test_consolidate import FakeEmbedder as QueryEmbedder  # noqa: E402
from tests.test_engine import CFG  # noqa: E402
from tests.test_library import FakeEmbedder, FakeEngine  # noqa: E402


class FakeLLM:
    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at

    async def chat(self, messages, slot, **kw):
        self.calls.append(messages)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise asyncio.CancelledError()  # they came back
        system, user = messages[0]["content"], messages[1]["content"]
        kind = "BOOK" if "whole of" in system else "MERGED" if "Merge" in system else "PART" if "part " in system else "CH"
        return f"**{kind}** " + " ".join(user.split()[:12]).rstrip(".") + "."  # a finished sentence (SB0 check)


async def _flow():
    db = DB(":memory:")
    Memory(db)
    vault = Vault(TMP / "vault")
    lib = library.Library({"library": {"chunk_tokens": 40, "overlap": 15, "embed_batch": 8}}, db, FakeEmbedder(),
                          vault, FakeEngine())
    doc, _ = await lib.add(BOOK.encode(), "lighthouse.md", "web", "web")
    await asyncio.gather(*lib.tasks)
    await lib.run()
    secs = lib.sections(doc["id"])
    rebuilt = lib.section_text(secs[0]["id"])
    assert rebuilt.count("The storm came from the north at night.") == 1, "the overlap isn't repeated"
    assert "gulls sat on the railing" in rebuilt and len(lib.chunks(doc["id"])) > 3

    llm = FakeLLM(fail_at=2)
    s = Summaries(CFG, db, llm, lib, FakeEmbedder())
    assert s.pending() and [d["id"] for d in s.todo()] == [doc["id"]]
    try:
        await s.run()
        raise AssertionError("expected the cancel")
    except asyncio.CancelledError:
        pass
    done = [x["summary"] for x in lib.sections(doc["id"])]
    assert done[0] and done[0].startswith("CH The storm") and done[1] is None, done  # chapter 1 kept, markdown gone
    s.llm = FakeLLM()
    summaries.PIECE_TOKENS = 30  # make chapter 3 too long for one call: pieces, then a merge
    await s.run()
    summaries.PIECE_TOKENS = 5000
    secs = lib.sections(doc["id"])
    assert all(x["summary"] for x in secs) and secs[2]["summary"].startswith("MERGED"), [x["summary"] for x in secs]
    d = lib.document(doc["id"])
    assert d["summary"].startswith("BOOK") and not s.pending()
    kinds = db.conn.execute("SELECT section_id, kind FROM lib.chunks WHERE kind = 'summary' ORDER BY section_id").fetchall()
    assert [k[0] for k in kinds] == [0] + [x["id"] for x in secs], [tuple(k) for k in kinds]
    assert db.conn.execute("SELECT COUNT(*) FROM lib.chunks_vec WHERE rowid IN "
                           "(SELECT id FROM lib.chunks WHERE kind = 'summary')").fetchone()[0] == 4
    note = vault.read(d["note_path"])
    assert "## Summary\nBOOK" in note and "1. Chapter 1: The Storm\n    CH The storm" in note, note
    # summarizing again replaces, never duplicates
    await s.store(d, secs[0], "CH again")
    assert db.conn.execute("SELECT COUNT(*) FROM lib.chunks WHERE kind = 'summary' AND section_id = ?",
                           (secs[0]["id"],)).fetchone()[0] == 1

    # "what happens in chapter 2?": her notes on that chapter come first, then a passage
    books.COS_ASKED, books.Z_ASKED = 0.15, 0.0
    b = Books(CFG, db, lib, QueryEmbedder(), Persona(CFG, db, TMP / "vault"))
    notes = await b.before_turn([db.add_message("user", "web", "what happens in chapter 2?")])
    assert notes[0].startswith('from their library (the book they are reading), «The Lighthouse Keeper», your notes on '
                               'ch. 2 "Chapter 2: The Letter": «'), notes
    assert len(notes) == 2 and 'ch. 2 "Chapter 2: The Letter": «' in notes[1], notes
    print("ok summaries (overlap, pieces, merge, book, index, resume, note, chapter questions)")


def test_summaries():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_summaries()
    print("all summary tests passed")
