"""Nightly "sleep" (PLAN P3.3, DECISIONS D14): consolidate the day and rebuild what Laika carries.

Runs once a night (03:00 Europe/Madrid, or at the first quiet moment after) on the background
slot, and only while the user is away (jobs.py). It closes any open episode, merges duplicate facts,
retires open loops whose time has passed, rewrites her portrait of him as a revision of the previous
one (never a summary of summaries), and builds the memory part of the system prompt. That snapshot
is the only thing that changes the prompt, so the cache is rebuilt once a night: the live window is
trimmed to the last ~2k tokens (episodes now hold the rest) and the new prefix is pre-warmed.
"""
import datetime
import logging
import re
import time
import zoneinfo

from .config import personal, user_name
from .persona import prompt_tokens
from .vault import fact_line

log = logging.getLogger(__name__)

PORTRAIT = """You are Laika, an AI friend of {user}. You keep a short portrait of him that helps you \
be a good friend: who he is, his story as far as you know it, what he's going through now, the people \
in his life (one line each), how he likes to talk, and anything to be careful with. Revise the current \
portrait with what you learned: keep what is still true, change what changed, add what is new and \
important. Never invent anything, and leave out what you don't know. Write it in English, in the third \
person ("He…"), plain and warm, at most 250 words, as a few short paragraphs that each start with a \
label like "Who he is:". No lists, no markdown."""
SUMMARY = """You keep short notes about the people and things in {user}'s life. From the facts below, \
write one or two plain sentences in English about "{name}" as {user}'s friend Laika knows it. Never \
add anything that isn't in the facts."""
DUPLICATE = 0.95
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def day_name(day):
    """'Friday 24 Oct': absolute, because the snapshot may be read on a later day than it was built."""
    return f"{WEEKDAYS[day.weekday()]} {day.day} {day:%b}"


def parse_day(s):
    try:
        return datetime.date.fromisoformat(s) if s else None
    except ValueError:
        return None


