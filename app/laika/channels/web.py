"""Web channel: REST + WebSocket for the PWA. Every event is broadcast to all open tabs."""
import asyncio
import logging
import re

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

log = logging.getLogger(__name__)

SETTINGS = {
    "practice_lang": {"en", "ru", "zh"},
    "corrections": {"on", "end", "off"},
    "voice_mode": {"mirror", "always", "never"},
    "pinyin": {True, False},
    "voice_speed": {1.0, 0.85, 0.7},
    "level_en": None, "level_ru": None, "level_zh": None,  # free text, e.g. "B1"
}


MAX_MESSAGE = 300_000  # long pastes are allowed: the library keeps them, the chat keeps the start (P4.1)

# The slim image has no /etc/mime.types, so map audio types explicitly (Safari needs them).
AUDIO_TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav",
               ".webm": "audio/webm", ".mp4": "audio/mp4", ".bin": "application/octet-stream"}


class WebChannel:
    name = "web"

    def __init__(self, engine, db, cfg, voice, library=None, vocab=None):
        self.engine, self.db, self.cfg, self.voice, self.library, self.vocab = engine, db, cfg, voice, library, vocab
        self.sockets = set()
        self.router = APIRouter()
        r = self.router
        r.add_api_route("/api/history", self.history, methods=["GET"])
        r.add_api_route("/api/settings", self.get_settings, methods=["GET"])
        r.add_api_route("/api/settings", self.put_settings, methods=["PUT"])
        r.add_api_route("/api/profile", self.profile, methods=["GET"])
        r.add_api_route("/api/voice", self.upload_voice, methods=["POST"])
        r.add_api_route("/api/audio/{message_id}", self.audio, methods=["GET"])
        r.add_api_route("/api/offer/{message_id}", self.offer, methods=["POST"])
        r.add_api_route("/api/library", self.upload_library, methods=["POST"])
        r.add_api_route("/api/library/{doc_id}/anki", self.anki, methods=["GET"])
        r.add_api_websocket_route("/ws", self.ws)

    async def deliver(self, event):
        if not self.sockets:
            return
        if event["type"] == "settings" and "settings" not in event:  # the engine changed one (W5)
            event = {**event, "settings": self.settings()}
        event = {k: v for k, v in event.items() if k != "path"}
        dead = []
        for ws in list(self.sockets):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        self.sockets.difference_update(dead)

    async def history(self, before: int | None = Query(None), limit: int = Query(50, le=200)):
        return {"messages": self.db.history(before, limit)}

    def settings(self):
        lang_cfg = self.cfg["language"]
        return {
            "practice_lang": self.engine.persona.practice_lang(),
            "corrections": self.db.get_setting("corrections", lang_cfg["corrections"]),
            "voice_mode": self.db.get_setting("voice_mode", self.cfg["voice"]["mode"]),
            "pinyin": self.db.get_setting("pinyin", lang_cfg["pinyin"]),
            "voice_speed": self.db.get_setting("voice_speed", 1.0),
            **{f"level_{c}": self.engine.persona.level(c) for c in ("en", "ru", "zh")},
        }

    async def get_settings(self):
        return self.settings()

    async def put_settings(self, body: dict):
        for key, value in body.items():
            allowed = SETTINGS.get(key, ())
            if key not in SETTINGS or (allowed is not None and value not in allowed):
                return JSONResponse({"error": f"invalid setting {key}={value!r}"}, status_code=400)
        for key, value in body.items():
            if key == "practice_lang":  # W5: the same path as /lang and the offer (closes an open offer)
                await self.engine.set_practice_lang(value, "web")
            else:
                self.db.set_setting(key, str(value)[:40] if SETTINGS[key] is None else value)
        await self.deliver({"type": "settings", "settings": self.settings()})
        return self.settings()

    async def offer(self, message_id: int, body: dict):
        """His answer to "Switch to …?" (W5): {"accept": true} switches, false is "Not now"."""
        return {"ok": await self.engine.answer_offer(message_id, bool(body.get("accept")), "web")}

    async def upload_voice(self, file: UploadFile = File(...)):
        data = await file.read()
        if not data or len(data) > 20 * 2**20:
            raise HTTPException(400, "empty or too large (max 20 MB)")
        msg = await self.voice.receive_note("web", data, file.filename or "voice.webm")
        return {"message": msg}

    async def upload_library(self, file: UploadFile = File(...)):
        """A book or file from the 📎 button (P4.1). Progress and refusals come back as 📚 messages."""
        limit = self.cfg["library"].get("max_upload_mb", 100) * 2**20
        data = await file.read(limit + 1)
        if not data or len(data) > limit or not self.library:
            raise HTTPException(400, "empty or too large")
        doc, reply = await self.library.add(data, file.filename or "file.txt", "web", "web")
        return {"ok": reply is None, "reply": reply, "document": doc and {"id": doc["id"], "title": doc["title"]}}

    async def anki(self, doc_id: int):
        """The Anki deck of a book (P4.6), built fresh so it has the latest glosses."""
        from ..anki import export, filename
        doc = self.library.document(doc_id) if self.library else None
        if not doc or doc["status"] != "ready" or not self.vocab:
            raise HTTPException(404)
        path, _ = export(self.vocab, doc)
        if not path:
            raise HTTPException(404, "no words yet")
        return FileResponse(path, media_type="application/octet-stream", filename=filename(doc))

    async def audio(self, message_id: int):
        msg = self.db.get_message(message_id)
        if not msg or not msg["audio_path"]:
            raise HTTPException(404)
        path = self.voice.audio_dir / msg["audio_path"]
        return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix, "application/octet-stream"))

    async def profile(self):
        text = self.engine.persona.persona_path.read_text(encoding="utf-8")
        m = re.search(r"^Name:\s*(.+)$", text, re.M)
        name = m.group(1).strip() if m else ""
        return {"name": "" if name.startswith("(") else name}

    async def ws(self, websocket: WebSocket):
        allowed = self.cfg["secrets"]["allowed_ts_login"]
        if allowed and websocket.headers.get("tailscale-user-login") != allowed:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        self.sockets.add(websocket)
        try:
            while True:
                event = await websocket.receive_json()
                kind = event.get("type")
                if kind == "message" and str(event.get("text", "")).strip():
                    meta = {"client_id": event.get("client_id")} if event.get("client_id") else None
                    await self.engine.receive("web", str(event["text"])[:MAX_MESSAGE], meta=meta)
                elif kind == "typing":
                    await self.engine.typing(active=bool(event.get("active", True)), draft=bool(event.get("draft")),
                                             recording=bool(event.get("recording")))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("websocket error")
        finally:
            self.sockets.discard(websocket)
