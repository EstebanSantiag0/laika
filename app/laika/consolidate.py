"""Episode close (PLAN P3.3, DECISIONS D14): turn a stretch of conversation into memory.

Once the user has been away for a while, the unconsolidated messages are cut into episodes (long
silences, size). For each episode, one schema-constrained call on the background slot writes Laika's
diary gist and the facts, people, open loops and shared moments worth keeping. Facts are compared
with what she already knows (same / update / replace / new, as in Mem0), checked against tombstones
(things he asked her to forget), and low-confidence ones wait in the Inbox. Everything async
(model, embeddings) happens before the first write, so an interrupted job leaves nothing half-done.
Vault notes are re-rendered; the system prompt itself only changes at night (nightly.py).
"""
import collections
import datetime
import logging
import re
import zoneinfo

from . import slop
from .config import DEFAULT_USER, personal, user_name
from .vault import fact_line

log = logging.getLogger(__name__)

PROMPT = """You are the memory of Laika, an AI friend who texts with {user}. {user} is {user_about}. Below \
is one stretch of their chat ({when}). Write, in English, what Laika should remember from it.

- "title": a few words.
- "diary": Laika's private diary entry about this stretch: first person ("he told me…"), past tense, \
warm and plain, at most 80 words. Paraphrase; never quote him word for word. Say how he seemed to feel.
- "facts": what is worth remembering about {user} and his life: people, work, studies, plans, \
worries, opinions he argued, tastes, habits, his languages. One self-contained sentence each, with \
absolute dates when time matters (this chat happened on {date}). Only what he said or clearly \
meant; nothing Laika said (that goes in "laika_self"). Skip greetings, small talk and language drills.
  - "about": "{user}", or the name of the person or thing the fact is about (also listed in "entities").
  - "op": compare with the known facts below. "same" if it is already known (give its id), "update" \
if it adds detail to one (its id, and the full new sentence), "replace" if that one is no longer true \
(its id), "new" otherwise (id 0).
  - "importance" 0-1 (1 = core to who he is or what he's going through), "confidence" 0-1 (how sure \
you are he meant it), "sensitive": health, family conflict, money, sex or crises.
  - "areas": the parts of his life it belongs to, at most 2 of: {areas}. An empty list if none fits.
  - "polarity": for a taste or opinion, "like" if he likes, loves or enjoys it, "dislike" if he \
dislikes or hates it; "none" for everything else.
  - "time_scope": "permanent" (lasting: who he is, his tastes, his people), "stage" (true for this \
period of his life and likely to change: looking for internships, living in Seville for now, preparing \
an exam), or "dated" (an event or deadline on a day: give "valid_from", and "valid_to" when it ends \
later). Dates as YYYY-MM-DD.
- "entities": people and things he talked about that matter (never Laika or anything of hers, never \
{user} himself): name, kind (person = a human being; work = a book, song, film or project; place; topic; \
thing = an object), aliases (other spellings or scripts he used).
- "intentions": what Laika should follow up on later, with dates as YYYY-MM-DD (an exam on Friday: \
"Ask how his exam went", due_from that Friday). Leave empty if nothing. Leave out reminders he \
explicitly asked for ("remind me…", "recuérdame…"): the app keeps those separately.
- "us": shared jokes, nicknames, references or moments between them.
- "laika_self": lasting opinions or tastes Laika herself expressed (not passing moods), so she stays \
consistent. Plain words, no quotation marks.
- "arousal" (how emotionally intense), "self_rel" (how much it's about who he is), "future_rel" (how \
much it matters later): 0-1.

Known facts (id: text):
{known}

What Laika already knows about him:
{portrait}"""

