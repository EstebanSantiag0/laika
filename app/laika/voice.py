"""Voice notes in (speech-to-text) and voice replies out (text-to-speech), via the voice service."""
import asyncio
import logging
import pathlib
import re
import time

import httpx

from . import config, lang

log = logging.getLogger(__name__)

ZH_PROMPT = "以下是普通话的句子，使用简体中文。"  # nudges Whisper towards simplified characters
LOW_CONFIDENCE = -1.0  # mean log-prob below this: retry with automatic language detection

# The user asking to hear her (W3): the whole reply is spoken, whatever the voice mode (except "never").
# W6: "could you say that using you voice?" (msg 178) was missed, so only her own 🎤 choice was spoken.
VOICE_REQUEST = re.compile(
    r"\b(send|give) (me )?(a |an |your )?(voice( note| message)?|audio)\b|\bvoice (note|message)\b"
    r"|\b(say|read) (it|that|this)( one)? (out loud|aloud)\b|\bhow (do|would) you (say|pronounce)\b"
    r"|\bcan i hear (you|it|your voice)\b"
    r"|\b(say|read|pronounce|repeat)\b[^.!?\n]{0,60}?\b(using|with|in) (your|you|ur) voice\b"
    r"|\b(with|using) (your|you|ur) voice\b"
    r"|\b(m[aá]nd|envi|env[ií]|pas)\w* (me |un )*(audio|nota de voz)|\bnota de voz\b|\ben voz alta\b"
    r"|\bc[oó]mo se pronuncia\b|\b(o[ií]r|escuchar) tu voz\b|\bcon tu (propia )?voz\b|\bdilo\b"
    r"|\bpron[uú]ncia(me|lo|la)"
    r"|голосов(ое|ым|ую|ое сообщение)|скажи вслух|как (это )?произносится|произнеси|\bголосом\b|скажи это"
    r"|发(个|一条)?语音|语音消息|怎么(读|念|发音)|(读|念)(一下|出来)|用(你的)?声音|说一下|说给我听",
    re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def speech_text(text, code):
    """What the voice says for a bubble: no emojis or markdown; RU/ZH bubbles without the English glosses
    and pinyin in parentheses ("кошка (cat)": the help stays in the caption)."""
    text = _EMOJI.sub("", text)
    text = re.sub(r"[*_#`~]+", "", text)
    if code in ("ru", "zh") and re.search(r"\w", stripped := lang.GLOSS.sub("", text)):
        text = stripped
    return re.sub(r"[ \t]{2,}", " ", text).strip()


class Voice:
    def __init__(self, cfg, db, engine):
        self.cfg, self.db, self.engine = cfg, db, engine
        self.url = cfg["voice"]["url"].rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10))
        self.audio_dir = config.DATA_DIR / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.queue = asyncio.Queue()
        self.worker = None

    def mode(self):
        return self.db.get_setting("voice_mode", self.cfg["voice"]["mode"])

    def wants_voice(self, batch):
        mode = self.mode()
        if mode == "never":
            return False
        asked = any(VOICE_REQUEST.search(m["text"]) for m in batch if not m["meta"].get("voice"))
        return mode == "always" or asked or any(m["meta"].get("voice") for m in batch)

    def allowed(self):
        """May she choose to send a voice note on her own (🎤)? Not when the user set voice to never."""
        return self.mode() != "never"

    # in -----------------------------------------------------------------------------------
    async def receive_note(self, channel, data, filename, meta=None):
        """Store the user's voice note, transcribe it, and pass the text to the conversation."""
        suffix = pathlib.Path(filename).suffix or ".bin"
        name = f"in-{int(time.time() * 1000)}{suffix}"
        (self.audio_dir / name).write_bytes(data)
        async with self.engine.incoming():  # she doesn't answer while his voice note is being transcribed
            result = await self.transcribe(data, filename)
        text = result["text"].strip()
        if not text:
            msg = self.db.add_message("system", channel, "🎤 (I couldn't hear anything in that voice note)")
            await self.engine.publish({"type": "message", "message": msg, "origin": channel})
            return None
        return await self.engine.receive(channel, text, voice=True, audio_path=name,
                                         meta={**(meta or {}), "stt_lang": result["language"],
                                               "duration": result["duration"]})

    async def transcribe(self, data, filename):
        code = self.engine.persona.practice_lang()
        result = await self._stt(data, filename, code, ZH_PROMPT if code == "zh" else "")
        if result.get("avg_logprob", 0) < LOW_CONFIDENCE:
            auto = await self._stt(data, filename, "", "")
            log.info("low-confidence %s transcript (%.2f); auto-detect gave %s (%.2f)", code,
                     result.get("avg_logprob", 0), auto.get("language"), auto.get("avg_logprob", 0))
            if auto.get("avg_logprob", -99) > result.get("avg_logprob", -99):
                result = auto
        return result

    async def _stt(self, data, filename, language, prompt):
        r = await self.client.post(f"{self.url}/v1/audio/transcriptions", files={"file": (filename, data)},
                                   data={"language": language, "prompt": prompt})
        r.raise_for_status()
        return r.json()

    # out ----------------------------------------------------------------------------------
    async def speak(self, text, code, fmt):
        r = await self.client.post(f"{self.url}/v1/audio/speech", json={
            "input": speech_text(text, code), "voice": self.cfg["voice"]["default_voice"], "language": code or "auto",
            "speed": float(self.db.get_setting("voice_speed", 1.0)), "response_format": fmt})
        r.raise_for_status()
        return r.content

    async def on_bubble(self, msg, origin, voice):
        if not voice:
            return
        await self.queue.put((msg, origin))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._work())

    async def drained(self):
        """Wait until every queued bubble has been spoken (keeps corrections after the voice notes)."""
        await self.queue.join()

    async def _work(self):
        while True:
            msg, origin = await self.queue.get()
            try:
                if not speech_text(msg["text"], msg["lang"]):  # e.g. an emoji-only bubble: send it as text
                    await self.engine.publish({"type": "audio_failed", "message": msg, "origin": origin})
                    continue
                await self.engine.publish({"type": "recording", "origin": origin})
                fmt = "opus" if origin == "telegram" else "m4a"
                data = await self.speak(msg["text"], msg["lang"], fmt)
                name = f"out-{msg['id']}.{'ogg' if fmt == 'opus' else fmt}"
                (self.audio_dir / name).write_bytes(data)
                self.db.update_message(msg["id"], audio_path=name)
                await self.engine.publish({"type": "audio", "message": self.db.get_message(msg["id"]),
                                           "path": str(self.audio_dir / name), "origin": origin})
            except Exception:
                log.exception("text-to-speech failed for message %s", msg["id"])
                await self.engine.publish({"type": "audio_failed", "message": msg, "origin": origin})
            finally:
                self.queue.task_done()
