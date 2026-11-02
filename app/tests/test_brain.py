"""SB1 (docs/PLAN-second-brain.md): saved items, links into the brain, likes/dislikes as facts.

- "apunta esta idea…" saves an idea with areas, tags and links; links come only from the candidates
  (an id the model made up is dropped); the turn note confirms; a vault note is written.
- A bare link becomes a saved item (format link, or video for a reel).
- "no, es para después" changes the kind of what he just saved (the note moves).
- "guarda que me encanta…" saves a fact with polarity like, not an item.
- Ordinary chat saves nothing; a failed capture says so instead of pretending.
- Saved items come up in conversation when they fit, not twice in a row.
- A library document becomes a saved item and is filed by the background job.
- A link he hid is never recreated.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_brain
"""
import asyncio
import re
import os
import pathlib
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import library  # noqa: E402
from laika.areas import Areas  # noqa: E402
from laika.brain import Brain  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_consolidate import FakeEmbedder  # noqa: E402
from tests.test_engine import CFG  # noqa: E402
from tests.test_library import PARA, FakeEngine  # noqa: E402
from tests.test_memory import fake_embed  # noqa: E402

CFG = {**CFG, "user": {"name": "Dasha", "pronouns": "he"}}


class FakeLLM:
    """Answers like the real classifier would, from the last message; records the candidates it saw."""
    def __init__(self):
        self.prompts, self.fail = [], False

    async def chat(self, messages, slot, json_schema=None, **kw):
        if self.fail:
            raise RuntimeError("llm down")
        system, user = messages[0]["content"], messages[1]["content"]
        self.prompts.append(system)
        last = user.splitlines()[-1].lower()
        cands = [ln.split(":")[0] for ln in system.split("Candidates (id: what it is):")[1].splitlines() if ":" in ln]
        robot = next((c for c in cands if c.startswith("E")), None)
        if "is_save" not in json_schema["properties"]:  # filing a library document
            return {"title": "Guía de riego", "text": "Una guía sobre riego automático.", "areas": ["projects"],
                    "tags": ["riego", "Arduino Uno"], "links": [{"id": robot, "reason": "para el robot"}] if robot else []}
        if "idea" in last:
            return {"is_save": True, "kind": "idea", "title": "Sensor de humedad para el robot",
                    "text": "Usar un sensor de humedad para que el robot sepa cuándo regar.", "areas": ["projects"],
                    "tags": ["robotica", "Riego Automático", "x", "y"],
                    "links": ([{"id": robot, "reason": "podría servir para el robot"}] if robot else [])
                    + [{"id": "F999", "reason": "made up"}]}
        if "http" in last:
            return {"is_save": True, "kind": "saved", "title": "Un reel de cocina", "text": "", "areas": [],
                    "tags": ["cocina"], "links": []}
        if "encanta" in last:
            return {"is_save": True, "kind": "like", "title": "Ramen picante", "text": "le encanta el ramen picante",
                    "areas": [], "tags": [], "links": []}
        return {"is_save": False, "kind": "saved", "title": "", "text": "", "areas": [], "tags": [], "links": []}


