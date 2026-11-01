"""W8.3 backfill: give the facts Laika learned before W8.3 their life areas and time scope.

Two steps, so Dasha sees the result before anything changes:
1. plan: classify every active fact with the model (12 per call, background slot) and write a plan
   file plus a readable sample. Run it on a COPY of the database (it only reads the facts).
2. apply: write a reviewed plan into a database: area links, time_scope, valid_from/valid_to,
   review_after for "stage" facts, then the nightly rule (past dated facts → archived, never
   deleted) and the entity/episode areas. Back up the live database first.

Run (host, from ~/laika):
  python3 -c "import sqlite3; s=sqlite3.connect('file:data/laika.db?mode=ro', uri=True); \
d=sqlite3.connect('<scratch>/laika.db'); s.backup(d)"
  docker run --rm --network laika_default -v "$PWD/app:/app:ro" -v "$PWD/scripts:/scripts:ro" \
    -v "$PWD/config.yaml:/cfg/config.yaml:ro" -v <scratch>:/work -e LAIKA_CONFIG=/cfg/config.yaml \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 \
    python /scripts/backfill_areas.py plan /work/laika.db /work/plan.json
  ... the same with: apply /work/laika.db /work/plan.json   (first on the copy, then on data/laika.db)
"""
import asyncio
import collections
import datetime
import json
import pathlib
import sys
import zoneinfo

from laika import config
from laika.areas import Areas
from laika.consolidate import time_fields
from laika.db import DB
from laika.llm import LLM
from laika.memory import Memory

BATCH = 12
PROMPT = """You sort what Laika (an AI friend) remembers about Dasha, a Spanish student, into the parts \
of his life, and say how long each memory stays true. Today is {today}. Each fact shows the day it was \
learned. For every fact give:
- "areas": at most 2 of: {areas}. An empty list if none fits (e.g. things about Laika and him as friends).
- "time_scope": "permanent" (lasting: who he is, his tastes, his people, his past), "stage" (true for \
this period of his life and likely to change: looking for internships, living somewhere for now, \
preparing an exam, a current project), or "dated" (an event or a deadline on a day).
- "valid_from" / "valid_to": YYYY-MM-DD when the fact names or implies a day or a period (an event on \
2026-10-25: both that day; "this week": that week), else null. Use the day it was learned for "today", \
"tonight", "tomorrow"."""
SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "integer"}, "areas": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
    "time_scope": {"enum": ["permanent", "stage", "dated"]},
    "valid_from": {"type": ["string", "null"]}, "valid_to": {"type": ["string", "null"]}},
    "required": ["id", "areas", "time_scope", "valid_from", "valid_to"]}}}, "required": ["facts"]}


async def plan(db_path, out_path):
    cfg = config.load()
    db = DB(pathlib.Path(db_path))
    Memory(db)
    areas = Areas(cfg, db)
    llm = LLM(cfg)
    tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
    today = datetime.datetime.now(tz).date()
    facts = [dict(r) for r in db.conn.execute("SELECT id, text, category, recorded_at FROM facts "
                                              "WHERE status = 'active' ORDER BY id")]
    system = PROMPT.format(today=today.isoformat(), areas=areas.prompt_list())
    schema = json.loads(json.dumps(SCHEMA))
    schema["properties"]["facts"]["items"]["properties"]["areas"] = areas.schema()
    result = {}
    for i in range(0, len(facts), BATCH):
        part = facts[i:i + BATCH]
        lines = "\n".join(f"{f['id']}: ({datetime.datetime.fromtimestamp(f['recorded_at'], tz):%Y-%m-%d}, "
                          f"{f['category']}) {f['text']}" for f in part)
        out = await llm.chat([{"role": "system", "content": system}, {"role": "user", "content": lines}],
                             cfg["llm"]["background_slot"], json_schema=schema, temperature=0.0, max_tokens=1500)
        for x in out["facts"]:
            if x["id"] in {f["id"] for f in part}:
                f = next(f for f in part if f["id"] == x["id"])
                result[x["id"]] = {"areas": areas.clean(x["areas"]), **time_fields(
                    {**x, "text": f["text"], "category": f["category"]}, today,
                    cfg["memory"].get("stage_review_days", 90))}
        print(f"classified {min(i + BATCH, len(facts))}/{len(facts)}", flush=True)
    missing = [f["id"] for f in facts if f["id"] not in result]
    by_id = {f["id"]: f for f in facts}
    for fid, r in result.items():
        end = r["valid_to"] or r["valid_from"]
        r["text"] = by_id[fid]["text"]
        r["would_archive"] = r["time_scope"] == "dated" and bool(end) and end < today.isoformat()
    pathlib.Path(out_path).write_text(json.dumps({"today": today.isoformat(), "facts": result, "missing": missing},
                                                 ensure_ascii=False, indent=1))
    report(result, missing, areas)


def report(result, missing, areas):
    names = {a["id"]: a["name"] for a in areas.list()}
    per_area = collections.Counter(a for r in result.values() for a in r["areas"])
    scopes = collections.Counter(r["time_scope"] for r in result.values())
    print(f"\n{len(result)} facts classified, {len(missing)} missing {missing or ''}")
    print("areas:", ", ".join(f"{names[a]} {n}" for a, n in per_area.most_common()),
          f"| no area {sum(1 for r in result.values() if not r['areas'])}")
    print("time scope:", dict(scopes), "| would be archived now (dated, day passed):",
          sum(r["would_archive"] for r in result.values()))


async def apply(db_path, plan_path):
    cfg = config.load()
    db = DB(pathlib.Path(db_path))
    Memory(db)
    areas = Areas(cfg, db)
    data = json.loads(pathlib.Path(plan_path).read_text())
    db.conn.execute("BEGIN")
    try:
        n = 0
        for fid, r in data["facts"].items():
            row = db.conn.execute("SELECT status FROM facts WHERE id = ?", (int(fid),)).fetchone()
            if not row or row[0] != "active":
                continue
            areas.set("fact", int(fid), r["areas"])
            db.conn.execute("UPDATE facts SET time_scope = ?, valid_from = COALESCE(?, valid_from), "
                            "valid_to = COALESCE(?, valid_to), review_after = ? WHERE id = ?",
                            (r["time_scope"], r["valid_from"], r["valid_to"], r["review_after"], int(fid)))
            n += 1
        today = data["today"]
        archived = db.conn.execute(
            "UPDATE facts SET status = 'archived', time_scope = 'archived', valid_to = COALESCE(valid_to, valid_from) "
            "WHERE status = 'active' AND time_scope = 'dated' AND COALESCE(valid_to, valid_from) < ?",
            (today,)).rowcount
        derived = areas.derive()
        db.conn.execute("COMMIT")
    except Exception:
        db.conn.execute("ROLLBACK")
        raise
    print(f"applied to {n} facts; archived {archived} past dated facts; areas derived for {derived} "
          f"entities/episodes")


if __name__ == "__main__":
    cmd, db_path, other = sys.argv[1:4]
    asyncio.run({"plan": plan, "apply": apply}[cmd](db_path, other))
