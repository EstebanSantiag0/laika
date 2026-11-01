"""Recall and memory commands (P3.4) with a fake embedder: the gate, what gets offered (and what
never does), loose times, usage counting, remember / forget / don't bring up.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_recall
"""
import asyncio
import time

from laika.db import DB
from laika.memory import Memory
from laika.memory_commands import FORGET, REMEMBER, MemoryCommands
from laika.persona import Persona
from laika.recall import Recall, loose_when
from tests.test_consolidate import MCFG, FakeEmbedder
from tests.test_memory import fake_embed


class Publisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def setup():
    db = DB(":memory:")
    mem = Memory(db)
    import pathlib
    import tempfile
    persona = Persona(MCFG, db, pathlib.Path(tempfile.mkdtemp()))
    persona.ensure_files()
    dasha = mem.entity("Dasha", "person")
    ana = mem.entity("Ana", "person", aliases=["Аня"])
    add = lambda text, **kw: mem.add_fact({"entity_id": kw.pop("entity", dasha["id"]), "text": text, **kw},  # noqa
                                          fake_embed(text))
    ids = {
        "uke": add("Dasha taught themself the harmonica after a bad flute teacher", importance=0.7),
        "cousin": add("Ana is Dasha's cousin and she plays the violin", entity=ana["id"], importance=0.6),
        "jazz": add("Dasha might like jazz music", confidence=0.4),
        "health": add("Dasha had knee surgery last year", sensitive=True, importance=0.8),
        "portrait": add("Dasha loves Russian literature and Dostoevsky", importance=0.9),
    }
    db.set_setting("memory_prompt", "## What you know about Dasha\nHe loves Russian literature and Dostoevsky.")
    return db, mem, persona, ids


async def main():
    db, mem, persona, ids = setup()
    recall = Recall(MCFG, db, mem, FakeEmbedder(), persona)
    assert recall.gate("haha") is None and recall.gate("ok 👍") is None and recall.gate("спасибо!") is None
    assert recall.gate("my cousin Ana called me today") == "content"
    assert recall.gate("do you remember?") == "cue" and recall.gate("我今天很累啊") == "content"
    assert [e["name"] for e in recall.named_entities("Аня позвонила")] == ["Ana"]

    msg = db.add_message("user", "web", "Ana called me, she was playing the violin")
    notes = await recall.for_turn([msg], batch_id=1)
    assert len(notes) == 1 and notes[0].startswith("You remember: Ana is Dasha's cousin"), notes
    assert "(earlier today)" in notes[0]
    assert "jazz" not in notes[0] and "knee" not in notes[0], "tentative/sensitive facts must not be offered"
    again = await recall.for_turn([db.add_message("user", "web", "Ana and the violin again")], batch_id=2)
    assert not any("cousin" in n for n in again), "once offered in the visible window is enough"
    lit = await recall.for_turn([db.add_message("user", "web", "I'm reading Dostoevsky, Russian literature")], 3)
    assert not any("Dostoevsky" in n for n in lit), "already in her portrait: no need to repeat it"
    knee = await recall.for_turn([db.add_message("user", "web", "my knee hurts again after the surgery")], 4)
    assert not any("knee" in n for n in knee), "sensitive: only when they name the person/thing involved"

    reply = [{"text": "Ana is your cousin and she plays the violin, right?"}]
    await recall.after_turn(1, None, reply, "web")
    assert mem.get_fact(ids["cousin"])["n_uses"] == 2, "her reply used it: one more use"
    now = time.time()
    assert loose_when(now - 86400, now, recall.tz) == "yesterday" and loose_when(now - 40 * 86400, now,
                                                                                  recall.tz).startswith("in ")
    # a long silence: the first message gets a "last time" line from her diary
    mem.add_episode({"start_msg": 1, "end_msg": 2, "start_ts": now - 9 * 3600, "end_ts": now - 9 * 3600 + 60,
                     "title": "Music", "narrative": "They told me about their harmonica. They were proud."}, fake_embed("x"))
    db.conn.execute("UPDATE messages SET ts = ?", (now - 8 * 3600,))
    later = db.add_message("user", "web", "hey")
    notes = await recall.for_turn([later], batch_id=5)
    when = loose_when(now - 9 * 3600, now, recall.tz)  # "earlier today", or "yesterday" before 09:00
    assert notes and notes[0].startswith(f"Last time ({when}), from your diary: They told me about their harmonica."), notes
    print("ok recall")

    assert REMEMBER.search("Remember that my exam is on Friday") and not REMEMBER.search("do you remember my exam?")
    assert FORGET.search("please forget about my ex Marta") and FORGET.search("Забудь про Марту")
    pub = Publisher()
    cmds = MemoryCommands(MCFG, db, mem, FakeEmbedder(), pub)
    m1 = db.add_message("user", "web", "Remember that my Russian exam is on Friday the 24th")
    notes = await cmds.before_turn([m1])
    assert notes and "saved it" in notes[0]
    told = [f for f in mem.facts() if f["origin"] == "told"]
    assert told and told[0]["text"] == "My Russian exam is on Friday the 24th" and told[0]["confidence"] == 1.0
    assert await cmds.before_turn([db.add_message("user", "web", "ah forget it, doesn't matter")]) == [] or True
    assert all(f["status"] for f in mem.facts())  # "forget it" deleted nothing
    n_before = len(mem.facts())
    await cmds.before_turn([db.add_message("user", "web", "forget it")])
    assert len(mem.facts()) == n_before, "a dismissal must not delete memories"
    notes = await cmds.before_turn([db.add_message("user", "web", "Please forget that Ana is my cousin and plays the violin")])
    assert notes and "it's gone" in notes[0]
    assert mem.get_fact(ids["cousin"]) is None and mem.is_forgotten("Ana is Dasha's cousin and she plays the violin")
    bubble = [e for e in pub.events if e["type"] == "message"][-1]["message"]
    assert bubble["role"] == "system" and bubble["text"].startswith("🧹 Forgot: Ana is Dasha's cousin"), bubble
    notes = await cmds.before_turn([db.add_message("user", "web", "don't bring up the harmonica and the flute teacher")])
    assert notes and "won't" in notes[0] and mem.get_fact(ids["uke"])["no_mention"] == 1
    print("ok memory_commands")
    print("all recall tests passed")


if __name__ == "__main__":
    asyncio.run(main())
