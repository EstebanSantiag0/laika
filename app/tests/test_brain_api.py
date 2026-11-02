"""SB7 + SB7b: the second brain's API (docs/PLAN-second-brain.md §5) with fakes, over HTTP.

Items (file / url / text, duplicates, 413/415/422, list paging and filters, PATCH, resurface, file,
page PNG), search, likes, tags, and graph editing: PATCH nodes (409 on stale edits, 422), DELETE with
cascades and dry_run, links (add, duplicate, relabel, remove, hide, restore), the trash, tag rename
and merge with undo, and the purge after 30 days. Trashed things leave the graph and recall.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_brain_api
"""
import asyncio
import os
import pathlib
import tempfile
import time
import urllib.parse

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from laika import library  # noqa: E402
from laika.api import Api  # noqa: E402
from laika.api_brain import BrainApi, install_errors, normalize_url  # noqa: E402
from laika.areas import Areas  # noqa: E402
from laika.brain import Brain  # noqa: E402
from laika.consolidate import Consolidator  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.edits import Edits  # noqa: E402
from laika.graph_edit import GraphEdit  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.reminders import Reminders  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_brain import CFG, FakeLLM  # noqa: E402
from tests.test_consolidate import FakeEmbedder  # noqa: E402
from tests.test_library import PARA, FakeEngine, make_pdf  # noqa: E402
from tests.test_memory import fake_embed  # noqa: E402
from tests.test_reminders import Engine  # noqa: E402

CFG = {**CFG, "library": {"chunk_tokens": 200, "overlap": 0, "embed_batch": 8, "max_upload_mb": 1}}


def enc(node_id):
    return urllib.parse.quote(node_id, safe="")


