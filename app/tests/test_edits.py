"""Dasha's Obsidian edits win (P3.5): deleted lines, reworded facts, Inbox ticks, the portrait,
and Laika's own rewrites changing nothing.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_edits
"""
import asyncio
import json
import pathlib
import tempfile

from laika.consolidate import Consolidator
from laika.db import DB
from laika.edits import Edits
from laika.memory import Memory
from laika.vault import Vault, fact_line
from tests.test_consolidate import MCFG, FakeEmbedder, FakeLLM
from tests.test_memory import fake_embed


async def main():
    db = DB(":memory:")
    mem = Memory(db)
    vault = Vault(pathlib.Path(tempfile.mkdtemp()), "Dasha")
    ana = mem.entity("Ana", "person")
    db.conn.execute("UPDATE entities SET episodes = 2 WHERE id = ?", (ana["id"],))
    add = lambda text, **kw: mem.add_fact({"entity_id": ana["id"], "text": text, **kw}, fake_embed(text))  # noqa
    cousin, violin, wrong = add("Ana is their cousin"), add("Ana plays the violin"), add("Ana lives in Lima")
    maybe = add("Ana might move to Spain", confidence=0.4)
    cons = Consolidator(MCFG, db, FakeLLM(), mem, FakeEmbedder(), vault)
    cons.render_entity(ana["id"])
    cons.render_inbox()
    edits = Edits(db, mem, FakeEmbedder(), vault)
    assert await edits.scan() == 0, "her own notes must not look like edits"
    # Consolidation updates a fact after the note was written; a restart's scan must keep the new wording
    # (on 2026-10-20 the stale line was taken for their edit and the fact was reverted).
    mem.update_fact(cousin, embedding=fake_embed("Ana is their older cousin"), text="Ana is their older cousin")
    assert await edits.scan() == 0 and mem.get_fact(cousin)["text"] == "Ana is their older cousin"
    data = vault.rendered()  # the index before 2026-10-20 kept only the ids: then rewording isn't detected
    data["People/Ana.md"] = sorted(int(k) for k in data["People/Ana.md"])
    vault.path(".laika/rendered.json").write_text(json.dumps(data))
    assert vault.rendered_lines("People/Ana.md") == {cousin: None, violin: None, wrong: None}
    assert await edits.scan() == 0 and mem.get_fact(cousin)["text"] == "Ana is their older cousin"
    cons.render_entity(ana["id"])

    path = vault.path("People/Ana.md")
    text = path.read_text()
    text = text.replace(fact_line(mem.get_fact(wrong)) + "\n", "")  # they delete a wrong fact
    text = text.replace("Ana plays the violin", "Ana plays the cello, not the violin")  # and corrects another
    text += "\nMy own note: Ana's birthday is in May.\n"
    path.write_text(text)
    assert await edits.apply("People/Ana.md") == 2
    assert mem.get_fact(wrong)["status"] == "rejected" and mem.is_forgotten("Ana lives in Lima")
    assert not any(ref == wrong for _, ref, _ in mem.search_vec(fake_embed("Ana lives in Lima"), k=5)), "still indexed"
    edited = mem.get_fact(violin)
    assert edited["text"] == "Ana plays the cello, not the violin" and edited["origin"] == "edited", edited
    assert mem.get_fact(cousin)["status"] == "active"
    cons.render_entity(ana["id"])  # Laika re-renders: their note text survives and nothing else changes
    assert "My own note: Ana's birthday is in May." in path.read_text()
    assert await edits.apply("People/Ana.md") == 0

    inbox = vault.path("Laika/Inbox.md")
    inbox.write_text(inbox.read_text().replace("- [ ] Ana might move", "- [x] Ana might move"))
    assert await edits.apply("Laika/Inbox.md") == 1 and mem.get_fact(maybe)["status"] == "active"

    db.set_setting("portrait", "Who they are: a language lover.")
    vault.write("Dasha/Profile.md", "Who they are: a language lover.\n\n## What I know\n- x ^f999", title="Dasha")
    assert await edits.apply("Dasha/Profile.md") == 0
    p = vault.path("Dasha/Profile.md")
    p.write_text(p.read_text().replace("a language lover.", "a language lover and a philosopher at heart."))
    assert await edits.apply("Dasha/Profile.md") == 1
    assert db.get_setting("portrait") == "Who they are: a language lover and a philosopher at heart."
    assert db.get_setting("portrait_edited") is True

    gone = vault.path("People/Ana.md")
    gone.write_text("---\ntype: person\n---\n# Ana\n(sync conflict: section lost)\n")
    assert await edits.apply("People/Ana.md") == 0 and mem.get_fact(cousin)["status"] == "active", \
        "a vanished section must not delete anything"
    print("ok edits")


if __name__ == "__main__":
    asyncio.run(main())
