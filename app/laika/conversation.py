"""Conversation engine: one continuous conversation shared by all channels.

The user's messages are stored immediately and collected into a batch. A turn starts after a
quiet gap, and never while he is composing (typing, holding a draft, recording, or a voice note
still being transcribed). The reply is streamed and split into bubbles on blank lines; each bubble
waits until he is not composing, then goes out with a short "typing" pause. A new message stops
the reply wherever it is: if nothing was sent yet, the next turn answers everything together; if
some bubbles were sent, the rest is dropped and the next turn knows she was interrupted.
"""
import asyncio
import contextlib
import logging
import re
import time

from . import lang
from .config import personal
from .persona import prompt_tokens

log = logging.getLogger(__name__)

_SPLIT = re.compile(r"\n\s*\n")
_NOISE = re.compile(r"^\s*(?:-{3,}|\*{3,}|(?:message|mensaje|сообщение)\s*\d+\s*[:.]\s*)$", re.I | re.M)
_THINK = re.compile(r"<think>.*?</think>|<\|channel>thought.*?<channel\|>", re.S)
TYPING = object()  # queue marker: the model started writing another bubble
POLL = 0.2  # seconds between checks while the user is composing
RECORDING_HOLD_S = 4  # a recording client refreshes every 2 s
INTERRUPTED_NOTE = "he wrote this while you were still replying; finish or drop your earlier thought, as feels natural"
VOICE_NOTE = "your reply will be sent as voice notes, so write it the way you'd say it out loud"
MIC = "🎤"  # a bubble starting with this is sent as a voice note (W3: she chooses to speak)
OFFER_NOTE = "he wrote in {said}; the practice language is still {current} ({why})"

COMMANDS = {
    "lang": ("practice_lang", {"en", "ru", "zh"}),
    "corrections": ("corrections", {"on", "end", "off"}),
    "voice": ("voice_mode", {"mirror", "always", "never"}),
}

# Telegram gives no typing signal, so messages that look unfinished get more patience.
_UNFINISHED_END = re.compile(r"(?:,|…|\.\.\.|:|-|—|–)\s*$")
_CONJUNCTIONS = {"and", "but", "so", "or", "because", "like", "y", "pero", "que", "porque", "o", "entonces",
                 "и", "но", "а", "что", "или", "потому", "потому что", "ну"}
_ZH_CONJUNCTIONS = ("因为", "但是", "然后", "所以", "而且", "还有", "可是", "就是")
_OPENERS = {"hey", "hi", "hello", "listen", "guess what", "so", "ok so", "oye", "mira", "hola", "sabes qué",
            "слушай", "привет", "знаешь", "кстати", "你知道吗", "我跟你说", "听我说", "对了"}


def looks_unfinished(text):
    """True when a message probably has a continuation coming (trailing comma, 'and', 'hey'...)."""
    t = text.strip().lower()
    if not t or t.endswith(("?", "!", "？", "！")):
        return False
    if _UNFINISHED_END.search(t) or t.endswith(_ZH_CONJUNCTIONS):
        return True
    words = re.findall(r"[\w']+", t)
    if words and (words[-1] in _CONJUNCTIONS or " ".join(words[-2:]) in _CONJUNCTIONS):
        return True
    return t.rstrip(".!¡¿?,") in _OPENERS