class Nightly:
    def __init__(self, cfg, db, llm, memory, vault, consolidator, persona, areas=None):
        self.cfg, self.db, self.llm, self.mem, self.vault = cfg, db, llm, memory, vault
        self.consolidator, self.persona, self.areas = consolidator, persona, areas
        self.tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
        self.slot = cfg["llm"]["background_slot"]

    def today(self):
        return datetime.datetime.now(self.tz).date()

    async def run(self):
        t0 = time.time()
        closed = await self.consolidator.close_all()
        merged = self.merge_duplicates()
        archived_facts, reviews = self.archive_facts(), self.review_stages()
        if self.areas:
            self.areas.derive()
        retired = self.retire_intentions()
        await self.summarize_entities()
        await self.rewrite_portrait()
        self.build_memory_prompt()
        archived = self.archive_rounds()
        self.trim_history()
        self.mem.backup(self.vault.root / ".laika" / "backup")
        self.vault.commit(f"Nightly {self.today().isoformat()}")
        await self.llm.warm(self.persona.prefix_messages(), self.cfg["llm"]["conversation_slot"])
        self.db.set_setting("nightly_done", self.today().isoformat())
        log.info("nightly done in %.0f s: %d episodes closed, %d duplicates merged, %d facts archived, %d stage "
                 "checks, %d loops retired, %d rounds archived", time.time() - t0, closed, merged, archived_facts,
                 reviews, retired, archived)

    def merge_duplicates(self):
        """Near-identical active facts: keep the more used one, the other points to it."""
        merged = 0
        for f in self.mem.facts():
            if (current := self.mem.get_fact(f["id"])) is None or current["status"] != "active":
                continue
            vec = self.db.conn.execute("SELECT v.embedding FROM items i JOIN items_vec v ON v.rowid = i.id "
                                       "WHERE i.kind = 'fact' AND i.ref_id = ?", (f["id"],)).fetchone()
            if vec is None:
                continue
            for kind, ref, cos in self.mem.search_vec(_floats(vec[0]), k=3, kinds=("fact",)):
                other = self.mem.get_fact(ref) if ref != f["id"] else None
                if other and other["status"] == "active" and cos >= DUPLICATE and other["entity_id"] == f["entity_id"]:
                    keep, drop = (f, other) if f["n_uses"] >= other["n_uses"] else (other, f)
                    self.db.conn.execute("UPDATE facts SET status = 'superseded', superseded_by = ? WHERE id = ?",
                                         (keep["id"], drop["id"]))
                    merged += 1
                    break
        return merged

    def archive_facts(self):
        """W8.3: a dated fact whose day has passed becomes history (status 'archived'). Never deleted:
        it stays in the vault's "Before" sections and recall can still find it, with a lower weight."""
        today, n = self.today(), 0
        for f in self.mem.facts():
            if f.get("time_scope") != "dated":
                continue
            end = parse_day(f["valid_to"]) or parse_day(f["valid_from"])
            if end and end < today:
                self.db.conn.execute("UPDATE facts SET status = 'archived', time_scope = 'archived', "
                                     "valid_to = COALESCE(valid_to, valid_from) WHERE id = ?", (f["id"],))
                n += 1
        return n

    def review_stages(self):
        """W8.3: a "stage" fact (true for now) past its review day becomes an open loop: she asks, when it
        fits, whether it's still true; consolidation then updates or replaces it from his answer."""
        today, n = self.today(), 0
        days = self.cfg["memory"].get("stage_review_days", 90)
        for f in self.mem.facts():
            if f.get("time_scope") != "stage" or not (due := parse_day(f.get("review_after"))) or due > today:
                continue
            words = [w for w in re.findall(r"\w{5,}", f["text"]) if w.lower() != user_name(self.cfg).lower()][:4]
            self.mem.add_intention({"text": f"Check, when it fits, whether this is still true: {f['text']}",
                                    "entity_id": f["entity_id"], "due_from": today.isoformat(),
                                    "due_to": (today + datetime.timedelta(days=7)).isoformat(), "cue_terms": words,
                                    "evidence": [f"fact:{f['id']}"]})
            self.db.conn.execute("UPDATE facts SET review_after = ? WHERE id = ?",
                                 ((today + datetime.timedelta(days=days)).isoformat(), f["id"]))
            n += 1
        return n

    def chat_days_after(self, day):
        """Days after `day` (local dates) on which the user wrote at least once (W8.1)."""
        start = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(), self.tz).timestamp()
        days = {datetime.datetime.fromtimestamp(ts, self.tz).date() for (ts,) in self.db.conn.execute(
            "SELECT ts FROM messages WHERE role = 'user' AND ts >= ?", (start,))}
        return len(days)

    def retire_intentions(self):
        """Open loops are followed up once around their time. Once she raised one ("asked") it is done.
        An open one expires after 3 days on which he actually chatted after its day (an undated one after
        5 chat days since it was noted), so days with the server off or him away don't count (W8.1)."""
        retired = 0
        for it in self.mem.intentions(("open", "asked")):
            if it["status"] == "asked":
                self.mem.set_intention(it["id"], status="done")
                retired += 1
                continue
            due = parse_day(it["due_to"]) or parse_day(it["due_from"])
            if due is not None and self.chat_days_after(due) >= 3:
                self.mem.set_intention(it["id"], status="dropped")
                retired += 1
            elif due is None:
                noted = datetime.datetime.fromtimestamp(it["created"], self.tz).date()
                if self.chat_days_after(noted) >= 5:
                    self.mem.set_intention(it["id"], status="dropped")
                    retired += 1
        return retired

    async def summarize_entities(self):
        """One or two sentences per person/topic that has a note, from its facts (rewritten only if they changed)."""
        for ent in self.mem.entities():
            if not ent["note_path"]:
                continue
            facts = self.mem.facts(ent["id"])
            stamp = ",".join(str(f["id"]) for f in facts)
            if not facts or self.db.get_setting(f"summary_facts_{ent['id']}") == stamp:
                continue
            text = await self.llm.chat([
                {"role": "system", "content": personal(SUMMARY, self.cfg).format(name=ent["name"])},
                {"role": "user", "content": "\n".join(f"- {f['text']}" for f in facts)}],
                self.slot, temperature=0.3, max_tokens=120)
            self.db.conn.execute("UPDATE entities SET summary = ?, updated = ? WHERE id = ?",
                                 (_plain(text), time.time(), ent["id"]))
            self.db.set_setting(f"summary_facts_{ent['id']}", stamp)
            self.consolidator.render_entity(ent["id"])

    async def rewrite_portrait(self):
        facts = self.mem.facts()
        about_him = [f for f in facts if f["category"] not in ("us", "laika_self") and not f["no_mention"]]
        about_him.sort(key=lambda f: f["importance"] + 0.1 * self.mem.activation(f), reverse=True)
        week_ago = time.time() - 7 * 86400
        recent = [e for e in self.mem.episodes_between(week_ago, time.time() + 1)]
        if not about_him and not recent:
            return
        lines = "\n".join(f"- {f['text']}" for f in about_him[:40])
        diary = "\n".join(f"- ({datetime.datetime.fromtimestamp(e['start_ts'], self.tz):%a %d %b}) {e['narrative']}"
                          for e in recent[-12:])
        user = (f"Current portrait:\n{self.db.get_setting('portrait') or '(none yet)'}\n\n"
                f"What you know (most important first):\n{lines or '(nothing new)'}\n\n"
                f"Recent diary entries:\n{diary or '(none)'}")
        system = personal(PORTRAIT + (" {user} edited the current portrait himself: keep his wording and only add "
                                      "what is new." if self.db.get_setting("portrait_edited") else ""), self.cfg)
        text = await self.llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                   self.slot, temperature=0.3, max_tokens=600)
        portrait = _plain(text)
        if portrait:
            self.db.set_setting("portrait", portrait)
            by_category = {}
            for f in about_him:
                by_category.setdefault(f["category"], []).append(f)
            body = portrait + "\n\n## What I know\n" + "\n\n".join(
                f"### {cat.capitalize()}\n" + "\n".join(fact_line(f) for f in fs) for cat, fs in sorted(by_category.items()))
            self.vault.write(self.vault.profile, body, title=user_name(self.cfg), kind="profile")

    def build_memory_prompt(self):
        """The memory part of the system prompt: her portrait of him, what's going on now, the two of them."""
        today = self.today()
        loops = []
        for it in self.mem.intentions(("open",)):
            start, end = parse_day(it["due_from"]), parse_day(it["due_to"])
            if start and (start - today).days > 7:
                continue  # too far ahead to matter this week
            if start and end and end != start:
                loops.append(f"- {it['text']} ({day_name(start)} to {day_name(end)})")
            elif start:
                loops.append(f"- {it['text']} ({day_name(start)})")
            else:
                loops.append(f"- {it['text']}")
        recent = self.mem.episodes_between(time.time() - 3 * 86400, time.time() + 1)
        facts = self.mem.facts()
        us = sorted((f for f in facts if f["category"] == "us"), key=self.mem.activation, reverse=True)[:8]
        own = sorted((f for f in facts if f["category"] == "laika_self"), key=self.mem.activation, reverse=True)[:8]
        sections = []
        if portrait := self.db.get_setting("portrait"):
            sections.append(f"## What you know about {user_name(self.cfg)}\n" + portrait)
        now = []
        if loops:
            now.append("Things to follow up on when it fits (once):\n" + "\n".join(loops[:6]))
        if recent:
            now.append("Lately you talked about: " + "; ".join(e["title"] for e in recent[-5:]) + ".")
        if now:
            sections.append("## Lately\n" + "\n\n".join(now))
        if us:
            sections.append("## You two\n" + "\n".join(f"- {f['text']}" for f in us))
        if own:
            sections.append("## Things you've said about yourself\n" + "\n".join(f"- {f['text']}" for f in own))
        self.db.set_setting("memory_prompt", "\n\n".join(sections))
        self.vault.write(f"{self.vault.user}/Now.md", "\n\n".join(now) or "(nothing right now)", title="Now", kind="now")
        self.vault.write("Laika/Us.md", "\n".join(fact_line(f) for f in us) or "(nothing yet)", title="Us",
                         kind="us")
        self.vault.write("Laika/Laika.md", "\n".join(fact_line(f) for f in own) or "(nothing yet)",
                         title="Laika", kind="laika")

    def archive_rounds(self, days=30):
        """Verbatim fades, gist stays: old rounds from unremarkable episodes leave the search index."""
        cutoff, archived = time.time() - days * 86400, 0
        rows = self.db.conn.execute(
            "SELECT r.id FROM rounds r LEFT JOIN episodes e ON e.id = (SELECT episode_id FROM messages WHERE id = r.msg_from) "
            "WHERE r.archived = 0 AND r.ts < ? AND MAX(COALESCE(e.arousal, 0), COALESCE(e.self_rel, 0), "
            "COALESCE(e.future_rel, 0)) < 0.5", (cutoff,)).fetchall()
        for r in rows:
            self.mem.archive_round(r[0])
            archived += 1
        return archived

    def trim_history(self):
        """Keep only the last ~2k tokens verbatim; the episodes and the portrait hold the rest."""
        keep = self.cfg["memory"].get("history_after_nightly", 2000)
        msgs = self.db.conversation_since(self.db.get_setting("history_start_id", 0))
        total, start = 0, None
        for m in reversed(msgs):
            total += prompt_tokens(m)
            if total > keep:
                break
            start = m["id"]
        if start is not None and msgs and start != msgs[0]["id"]:
            self.db.set_setting("history_start_id", start)
        self.db.set_setting("running_summary", "")


def _floats(blob):
    import struct
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _plain(text):
    text = re.sub(r"[*#`_]+", "", text or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
