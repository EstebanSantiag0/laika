"""Recall at turn start (PLAN P3.4, DECISIONS D14): a few memories in the turn's note, when they help.

A cheap gate skips filler ("haha", "ok"). Otherwise his batch is embedded once and candidates come
from vector search, full-text search, the people and things he names (and their neighbours), and
open loops whose cue words he used. Each candidate is scored like human memory:

    A = B + 1.5·I + 1.0·z_sim + 0.4·z_bm25 + 0.5·spread + 0.3·ctx + 0.7·goal

B is ACT-R base-level activation (how recently and often it was used), I its importance, z_* are
z-scores within this turn's candidates, spread rewards the entities he mentioned, ctx the language
they speak now, goal an open loop. Only clear winners get in (at most 3 facts + 1 episode line,
diversified), phrased as gist with a loose time; if nothing clears the bar she simply doesn't
remember, which is better than a wrong memory. What was offered is logged in `injections`, and a
memory counts as used only if her reply actually draws on it.
"""
import datetime
import json
import logging
import math
import re
import time
import zoneinfo

import sqlite_vec

from .config import personal, user_name

log = logging.getLogger(__name__)

FILLER = re.compile(r"^[\W\d_]*(ok(ay)?|k|ha(ha)+|ja(ja)+|lol|hm+|ah+|oh+|yes|no|yeah|yep|nope|s[ií]|vale|"
                    r"ага|да|нет|ок|ха(ха)+|哈哈+|嗯+|好的?|对|是的?|thanks|gracias|спасибо|谢谢)[\W\d_]*$", re.I)
CUES = re.compile(r"\b(remember|recuerdas|acuerdas|last time|la otra vez|the other day|el otro d[ií]a|"
                  r"did i (tell|mention)|te (cont[eé]|dije)|you know my|again|still)\b|помнишь|в прошлый раз|"
                  r"я (тебе )?говорил|还记得|上次|我跟你说过", re.I)
WEIGHTS = {"B": 1.0, "I": 1.5, "sim": 1.0, "bm25": 0.4, "spread": 0.5, "ctx": 0.3, "goal": 0.7}
TAU = 1.0          # minimum score to be offered
COS_MIN = 0.42     # ...a minimum absolute similarity (calibrated: related 0.46-0.57, unrelated 0.24-0.35)...
STANDS_OUT = 2.0   # ...and it must stand out from all her memories (z of its similarity), unless he named
                   # the person or thing (BENCHMARKS P3.4)
USED_COS = 0.55    # her reply drew on it
MAX_FACTS, MAX_EPISODES = 3, 1
SINCE_LAST_TIME_S = 3 * 3600
ARCHIVED_PENALTY = 1.0  # W8.3: an archived (past) fact needs a clearly better match to be offered
# W8.1 open loops (calibrated 2026-11-01 on real loops, docs/PLAN-w8.md):
LOOP_CUE_COS = 0.44   # he used one of its cue words and his message is about it (python "logic" 0.41: no)
LOOP_COS = 0.58       # ...or it is clearly about it, without a cue word
ASKED_CUE_COS = 0.55  # one of her bubbles raises the loop and holds a cue word...
ASKED_COS = 0.64      # ...or is clearly about it (right pairs 0.67-0.89, wrong ones up to 0.58)


def loose_when(ts, now, tz):
    """'earlier today', 'yesterday', 'this week', 'last week', 'in October', 'a while ago'."""
    d, today = datetime.datetime.fromtimestamp(ts, tz).date(), datetime.datetime.fromtimestamp(now, tz).date()
    days = (today - d).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "this week"
    if days < 14:
        return "last week"
    if days < 120:
        return f"in {d:%B}"
    return "a while ago"


def _z(values):
    if len(values) < 2:
        return [0.0] * len(values)
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) or 1.0
    return [(v - mean) / sd for v in values]


def _words(text):
    return {w for w in re.findall(r"\w{4,}", text.lower())}


