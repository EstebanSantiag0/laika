"""The user's explicit memory requests, handled before she answers (PLAN P3.4, DECISIONS D14).

- "remember that…" / "recuerda que…" / "запомни…" / "记住…": saved at once as something he told her.
- "forget…" / "olvida…" / "забудь…" / "忘记…": really forgotten. Matching facts are deleted, matching
  sentences leave her diary, verbatim rounds that contain it are deleted, and a tombstone (a digest
  and a vector, never the text) keeps it from being learned again. A system bubble shows what went.
  The chat log itself stays: it is his record.
- "don't bring up…" / "no menciones…" / "не упоминай…" / "别提…": she keeps it but never raises it.

Only clear imperatives count: "do you remember…?" is a question for recall, and "forget it" (never
mind) is not a command.
"""
import logging
import re

from .config import personal, user_name
from .memory import digest

log = logging.getLogger(__name__)

_START = r"(?:^|(?<=[.!?¡¿。！？\n])\s*)(?:please,? |pls,? |por favor,? |пожалуйста,? |请)?"
REMEMBER = re.compile(_START + r"(?:remember|don'?t forget|recuerda|acu[eé]rdate de|no olvides|запомни|记住)"
                      r"[,:]?\s*(?:that |que |, ?что |что )?(?P<what>[^.?!。！？\n]{4,})", re.I)
FORGET = re.compile(_START + r"(?:forget|olvida|olv[ií]date de|забудь|忘记|忘掉)[,:]?\s*(?:about |that |what i said "
                    r"about |lo de |que |eso de |про |о |что )?(?P<what>[^.?!。！？\n]{3,})", re.I)
NO_MENTION = re.compile(_START + r"(?:don'?t|do not|never) (?:bring up|mention|talk about)|"
                        + _START + r"no (?:menciones|hables de|saques el tema de)|"
                        + _START + r"не (?:упоминай|вспоминай|говори (?:о|об|про))|" + _START + r"别(?:再)?提",
                        re.I)
_NO_MENTION_WHAT = re.compile(r"(?:bring up|mention|talk about|menciones|hables de|saques el tema de|упоминай|"
                              r"вспоминай|говори (?:о|об|про)|别(?:再)?提)\s*(?:again |más |больше )?"
                              r"(?P<what>[^.?!。！？\n]{3,})", re.I)
DISMISSAL = re.compile(r"^(?:it|about it|that|this|everything|lo|eso|todo|это|всё|все|它|这个)\W*$", re.I)
FORGET_COS = 0.6
NO_MENTION_COS = 0.55


def _content(what):
    what = what.strip(" ,;:")
    return None if not what or DISMISSAL.match(what) else what


class MemoryCommands:
    def __init__(self, cfg, db, memory, embedder, engine):
        self.cfg, self.db, self.mem, self.embedder, self.engine = cfg, db, memory, embedder, engine

    async def before_turn(self, batch, batch_id=None):
        """Notes for this turn about what she just saved, forgot or will leave alone."""
        notes = []
        for m in batch:
            text = m["text"]
            if (hit := FORGET.search(text)) and (what := _content(hit["what"])):
                notes.append(await self.forget(what, m["channel"]))
            elif NO_MENTION.search(text) and (hit := _NO_MENTION_WHAT.search(text)) and (what := _content(hit["what"])):
                notes.append(await self.no_mention(what))
            elif (hit := REMEMBER.search(text)) and (what := _content(hit["what"])):
                notes.append(await self.remember(what, m["id"]))
        return notes

    async def remember(self, what, msg_id):
        vec = await self.embedder.one(what)
        me = self.mem.entity(user_name(self.cfg), "person")
        self.db.conn.execute("DELETE FROM tombstones WHERE digest = ?", (digest(what),))  # his request wins
        self.mem.add_fact({"entity_id": me["id"], "text": what[0].upper() + what[1:], "category": "identity",
                           "importance": 0.7, "confidence": 1.0, "origin": "told", "evidence": [msg_id, msg_id]}, vec)
        log.info("memory: saved something the user asked her to remember")
        return personal("he asked you to remember this, and you saved it: ", self.cfg) + what

    async def forget(self, what, channel):
        vec = await self.embedder.one(what, query=True)
        words = {w for w in re.findall(r"\w{3,}", what.lower())}
        facts = [ref for kind, ref, cos in self.mem.search_vec(vec, k=10, kinds=("fact",)) if cos >= FORGET_COS]
        texts = [self.mem.get_fact(f)["text"] for f in facts if self.mem.get_fact(f)]
        vecs = await self.embedder.embed(texts) if texts else []
        rounds = [r["id"] for r in self.db.conn.execute("SELECT id, text FROM rounds")
                  if words and all(w in r["text"].lower() for w in words)]
        self.mem.forget(fact_ids=facts, round_ids=rounds, texts=texts + [what],
                        embeddings=vecs + [await self.embedder.one(what)])
        trimmed = 0
        for ep in self.db.conn.execute("SELECT id, narrative FROM episodes").fetchall():
            sentences = re.split(r"(?<=[.!?])\s+", ep["narrative"])
            kept = [s for s in sentences if not (words and all(w in s.lower() for w in words))]
            if len(kept) < len(sentences):
                self.db.conn.execute("UPDATE episodes SET narrative = ? WHERE id = ?", (" ".join(kept), ep["id"]))
                trimmed += 1
        listed = "; ".join(t[:60] for t in texts[:5])
        bubble = (f"🧹 Forgot: {listed}" if texts else "🧹 Forgot what you asked") + \
                 (f" (+{len(rounds)} saved messages, {trimmed} diary lines)" if rounds or trimmed else "")
        msg = self.db.add_message("system", channel, bubble)
        await self.engine.publish({"type": "message", "message": msg, "origin": channel})
        log.info("memory: forgot %d facts, %d rounds, %d diary lines", len(facts), len(rounds), trimmed)
        return personal("he asked you to forget this ({what}); it's gone from your memory, so don't bring it up",
                        self.cfg).format(what=what)

    async def no_mention(self, what):
        vec = await self.embedder.one(what, query=True)
        hits = [ref for kind, ref, cos in self.mem.search_vec(vec, k=10, kinds=("fact",)) if cos >= NO_MENTION_COS]
        for ref in hits:
            self.mem.update_fact(ref, no_mention=1)
        return personal("he asked you not to bring this up ({what}); you won't unless he does", self.cfg).format(what=what)