CATEGORIES = ["identity", "preference", "relationship", "goal", "worry", "opinion", "event", "habit", "skill",
              "language"]
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "diary": {"type": "string"},
        "mood": {"type": "string"},
        "arousal": {"type": "number"},
        "self_rel": {"type": "number"},
        "future_rel": {"type": "number"},
        "entities": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "kind": {"enum": ["person", "place", "work", "topic", "thing"]},
            "aliases": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "kind", "aliases"]}},
        "facts": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "about": {"type": "string"}, "category": {"enum": CATEGORIES},
            "op": {"enum": ["new", "same", "update", "replace"]}, "id": {"type": "integer"},
            "importance": {"type": "number"}, "confidence": {"type": "number"}, "sensitive": {"type": "boolean"},
            "areas": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
            "time_scope": {"enum": ["permanent", "stage", "dated"]},
            "polarity": {"enum": ["like", "dislike", "none"]},
            "valid_from": {"type": "string"}, "valid_to": {"type": "string"}},
            "required": ["text", "about", "category", "op", "id", "importance", "confidence", "sensitive", "areas",
                         "time_scope", "polarity"]}},
        "intentions": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "due_from": {"type": "string"}, "due_to": {"type": "string"},
            "cue_terms": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "due_from", "due_to", "cue_terms"]}},
        "us": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
        "laika_self": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
    },
    "required": ["title", "diary", "mood", "arousal", "self_rel", "future_rel", "entities", "facts", "intentions",
                 "us", "laika_self"],
}
DUPLICATE = 0.92   # a "new" fact this close to a known one is the same fact
NOTE_IMPORTANCE = 0.7  # an entity gets its own note after 2 episodes, or once if this important
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_IN_TEXT = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b")
_BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]')