class Engine:
    def __init__(self, cfg, db, llm, persona):
        self.cfg, self.db, self.llm, self.persona = cfg, db, llm, persona
        self.channels = []        # adapters with `async deliver(event)`
        self.after_turn = []      # hooks: async fn(batch_id, user_msgs, bubbles, origin)
        self.on_trim = []         # hooks: async fn(old_start_id, new_start_id)
        self.annotate = lambda text, code: {}  # extra meta for each bubble (e.g. pinyin)
        self.voice_policy = lambda batch: False  # whether this turn's bubbles are also spoken
        self.voice_allowed = lambda: True         # whether she may choose to speak one bubble (🎤)
        self.on_bubble = []       # hooks: async fn(message, origin, voice), run in order per bubble
        self.before_turn = []     # hooks: async fn(batch, batch_id) -> extra notes (memory commands, recall)
        self.long_text = None     # hook: async fn(channel, text) -> text to keep (P4.1: long pastes go to the library)
        self.commands = {}        # extra slash commands: name -> async fn(channel, arg) -> reply text or None (P4.2)
        self.pending = []
        self.first_pending = None
        self.deadline = 0.0
        self.timer = None
        self.turn = None
        self.composing_until = 0.0  # monotonic time until which the user counts as composing
        self.inbound = 0            # voice notes still being transcribed
        self.interrupted = False    # the next turn starts with a note that she was cut off

    # input ------------------------------------------------------------------------------
    async def receive(self, channel, text, voice=False, audio_path=None, meta=None):
        text = text.strip()
        if text.startswith("/") and await self._command(channel, text):
            return None
        meta = {**(meta or {}), **({"voice": True} if voice else {})}
        if self.long_text and not voice:
            text = await self.long_text(channel, text)
        msg = self.db.add_message("user", channel, text, lang=lang.detect(text), status="received",
                                  meta=meta, audio_path=audio_path)
        await self.publish({"type": "message", "message": msg, "origin": channel})
        now = time.monotonic()
        self.composing_until = min(self.composing_until, now)  # sending ends this message's typing
        if self.turn and not self.turn.done():
            log.info("new message during the reply: stopping it")
            self.turn.cancel()
        self.pending.append(msg)
        if self.first_pending is None:
            self.first_pending = now
        self._schedule(now + self._quiet(channel, voice, msg))
        return msg

    async def typing(self, active=True, draft=False, recording=False):
        """Composing signal from a channel: a keystroke, a held draft, recording, or stop."""
        b, now = self.cfg["batching"], time.monotonic()
        if not active:
            self.composing_until = now
        elif recording:
            self.composing_until = max(self.composing_until, now + RECORDING_HOLD_S)
        else:
            self.composing_until = now + (b["draft_hold_s"] if draft else b["typing_idle_s"])

    def composing(self):
        return self.inbound > 0 or time.monotonic() < self.composing_until

    @contextlib.asynccontextmanager
    async def incoming(self):
        """Hold replies while an inbound item (a voice note) is still being processed."""
        self.inbound += 1
        try:
            yield
        finally:
            self.inbound -= 1

    def _quiet(self, channel, voice, msg):
        b = self.cfg["batching"]
        if voice:
            return b["voice_quiet_s"]
        if channel != "telegram":
            return b["web_quiet_s"]
        quiet = b["telegram_quiet_s"]
        if looks_unfinished(msg["text"]):
            quiet = max(quiet, b["telegram_unfinished_s"])
        earlier = [m for m in self.pending if m["id"] != msg["id"]]
        if earlier and (gap := msg["ts"] - earlier[-1]["ts"]) < 60:
            quiet = max(quiet, min(b["burst_max_s"], b["burst_factor"] * gap))
        return quiet

    def _schedule(self, deadline):
        self.deadline = deadline
        if self.timer is None or self.timer.done():
            self.timer = asyncio.create_task(self._wait_quiet())

    async def _wait_quiet(self):
        b = self.cfg["batching"]
        while True:
            now = time.monotonic()
            start = self.first_pending if self.first_pending is not None else now
            if self.composing() and now - start < b["max_hold_s"]:
                await asyncio.sleep(POLL)
                continue
            due = min(self.deadline, start + b["max_wait_s"])
            if now >= due:
                break
            await asyncio.sleep(min(due - now, POLL))
        if self.turn and not self.turn.done():
            await asyncio.wait({self.turn})
        if self.pending:
            self.turn = asyncio.create_task(self._run_turn())

    async def _command(self, channel, text):
        name, _, arg = text[1:].partition(" ")
        name = name.lower()
        if name in self.commands:
            if reply := await self.commands[name](channel, arg.strip()):
                msg = self.db.add_message("system", channel, reply)
                await self.publish({"type": "message", "message": msg, "origin": channel})
            return True
        if name not in COMMANDS:
            return False
        key, allowed = COMMANDS[name]
        arg = arg.strip().lower()
        if arg in allowed and key == "practice_lang":
            await self.set_practice_lang(arg, channel)
            return True
        if arg not in allowed:
            reply = f"/{name} {' | '.join(sorted(allowed))}"
        else:
            self.db.set_setting(key, arg)
            reply = f"✓ {key} = {lang.NATIVE_NAMES.get(arg, arg)}"
        msg = self.db.add_message("system", channel, reply)
        await self.publish({"type": "message", "message": msg, "origin": channel})
        return True

    # turn -------------------------------------------------------------------------------
    async def _run_turn(self):
        batch, self.pending, self.first_pending = self.pending, [], None
        origin = batch[-1]["channel"]
        batch_id = self.db.open_batch(origin)
        for m in batch:
            self.db.update_message(m["id"], status="read", batch_id=batch_id)
        await self.publish({"type": "read", "ids": [m["id"] for m in batch], "origin": origin})
        offered = await self._offer_language(batch, origin)
        voice = self.voice_policy(batch)
        notes = ([personal(INTERRUPTED_NOTE, self.cfg)] if self.interrupted else []) + ([VOICE_NOTE] if voice else []) + \
            ([offered] if offered else [])
        for hook in self.before_turn:
            try:
                notes += await hook(batch, batch_id)
            except Exception:
                log.exception("before-turn hook %s failed", getattr(hook, "__name__", hook))
        bubbles = []
        try:
            raw = await self._generate(batch_id, batch, origin, voice, bubbles, notes)
        except asyncio.CancelledError:
            if bubbles:  # he got part of the answer: drop the rest, answer his new message next
                self.db.close_batch(batch_id, "interrupted")
                self.interrupted = True
                for hook in self.after_turn:
                    asyncio.create_task(self._safe(hook, batch_id, batch, bubbles, origin))
            else:  # nothing was said yet: answer everything together
                self.db.close_batch(batch_id, "cancelled")
                self.pending = batch + self.pending
                if self.first_pending is None:
                    self.first_pending = time.monotonic()
            await self.publish({"type": "typing_stop", "origin": origin})
            raise
        except Exception:
            log.exception("turn %s failed", batch_id)
            self.db.close_batch(batch_id, "failed")
            await self.publish({"type": "typing_stop", "origin": origin})
            await self.publish({"type": "error", "text": "I couldn't answer (the model failed). Try again in a moment.",
                                "origin": origin})
            return
        self.interrupted = False
        self.db.close_batch(batch_id, "done", raw=raw)
        self._trim_history()
        for hook in self.after_turn:
            asyncio.create_task(self._safe(hook, batch_id, batch, bubbles, origin))

    # practice language (W5, DECISIONS D17: ask first) -------------------------------------
    async def _offer_language(self, batch, origin):
        """He clearly wrote in another practice language: ask whether to switch, and change nothing.
        Returns a note for this turn, so she keeps to the practice language without being confused."""
        text = " ".join(m["text"] for m in batch)
        detected = lang.detect(text)
        current = self.persona.practice_lang()
        cjk, cyr, latin = lang.counts(text)
        # Mixed messages ("what does привет mean?") keep the current language.
        uses_current = {"zh": cjk, "ru": cyr, "en": latin}[current] > 0
        words = len(text.split()) + cjk // 2  # Chinese has no spaces: count ~2 characters per word
        if detected not in lang.PRACTICE or detected == current or uses_current or words < 3:
            return None
        said, still = lang.NAMES[detected], lang.NAMES[current]
        if self.db.get_setting("lang_offer_snooze", {}).get(detected, 0) > time.time():
            return personal(OFFER_NOTE.format(said=said, current=still, why="he chose to keep it for now"), self.cfg)
        offer = self._open_offer()
        if offer and offer["meta"]["offer"]["practice_lang"] != detected:
            await self._close_offer(offer, "expired")  # one open offer at a time: the newest language
            offer = None
        if offer is None:
            msg = self.db.add_message("system", origin, f"Switch to {lang.NATIVE_NAMES[detected]}?",
                                      meta={"offer": {"practice_lang": detected}})
            self.db.set_setting("lang_offer", msg["id"])
            await self.publish({"type": "message", "message": msg, "origin": origin})
        return personal(OFFER_NOTE.format(said=said, current=still, why="the app asked him whether to switch"), self.cfg)

    async def set_practice_lang(self, code, origin):
        """The one way the practice language changes: /lang, a "Switch" tap, or the PWA selector."""
        self.db.set_setting("practice_lang", code)
        msg = self.db.add_message("system", origin, f"✓ practice_lang = {lang.NATIVE_NAMES[code]}")
        await self.publish({"type": "message", "message": msg, "origin": origin})
        await self.publish({"type": "settings", "origin": origin})
        if offer := self._open_offer():
            await self._close_offer(offer, "switched" if offer["meta"]["offer"]["practice_lang"] == code
                                    else "dismissed")

    async def answer_offer(self, message_id, accept, origin):
        """His tap on "Switch" or "Not now". False if that offer is no longer open."""
        offer = self._open_offer()
        if not offer or offer["id"] != message_id:
            return False
        code = offer["meta"]["offer"]["practice_lang"]
        if accept:
            await self.set_practice_lang(code, origin)
        else:
            snooze = self.db.get_setting("lang_offer_snooze", {})
            snooze[code] = time.time() + self.cfg["language"].get("offer_snooze_h", 3) * 3600
            self.db.set_setting("lang_offer_snooze", snooze)
            await self._close_offer(offer, "not_now")
        return True

    def _open_offer(self):
        msg = self.db.get_message(self.db.get_setting("lang_offer") or 0)
        offer = msg and msg["meta"].get("offer")
        return msg if offer and not offer.get("answer") else None

    async def _close_offer(self, offer, answer):
        """Record the answer and publish the message again, so channels can remove its buttons."""
        self.db.set_setting("lang_offer", None)
        self.db.set_meta(offer["id"], offer={**offer["meta"]["offer"], "answer": answer})
        await self.publish({"type": "message", "message": self.db.get_message(offer["id"]), "origin": offer["channel"],
                            "update": True})

    async def _generate(self, batch_id, batch, origin, voice, bubbles, notes):
        """Stream the reply into bubbles; returns the reply exactly as generated."""
        messages, anchor, note = self.persona.build_turn(batch, notes)
        queue = asyncio.Queue()
        deliver = asyncio.create_task(self._deliver(queue, batch_id, origin, bubbles, voice, (anchor, note)))
        # `signalled` False: the first real text of each bubble shows "typing", not the reading phase.
        max_bubbles, produced, buf, signalled, raw = self.cfg["bubbles"]["max"], 0, "", False, ""
        guard = self.persona.practice_lang() == "en"
        grounding = self.persona.grounding(messages) if guard else None
        try:
            async for delta in self.llm.stream_chat(messages, self.cfg["llm"]["conversation_slot"], guard=guard,
                                                    grounding=grounding):
                raw += delta
                buf += delta
                while produced < max_bubbles - 1 and (m := _SPLIT.search(buf)):
                    part, buf = _clean(buf[:m.start()]), buf[m.end():]
                    if part:
                        await queue.put(part)
                        produced, signalled = produced + 1, False
                if not signalled and buf.strip():
                    await queue.put(TYPING)
                    signalled = True
            if tail := _clean(buf):
                await queue.put(tail)
            await queue.put(None)
            await deliver
        finally:
            if not deliver.done():
                deliver.cancel()
        return raw

    async def _deliver(self, queue, batch_id, origin, bubbles, voice, anchor_note):
        b = self.cfg["bubbles"]
        last, chose_voice, practice = time.monotonic(), False, self.persona.practice_lang()
        while (item := await queue.get()) is not None:
            if item is TYPING:
                if not self.composing():
                    await self.publish({"type": "typing", "origin": origin})
                continue
            target = min(b["max_delay_s"], b["min_delay_s"] + len(item) * b["ms_per_char"] / 1000)
            if (wait := target - (time.monotonic() - last)) > 0:
                await asyncio.sleep(wait)
            if self.composing():
                await self._hold_while_composing(origin)
            speak = voice
            if item.startswith(MIC):  # she chose to say this one out loud
                item = item.lstrip(MIC + "\ufe0f").strip()
                if not voice and not chose_voice and self.voice_allowed():
                    speak = chose_voice = True
                if not item:
                    continue
            code = lang.bubble_lang(item, practice)  # W6: Chinese with pinyin/English glosses is still zh
            msg = self.db.add_message("companion", origin, item, lang=code, batch_id=batch_id,
                                      meta=self.annotate(item, code))
            if not bubbles:  # she answered: from now on this user turn is replayed with the note it was sent with
                self.db.set_meta(anchor_note[0], note=anchor_note[1])
            bubbles.append(msg)
            await self.publish({"type": "message", "message": msg, "origin": origin, "voice": speak})
            for hook in self.on_bubble:
                await hook(msg, origin, speak)
            last = time.monotonic()
        await self.publish({"type": "typing_stop", "origin": origin})

    async def _hold_while_composing(self, origin):
        """He started typing while she was replying: stop the dots and wait, like a person would."""
        await self.publish({"type": "typing_stop", "origin": origin})
        started = time.monotonic()
        while self.composing() and time.monotonic() - started < self.cfg["batching"]["max_hold_s"]:
            await asyncio.sleep(POLL)
        await self.publish({"type": "typing", "origin": origin})
        await asyncio.sleep(self.cfg["bubbles"]["min_delay_s"])

    def _trim_history(self):
        """Move the history start forward in one jump when over budget (keeps the prompt cache warm)."""
        h = self.cfg["history"]
        start = self.db.get_setting("history_start_id", 0)
        msgs = self.db.conversation_since(start)
        sizes = [prompt_tokens(m) for m in msgs]
        if sum(sizes) <= h["budget_tokens"]:
            return
        keep, total = h["budget_tokens"] * h["keep_after_trim"], 0
        for i in range(len(msgs) - 1, -1, -1):
            total += sizes[i]
            if total > keep:
                new_start = msgs[i + 1]["id"] if i + 1 < len(msgs) else msgs[-1]["id"]
                self.db.set_setting("history_start_id", new_start)
                log.info("history trimmed: start %s -> %s", start, new_start)
                for hook in self.on_trim:
                    asyncio.create_task(self._safe(hook, start, new_start))
                return

    # output -----------------------------------------------------------------------------
    async def publish(self, event):
        for ch in self.channels:
            try:
                await ch.deliver(event)
            except Exception:
                log.exception("channel %s failed to deliver %s", getattr(ch, "name", ch), event.get("type"))

    async def _safe(self, fn, *args):
        try:
            await fn(*args)
        except Exception:
            log.exception("background hook %s failed", getattr(fn, "__name__", fn))


def _clean(text):
    text = _THINK.sub("", text)
    text = _NOISE.sub("", text)
    return text.strip()
