"""The user's edits in Obsidian win (PLAN P3.5, DECISIONS D14).

The vault is watched (and fully scanned at startup). In a note Laika wrote:
- a fact line he deleted is rejected and tombstoned, so it is not learned again;
- a fact line he reworded becomes the fact (origin "edited", confidence 1.0). "Reworded" means the line
  differs from what Laika wrote there, not from the fact's current text: a fact consolidation updated
  after the note was written is not his edit (that mistake reverted three facts on 2026-10-20);
- a ticked Inbox checkbox confirms a tentative fact;
- if he rewrites the portrait at the top of <name>/Profile.md, his version becomes her portrait.
Her own rewrites of a note match the database, so they change nothing here. If a Laika section
disappears entirely (a sync conflict, say), nothing is deleted; it is only logged.
"""
import asyncio
import logging
import re

from .vault import checked_ids, fact_ids, shown_text

log = logging.getLogger(__name__)

SKIP = (".laika/", ".git/", ".obsidian/", ".trash/")


def _norm(text):
    return re.sub(r"\s+", " ", text or "").strip().rstrip(".").lower()


class Edits:
    def __init__(self, db, memory, embedder, vault):
        self.db, self.mem, self.embedder, self.vault = db, memory, embedder, vault

    async def scan(self):
        """Apply edits made while the app was down (or before the watcher existed)."""
        changed = 0
        for rel in list(self.vault.rendered()) + [self.vault.profile]:
            changed += await self.apply(rel)
        return changed

    async def watch(self):
        from watchfiles import awatch
        await self.scan()
        root = str(self.vault.root)
        async for changes in awatch(root, debounce=2000, step=200):
            rels = {p[len(root):].lstrip("/") for _, p in changes if p.endswith(".md")}
            for rel in sorted(r for r in rels if not r.startswith(SKIP)):
                try:
                    await self.apply(rel)
                except Exception:
                    log.exception("could not apply edits in %s", rel)

    async def apply(self, rel):
        """Compare one note with what Laika wrote into it; returns how many edits were applied."""
        if rel.startswith("Brain/") and getattr(self, "brain", None):  # SB7b: a saved item's note
            return await self.brain.apply_vault_edit(rel)
        section = self.vault.section(rel)
        rendered = self.vault.rendered_lines(rel)
        if section is None:
            if rendered:
                log.warning("Laika section missing in %s; leaving its %d facts alone", rel, len(rendered))
            return 0
        lines, ticked, applied = fact_ids(section), checked_ids(section), 0
        for fid in rendered.keys() - lines.keys():  # he deleted the line
            if (f := self.mem.get_fact(fid)) and f["status"] != "rejected":
                vec = await self.embedder.one(f["text"])
                self.mem.update_fact(fid, status="rejected", origin="edited")
                self.mem._unindex("fact", fid)  # out of recall; the row stays as "rejected" history
                self.mem.forget(texts=[f["text"]], embeddings=[vec])  # tombstone: never learned again
                log.info("the user deleted fact %s in %s", fid, rel)
                applied += 1
        for fid, line in lines.items():
            f = self.mem.get_fact(fid)
            if not f or f["status"] == "rejected":
                continue
            text, before = shown_text(line), rendered.get(fid)
            if text and before is not None and _norm(text) != _norm(shown_text(before)):  # he reworded it
                self.mem.update_fact(fid, embedding=await self.embedder.one(text), text=text, origin="edited",
                                     confidence=1.0)
                log.info("the user edited fact %s in %s", fid, rel)
                applied += 1
            if fid in ticked and f["status"] == "tentative":  # he confirmed it in the Inbox
                self.mem.update_fact(fid, status="active", confidence=max(f["confidence"], 0.9), origin="edited")
                log.info("the user confirmed fact %s", fid)
                applied += 1
        if rel == self.vault.profile:
            applied += self.portrait(section)
        self.vault.set_rendered(rel, lines)  # his version is the new baseline
        return applied

    def portrait(self, section):
        """His rewrite of the portrait (the text above "## What I know") becomes hers."""
        his = section.split("\n## What I know", 1)[0].strip()
        ours = self.db.get_setting("portrait") or ""
        if his and _norm(his) != _norm(ours):
            self.db.set_setting("portrait", his)
            self.db.set_setting("portrait_edited", True)
            log.info("the user edited the portrait in %s", self.vault.profile)
            return 1
        return 0


async def run(edits):
    while True:
        try:
            await edits.watch()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("vault watcher stopped; restarting in 60 s")
            await asyncio.sleep(60)
