"""SB6 (docs/PLAN-second-brain.md): resurfacing.

- A due idea comes back once in a light moment in the day: a note for Laika, then the next step
  (3 → 10 → 30 days); not twice a day, not at night, not when he asks something or writes a lot.
- GET /api/resurface's list (Brain.due) and the "later" step.
- Nightly: "for later" items that came back three times untouched are archived after 60 days; Sunday's
  "still want these?" note goes once; tags that mean the same are merged (and the merge is undoable).
- Saving something close to an older item: she may mention it.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_sb6
"""
import asyncio
import datetime
import os
import pathlib
import tempfile
import time
import zoneinfo

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import library  # noqa: E402
from laika.areas import Areas  # noqa: E402
from laika.brain import Brain  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.graph_edit import GraphEdit  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.reminders import Reminders  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_brain import CFG, FakeLLM  # noqa: E402
from tests.test_consolidate import FakeEmbedder  # noqa: E402
from tests.test_library import FakeEngine  # noqa: E402
from tests.test_reminders import Engine  # noqa: E402

TZ = zoneinfo.ZoneInfo("Europe/Madrid")
DAY = 86400


def at(y, m, d, h):
    return datetime.datetime(y, m, d, h, tzinfo=TZ).timestamp()


class TagLLM(FakeLLM):
    async def chat(self, messages, slot, json_schema=None, **kw):
        if "same topic" in messages[0]["content"]:
            a, b = [ln.split(": ", 1)[1] for ln in messages[1]["content"].splitlines()]
            return {"same": sorted(a.split("-")) == sorted(b.split("-"))}
        return await super().chat(messages, slot, json_schema=json_schema, **kw)


async def main():
    db = DB(":memory:")
    mem = Memory(db)
    areas = Areas(CFG, db)
    vault = Vault(TMP / "vault", "Dasha")
    engine = FakeEngine()
    lib = library.Library({**CFG, "library": {}}, db, FakeEmbedder(), vault, engine)
    brain = Brain(CFG, db, TagLLM(), mem, FakeEmbedder(), vault, engine, areas, lib)
    edit = GraphEdit(CFG, db, mem, FakeEmbedder(), vault, brain, Reminders(CFG, db, None, Engine([])), areas, lib)

    noon = at(2026, 11, 4, 12)
    idea = await brain.add("idea", "Huerto en la azotea", "Plantar tomates en macetas")
    db.conn.execute("UPDATE brain_items SET created = ?, resurface_at = ? WHERE id = ?", (noon - 4 * DAY, noon - DAY, idea["id"]))
    assert [i["id"] for i in brain.due(noon)] == [idea["id"]]
    assert brain._resurface_note("¿qué opinas de esto?", now=noon) == [], "not when he asks something"
    assert brain._resurface_note("x " * 200, now=noon) == [], "not when he writes a lot"
    assert brain._resurface_note("jaja ok", now=at(2026, 11, 4, 23)) == [], "not at night"
    notes = brain._resurface_note("jaja ok", now=noon)
    assert notes and "4 days ago he saved an idea" in notes[0] and "Huerto en la azotea" in notes[0], notes
    item = brain.get(idea["id"])
    assert item["resurface_step"] == 1 and abs(item["resurface_at"] - (noon + 10 * DAY)) < 1, item
    db.conn.execute("UPDATE brain_items SET resurface_at = ? WHERE id = ?", (noon - 1, idea["id"]))
    assert brain._resurface_note("jaja ok", now=noon + 60) == [], "once a day"
    assert brain._resurface_note("jaja ok", now=noon + DAY), "the next day it can"
    brain.advance(brain.get(idea["id"]), noon)
    assert brain.get(idea["id"])["resurface_at"] is None, "after 3 → 10 → 30 days it stops"

    # nightly: archive an old "for later" that went through every step; Sunday digest; tag merges
    sunday = at(2026, 11, 8, 3)
    old = await brain.add("for_later", "Ver la serie de robots", "")
    recent = await brain.add("for_later", "Leer sobre riego", "")
    db.conn.execute("UPDATE brain_items SET created = ?, resurface_at = NULL WHERE id = ?", (sunday - 70 * DAY, old["id"]))
    db.conn.execute("UPDATE brain_items SET created = ? WHERE id = ?", (sunday - 20 * DAY, recent["id"]))
    await brain.add("saved", "uno", tags=["sensor-humedad"])
    await brain.add("saved", "dos", tags=["sensor-humedad", "arduino"])
    c = await brain.add("saved", "tres", tags=["humedad-sensor"])
    archived, merged = await brain.nightly(edit, now=sunday)
    assert archived == 1 and brain.get(old["id"])["status"] == "archived"
    assert merged == 1 and brain.get(c["id"])["tags"] == ["sensor-humedad"], brain.get(c["id"])["tags"]
    trash = edit.trash(lambda nid: None)["nodes"]
    assert trash[0]["id"] == "tag:humedad-sensor" and trash[0]["renamed_to"] == "sensor-humedad", "undoable"
    digest = brain._digest_note(now=at(2026, 11, 8, 11))
    assert digest and "Leer sobre riego" in digest[0] and "Ver la serie" not in digest[0], digest
    assert brain._digest_note(now=at(2026, 11, 8, 12)) == [], "once"

    # saving something close to an older item: she may say they go together
    db.conn.execute("UPDATE brain_items SET status = 'active' WHERE id = ?", (old["id"],))
    notes = await brain.before_turn([db.add_message("user", "web", "apunta esta idea: plantar tomates en macetas en la azotea")])
    assert any("it goes with something he saved" in n and "Huerto en la azotea" in n for n in notes), notes
    print("ok sb6 (resurface once a day in light moments, steps, due list, archive, Sunday digest, tag merge, related)")


def test_sb6():
    asyncio.run(main())


if __name__ == "__main__":
    test_sb6()
