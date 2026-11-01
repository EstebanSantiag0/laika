"""Life areas and time scope (W8.3) with a fake embedder: config sync, cleaning model answers, links,
derived entity/episode areas, time fields from the model, nightly archiving and stage checks, archived
facts in recall and in the graph.

Run: docker run --rm -v "$PWD/app:/app:ro" -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 \
       python -m tests.test_areas
"""
import asyncio
import datetime
import pathlib
import tempfile
import time
import zoneinfo

from laika.api import Api
from laika.areas import DEFAULT, Areas
from laika.consolidate import time_fields
from laika.db import DB
from laika.memory import Memory
from laika.nightly import Nightly
from laika.persona import Persona
from laika.recall import Recall
from laika.reminders import Reminders
from laika.vault import Vault
from tests.test_consolidate import MCFG, FakeEmbedder
from tests.test_memory import fake_embed
from tests.test_reminders import Engine

TZ = zoneinfo.ZoneInfo("Europe/Madrid")
TODAY = datetime.datetime.now(TZ).date()


def day(n):
    return (TODAY + datetime.timedelta(days=n)).isoformat()


async def main():
    db = DB(":memory:")
    mem = Memory(db)
    cfg = {**MCFG, "areas": DEFAULT}
    areas = Areas(cfg, db)
    assert [a["id"] for a in areas.list()][:3] == ["study", "projects", "finance"]
    assert areas.clean(["Idiomas", "languages", "study (Estudios)", "nope", "HEALTH"]) == ["languages", "study",
                                                                                         "health"]
    assert areas.schema()["items"]["enum"] == areas.ids()
    Areas({**MCFG, "areas": DEFAULT[:2] + [{"id": "music", "name": "Música"}]}, db)
    assert [a["id"] for a in areas.list()] == ["study", "projects", "music"], "removed ids are hidden"
    areas.sync(DEFAULT)

    tf = time_fields({"time_scope": "permanent", "valid_from": "2026-10-18", "text": "They are from Spain"}, TODAY)
    assert tf == {"time_scope": "permanent", "valid_from": None, "valid_to": None, "review_after": None}
    tf = time_fields({"time_scope": "stage", "valid_from": "2026-10-24", "text": "They are in Seville"}, TODAY)
    assert tf["time_scope"] == "stage" and tf["review_after"] == day(90) and tf["valid_from"] == "2026-10-24"
    tf = time_fields({"time_scope": "permanent", "category": "event", "text": "A meeting at 1pm on 2026-10-20."},
                     TODAY)
    assert tf["time_scope"] == "dated" and tf["valid_from"] == tf["valid_to"] == "2026-10-20"
    assert time_fields({"time_scope": "dated", "text": "no day"}, TODAY)["time_scope"] == "permanent"

    me = mem.entity("Dasha", "person")
    add = lambda text, **kw: mem.add_fact({"entity_id": me["id"], "text": text, **kw}, fake_embed(text))  # noqa
    russian = add("Dasha is studying Russian", time_scope="stage", review_after=day(-1), importance=0.8)
    party = add("Dasha went to a noisy band party", time_scope="dated", valid_from=day(-2), valid_to=day(-2),
                importance=0.6)
    exam = add("Dasha has a Russian exam", time_scope="dated", valid_from=day(3), valid_to=day(3))
    areas.set("fact", russian, ["languages", "study"])
    areas.set("fact", party, ["people"])
    areas.set("fact", exam, ["languages"])
    assert areas.of("fact", russian) == ["study", "languages"], "ordered like the config"
    assert areas.derive() == 1 and areas.of("entity", me["id"]) == ["study", "languages"], "top 2, config order"

    root = pathlib.Path(tempfile.mkdtemp())
    persona = Persona(MCFG, db, root)
    persona.ensure_files()
    nightly = Nightly(MCFG, db, None, mem, Vault(root), None, persona, areas)
    assert nightly.archive_facts() == 1
    assert mem.get_fact(party)["status"] == "archived" and mem.get_fact(party)["time_scope"] == "archived"
    assert mem.get_fact(exam)["status"] == "active", "a future date stays"
    assert nightly.review_stages() == 1 and nightly.review_stages() == 0, "once per review period"
    loop = mem.intentions()[0]
    assert loop["text"].endswith("Dasha is studying Russian") and loop["evidence"] == [f"fact:{russian}"]
    assert mem.get_fact(russian)["review_after"] == day(90)

    recall = Recall(MCFG, db, mem, FakeEmbedder(), persona)
    import laika.recall as R  # the bars are calibrated on the live store; three facts can't "stand out"
    bars = R.TAU, R.COS_MIN, R.STANDS_OUT
    R.TAU, R.COS_MIN, R.STANDS_OUT = -9, 0.3, -9
    picked = recall.pick("the noisy band party", fake_embed("the noisy band party"), time.time())
    R.TAU, R.COS_MIN, R.STANDS_OUT = bars
    texts = [p["text"] for p in picked]
    assert any(t.startswith("in the past: Dasha went to a noisy band party") for t in texts), texts

    rem = Reminders(MCFG, db, None, Engine([]))
    r = rem.add("Pay rent", time.time() + 60, areas=["finance"])
    g = Api(MCFG, db, mem, rem, areas).build_graph("all")
    by_id = {n["id"]: n for n in g["nodes"]}
    assert len(g["areas"]) == 10 and "area:finance" in by_id and by_id["area:finance"]["kind"] == "area"
    assert by_id[f"fact:{party}"]["time_scope"] == "archived" and by_id[f"fact:{russian}"]["time_scope"] == "stage"
    assert by_id[f"fact:{russian}"]["areas"] == ["study", "languages"]
    assert by_id[f"reminder:{r['id']}"]["areas"] == ["finance"]
    edges = {(e["source"], e["target"], e["kind"]) for e in g["edges"]}
    assert (f"reminder:{r['id']}", "area:finance", "in_area") in edges
    assert (f"entity:{me['id']}", "area:languages", "in_area") in edges
    assert all(e["source"] in by_id and e["target"] in by_id for e in g["edges"]), "no dangling edges"
    print("test_areas: all passed")


if __name__ == "__main__":
    asyncio.run(main())
