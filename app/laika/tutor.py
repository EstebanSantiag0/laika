"""Language coaching that runs beside the conversation, on the background slot.

- After each turn, a tutor pass returns JSON corrections for the user's messages, sent as one ✏️ bubble
  (mode "on"), or collected and sent once the conversation goes quiet (mode "end").
- Chinese bubbles get a pinyin line (for the Chinese only, W6).
- What it found (or that it didn't run) goes to `on_checked`: his words feed the vocabulary (P4.4).
"""
import asyncio
import logging
import re

from pypinyin import Style, pinyin

from . import lang
from .config import personal
from .persona import level_band

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "better": {"type": "string"},
                    "why": {"type": "string"},
                    "type": {"enum": ["grammar", "vocabulary", "spelling", "word_order", "register"]},
                },
                "required": ["original", "better", "why", "type"],
            },
        },
    },
    "required": ["corrections"],
}

PROMPT = """You are a precise {lang} teacher. {user} (native Spanish speaker, {lang} level: {level}) \
wrote the messages below in a casual chat. Find his real mistakes: grammar, wrong words, spelling, word \
order, or phrasing a native speaker would not use. Ignore capitalization and punctuation that is normal \
in chats, and ignore obvious one-off typos. Give at most 3 corrections, most important first. "original" \
is his exact words, "better" is the corrected version, "why" is one short, simple sentence in {why_lang}. \
{focus} If there are no real mistakes, return an empty list."""

FOCUS = {
    "beginner": "He is a beginner: only correct what matters for being understood or what is very basic.",
    "intermediate": "Focus on grammar and word choice that would sound wrong to a native speaker.",
    "advanced": "His level is very high: point out only unnatural phrasing, collocations, register or nuance, "
                "the kind of thing a native editor would change.",
}

END_MODE_IDLE_S = 600
_HAN = re.compile("([\u3400-\u4dbf\u4e00-\u9fff]+)")
_FULLWIDTH = str.maketrans({"，": ", ", "。": ". ", "！": "! ", "？": "? ", "、": ", ", "；": "; ", "：": ": "})


class Tutor:
    def __init__(self, cfg, db, llm, engine):
        self.cfg, self.db, self.llm, self.engine = cfg, db, llm, engine
        self.end_timer = None
        self.before_send = None   # awaitable factory, e.g. wait for pending voice notes
        self.on_checked = None    # fn(texts, code, fixes or None): his words for the vocabulary (P4.4)

    def mode(self):
        return self.db.get_setting("corrections", self.cfg["language"]["corrections"])

    async def after_turn(self, batch_id, batch, bubbles, origin):
        mode = self.mode()
        code = self.engine.persona.practice_lang()
        texts = [m["text"] for m in batch if lang.detect(m["text"]) == code]
        if not texts:
            return
        if mode == "off":
            self._checked(texts, code, None)
            return
        level = self.engine.persona.level(code)
        band = level_band(level)
        system = personal(PROMPT, self.cfg).format(lang=lang.NAMES[code], level=level, focus=personal(FOCUS[band], self.cfg),
                               why_lang="English" if band == "beginner" else lang.NAMES[code])
        result = await self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(texts)}],
            self.cfg["llm"]["background_slot"], json_schema=SCHEMA, temperature=0.2)
        fixes = [c for c in result.get("corrections", [])
                 if c.get("original", "").strip() and c["original"].strip() != c.get("better", "").strip()]
        self._checked(texts, code, fixes)
        if not fixes:
            return
        if mode == "on":
            await self.send(fixes, code, origin, batch_id)
        else:
            stored = self.db.get_setting("pending_corrections", [])
            self.db.set_setting("pending_corrections", stored + [{**f, "lang": code} for f in fixes])
            self._restart_end_timer(origin)

    def _checked(self, texts, code, fixes):
        if self.on_checked:
            try:
                self.on_checked(texts, code, fixes)
            except Exception:
                log.exception("vocabulary update failed")

    async def send(self, fixes, code, origin, batch_id=None):
        if self.before_send:
            await self.before_send()
        text = "\n".join(f"{f['original']} → {f['better']} ({f['why']})" for f in fixes)
        msg = self.db.add_message("correction", origin, text, lang=code, batch_id=batch_id,
                                  meta={"corrections": fixes})
        await self.engine.publish({"type": "message", "message": msg, "origin": origin})

    def _restart_end_timer(self, origin):
        if self.end_timer and not self.end_timer.done():
            self.end_timer.cancel()
        self.end_timer = asyncio.create_task(self._flush_after_idle(origin))

    async def _flush_after_idle(self, origin):
        await asyncio.sleep(END_MODE_IDLE_S)
        if self.engine.pending or (self.engine.turn and not self.engine.turn.done()):
            self._restart_end_timer(origin)
            return
        fixes = self.db.get_setting("pending_corrections", [])
        if fixes:
            self.db.set_setting("pending_corrections", [])
            await self.send(fixes[-6:], fixes[-1]["lang"], origin)

    def annotate(self, text, code):
        """Extra metadata for a companion bubble (pinyin for Chinese)."""
        if code != "zh" or not (line := pinyin_line(text)):
            return {}
        return {"pinyin": line}


def pinyin_line(text):
    """Pinyin for the Chinese in a bubble only: glosses and inline pinyin in parentheses are left out,
    and English between two Chinese phrases becomes " · ", unless it's one word (a name: 我是 María，你呢？)."""
    stripped = lang.GLOSS.sub("", text)
    pieces = _HAN.split(stripped if _HAN.search(stripped) else text)  # odd pieces are Chinese
    out = []
    for i in range(1, len(pieces), 2):
        if i > 1:
            out.append(_between(pieces[i - 1]))
        out.append(" ".join(s[0] for s in pinyin(pieces[i], style=Style.TONE)))
    if len(pieces) > 1:  # after the last Chinese, to the end of that sentence: 你呢？ / 我叫 Laika.
        out.append(_between(re.match(r".*?(?:[.!?…。！？]+|$)", pieces[-1], re.S)[0], tail=True))
    line = re.sub(r"\s+([,.!?;:…])", r"\1", "".join(out))  # no space before punctuation
    return re.sub(r"\s{2,}", " ", line).strip().rstrip(",;:")


def _between(text, tail=False):
    """Non-Chinese text in the pinyin line: kept when it's one word at most (a name), else left out."""
    text = re.sub(r"[^\w\s,.!?;:…-]", "", text.translate(_FULLWIDTH))
    if len(re.findall(r"[A-Za-z]+", text)) <= 1:
        return f" {text} "
    return re.match(r"\s*[,.!?;:…]*", text)[0] if tail else " · "
