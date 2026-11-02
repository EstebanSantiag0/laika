"""SB1 backfill (D22): mark the likes and dislikes among the facts Laika learned before SB1.

It applies the result after a database backup and writes a readable list to review afterwards (or
review the list first and apply later: the two steps are separate).
Only `polarity` changes (like / dislike / NULL); undo with
`UPDATE facts SET polarity = NULL WHERE id IN (...)` using the ids in the list.

1. plan: classify the active facts (12 per call, background slot) → a JSON plan + a Markdown list.
   Run it on a COPY of the database (it only reads).
2. apply: write the plan's polarities into a database (first the copy, then data/laika.db).

Run (host, from ~/laika), the same way as scripts/backfill_areas.py:
  docker run --rm --network laika_default -v "$PWD/app:/app:ro" -v "$PWD/scripts:/scripts:ro" \
    -v "$PWD/config.yaml:/cfg/config.yaml:ro" -v <scratch>:/work -e LAIKA_CONFIG=/cfg/config.yaml \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 \
    python /scripts/backfill_polarity.py plan /work/laika.db /work/polarity.json /work/polarity.md
  ... then: apply <db> /work/polarity.json
"""
import asyncio
import json
import pathlib
import re
import sys

from laika import config
from laika.db import DB
from laika.llm import LLM
from laika.memory import Memory

BATCH = 12
PROMPT = """Below are things Laika (an AI friend) remembers about {user}. For each one, say whether it \
is a clear taste of his: something he likes, loves or enjoys doing or having ("like"), or something he \
dislikes, hates or avoids ("dislike"). Everything else is "none": beliefs and views ("he thinks…", \
"he views…"), motivations, things he finds hard, feelings about Laika or about people, plans, facts \
about his life. When in doubt, "none"."""
SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "integer"}, "polarity": {"enum": ["like", "dislike", "none"]}},
    "required": ["id", "polarity"]}}}, "required": ["facts"]}
SKIP = ("laika_self", "us", "event")
# The model alone was noisy on real facts (a plain activity read as a like, a difficulty as a dislike), so a fact also needs taste words to be marked. Missing a taste is cheap
# (consolidation sets polarity from now on); a wrong one shows up in his likes list.
TASTE = re.compile(r"\b(?:likes?|loves?|enjoys?|prefers?|dislikes?|hates?|can'?t stand|boring|annoy\w*|"
                   r"interested in|fan of|favou?rite|passion)\b", re.I)


async def plan(db_path, out_json, out_md):
    cfg = config.load()
    db = DB(pathlib.Path(db_path))
    Memory(db)
    llm = LLM(cfg)
    facts = [dict(r) for r in db.conn.execute(
        "SELECT id, text, category FROM facts WHERE status = 'active' AND polarity IS NULL ORDER BY id")
        if r["category"] not in SKIP]
    out = {}
    for i in range(0, len(facts), BATCH):
        part = facts[i:i + BATCH]
        lines = "\n".join(f"{f['id']}: {f['text']}" for f in part)
        res = await llm.chat([{"role": "system", "content": config.personal(PROMPT, cfg)}, {"role": "user", "content": lines}],
                             cfg["llm"]["background_slot"], json_schema=SCHEMA, temperature=0.0, max_tokens=600)
        ids = {f["id"] for f in part}
        for r in res.get("facts", []):
            text = next((f["text"] for f in part if f["id"] == r.get("id")), "")
            if r.get("id") in ids and r.get("polarity") in ("like", "dislike") and TASTE.search(text):
                out[r["id"]] = r["polarity"]
        print(f"{min(i + BATCH, len(facts))}/{len(facts)} facts, {len(out)} tastes so far", flush=True)
    pathlib.Path(out_json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    by_id = {f["id"]: f for f in facts}
    md = [f"# Likes and dislikes backfill (SB1), {len(facts)} facts read", ""]
    for pol in ("like", "dislike"):
        md += [f"## {pol}s", ""] + [f"- #{fid}: {by_id[int(fid)]['text']}" for fid, p in out.items() if p == pol] + [""]
    pathlib.Path(out_md).write_text("\n".join(md), encoding="utf-8")
    await llm.client.aclose()


def apply(db_path, plan_json):
    db = DB(pathlib.Path(db_path))
    Memory(db)  # adds the polarity column if missing
    plan_ = json.loads(pathlib.Path(plan_json).read_text(encoding="utf-8"))
    n = 0
    for fid, pol in plan_.items():
        n += db.conn.execute("UPDATE facts SET polarity = ? WHERE id = ? AND polarity IS NULL", (pol, int(fid))).rowcount
    print(f"applied {n} polarities")


if __name__ == "__main__":
    if sys.argv[1] == "plan":
        asyncio.run(plan(*sys.argv[2:5]))
    else:
        apply(*sys.argv[2:4])
