"""SB0 (docs/PLAN-second-brain.md): one bad chunk or summary can't block the library any more.

- A chunk the embed service rejects (400) is isolated from its batch, counted, and given up on after 3
  runs; the document ends `ready` (`ready_partial` in the API) and a book added after it is not blocked.
- If the service itself is down (connection refused), the run raises and nothing is counted.
- Transient errors are retried with backoff; a 400 is not.
- An unusable summary is retried once at a low temperature, then saved as failed; a summary call
  that keeps failing is given up on after 3 attempts, and the book summary waits for its chapters.
- The nightly reconcile reopens a ready document that still has chunks to embed.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_sb0
"""
import asyncio
import os
import pathlib
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

import httpx  # noqa: E402

from laika import library, retry, summaries  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.summaries import Summaries  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_engine import CFG  # noqa: E402
from tests.test_library import PARA, FakeEngine  # noqa: E402
from tests.test_memory import fake_embed  # noqa: E402

REQ = httpx.Request("POST", "http://embed/v1/embeddings")


def status_error(code):
    return httpx.HTTPStatusError(f"{code}", request=REQ, response=httpx.Response(code, request=REQ))


class PoisonEmbedder:
    """Rejects any batch holding the word POISON with a 400, as llama-server does for a bad input."""
    def __init__(self, down=False):
        self.calls, self.down = 0, down

    async def embed(self, texts, query=False):
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("connection refused", request=REQ)
        if any("POISON" in t for t in texts):
            raise status_error(400)
        return [fake_embed(t) for t in texts]


async def _embedding_flow():
    db = DB(":memory:")
    Memory(db)
    lib = library.Library({"library": {"chunk_tokens": 60, "overlap": 0, "embed_batch": 4}}, db, PoisonEmbedder(),
                          Vault(TMP / "vault"), FakeEngine())
    bad, _ = await lib.add(("Bad Book\n\n" + PARA * 3 + "POISON here. " + PARA * 3).encode(), "bad.txt", "web", "web")
    await asyncio.gather(*lib.tasks)
    good, _ = await lib.add(("Good Book\n\n" + PARA * 4).encode(), "good.txt", "web", "web")
    await asyncio.gather(*lib.tasks)

    # the service is down: the run raises, nothing is counted against any chunk
    lib.embedder = PoisonEmbedder(down=True)
    try:
        await lib.run()
        raise AssertionError("expected the connection error")
    except httpx.ConnectError:
        pass
    assert db.conn.execute("SELECT COALESCE(SUM(attempts), 0) FROM lib.chunks").fetchone()[0] == 0

    # run 1: the bad chunk is isolated and counted once; everything else is embedded, the good book is ready
    lib.embedder = PoisonEmbedder()
    await lib.run()
    assert lib.document(good["id"])["status"] == "ready", "a book added after a bad one is not blocked"
    row = db.conn.execute("SELECT attempts, embedded, last_error FROM lib.chunks WHERE text LIKE '%POISON%'").fetchone()
    assert (row["attempts"], row["embedded"]) == (1, 0) and "HTTPStatusError" in row["last_error"], dict(row)
    assert lib.document(bad["id"])["status"] == "indexing"
    # runs 2 and 3: given up on; the book is ready without it, keyword search still finds it
    await lib.run()
    await lib.run()
    row = db.conn.execute("SELECT id, attempts, embedded FROM lib.chunks WHERE text LIKE '%POISON%'").fetchone()
    assert (row["attempts"], row["embedded"]) == (3, -1), dict(row)
    doc = lib.document(bad["id"])
    assert doc["status"] == "ready" and lib.state(doc) == "ready_partial" and lib.state(lib.document(good["id"])) == "ready"
    n = lib.counts(bad["id"])
    assert n["chunks"]["failed"] == 1 and n["chunks"]["todo"] == 0 and n["chunks"]["done"] >= 1, n
    assert lib.progress(bad["id"]) == 1.0
    assert db.conn.execute("SELECT rowid FROM lib.chunks_fts WHERE chunks_fts MATCH 'poison'").fetchall()
    assert "1 passage(s) couldn't be embedded" in lib.vault.read(doc["note_path"])
    assert not lib.pending()

    # reconcile: a ready document with chunks left to embed goes back to indexing, then finishes
    db.conn.execute("UPDATE lib.chunks SET embedded = 0 WHERE doc_id = ? AND id = "
                    "(SELECT MIN(id) FROM lib.chunks WHERE doc_id = ?)", (good["id"], good["id"]))
    assert lib.reconcile() == [good["id"]] and lib.pending()
    await lib.run()
    assert lib.document(good["id"])["status"] == "ready" and lib.reconcile() == []
    print("ok sb0 embedding (isolate, count, give up, not blocked, service down, counts, note, reconcile)")
    return db, lib


