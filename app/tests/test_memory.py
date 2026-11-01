"""Memory store (P3.2): activation math, search, supersede, forget + tombstones, episodes, backup, vault.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_memory
"""
import hashlib
import math
import pathlib
import re
import shutil
import tempfile
import time

from laika.db import DB
from laika.memory import DAY, DIM, Memory, base_level
from laika.vault import Vault, fact_ids, fact_line


def fake_embed(text):
    """Bag-of-words hashed into DIM dims and normalized: cosine ≈ word overlap."""
    v = [0.0] * DIM
    for w in re.findall(r"[\w']+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


def test_activation():
    now = time.time()
    for age, expected in ((1 / 24, 1.59), (1, 0.0), (30, -1.70), (365, -2.95)):
        b = base_level(1, now - age * DAY, [now - age * DAY], now)
        assert abs(b - expected) < 0.02, (age, b)
    once = base_level(1, now - 30 * DAY, [now - 30 * DAY], now)
    often = base_level(5, now - 30 * DAY, [now - 2 * DAY, now - DAY, now - 3600], now)
    assert often > once + 1, (often, once)
    print("ok activation")


def test_store():
    db = DB(":memory:")
    mem = Memory(db)
    ana = mem.entity("Ana", "person", aliases=["Аня"])
    assert mem.entity("Аня")["id"] == ana["id"], "alias in another script should find the same person"
    uke = mem.add_fact({"entity_id": None, "text": "Dasha taught themself the harmonica from a website",
                        "category": "skill", "importance": 0.6}, fake_embed("Dasha taught themself the harmonica"))
    sis = mem.add_fact({"entity_id": ana["id"], "text": "Ana is Dasha's cousin and lives in Puebla",
                        "category": "relationship"}, fake_embed("Ana is Dasha's cousin and lives in Puebla"))
    hits = mem.search_vec(fake_embed("did I tell you about the harmonica"), k=2)
    assert hits[0][:2] == ("fact", uke), hits
    assert mem.search_text("harmonica website")[0][:2] == ("fact", uke)
    moved = mem.supersede(sis, {"entity_id": ana["id"], "text": "Ana lives in Monterrey now", "category": "relationship",
                                "valid_from": "2026-10"}, fake_embed("Ana lives in Monterrey now"))
    old = mem.get_fact(sis)
    assert old["status"] == "superseded" and old["superseded_by"] == moved and old["valid_to"] == "2026-10", old
    assert [f["id"] for f in mem.facts(ana["id"])] == [moved]
    before = mem.activation(mem.get_fact(uke))
    for _ in range(4):
        mem.use("fact", uke)
    after = mem.get_fact(uke)
    assert after["n_uses"] == 5 and len(after["recent_ts"]) == 3 and mem.activation(after) >= before
    low = mem.add_fact({"text": "Maybe they like jazz", "confidence": 0.4}, fake_embed("Maybe they like jazz"))
    assert mem.get_fact(low)["status"] == "tentative"
    print("ok store")
    return db, mem, uke


def test_forget(db, mem, uke):
    text = mem.get_fact(uke)["text"]
    mem.forget(fact_ids=[uke], texts=[text], embeddings=[fake_embed(text)])
    assert mem.get_fact(uke) is None
    assert not any(h[:2] == ("fact", uke) for h in mem.search_vec(fake_embed("harmonica website"), k=5))
    assert not mem.search_text("harmonica")
    assert mem.is_forgotten(text)
    paraphrase = text + " once"  # different digest, nearly the same vector
    assert mem.is_forgotten(paraphrase, fake_embed(paraphrase))
    assert not mem.is_forgotten("Ana lives in Monterrey now", fake_embed("Ana lives in Monterrey now"))
    print("ok forget")


def test_episodes(db, mem):
    ids = [db.add_message("user", "web", t)["id"] for t in ("I saw Ana today", "we talked about music")]
    ana = mem.entity("Ana")
    music = mem.entity("music", "topic")
    ep = mem.add_episode({"start_msg": ids[0], "end_msg": ids[-1], "start_ts": time.time() - 60, "end_ts": time.time(),
                          "title": "Ana and music", "narrative": "They told me they saw their cousin Ana and they talked "
                          "about music.", "entity_ids": [ana["id"], music["id"]], "arousal": 0.3},
                         fake_embed("Ana music cousin"), facts_text="Ana is their cousin.")
    assert db.conn.execute("SELECT episode_id FROM messages WHERE id = ?", (ids[1],)).fetchone()[0] == ep
    assert mem.get_entity(ana["id"])["episodes"] == 1 and mem.neighbours(ana["id"]) == {music["id"]: 1}
    assert mem.search_vec(fake_embed("my cousin Ana"), k=3, kinds=("episode",))[0][:2] == ("episode", ep)
    assert mem.last_consolidated_msg() == ids[-1]
    print("ok episodes")


def test_backup(mem):
    d = pathlib.Path(tempfile.mkdtemp())
    for day in range(3):
        target = mem.backup(d, keep=2)
        target.rename(d / f"laika-2026-10-0{day + 1}.db")
    mem.backup(d, keep=2)
    assert len(list(d.glob("laika-*.db"))) == 2
    print("ok backup")


def test_vault():
    root = pathlib.Path(tempfile.mkdtemp())
    v = Vault(root)
    facts = [{"id": 12, "text": "They taught themself the harmonica", "category": "skill"},
             {"id": 7, "text": "Lived in Puebla", "category": "identity", "status": "superseded", "valid_to": "2026-09"}]
    body = "They love languages.\n\n## Facts\n" + "\n".join(fact_line(f) for f in facts)
    assert v.write("People/Dasha.md", body, title="Dasha", kind="person", aliases=["Эстебан"])
    text = v.read("People/Dasha.md")
    assert text.startswith("---\ntype: person") and 'aliases: ["Эстебан"]' in text and "^f12" in text
    # Dasha adds their own text outside the markers: Laika's rewrite must keep it
    v.path("People/Dasha.md").write_text(text + "\nMy own note: call mom on Sunday.\n", encoding="utf-8")
    assert v.write("People/Dasha.md", body.replace("languages", "languages and philosophy"))
    text = v.read("People/Dasha.md")
    assert "My own note: call mom on Sunday." in text and "languages and philosophy" in text
    assert not v.write("People/Dasha.md", body.replace("languages", "languages and philosophy")), "no-op rewrite"
    assert fact_ids(v.section("People/Dasha.md")) == {12: "They taught themself the harmonica",
                                                        7: "(until 2026-09) Lived in Puebla"}
    prompt = v.prompt_text("People/Dasha.md")
    assert "^f" not in prompt and "---" not in prompt and "laika:start" not in prompt and "# Dasha" not in prompt
    if shutil.which("git"):
        assert v.commit("test: first") and not v.commit("test: nothing changed")
        assert (root / ".gitignore").read_text().startswith(".laika/backup/")
    print("ok vault")


def main():
    test_activation()
    db, mem, uke = test_store()
    test_forget(db, mem, uke)
    test_episodes(db, mem)
    test_backup(mem)
    test_vault()
    print("all memory tests passed")


if __name__ == "__main__":
    main()
