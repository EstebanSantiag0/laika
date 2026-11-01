"""Read-only API for laika-app (W8.2, docs/API.md) with a fake embedder: the graph's nodes and edges,
scopes, and the reminder endpoints.

Run: docker run --rm -v "$PWD/app:/app:ro" -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 \
       python -m tests.test_api
"""
import asyncio
import re
import time

import httpx
from fastapi import FastAPI

from laika.api import Api
from laika.db import DB
from laika.memory import Memory
from laika.reminders import Reminders
from tests.test_consolidate import MCFG
from tests.test_memory import fake_embed
from tests.test_reminders import Engine


def setup():
    db = DB(":memory:")
    mem = Memory(db)
    ana = mem.entity("Ana", "person")
    uke = mem.entity("harmonica", "thing")
    mem.link([ana["id"], uke["id"]])
    ep = mem.add_episode({"start_msg": 1, "end_msg": 2, "start_ts": time.time() - 60, "end_ts": time.time(),
                          "title": "Music talk", "narrative": "We talked about music.", "entity_ids": [ana["id"]]},
                         fake_embed("music talk"))
    f1 = mem.add_fact({"entity_id": ana["id"], "text": "Ana plays the harmonica every day", "episode_id": ep,
                       "importance": 0.7}, fake_embed("Ana plays the harmonica every day"))
    f2 = mem.add_fact({"entity_id": ana["id"], "text": "Ana plays the harmonica every single day"},
                      fake_embed("Ana plays the harmonica every single day"))
    f3 = mem.add_fact({"entity_id": uke["id"], "text": "Old fact"}, fake_embed("old fact"))
    db.conn.execute("UPDATE facts SET status = 'superseded' WHERE id = ?", (f3,))
    rem = Reminders(MCFG, db, None, Engine([]))
    r1 = rem.add("Call Ana", time.time() + 3600, source_message_id=5)
    r2 = rem.add("Done already", time.time() - 3600)
    db.conn.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (r2["id"],))
    app = FastAPI()
    app.include_router(Api(MCFG, db, mem, rem).router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://laika")
    return client, dict(ana=ana["id"], uke=uke["id"], ep=ep, f1=f1, f2=f2, f3=f3, r1=r1["id"], r2=r2["id"])


async def main():
    client, ids = setup()
    g = (await client.get("/api/graph")).json()
    assert set(g) == {"generated_at", "scope", "areas", "nodes", "edges"} and g["scope"] == "active"
    by_id = {n["id"]: n for n in g["nodes"]}
    assert f"fact:{ids['f3']}" not in by_id and f"reminder:{ids['r2']}" not in by_id, "active scope only"
    n = by_id[f"fact:{ids['f1']}"]
    assert set(n) >= {"id", "kind", "label", "text", "areas", "time_scope", "valid_from", "valid_to", "due_at",
                      "importance", "created_at"}
    assert n["kind"] == "fact" and n["time_scope"] == "permanent" and n["importance"] == 0.7
    assert re.search(r"[+-]\d\d:\d\d$", n["created_at"]), "ISO with the offset"
    assert by_id[f"reminder:{ids['r1']}"]["due_at"] and by_id[f"reminder:{ids['r1']}"]["time_scope"] == "reminder"
    assert by_id[f"entity:{ids['ana']}"]["subkind"] == "person"
    edges = {(e["source"], e["target"], e["kind"]) for e in g["edges"]}
    assert (f"fact:{ids['f1']}", f"entity:{ids['ana']}", "about") in edges
    assert (f"fact:{ids['f1']}", f"episode:{ids['ep']}", "in_episode") in edges
    assert (f"episode:{ids['ep']}", f"entity:{ids['ana']}", "about") in edges
    assert (f"entity:{ids['ana']}", f"entity:{ids['uke']}", "related") in edges
    assert (f"fact:{ids['f1']}", f"fact:{ids['f2']}", "similar") in edges
    assert all(e["source"] in by_id and e["target"] in by_id for e in g["edges"]), "no dangling edges"

    g = (await client.get("/api/graph?scope=all")).json()
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id[f"fact:{ids['f3']}"]["time_scope"] == "archived" and f"reminder:{ids['r2']}" in by_id
    assert (await client.get("/api/graph?scope=nope")).status_code == 422

    pending = (await client.get("/api/reminders")).json()
    assert [r["id"] for r in pending] == [ids["r1"]] and pending[0]["source_message_id"] == 5
    assert len((await client.get("/api/reminders?status=all")).json()) == 2
    r = (await client.post(f"/api/reminders/{ids['r1']}/snooze", json={"minutes": 15})).json()
    assert r["status"] == "snoozed" and r["snoozes"] == 1
    assert (await client.post(f"/api/reminders/{ids['r1']}/snooze", json={"minutes": 0})).status_code == 400
    assert (await client.post(f"/api/reminders/{ids['r1']}/snooze")).json()["snoozes"] == 2, "no body: default minutes"
    assert (await client.post(f"/api/reminders/{ids['r1']}/done")).json()["status"] == "done"
    assert (await client.post(f"/api/reminders/{ids['r1']}/snooze", json={"minutes": 5})).status_code == 404
    assert (await client.post("/api/reminders/999/done")).status_code == 404
    assert (await client.get("/api/reminders")).json() == []
    print("test_api: all passed")


if __name__ == "__main__":
    asyncio.run(main())
