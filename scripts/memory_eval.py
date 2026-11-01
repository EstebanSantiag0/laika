"""Memory evaluation (PLAN P3.7): run the probe set through recall on a copy of the live database.

Probes live in the vault (vault/.laika/memory_probes.yaml): they are about Dasha, so they stay
out of git. A probe passes when one of its expected words appears in what recall offers, or in the
prompt snapshot she already carries; an `expect: []` probe passes only if nothing is offered.

Run (host):
  mkdir -p /tmp/memeval && python3 -c "import sqlite3; s=sqlite3.connect('file:data/laika.db?mode=ro', uri=True); \
d=sqlite3.connect('/tmp/memeval/laika.db'); s.backup(d)"
  docker run --rm --network laika_default -v "$PWD/app:/app" -v "$PWD/scripts:/scripts:ro" \
    -v "$PWD/vault:/vault:ro" -v "$PWD/config.yaml:/config.yaml:ro" -v /tmp/memeval:/eval \
    -e LAIKA_CONFIG=/config.yaml -e PYTHONPATH=/app -w /app local/laika-app:0.1 python /scripts/memory_eval.py
"""
import asyncio
import pathlib
import time

import yaml

from laika import config
from laika.db import DB
from laika.embed import Embedder
from laika.memory import Memory
from laika.persona import Persona
from laika.recall import Recall


async def main():
    cfg = config.load()
    cfg["embed"]["url"] = "http://embed:8080"
    db = DB(pathlib.Path("/eval/laika.db"))
    # This copy measures retrieval, not etiquette: forget what was already offered in the live chat
    # ("once is enough" would hide those memories; it made the harmonica probe fail on 2026-10-20).
    db.conn.execute("DELETE FROM injections")
    mem = Memory(db)
    recall = Recall(cfg, db, mem, Embedder(cfg), Persona(cfg, db, pathlib.Path("/vault")))
    probes = yaml.safe_load(pathlib.Path("/vault/.laika/memory_probes.yaml").read_text(encoding="utf-8"))
    snapshot = (db.get_setting("memory_prompt") or "").lower()
    results = {}
    for p in probes:
        picked, t0 = [], time.time()
        if recall.gate(p["q"]):
            picked = recall.pick(p["q"], await recall.embedder.one(p["q"], query=True), time.time())
        offered = " ".join(x["text"] for x in picked).lower()
        if p["expect"]:
            ok = any(w.lower() in offered or w.lower() in snapshot for w in p["expect"])
        else:
            ok = not picked
        results.setdefault(p["category"], []).append(ok)
        print(f"{'PASS' if ok else 'FAIL'} [{p['category']}] {p['q']}  ->  "
              f"{[x['text'][:60] for x in picked] or '(nothing)'}  {time.time() - t0:.2f}s")
    total = sum(sum(v) for v in results.values()), sum(len(v) for v in results.values())
    print("\n" + ", ".join(f"{k} {sum(v)}/{len(v)}" for k, v in results.items()) + f"; total {total[0]}/{total[1]}")


asyncio.run(main())
