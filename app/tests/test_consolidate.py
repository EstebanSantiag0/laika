"""Consolidation and the nightly sleep (P3.3) with a fake model and embedder: episodes, the four fact
operations, tombstones, Inbox, notes, the memory snapshot in the prompt, and a job that yields.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_consolidate
"""
import asyncio
import datetime
import pathlib
import re
import tempfile
import time

from laika.consolidate import Consolidator, rounds, segments
from laika.db import DB
from laika.jobs import Jobs
from laika.memory import Memory
from laika.nightly import Nightly
from laika.persona import Persona
from laika.vault import Vault
from tests.test_engine import CFG
from tests.test_memory import fake_embed

MCFG = {**CFG, "memory": {"tz": "Europe/Madrid", "episode_gap_min": 30, "episode_max_msgs": 40,
                          "idle_close_min": 30, "nightly": "03:00", "history_after_nightly": 30}}


class FakeEmbedder:
    async def embed(self, texts, query=False):
        return [fake_embed(t) for t in texts]

    async def one(self, text, query=False):
        return fake_embed(text)


class FakeLLM:
    """Extraction answers depend on the known facts in the prompt, like the real model's should."""

    def __init__(self):
        self.calls, self.warmed = [], []

    async def chat(self, messages, slot, json_schema=None, **kw):
        system, user = messages[0]["content"], messages[-1]["content"]
        self.calls.append((slot, system[:40]))
        if json_schema is None:
            return "Who they are: a Spanish language lover who **taught themself** harmonica."
        known = {text: int(i) for i, text in re.findall(r"^(\d+): (.+)$", system, re.M)}
        uke = known.get("Dasha taught themself the harmonica", 0)
        fact = lambda text, about="Dasha", op="new", id=0, imp=0.6, conf=0.9: {  # noqa: E731
            "text": text, "about": about, "category": "skill", "op": op, "id": id, "importance": imp,
            "confidence": conf, "sensitive": False}
        base = {"mood": "happy", "arousal": 0.4, "self_rel": 0.6, "future_rel": 0.3, "us": [], "laika_self": [],
                "intentions": [], "entities": [{"name": "Ana", "kind": "person", "aliases": ["Аня"]}]}
        if "flute" in user:
            return {**base, "title": "Music", "diary": "They told me how they taught themself the harmonica.",
                    "facts": [fact("Dasha taught themself the harmonica", imp=0.8),
                              fact("Ana is Dasha's cousin", about="Аня"),
                              fact("Dasha might like jazz", conf=0.4)],
                    "intentions": [{"text": "Ask how their Russian exam went", "due_from": "2026-10-24",
                                    "due_to": "2026-10-26", "cue_terms": ["exam"]}],
                    "us": ["They joke that the harmonica is their rebellion"],
                    "laika_self": ["Laika thinks discipline matters more than passion"]}
        if "Monterrey" in user:
            return {**base, "title": "Ana moved", "diary": "They told me their cousin moved to Monterrey.",
                    "facts": [fact("Dasha taught themself the harmonica", op="same", id=uke),
                              fact("Dasha taught themself the harmonica from YouTube videos", op="update", id=uke),
                              fact("Ana lives in Monterrey", about="Ana")]}
        return {**base, "title": "Again", "diary": "They mentioned Ana again.",
                "facts": [fact("Ana lives in Monterrey", about="Ana")]}

    async def warm(self, messages, slot):
        self.warmed.append(slot)


def chat(db, start_ts, lines):
    for i, (role, text) in enumerate(lines):
        msg = db.add_message(role, "web", text, lang="en")
        db.conn.execute("UPDATE messages SET ts = ? WHERE id = ?", (start_ts + 60 * i, msg["id"]))