class Recall:
    def __init__(self, cfg, db, memory, embedder, persona):
        self.cfg, self.db, self.mem, self.embedder, self.persona = cfg, db, memory, embedder, persona
        self.tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
        self.offered = {}  # batch_id -> [(kind, ref_id)] for the usage check after the turn

    def gate(self, text):
        t = text.strip()
        cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in t)
        if not t or FILLER.match(t) or (len(_words(t)) < 2 and cjk < 4 and not CUES.search(t)):
            return None
        return "cue" if CUES.search(t) else "content"

    def named_entities(self, text):
        low, me = text.lower(), user_name(self.cfg).lower()
        out = []
        for e in self.mem.entities():
            names = [e["name"], *e["aliases"]]
            if e["name"].lower() != me and any(re.search(rf"(?<!\w){re.escape(n.lower())}(?!\w)", low)
                                                      for n in names if len(n) > 1):
                out.append(e)
        return out

    def _vector(self, kind, ref_id):
        row = self.db.conn.execute("SELECT v.embedding FROM items i JOIN items_vec v ON v.rowid = i.id "
                                   "WHERE i.kind = ? AND i.ref_id = ?", (kind, ref_id)).fetchone()
        return list(sqlite_vec.deserialize_float32(row[0])) if row and hasattr(sqlite_vec, "deserialize_float32") \
            else (_unpack(row[0]) if row else None)

    async def for_turn(self, batch, batch_id=None):
        """Notes to add to this turn: 'You remember: …' and, after a long silence, 'Last time: …'."""
        text = "\n".join(m["text"] for m in batch)
        reason = self.gate(text)
        now = time.time()
        notes = []
        last = self.db.conn.execute("SELECT MAX(ts) FROM messages WHERE role = 'user' AND id < ?",
                                    (batch[0]["id"],)).fetchone()[0]
        if last and now - last >= SINCE_LAST_TIME_S and (ep := self.mem.last_episode()):
            gist = re.split(r"(?<=[.!?])\s", ep["narrative"].strip())[0]
            notes.append(f"Last time ({loose_when(ep['end_ts'], now, self.tz)}), from your diary: {gist}")
        if reason is None:
            return notes
        try:
            query = await self.embedder.one(text, query=True)
        except Exception as e:  # the embed service is down: talk without memories
            log.warning("recall skipped: %s", e)
            return notes
        for it in await self.cued_loops(text, query):
            notes.append(personal("Something you meant to follow up on, and he just touched on it: ", self.cfg) + it["text"])
            self.db.conn.execute("INSERT INTO injections(ts, batch_id, reason, items, tokens) VALUES (?, ?, ?, ?, ?)",
                                 (now, batch_id, "loop", json.dumps([["intention", it["id"], 0]]), len(it["text"]) // 4))
        picked = self.pick(text, query, now)
        if picked:
            facts = [p for p in picked if p["kind"] == "fact"]
            eps = [p for p in picked if p["kind"] == "episode"]
            if facts:
                notes.append("You remember: " + "; ".join(
                    f"{p['text']} ({loose_when(p['ts'], now, self.tz)})" for p in facts))
            for p in eps:
                notes.append(f"From your diary ({loose_when(p['ts'], now, self.tz)}): {p['text']}")
            self.db.conn.execute("INSERT INTO injections(ts, batch_id, reason, items, tokens) VALUES (?, ?, ?, ?, ?)",
                                 (now, batch_id, reason, json.dumps([[p["kind"], p["id"], round(p["score"], 2)]
                                                                     for p in picked]),
                                  sum(len(n) for n in notes) // 4))
            if batch_id is not None:
                self.offered[batch_id] = [(p["kind"], p["id"]) for p in picked]
        return notes

    def pick(self, text, query, now):
        named = self.named_entities(text)
        named_ids = {e["id"] for e in named}
        near = {}
        for e in named:
            for other, w in self.mem.neighbours(e["id"]).items():
                near[other] = max(near.get(other, 0), w)
        top_w = max(near.values(), default=1)
        population = [c for _, _, c in self.mem.search_vec(query, k=200, kinds=("fact", "episode"))]
        mean = sum(population) / len(population) if population else 0.0
        sd = math.sqrt(sum((c - mean) ** 2 for c in population) / len(population)) if len(population) > 1 else 1.0
        sd = sd or 1.0
        cands = {}
        for kind, ref, cos in self.mem.search_vec(query, k=20, kinds=("fact", "episode")):
            cands[(kind, ref)] = {"cos": cos, "bm25": 0.0}
        for kind, ref, score in self.mem.search_text(text, k=20, kinds=("fact", "episode")):
            cands.setdefault((kind, ref), {"cos": None, "bm25": 0.0})["bm25"] = -score
        for e in named:
            for f in self.mem.facts(e["id"]):
                cands.setdefault(("fact", f["id"]), {"cos": None, "bm25": 0.0})
        open_cues = {}
        for it in self.mem.intentions(("open",)):
            if any(c.lower() in text.lower() for c in it["cue_terms"] if len(c) > 2):
                open_cues[it["entity_id"]] = True
        snapshot = (self.db.get_setting("memory_prompt") or "").lower()
        recent = self.recently_offered()
        lang_now = self.persona.practice_lang()
        rows = []
        for (kind, ref), c in cands.items():
            if (kind, ref) in recent:
                continue
            if kind == "fact":
                f = self.mem.get_fact(ref)
                if not f or f["status"] not in ("active", "archived") or f["no_mention"] or \
                        f["category"] in ("laika_self",):
                    continue
                if f["sensitive"] and f["entity_id"] not in named_ids:
                    continue  # painful or private things only when he brings them up
                words = _words(f["text"])
                if words and len(words & set(re.findall(r"\w{4,}", snapshot))) / len(words) >= 0.7:
                    continue  # she already carries it in the portrait or "Lately"
                entity, b, imp, ts, item_text = f["entity_id"], self.mem.activation(f, now), f["importance"], \
                    f["recorded_at"], f["text"]
                if f["status"] == "archived":  # W8.3: history, not the present
                    b, imp, item_text = b - ARCHIVED_PENALTY, imp * 0.5, f"in the past: {f['text']}"
                ep_lang = None
            else:
                ep = self.mem.get_episode(ref)
                if not ep:
                    continue
                entity = None
                b, imp, ts = 0.0, max(ep["arousal"], ep["self_rel"], ep["future_rel"]), ep["end_ts"]
                item_text = re.split(r"(?<=[.!?])\s", ep["narrative"].strip())[0]
                ep_lang = ep["lang"]
            if c["cos"] is None:
                vec = self._vector(kind, ref)
                c["cos"] = sum(a * b2 for a, b2 in zip(query, vec)) if vec else 0.0
            spread = 1.0 if entity in named_ids else (near.get(entity, 0) / top_w * 0.5 if entity in near else 0.0)
            rows.append({"kind": kind, "id": ref, "text": item_text, "ts": ts, "cos": c["cos"], "bm25": c["bm25"],
                         "B": b, "I": imp, "spread": spread, "ctx": 1.0 if ep_lang == lang_now else 0.0,
                         "goal": 1.0 if entity is not None and entity in open_cues else 0.0, "entity": entity})
        if not rows:
            return []
        for r in rows:
            r["sim"] = (r["cos"] - mean) / sd  # how much it stands out among all her memories
        for r, z in zip(rows, _z([r["bm25"] for r in rows])):
            r["zbm"] = z
        for r in rows:
            r["score"] = (WEIGHTS["B"] * r["B"] + WEIGHTS["I"] * r["I"] + WEIGHTS["sim"] * r["sim"]
                          + WEIGHTS["bm25"] * r["zbm"] + WEIGHTS["spread"] * r["spread"] + WEIGHTS["ctx"] * r["ctx"]
                          + WEIGHTS["goal"] * r["goal"])
        ok = [r for r in rows if r["score"] > TAU and (r["spread"] >= 1.0 or (r["cos"] >= COS_MIN and (
            r["sim"] >= STANDS_OUT or (r["bm25"] > 0 and r["sim"] >= STANDS_OUT - 0.5))))]
        ok.sort(key=lambda r: r["score"], reverse=True)
        picked, facts, eps = [], 0, 0
        for r in ok:  # MMR-like: skip near-duplicates of what's already picked
            if any(len(_words(r["text"]) & _words(p["text"])) / max(1, len(_words(r["text"]))) > 0.6 for p in picked):
                continue
            if r["kind"] == "fact" and facts < MAX_FACTS:
                picked.append(r)
                facts += 1
            elif r["kind"] == "episode" and eps < MAX_EPISODES:
                picked.append(r)
                eps += 1
        return picked

    def recently_offered(self):
        return recently_offered(self.db)

    async def _loop_vectors(self, loops):
        """Embeddings of open loops (a handful at most), cached by id and text."""
        cache = self.__dict__.setdefault("_loop_vecs", {})
        missing = [it for it in loops if (it["id"], it["text"]) not in cache]
        if missing:
            for it, vec in zip(missing, await self.embedder.embed([it["text"] for it in missing])):
                cache[(it["id"], it["text"])] = vec
        return [cache[(it["id"], it["text"])] for it in loops]

    async def cued_loops(self, text, query):
        """Open loops he just touched on (W8.1): a cue word plus a related message, or a clearly related
        one. Works for loops without an entity, unlike the `goal` boost. Once per visible history."""
        seen = self.recently_offered()
        loops = [it for it in self.mem.intentions(("open",)) if ("intention", it["id"]) not in seen]
        if not loops:
            return []
        low, out = text.lower(), []
        for it, vec in zip(loops, await self._loop_vectors(loops)):
            cos = sum(a * b for a, b in zip(query, vec))
            cue = any(re.search(rf"(?<!\w){re.escape(c.lower())}(?!\w)", low) for c in it["cue_terms"] if len(c) > 2)
            if cos >= LOOP_COS or (cue and cos >= LOOP_CUE_COS):
                out.append(it)
        return out[:1]

    async def mark_asked(self, bubbles):
        """W8.1: an open loop her reply raised counts as asked (it leaves the follow-up list)."""
        loops = self.mem.intentions(("open",))
        if not loops or not bubbles:
            return []
        vecs = await self._loop_vectors(loops)
        said = await self.embedder.embed([b["text"] for b in bubbles])
        asked = []
        for it, vec in zip(loops, vecs):
            for b, s in zip(bubbles, said):
                cos = sum(x * y for x, y in zip(s, vec))
                cue = any(re.search(rf"(?<!\w){re.escape(c.lower())}(?!\w)", b["text"].lower())
                          for c in it["cue_terms"] if len(c) > 2)
                if cos >= ASKED_COS or (cue and cos >= ASKED_CUE_COS):
                    self.mem.set_intention(it["id"], status="asked", asked_at=time.time())
                    log.info("open loop %s asked: %r", it["id"], it["text"][:60])
                    asked.append(it["id"])
                    break
        return asked

    async def after_turn(self, batch_id, batch, bubbles, origin):
        """A memory counts as used (one more ACT-R use) only if her reply drew on it."""
        try:
            await self.mark_asked(bubbles)
        except Exception as e:
            log.warning("open-loop check skipped: %s", e)
        offered = self.offered.pop(batch_id, None)
        if not offered or not bubbles:
            return
        reply = await self.embedder.one(" ".join(b["text"] for b in bubbles))
        used = []
        for kind, ref in offered:
            vec = self._vector(kind, ref)
            if vec and sum(a * b for a, b in zip(reply, vec)) >= USED_COS:
                self.mem.use(kind, ref)
                used.append([kind, ref])
        self.db.conn.execute("UPDATE injections SET used = ? WHERE batch_id = ?", (json.dumps(used), batch_id))


def recently_offered(db):
    """(kind, id) of items already offered within the visible history window: once is enough. Memories
    and book passages (kind 'chunk', P4.2) are both logged in `injections`."""
    start = db.get_setting("history_start_id", 0)
    row = db.conn.execute("SELECT ts FROM messages WHERE id >= ? ORDER BY id LIMIT 1", (start,)).fetchone()
    since = row[0] if row else time.time()
    out = set()
    for (items,) in db.conn.execute("SELECT items FROM injections WHERE ts >= ?", (since,)):
        out |= {(k, i) for k, i, *_ in json.loads(items)}
    return out


def _unpack(blob):
    import struct
    return list(struct.unpack(f"{len(blob) // 4}f", blob))