async def _flow():
    db = DB(":memory:")
    mem = Memory(db)
    areas = Areas(CFG, db)
    vault = Vault(TMP / "vault", "Dasha")
    engine = FakeEngine()
    llm = FakeLLM()
    lib = library.Library({"library": {"chunk_tokens": 200, "overlap": 0, "embed_batch": 8}}, db, FakeEmbedder(),
                          vault, engine)
    brain = Brain(CFG, db, llm, mem, FakeEmbedder(), vault, engine, areas, lib)
    lib.on_document.append(brain.on_document)
    robot = mem.entity("Robot de riego", "work")
    mem.add_fact({"entity_id": robot["id"], "text": "He is building a robot that waters the plants.", "category": "goal"},
                 fake_embed("He is building a robot that waters the plants."))

    def say(text):
        return db.add_message("user", "web", text)

    # an idea, linked to the robot (named in the message), the made-up id dropped
    notes = await brain.before_turn([say("apunta esta idea: un sensor de humedad para el Robot de riego")])
    items = brain.items()
    assert len(items) == 1 and items[0]["kind"] == "idea" and items[0]["areas"] == ["projects"], items
    idea = items[0]
    assert idea["tags"] == ["robotica", "riego-automático", "x"], idea["tags"]
    links = brain.links_of(idea["id"])
    assert [(lk["target"], lk["label"]) for lk in links] == [(f"entity:{robot['id']}", "podría servir para el robot")], links
    assert any(n.startswith("the app saved this in his second brain as an idea") and "Robot de riego" in n
               for n in notes), notes
    assert idea["resurface_at"] and idea["resurface_at"] > idea["created"]
    note = vault.read(idea["note_path"])
    assert idea["note_path"].startswith("Brain/Ideas/") and "- With: Robot de riego — podría servir" in note and \
        "#robotica" in note, note
    assert any(e["type"] == "item" and e["item"]["id"] == idea["id"] for e in engine.events)
    assert mem.search_text("sensor humedad", kinds=("brain",)), "items are searchable with the memories"

    # he corrects the kind: the note moves
    notes = await brain.before_turn([say("no, no es una idea, es para después")])
    fixed = brain.get(idea["id"])
    assert fixed["kind"] == "for_later" and fixed["note_path"].startswith("Brain/For later/"), fixed
    assert not vault.path(idea["note_path"]).exists() and vault.read(fixed["note_path"]), "the note moved"
    assert notes and "changed what he just saved" in notes[0], notes

    # a bare reel link: a saved video
    await brain.before_turn([say("https://www.instagram.com/reel/ABC123/")])
    reel = brain.items()[0]
    assert reel["kind"] == "saved" and reel["format"] == "video" and reel["url"].endswith("/reel/ABC123/"), reel

    # the same reel again from the chat: no second item
    n_before = len(brain.items(status="all"))
    notes = await brain.before_turn([say("https://instagram.com/reel/ABC123?igsh=xyz")])
    assert len(brain.items(status="all")) == n_before and "already saved this link" in notes[0], notes

    # a like: a fact with polarity, not an item
    n_items = len(brain.items(status="all"))
    notes = await brain.before_turn([say("guarda que me encanta el ramen picante")])
    taste = [f for f in mem.facts() if f.get("polarity") == "like"]
    assert len(taste) == 1 and taste[0]["category"] == "preference" and len(brain.items(status="all")) == n_items, taste
    assert "among his likes" in notes[0], notes

    # "olvida el reel de cocina": gone at once, with its note
    reel_note = brain.get(reel["id"])["note_path"]
    notes = await brain.before_turn([say("olvida el reel de cocina")])
    assert brain.get(reel["id"], trashed=True) is None and not vault.path(reel_note).exists(), "forgotten"
    assert any("gone from his saved things" in n for n in notes) and "🧹 Forgot from your saved things" in engine.said()[-1]
    assert not mem.search_text("cocina", kinds=("brain",))
    n_items = len(brain.items(status="all"))

    # ordinary chat: nothing; a failed capture with a clear cue: she says so
    assert not await brain.before_turn([say("hoy hizo mucho calor")]) or len(brain.items()) == n_items
    llm.fail = True
    notes = await brain.before_turn([say("guarda esto por favor")])
    assert any("couldn't save it" in n for n in notes), notes
    llm.fail = False

    # recall: a saved item comes up when the talk is about it, then not again right away
    notes = await brain._recall("estaba pensando en el sensor de humedad para que el robot sepa cuándo regar")
    assert any("something he saved" in n and "Sensor de humedad" in n for n in notes), notes
    assert not [n for n in await brain._recall("estaba pensando en el sensor de humedad para que el robot sepa cuándo "
                                               "regar") if "Sensor de humedad" in n], "not twice in a row"
    asked = await brain._recall("¿qué tengo guardado sobre el sensor?")
    assert any("Sensor de humedad" in n for n in asked), "when he asks, it comes up again"

    # a library document: a saved item, filed in the background
    doc, _ = await lib.add(("# Riego automático\n\n" + PARA * 3).encode(), "riego.md", "web", "web")
    item = brain.by_doc(doc["id"])
    assert item and item["kind"] == "saved" and not item["filed"] and item["format"] == "text", item
    await asyncio.gather(*lib.tasks)
    assert brain.pending()
    await brain.run()
    item = brain.get(item["id"])
    assert item["filed"] and item["areas"] == ["projects"] and item["tags"] == ["riego", "arduino-uno"], item
    assert item["title"] == "Riego automático" and not brain.pending(), item
    assert any(lk["target"] == f"entity:{robot['id']}" for lk in brain.links_of(item["id"]))
    pub = brain.public(item)
    assert pub["doc"]["state"] in ("indexing", "ready") and re.search(r"[+-]\d\d:\d\d$", pub["created_at"]), pub

    # a link he hid is not made again
    db.conn.execute("INSERT INTO graph_links(source, target, kind, origin, action, created, updated) "
                    "VALUES (?, ?, 'about', 'user', 'hide', 0, 0)", (f"item:{item['id']}", f"entity:{robot['id']}"))
    brain.set_links(item["id"], [(f"entity:{robot['id']}", "again")])
    assert not brain.links_of(item["id"]), brain.links_of(item["id"])

    # documents from before SB1 get their item once
    db.conn.execute("DELETE FROM brain_items WHERE doc_id = ?", (doc["id"],))
    assert await brain.backfill_library() == 1 and await brain.backfill_library() == 0
    print("ok brain (idea + links, correction, reel, like as fact, no save, failure, recall, library, hidden, backfill)")


def test_brain():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_brain()