async def main():
    assert [len(s) for s in segments([{"ts": t, "role": "user"} for t in (0, 60, 4000, 4060)], 1800, 40)] == [2, 2]
    convo = [{"id": 1, "role": "user", "ts": 0, "text": "hi"}, {"id": 2, "role": "companion", "ts": 1, "text": "hey"},
             {"id": 3, "role": "companion", "ts": 2, "text": "what's up"}, {"id": 4, "role": "user", "ts": 3, "text": "x"}]
    assert [(r[0], r[1]) for r in rounds(convo)] == [(1, 3), (4, 4)]

    db = DB(":memory:")
    mem = Memory(db)
    root = pathlib.Path(tempfile.mkdtemp())
    vault, llm = Vault(root, "Dasha"), FakeLLM()
    persona = Persona(MCFG, db, root)
    persona.ensure_files()
    cons = Consolidator(MCFG, db, llm, mem, FakeEmbedder(), vault)
    t0 = time.time() - 3 * 3600
    chat(db, t0, [("user", "I tried flute once but taught myself harmonica"), ("companion", "Nice!"),
                  ("user", "my cousin Аня plays too"), ("companion", "Cute")])
    chat(db, t0 + 3600, [("user", "Ana moved to Monterrey"), ("companion", "Big move!")])
    assert await cons.close_all() == 2 and not cons.pending()

    uke = next(f for f in mem.facts() if "harmonica" in f["text"])
    assert uke["text"] == "Dasha taught themself the harmonica from YouTube videos", uke  # "update" applied
    assert uke["n_uses"] == 3, uke["n_uses"]  # mentioned, "same", "update": three uses
    ana = mem.entity("Ana")
    assert {f["text"] for f in mem.facts(ana["id"])} == {"Ana is Dasha's cousin", "Ana lives in Monterrey"}
    assert [f["text"] for f in mem.facts(statuses=("tentative",))] == ["Dasha might like jazz"]
    assert {f["category"] for f in mem.facts()} >= {"us", "laika_self"}
    assert [i["text"] for i in mem.intentions()] == ["Ask how their Russian exam went"]
    assert db.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 3
    day = datetime.datetime.fromtimestamp(t0, cons.tz).date().isoformat()
    journal = vault.read(f"Journal/{day}.md")
    assert "· Music" in journal and "· Ana moved" in journal and "taught themself the harmonica" in journal, journal
    assert "Ana lives in Monterrey" in vault.read("People/Ana.md")  # two episodes: Ana gets her note
    assert "Dasha might like jazz" in vault.read("Laika/Inbox.md")

    moved = next(f for f in mem.facts(ana["id"]) if "Monterrey" in f["text"])
    mem.forget(fact_ids=[moved["id"]], texts=[moved["text"]], embeddings=[fake_embed(moved["text"])])
    chat(db, t0 + 7200, [("user", "talked to Ana today"), ("companion", "How is she?")])
    await cons.close_all()
    assert not any("Monterrey" in f["text"] for f in mem.facts()), "a forgotten fact was learned again"
    print("ok consolidate")

    nightly = Nightly(MCFG, db, llm, mem, vault, cons, persona)
    await nightly.run()
    prompt = persona.system_prompt()
    assert "## What you know about Dasha\nWho they are: a Spanish language lover who taught themself harmonica." in prompt
    from laika.nightly import day_name
    loop_line = f"Ask how their Russian exam went ({day_name(datetime.date(2026, 10, 24))} to " \
                f"{day_name(datetime.date(2026, 10, 26))})"
    assert loop_line in prompt or datetime.date(2026, 10, 24) - nightly.today() > datetime.timedelta(days=7), prompt
    assert "## You two\n- They joke that the harmonica is their rebellion" in prompt
    assert "Laika thinks discipline matters more than passion" in prompt
    assert "taught themself" in vault.read("Dasha/Profile.md") and "^f" in vault.read("Dasha/Profile.md")
    assert db.get_setting("nightly_done") == nightly.today().isoformat() and llm.warmed == [0]
    assert db.get_setting("history_start_id", 0) > 0 and db.get_setting("running_summary") == ""
    assert list((root / ".laika" / "backup").glob("laika-*.db"))
    assert "Who they are" in persona.grounding([]), "memory must count as grounding for callbacks"
    print("ok nightly")

    class Engine:
        pending, turn, typing_now = [], None, False

        def composing(self):
            return self.typing_now

    engine = Engine()
    jobs = Jobs(MCFG, db, engine, cons, nightly)
    chat(db, time.time(), [("user", "one more thing"), ("companion", "sure")])
    assert jobs.next_job() is None, "they just wrote: no job yet"
    db.set_setting("consolidate_requested", True)
    assert jobs.next_job() == "consolidate"

    async def slow():
        await asyncio.sleep(30)
    cons.close_all = slow
    task = asyncio.create_task(jobs.run("consolidate"))
    await asyncio.sleep(0.2)
    engine.typing_now = True  # they start typing: the job must step aside
    assert await task is False
    assert db.get_setting("consolidate_requested") is True, "a paused job is retried later"
    print("ok jobs")
    print("all consolidation tests passed")


if __name__ == "__main__":
    asyncio.run(main())