async def _retry_flow():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    real, retry.asyncio.sleep = retry.asyncio.sleep, fake_sleep
    try:
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise status_error(503)
            return "ok"

        assert await retry.retrying(flaky) == "ok" and len(calls) == 3 and len(slept) == 2
        assert all(0 <= s <= 2 for s in slept), slept  # full jitter under the caps 1 s, 2 s

        async def bad():
            calls.append(1)
            raise status_error(400)

        calls.clear()
        try:
            await retry.retrying(bad)
            raise AssertionError("a 400 isn't retried")
        except httpx.HTTPStatusError:
            assert len(calls) == 1

        async def slow():
            calls.append(1)
            raise httpx.ReadTimeout("slow", request=REQ)

        calls.clear()
        try:
            await retry.retrying(slow, timeouts=False)
        except httpx.ReadTimeout:
            assert len(calls) == 1, "llm.chat doesn't retry its 10-minute timeout"
        calls.clear()
        try:
            await retry.retrying(slow)
        except httpx.ReadTimeout:
            assert len(calls) == 3
    finally:
        retry.asyncio.sleep = real
    print("ok sb0 retry (transient retried with jitter, 400 not, timeouts only when allowed)")


class SummaryLLM:
    """Chapter 1 gets a usable summary; chapter 2's first answer is cut off (no final period) and its
    retry at 0.1 is fine; chapter 3 always errors with a 400 until `heal`."""
    def __init__(self):
        self.temps, self.heal = [], False

    async def chat(self, messages, slot, temperature=0.3, **kw):
        self.temps.append(temperature)
        system, user = messages[0]["content"], messages[1]["content"]
        if "whole of" in system:
            return "The whole book in short. " + " ".join(["Words."] * 10)
        if "Third" in user and not self.heal:
            raise status_error(400)
        if "Second" in user and temperature > 0.2:
            return "Cut off in the middle of a"
        return "A chapter about " + user.split()[0] + " and more."


async def _summary_flow():
    db = DB(":memory:")
    Memory(db)
    lib = library.Library({"library": {"chunk_tokens": 200, "overlap": 0, "embed_batch": 8}}, db, PoisonEmbedder(),
                          Vault(TMP / "vault2"), FakeEngine())
    text = "# Book\n\n## One\n\nFirst " + PARA + "\n\n## Two\n\nSecond " + PARA + "\n\n## Three\n\nThird " + PARA
    doc, _ = await lib.add(text.encode(), "book.md", "web", "web")
    await asyncio.gather(*lib.tasks)
    await lib.run()
    llm = SummaryLLM()
    s = Summaries(CFG, db, llm, lib, PoisonEmbedder())
    await s.run()
    secs = lib.sections(doc["id"])
    by = {x["title"]: x for x in secs}
    assert by["One"]["summary"].startswith("A chapter about First"), by["One"]
    assert by["Two"]["summary"].startswith("A chapter about Second") and 0.1 in llm.temps, "retried at 0.1"
    assert by["Three"]["summary"] is None and by["Three"]["summary_attempts"] == 1
    assert lib.document(doc["id"])["summary"] is None, "the book summary waits for the failed chapter"
    await s.run()
    await s.run()
    by = {x["title"]: x for x in lib.sections(doc["id"])}
    assert by["Three"]["summary"] == "" and by["Three"]["summary_attempts"] == 3 and by["Three"]["summary_error"]
    d = lib.document(doc["id"])
    assert d["summary"].startswith("The whole book") and not s.pending()
    n = lib.counts(doc["id"])
    assert n["sections"]["failed"] == 1 and n["sections"]["todo"] == 0 and lib.state(d) == "ready_partial", n

    # validation on its own
    long_src = "word " * 1500
    assert summaries.valid("A fine summary. " * 15, long_src)
    assert not summaries.valid("Too short.", long_src) and summaries.valid("Short but fine.", "tiny text")
    assert not summaries.valid("Ends without a stop", "tiny")
    assert not summaries.valid("Это краткое содержание главы.", "tiny")
    assert not summaries.valid("You keep notes on the books in his library.", "tiny")
    assert not summaries.valid("word " * 301 + ".", long_src)
    print("ok sb0 summaries (low-temperature retry, failure counted, given up, book waits, validation)")


def test_sb0():
    asyncio.run(_embedding_flow())
    asyncio.run(_retry_flow())
    asyncio.run(_summary_flow())


if __name__ == "__main__":
    test_sb0()