def setup():
    db = DB(":memory:")
    mem = Memory(db)
    areas = Areas(CFG, db)
    vault = Vault(TMP / "vault", "Dasha")
    engine = FakeEngine()
    lib = library.Library(CFG, db, FakeEmbedder(), vault, engine)
    brain = Brain(CFG, db, FakeLLM(), mem, FakeEmbedder(), vault, engine, areas, lib)
    lib.on_document.append(brain.on_document)
    cons = Consolidator(CFG, db, None, mem, FakeEmbedder(), vault, areas)
    rem = Reminders(CFG, db, None, Engine([]))
    edit = GraphEdit(CFG, db, mem, FakeEmbedder(), vault, brain, rem, areas, lib, cons)
    brain.edit = edit  # as in main.py
    lib.restore_trashed = brain.restore_doc
    graph = Api(CFG, db, mem, rem, areas, brain, edit)
    app = FastAPI()
    install_errors(app)
    app.include_router(graph.router)
    app.include_router(BrainApi(CFG, db, mem, FakeEmbedder(), brain, lib, edit, graph).router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://laika")
    return client, db, mem, brain, lib, edit, rem, vault


async def main():
    client, db, mem, brain, lib, edit, rem, vault = setup()
    me = mem.entity("Dasha", "person")
    robot = mem.entity("Robot de riego", "work")
    goal = mem.add_fact({"entity_id": robot["id"], "text": "He is building a robot that waters the plants.",
                         "category": "goal", "importance": 0.8}, fake_embed("He is building a robot that waters the plants."))
    taco = mem.add_fact({"entity_id": me["id"], "text": "He loves spicy ramen.", "category": "preference",
                         "polarity": "like"}, fake_embed("He loves spicy ramen."))
    crowd = mem.add_fact({"entity_id": me["id"], "text": "He dislikes cold coffee.", "category": "preference",
                          "polarity": "dislike"}, fake_embed("He dislikes cold coffee."))
    cons_render = edit.consolidator.render_entity
    cons_render(robot["id"])

    # -- items: text, url, file ---------------------------------------------------------------------------
    r = await client.post("/api/items", json={"text": "idea: un sensor de humedad para el Robot de riego"})
    assert r.status_code == 201, r.text
    idea = r.json()["item"]
    assert idea["kind"] == "idea" and idea["areas"] == ["projects"] and idea["created_at"] and idea["resurface_at"], idea
    assert idea["text"] == "idea: un sensor de humedad para el Robot de riego", "his words are kept (SB8)"
    r = await client.post("/api/items", json={"url": "https://www.example.com/article/?utm_source=x#top", "kind": "for_later",
                                              "tags": ["leer"]})
    link = r.json()["item"]
    assert r.status_code == 201 and link["kind"] == "for_later" and link["format"] == "link" and link["tags"] == ["leer"]
    r = await client.post("/api/items", json={"url": "https://example.com/article"})
    assert r.status_code == 200 and r.json()["duplicate"] and r.json()["item"]["id"] == link["id"], r.text
    assert normalize_url("https://WWW.Example.com/a/?igsh=1&b=2") == "https://example.com/a?b=2"
    lines = [s.strip() + "." for s in PARA.split(".") if s.strip()] * 4
    pdf = make_pdf([["Page one about the river."] + lines, ["Page two about the sea."] + lines])
    r = await client.post("/api/items", files={"file": ("river.pdf", pdf, "application/pdf")},
                          data={"kind": "saved", "areas": "study", "tags": "rio, mar", "note": "para el curso"})
    assert r.status_code == 201, r.text
    doc_item = r.json()["item"]
    assert doc_item["format"] == "pdf" and doc_item["areas"] == ["study"] and doc_item["tags"] == ["rio", "mar"] and \
        doc_item["note"] == "para el curso" and doc_item["doc"]["state"] == "queued", doc_item
    r = await client.post("/api/items", files={"file": ("river.pdf", pdf, "application/pdf")})
    assert r.status_code == 200 and r.json()["duplicate"] and r.json()["item"]["id"] == doc_item["id"]
    r = await client.post("/api/items", files={"file": ("x.mobi", b"x", "application/octet-stream")})
    assert r.status_code == 415 and "error" in r.json(), r.text
    r = await client.post("/api/items", files={"file": ("big.txt", b"x" * (2 * 2**20), "text/plain")})
    assert r.status_code == 413 and r.json()["max_mb"] == 1, r.text
    r = await client.post("/api/items", json={"text": "x", "kind": "nope"})
    assert r.status_code == 422 and r.json()["field"] == "kind", r.text
    r = await client.post("/api/items", json={})
    assert r.status_code == 400 and r.json()["error"]
    await asyncio.gather(*lib.tasks)
    await lib.run()
    await brain.run()
    got = (await client.get(f"/api/items/{doc_item['id']}")).json()["item"]
    assert got["doc"]["state"] == "ready" and got["doc"]["progress"]["chunks"]["todo"] == 0, got
    assert got["tags"] == ["rio", "mar"] and got["areas"] == ["study"], "what he set wins over the filing job"

    # file and page PNG
    r = await client.get(f"/api/items/{doc_item['id']}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content == pdf
    r = await client.get(f"/api/items/{doc_item['id']}/file", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10, r.status_code
    r = await client.get(f"/api/items/{doc_item['id']}/pages/2.png?w=300")
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG") and r.headers["content-type"] == "image/png"
    assert (await client.get(f"/api/items/{doc_item['id']}/pages/9.png")).status_code == 404
    assert (await client.get(f"/api/items/{idea['id']}/file")).status_code == 404

    # list: paging, filters
    page1 = (await client.get("/api/items?limit=2")).json()
    assert [i["id"] for i in page1["items"]] == [doc_item["id"], link["id"]] and page1["next_before"] == link["id"]
    page2 = (await client.get(f"/api/items?limit=2&before={page1['next_before']}")).json()
    assert [i["id"] for i in page2["items"]] == [idea["id"]] and page2["next_before"] is None
    assert [i["id"] for i in (await client.get("/api/items?kind=idea")).json()["items"]] == [idea["id"]]
    assert [i["id"] for i in (await client.get("/api/items?area=study&tag=rio")).json()["items"]] == [doc_item["id"]]
    assert [i["id"] for i in (await client.get("/api/items?q=sensor humedad")).json()["items"]] == [idea["id"]]
    assert (await client.get("/api/items?limit=500")).status_code == 400

    # PATCH an item; resurface actions
    r = await client.patch(f"/api/items/{link['id']}", json={"title": "Un artículo", "note": "leer el finde"})
    assert r.status_code == 200 and r.json()["item"]["title"] == "Un artículo"
    assert (await client.patch(f"/api/items/{link['id']}", json={"kind": "x"})).json()["field"] == "kind"
    r = await client.post(f"/api/items/{idea['id']}/resurface", json={"action": "later"})
    later = r.json()["item"]["resurface_at"]
    assert later and later > idea["resurface_at"], (later, idea["resurface_at"])
    db.conn.execute("UPDATE brain_items SET resurface_at = ? WHERE id = ?", (time.time() - 1, link["id"]))
    assert [i["id"] for i in (await client.get("/api/resurface")).json()["items"]] == [link["id"]]
    await client.post(f"/api/items/{link['id']}/resurface", json={"action": "dismiss"})
    assert (await client.get("/api/resurface")).json()["items"] == []

    # search: items and library chunks, with snippets
    res = (await client.get("/api/search?q=river")).json()["results"]
    assert res and res[0]["doc_id"] == got["doc"]["id"] and res[0]["item_id"] == doc_item["id"] and \
        "river" in res[0]["snippet"].lower() and res[0]["kind"] == "saved", res[:2]
    assert (await client.get("/api/search?q=")).status_code == 400

    # tags, likes
    tags = {t["tag"]: t["count"] for t in (await client.get("/api/tags")).json()}
    assert tags.get("rio") == 1 and tags.get("leer") == 1, tags
    likes = (await client.get("/api/likes")).json()
    assert [f["id"] for f in likes["likes"]] == [f"fact:{taco}"] and [f["id"] for f in likes["dislikes"]] == [f"fact:{crowd}"]
    r = await client.patch(f"/api/likes/{crowd}", json={"status": "archived"})
    assert r.status_code == 200 and not (await client.get("/api/likes")).json()["dislikes"], "archived ones never listed"
    assert (await client.patch(f"/api/likes/{taco}", json={"polarity": "meh"})).json()["field"] == "polarity"
    assert (await client.patch("/api/likes/9999", json={"polarity": "like"})).status_code == 404

    # -- the graph: items, tags, links with ids --------------------------------------------------------------
    g = (await client.get("/api/graph")).json()
    by = {n["id"]: n for n in g["nodes"]}
    assert by[f"item:{idea['id']}"]["kind"] == "idea" and "tag:rio" in by and by["tag:rio"]["kind"] == "tag"
    assert by[f"fact:{goal}"]["editable"] and by[f"fact:{goal}"]["updated_at"] and not by["area:study"]["deletable"]
    about = [e for e in g["edges"] if e["source"] == f"item:{idea['id']}" and e["kind"] == "about"]
    assert about and about[0]["id"].startswith("about:") and about[0]["origin"] == "auto" and about[0]["label"], about
    assert all(e["id"] and e["source"] in by and e["target"] in by for e in g["edges"])

    # PATCH nodes: fact text, stale check, bad field, entity rename, area, reminder
    fnode = by[f"fact:{goal}"]
    r = await client.patch(f"/api/nodes/{enc(f'fact:{goal}')}",
                           json={"text": "He builds a robot that waters plants.", "base_updated_at": fnode["updated_at"]})
    assert r.status_code == 200 and r.json()["node"]["text"] == "He builds a robot that waters plants." and \
        any(e["kind"] == "about" for e in r.json()["edges"]), r.text
    note = vault.read(mem.get_entity(robot["id"])["note_path"])
    assert "He builds a robot that waters plants." in note, note
    edits = Edits(db, mem, FakeEmbedder(), vault)
    edits.brain = brain
    assert await edits.scan() == 0, "the app's edit is not read back as his Obsidian edit"
    db.conn.execute("UPDATE facts SET updated = updated + 5 WHERE id = ?", (goal,))
    r = await client.patch(f"/api/nodes/{enc(f'fact:{goal}')}", json={"text": "x y z", "base_updated_at": fnode["updated_at"]})
    assert r.status_code == 409 and r.json()["error"], r.text
    assert (await client.patch(f"/api/nodes/{enc(f'fact:{goal}')}", json={"category": "x"})).json()["field"] == "category"
    assert (await client.patch(f"/api/nodes/{enc(f'fact:{goal}')}", json={"areas": ["nope"]})).status_code == 422
    r = await client.patch(f"/api/nodes/{enc('area:study')}", json={"name": "Escuela", "color": "#123456"})
    assert r.json()["node"]["label"] == "Escuela" and r.json()["node"]["color"] == "#123456"
    assert (await client.patch(f"/api/nodes/{enc('area:study')}", json={"color": "red"})).status_code == 422
    r1 = rem.add("Pagar la renta", time.time() + 3600)
    r = await client.patch(f"/api/nodes/{enc(f'reminder:{r1['id']}')}", json={"text": "Pagar renta",
                                                                               "due_at": "2030-01-02T10:00:00-06:00"})
    assert r.status_code == 200 and rem.get(r1["id"])["text"] == "Pagar renta"
    assert (await client.patch("/api/nodes/nope:1", json={})).status_code == 404

    # links: add, duplicate, relabel, self-loop, remove (trash), hide an automatic one, restore
    r = await client.post("/api/edges", json={"source": f"item:{link['id']}", "target": f"entity:{robot['id']}",
                                              "label": "para el robot"})
    assert r.status_code == 201, r.text
    lk = r.json()["edge"]
    assert lk["id"].startswith("link:") and lk["origin"] == "user"
    r = await client.post("/api/edges", json={"source": f"entity:{robot['id']}", "target": f"item:{link['id']}"})
    assert r.status_code == 200 and r.json()["duplicate"]
    assert (await client.post("/api/edges", json={"source": "fact:1", "target": "fact:1"})).status_code == 422
    assert (await client.post("/api/edges", json={"source": "fact:999", "target": "fact:1"})).json()["field"] == "source"
    r = await client.patch(f"/api/edges/{enc(lk['id'])}", json={"label": "útil"})
    assert r.json()["edge"]["label"] == "útil"
    assert (await client.delete(f"/api/edges/{enc(lk['id'])}")).json() == {"edges_removed": [lk["id"]]}
    auto = about[0]["id"]
    await client.delete(f"/api/edges/{enc(auto)}")
    await brain.update(idea["id"], text="otra vez")  # filing again must not bring the hidden link back
    brain.set_links(idea["id"], [(about[0]["target"], "again")])
    g = (await client.get("/api/graph")).json()
    ids_now = {e["id"] for e in g["edges"]}
    assert lk["id"] not in ids_now and auto not in ids_now, "removed and hidden"
    trash = (await client.get("/api/trash")).json()
    hidden_ids = {e["id"] for e in trash["hidden_edges"]}
    assert lk["id"] in hidden_ids and auto in hidden_ids, trash["hidden_edges"]
    await client.post(f"/api/trash/edges/{enc(auto)}/restore")
    await client.post(f"/api/trash/edges/{enc(lk['id'])}/restore")
    ids_now = {e["id"] for e in (await client.get("/api/graph")).json()["edges"]}
    assert lk["id"] in ids_now and auto in ids_now
    sim = next((e for e in (await client.get("/api/graph")).json()["edges"] if e["kind"] == "related"), None)
    in_area = f"in_area:item:{doc_item['id']}:area:study"
    assert (await client.delete(f"/api/edges/{enc(in_area)}")).status_code == 200
    assert brain.get(doc_item["id"])["areas"] == [], "removing an in_area link edits the areas"
    tagged = f"tagged:item:{doc_item['id']}:tag:mar"
    await client.delete(f"/api/edges/{enc(tagged)}")
    assert brain.get(doc_item["id"])["tags"] == ["rio"]
    assert sim is None or (await client.delete(f"/api/edges/{enc(sim['id'])}")).status_code == 200

    # DELETE with cascades: an entity with its facts (dry run first), then restore all of it
    r = await client.delete(f"/api/nodes/{enc(f'entity:{robot['id']}')}?with_facts=true&dry_run=true")
    assert r.json()["trashed"] == [f"entity:{robot['id']}", f"fact:{goal}"] and lk["id"] in r.json()["edges_removed"]
    assert mem.get_entity(robot["id"]).get("trashed_at") is None, "dry run changes nothing"
    r = await client.delete(f"/api/nodes/{enc(f'entity:{robot['id']}')}?with_facts=true")
    assert r.status_code == 200 and mem.get_fact(goal)["status"] == "trashed"
    g = (await client.get("/api/graph")).json()
    ids_nodes = {n["id"] for n in g["nodes"]}
    assert f"entity:{robot['id']}" not in ids_nodes and f"fact:{goal}" not in ids_nodes
    assert not [e for e in g["edges"] if f"entity:{robot['id']}" in (e["source"], e["target"])]
    assert all(c["id"] != robot["id"] for c in mem.entities()), "hidden from recall's named entities"
    t = (await client.get("/api/trash")).json()["nodes"]
    assert t[0]["id"] == f"entity:{robot['id']}" and t[0]["purge_at"] and t[0]["with"] == [f"fact:{goal}"], t[0]
    r = await client.post(f"/api/trash/{enc(f'entity:{robot['id']}')}/restore")
    assert set(r.json()["restored"]) == {f"entity:{robot['id']}", f"fact:{goal}"}
    assert mem.get_fact(goal)["status"] == "active" and lk["id"] in {e["id"] for e in r.json()["edges"]}
    assert (await client.post(f"/api/trash/{enc(f'entity:{robot['id']}')}/restore")).status_code == 404
    assert (await client.delete(f"/api/nodes/{enc('area:study')}")).status_code == 409

    # an item with its document: to the trash together, back together
    d_id = got["doc"]["id"]
    await client.delete(f"/api/nodes/{enc(f'item:{doc_item['id']}')}")
    assert lib.document(d_id)["status"] == "trashed" and not [d for d in lib.searchable() if d["id"] == d_id]
    assert (await client.get(f"/api/items/{doc_item['id']}")).status_code == 404
    await client.post(f"/api/trash/{enc(f'item:{doc_item['id']}')}/restore")
    assert lib.document(d_id)["status"] == "ready" and vault.read(brain.get(doc_item["id"])["note_path"])

    # SB8: the same file or link sent again while in the trash comes back (201, restored), never a hidden duplicate
    await client.delete(f"/api/nodes/{enc(f'item:{doc_item['id']}')}")
    r = await client.post("/api/items", files={"file": ("river.pdf", pdf, "application/pdf")})
    assert r.status_code == 201 and r.json()["restored"] and r.json()["item"]["id"] == doc_item["id"], r.text
    assert r.json()["item"]["doc"]["state"] == "ready" and lib.document(d_id)["status"] == "ready"
    await client.delete(f"/api/nodes/{enc(f'item:{link['id']}')}")
    r = await client.post("/api/items", json={"url": "https://example.com/article/"})
    assert r.status_code == 201 and r.json()["restored"] and r.json()["item"]["id"] == link["id"], r.text
    # SB8: base_updated_at is compared as an instant ("…Z" from a client that re-serialized it)
    import datetime
    node = (await client.get("/api/graph")).json()
    node = next(n for n in node["nodes"] if n["id"] == f"item:{link['id']}")
    z = datetime.datetime.fromisoformat(node["updated_at"]).astimezone(datetime.timezone.utc)
    z = z.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = await client.patch(f"/api/nodes/{enc(f'item:{link['id']}')}", json={"note": "z", "base_updated_at": z})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/api/nodes/{enc(f'item:{link['id']}')}", json={"note": "z", "base_updated_at": "ayer"})
    assert r.status_code == 422 and r.json()["field"] == "base_updated_at"

    # tag rename: dry run, merge, undo
    await brain.update(link["id"], tags=["leer", "rio"])
    r = await client.patch(f"/api/nodes/{enc('tag:leer')}?dry_run=true", json={"name": "rio"})
    assert r.json() == {"merges_into": "rio", "nodes_affected": 1}, r.json()
    r = await client.patch(f"/api/nodes/{enc('tag:leer')}", json={"name": "rio"})
    assert r.status_code == 200 and brain.get(link["id"])["tags"] == ["rio"], brain.get(link["id"])["tags"]
    t = (await client.get("/api/trash")).json()["nodes"]
    assert t[0]["id"] == "tag:leer" and t[0]["renamed_to"] == "rio"
    await client.post(f"/api/trash/{enc('tag:leer')}/restore")
    assert brain.get(link["id"])["tags"] == ["leer"], brain.get(link["id"])["tags"]

    # an episode in the trash leaves recall; the purge after 30 days forgets a fact for good
    ep = mem.add_episode({"start_msg": 1, "end_msg": 1, "start_ts": time.time(), "end_ts": time.time(),
                          "title": "Día", "narrative": "Hablamos.", "entity_ids": []}, fake_embed("hablamos"))
    await client.delete(f"/api/nodes/{enc(f'episode:{ep}')}")
    assert mem.get_episode(ep)["trashed_at"] and not mem.episodes_between(0, time.time() + 10)
    await client.delete(f"/api/nodes/{enc(f'fact:{taco}')}")
    assert await edit.purge(now=time.time() + 31 * 86400) >= 2
    assert mem.get_fact(taco) is None and mem.is_forgotten("He loves spicy ramen.") and mem.get_episode(ep) is None
    trashed_note = vault.path(f".trash/{brain.get(link['id'])['note_path']}")
    await client.delete(f"/api/nodes/{enc(f'item:{link['id']}')}")
    assert trashed_note.exists()
    await edit.purge(now=time.time() + 31 * 86400)
    assert brain.get(link["id"], trashed=True) is None and not trashed_note.exists(), "the trash copy goes too"

    # his Obsidian edit of an item note wins
    item = brain.get(idea["id"])
    path = vault.path(item["note_path"])
    path.write_text(path.read_text().replace("otra vez", "Un sensor capacitivo, no resistivo.")
                    .replace("- Saved:", "- Tags: #sensores\n- Saved:"), encoding="utf-8")
    assert await edits.apply(item["note_path"]) == 1
    item = brain.get(idea["id"])
    assert item["text"] == "Un sensor capacitivo, no resistivo." and item["tags"] == ["sensores"], item
    print("ok brain api (items, files, pages, list, resurface, search, likes, graph, edit, links, trash, tags, purge, vault)")


def test_brain_api():
    asyncio.run(main())


if __name__ == "__main__":
    test_brain_api()