def clamp(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def date_or_none(s):
    return s if isinstance(s, str) and _DATE.match(s.strip()) else None


def note_name(name):
    return _BAD_NAME.sub("", name).strip() or "Untitled"


def segments(msgs, gap_s, max_msgs):
    """Cut a conversation into episodes at long silences, and before a new message from him once a
    piece has grown to `max_msgs` messages."""
    seg = []
    for m in msgs:
        if seg and (m["ts"] - seg[-1]["ts"] > gap_s or (len(seg) >= max_msgs and m["role"] == "user")):
            yield seg
            seg = []
        seg.append(m)
    if seg:
        yield seg


def rounds(seg, name=DEFAULT_USER):
    """(first id, last id, ts, text) for each message of his plus her reply."""
    out, user, reply = [], [], []
    for m in seg + [None]:
        if m is None or (m["role"] == "user" and reply):
            if user or reply:
                msgs = user + reply
                text = "\n".join(f"{name if x['role'] == 'user' else 'Laika'}: {x['text']}" for x in msgs)
                out.append((msgs[0]["id"], msgs[-1]["id"], msgs[0]["ts"], text))
            user, reply = [], []
            if m is None:
                break
        (user if m["role"] == "user" else reply).append(m)
    return out


def time_fields(f, today, review_days=90):
    """time_scope, valid_from, valid_to and review_after for a fact from the model (W8.3)."""
    scope = f.get("time_scope") if f.get("time_scope") in ("permanent", "stage", "dated") else "permanent"
    start, end = date_or_none(f.get("valid_from")), date_or_none(f.get("valid_to"))
    named = _DATE_IN_TEXT.findall(f.get("text") or "")
    if scope != "dated" and f.get("category") == "event" and named:
        scope, start, end = "dated", start or named[0], end or named[-1]  # "a meeting at 1pm on 2026-10-20"
    if scope == "dated" and not (start or end):
        scope = "permanent"  # a "dated" fact without a date can't expire; keep it
    if scope == "permanent":
        start = end = None  # lasting: no validity window
    review = (today + datetime.timedelta(days=review_days)).isoformat() if scope == "stage" else None
    return {"time_scope": scope, "valid_from": start, "valid_to": end, "review_after": review}


class Consolidator:
    def __init__(self, cfg, db, llm, memory, embedder, vault, areas=None):
        self.cfg, self.db, self.llm, self.mem, self.embedder, self.vault = cfg, db, llm, memory, embedder, vault
        self.tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
        self.areas = areas  # W8.3 life areas (areas.py)

    def schema(self):
        """The extraction schema, with the configured area ids as the only allowed areas (W8.3)."""
        if not self.areas:
            return SCHEMA
        fact = {**SCHEMA["properties"]["facts"]["items"]}
        fact["properties"] = {**fact["properties"], "areas": self.areas.schema()}
        facts = {**SCHEMA["properties"]["facts"], "items": fact}
        return {**SCHEMA, "properties": {**SCHEMA["properties"], "facts": facts}}

    def pending(self):
        """User and companion messages not yet in any episode."""
        return self.db.conversation_since(self.mem.last_consolidated_msg() + 1)

    async def close_all(self):
        """Consolidate every stretch of conversation that isn't in an episode yet."""
        m = self.cfg["memory"]
        done = 0
        for seg in segments(self.pending(), m.get("episode_gap_min", 30) * 60, m.get("episode_max_msgs", 40)):
            if any(x["role"] == "user" for x in seg):
                await self.close(seg)
                done += 1
        return done

    def transcript(self, seg):
        lines, last_day = [], None
        for m in seg:
            t = datetime.datetime.fromtimestamp(m["ts"], self.tz)
            if (day := t.strftime("%A %d %B %Y")) != last_day:
                lines.append(f"({day})")
                last_day = day
            who = user_name(self.cfg) if m["role"] == "user" else "Laika"
            voice = "(voice note) " if m["meta"].get("voice") else ""
            lines.append(f"[{t:%H:%M}] {who}: {voice}{m['text']}")
        return "\n".join(lines)

    async def close(self, seg):
        """Write one episode, and everything drawn from it, into memory and the vault."""
        text = self.transcript(seg)
        start = datetime.datetime.fromtimestamp(seg[0]["ts"], self.tz)
        query = await self.embedder.one(text[-6000:], query=True)
        known = {}
        for _, ref, _ in self.mem.search_vec(query, k=15, kinds=("fact",)):
            if (f := self.mem.get_fact(ref)) and f["status"] in ("active", "tentative"):
                known[f["id"]] = f
        system = personal(PROMPT, self.cfg).format(when=start.strftime("%A %d %B %Y, from %H:%M"), date=start.date().isoformat(),
                               areas=self.areas.prompt_list() if self.areas else "(none)",
                               known="\n".join(f"{i}: {f['text']}" for i, f in known.items()) or "(none yet)",
                               portrait=self.db.get_setting("portrait") or "(nothing yet)")
        out = await self.llm.chat([{"role": "system", "content": system}, {"role": "user", "content": text}],
                                  self.cfg["llm"]["background_slot"], json_schema=self.schema(), temperature=0.3,
                                  max_tokens=1500)
        facts = [f for f in out.get("facts", []) if f.get("text", "").strip()]
        # her own lines must not carry AI-isms back into her prompt ("I find that profound")
        extras = [(cat, t.strip()) for cat in ("us", "laika_self") for t in out.get(cat, [])
                  if t.strip() and not slop.find(t)]
        convo = rounds(seg, user_name(self.cfg))
        key = f"{out['title']}. {out['diary']} " + " ".join(f["text"] for f in facts)
        vectors = await self.embedder.embed([f["text"] for f in facts] + [t for _, t in extras]
                                            + [r[3] for r in convo] + [key])
        fact_vecs, extra_vecs = vectors[:len(facts)], vectors[len(facts):len(facts) + len(extras)]
        round_vecs, key_vec = vectors[len(facts) + len(extras):-1], vectors[-1]
        self.db.conn.execute("BEGIN")
        try:
            episode_id = self._write(seg, out, known, facts, fact_vecs, extras, extra_vecs, convo, round_vecs,
                                     key_vec)
            self.db.conn.execute("COMMIT")
        except Exception:
            self.db.conn.execute("ROLLBACK")
            raise
        self.render(episode_id)
        log.info("episode %s closed: %r (%d messages, %d facts)", episode_id, out["title"], len(seg), len(facts))
        return episode_id

    def _write(self, seg, out, known, facts, fact_vecs, extras, extra_vecs, convo, round_vecs, key_vec):
        mem = self.mem
        me = mem.entity(user_name(self.cfg), "person")
        by_name = {}
        for e in out.get("entities", []):
            name = e.get("name", "").strip()
            if not name or name.lower() == user_name(self.cfg).lower() or "laika" in name.lower():
                continue
            aliases = [a.strip() for a in e.get("aliases", []) if a.strip() and a.strip().lower() != name.lower()]
            ent = mem.entity(name, e.get("kind", "topic"), aliases)
            for n in [ent["name"], name, *ent["aliases"]]:
                by_name[n.lower()] = ent
        arousal = clamp(out.get("arousal"))
        langs = collections.Counter(m["lang"] for m in seg if m["role"] == "user" and m.get("lang"))
        episode_id = mem.add_episode({
            "start_msg": seg[0]["id"], "end_msg": seg[-1]["id"], "start_ts": seg[0]["ts"], "end_ts": seg[-1]["ts"],
            "lang": langs.most_common(1)[0][0] if langs else None, "title": out["title"].strip(),
            "narrative": out["diary"].strip(), "mood": out.get("mood"), "arousal": arousal,
            "self_rel": clamp(out.get("self_rel")), "future_rel": clamp(out.get("future_rel")),
            "entity_ids": sorted({e["id"] for e in by_name.values()})}, key_vec, " ".join(f["text"] for f in facts))
        evidence = [seg[0]["id"], seg[-1]["id"]]
        for f, vec in zip(facts, fact_vecs):
            ent = by_name.get(f.get("about", "").strip().lower(), me)
            row = {"entity_id": ent["id"], "text": f["text"].strip(), "category": f.get("category", "identity"),
                   "importance": clamp(f.get("importance")), "arousal": arousal,
                   "confidence": clamp(f.get("confidence")), "sensitive": bool(f.get("sensitive")),
                   "episode_id": episode_id, "evidence": evidence, "polarity": f.get("polarity"),
                   **time_fields(f, datetime.datetime.now(self.tz).date(),
                                 self.cfg["memory"].get("stage_review_days", 90))}
            fid, op = self._fact(row, vec, f.get("op"), known.get(f.get("id")))
            if fid and self.areas and (op != "same" or not self.areas.of("fact", fid)):
                self.areas.set("fact", fid, f.get("areas") or [])
        for (category, text), vec in zip(extras, extra_vecs):
            self._fact({"entity_id": None, "text": text, "category": category, "importance": 0.5, "arousal": arousal,
                        "confidence": 0.9, "episode_id": episode_id, "evidence": evidence}, vec, "new", None)
        open_texts = {i["text"].lower() for i in mem.intentions()}
        for it in out.get("intentions", []):
            if it.get("text", "").strip() and it["text"].strip().lower() not in open_texts:
                mem.add_intention({"text": it["text"].strip(), "due_from": date_or_none(it.get("due_from")),
                                   "due_to": date_or_none(it.get("due_to")) or date_or_none(it.get("due_from")),
                                   "cue_terms": [c for c in it.get("cue_terms", []) if c.strip()],
                                   "evidence": evidence})
        for (first, last, ts, text), vec in zip(convo, round_vecs):
            mem.add_round(first, last, ts, text, vec)
        if self.areas:
            self.areas.derive()  # entities and episodes take the areas of their facts
        return episode_id

    def _fact(self, row, vec, op, old):
        """same / update / replace / new against what she knows; tombstoned facts are never re-learned."""
        mem = self.mem
        if mem.is_forgotten(row["text"], vec):
            log.info("skipped a fact the user asked her to forget")
            return None, None
        if old is None:  # "new", or an id the model made up: look for the same fact anyway
            hit = next(iter(mem.search_vec(vec, k=1, kinds=("fact",))), None)
            if hit and hit[2] >= DUPLICATE and (twin := mem.get_fact(hit[1])) and twin["status"] in ("active",
                                                                                                    "tentative"):
                old, op = twin, "same"
            else:
                op = "new"
        if op == "same":
            mem.use("fact", old["id"])
            return old["id"], op
        if op == "update":
            mem.update_fact(old["id"], embedding=vec, text=row["text"],
                            importance=max(old["importance"], row["importance"]),
                            confidence=max(old["confidence"], row["confidence"]),
                            time_scope=row["time_scope"], valid_from=row["valid_from"] or old["valid_from"],
                            valid_to=row["valid_to"] or old["valid_to"], review_after=row["review_after"],
                            polarity=row.get("polarity") if row.get("polarity") in ("like", "dislike") else old.get("polarity"))
            mem.use("fact", old["id"])
            return old["id"], op
        if op == "replace":
            return mem.supersede(old["id"], row, vec, valid_to=row["valid_from"] or
                                 datetime.date.today().isoformat()), op
        return mem.add_fact(row, vec), "new"

    # vault ---------------------------------------------------------------------------------------
    def render(self, episode_id):
        """Re-render the notes this episode touched: its journal day, its people/topics, the Inbox."""
        ep = self.mem.get_episode(episode_id)
        self.render_journal(datetime.datetime.fromtimestamp(ep["start_ts"], self.tz).date())
        for entity_id in ep["entity_ids"]:
            self.render_entity(entity_id)
        self.render_inbox()

    def render_journal(self, day):
        start = datetime.datetime.combine(day, datetime.time(), self.tz).timestamp()
        parts = []
        for ep in self.mem.episodes_between(start, start + 86400):
            t = datetime.datetime.fromtimestamp(ep["start_ts"], self.tz)
            parts.append(f"## {t:%H:%M} · {ep['title']}\n\n{ep['narrative']}")
        if parts:
            self.vault.write(f"Journal/{day.isoformat()}.md", "\n\n".join(parts), title=day.strftime("%A %d %B %Y"),
                             kind="journal")

    def render_entity(self, entity_id):
        ent = self.mem.get_entity(entity_id)
        facts = self.mem.facts(entity_id)
        if not ent or not (ent["episodes"] >= 2 or any(f["importance"] >= NOTE_IMPORTANCE for f in facts)):
            return
        folder = "People" if ent["kind"] == "person" else "Topics"
        rel = ent["note_path"] or f"{folder}/{note_name(ent['name'])}.md"
        past = self.mem.facts(entity_id, statuses=("past", "superseded", "archived"))
        episodes = [self.mem.get_episode(r[0]) for r in self.db.conn.execute(
            "SELECT id FROM episodes WHERE EXISTS (SELECT 1 FROM json_each(episodes.entity_ids) WHERE value = ?) "
            "ORDER BY start_ts", (entity_id,))]
        body = [ent["summary"]] if ent["summary"] else []
        if facts:
            body.append("## What I know\n" + "\n".join(fact_line(f) for f in facts))
        if past:
            body.append("## Before\n" + "\n".join(fact_line(f) for f in past))
        if episodes:
            body.append("## Mentioned\n" + "\n".join(
                f"- [[Journal/{datetime.datetime.fromtimestamp(e['start_ts'], self.tz):%Y-%m-%d}]] {e['title']}"
                for e in episodes))
        self.vault.write(rel, "\n\n".join(body), title=ent["name"], kind=ent["kind"], aliases=ent["aliases"])
        if not ent["note_path"]:
            self.db.conn.execute("UPDATE entities SET note_path = ? WHERE id = ?", (rel, entity_id))

    def render_inbox(self):
        tentative = self.mem.facts(statuses=("tentative",))
        body = ("Things I'm not sure about yet. Tick the ones that are right; delete the lines that are wrong.\n\n"
                + ("\n".join(fact_line(f, checkbox=True) for f in tentative) if tentative else "(nothing right now)"))
        self.vault.write("Laika/Inbox.md", body, title="Inbox", kind="inbox")
