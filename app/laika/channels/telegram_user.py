"""Telegram as Laika's own user account (MTProto via Telethon), DECISIONS D13.

A bot never learns that the user is typing; a user account does. His typing, recording and cancel
updates feed the engine's composing state (W1). Her read receipts are real (sent when her turn
starts), her "typing…"/"recording audio…" shows while she writes or records, and she is online
while chatting. Only the user's id is answered; anything else is ignored without logging its
content. Messages pass through Telegram's servers (as with the bot, D10).
"""
import asyncio
import logging

from telethon import TelegramClient, events, functions, types
from telethon.errors import FloodWaitError

from .. import config
from .telegram import TOO_BIG, format_message

log = logging.getLogger(__name__)

SESSION = config.DATA_DIR / "telegram-user"  # Telethon adds ".session"; mode 600, never committed
ACTION_REFRESH_S = 4  # Telegram drops a chat action after ~5 s unless it is refreshed
OFFLINE_AFTER_S = 45  # "last seen recently" this long after her last bubble


def make_client(cfg):
    s = cfg["secrets"]
    return TelegramClient(str(SESSION), int(s["tg_api_id"]), s["tg_api_hash"], device_model="Laika server",
                          system_version="Linux", app_version="laika", lang_code="en", system_lang_code="en")


class TelegramUserChannel:
    name = "telegram"

    def __init__(self, engine, db, cfg, voice, client=None, library=None):
        self.engine, self.db, self.cfg, self.voice, self.library = engine, db, cfg, voice, library
        self.allowed = int(cfg["secrets"]["telegram_user_id"])
        self.client = client or make_client(cfg)
        self.peer = None
        self.action_task = None
        self.offline_task = None

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.error("Laika's Telegram account is not logged in: run python -m laika.tg_login (PLAN W4)")
            return
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True, from_users=[self.allowed]))
        self.client.add_event_handler(self.on_action, events.UserUpdate(chats=[self.allowed]))
        try:
            await self.client.get_dialogs()  # caches the user's access hash so she can write first after a restart
            self.peer = await self.client.get_input_entity(self.allowed)
        except (ValueError, TypeError):
            log.info("the user's chat isn't known yet; it will be once he writes to Laika")
        log.info("telegram user account connected")

    async def stop(self):
        for task in (self.action_task, self.offline_task):
            if task:
                task.cancel()
        await self.client.disconnect()

    # in -----------------------------------------------------------------------------------
    async def on_message(self, event):
        if not event.is_private or event.sender_id != self.allowed:
            return
        self.peer = await event.get_input_chat()
        m = event.message
        if m.voice or m.audio:
            async with self.engine.incoming():
                data = await self.client.download_media(m, file=bytes)
                await self.voice.receive_note("telegram", data, "voice.ogg", meta={"tg_id": m.id})
        elif self.library and m.document and m.file and m.file.name and not (m.sticker or m.gif or m.video):
            await self.on_document(m)  # a book or file for the library (P4.1)
        elif self.library and m.photo:  # SB5: a screenshot or photo for his second brain
            await self.library.add(await self.client.download_media(m, file=bytes), f"foto-{m.id}.jpg", "telegram",
                                   "telegram", meta={"caption": m.message} if m.message else None)
            if m.message:
                await self.engine.receive("telegram", m.message, meta={"tg_id": m.id})
        elif m.message:
            await self.engine.receive("telegram", m.message, meta={"tg_id": m.id})

    async def on_document(self, m):
        limit = self.cfg["library"].get("max_upload_mb", 100) * 2**20  # downloads are held in memory
        if (m.file.size or 0) > limit:
            await self.library.say("telegram", TOO_BIG.format(limit=limit // 2**20))
        else:
            await self.library.add(await self.client.download_media(m, file=bytes), m.file.name, "telegram",
                                   "telegram", meta={"caption": m.message} if m.message else None)
        if m.message:
            await self.engine.receive("telegram", m.message, meta={"tg_id": m.id})

    async def on_action(self, event):
        if event.user_id != self.allowed:
            return
        if event.recording:  # recording a voice note (or a round video)
            await self.engine.typing(active=True, recording=True)
        elif event.typing:
            await self.engine.typing(active=True)
        elif event.cancel:
            await self.engine.typing(active=False)

    # out ----------------------------------------------------------------------------------
    async def deliver(self, event):
        if event.get("origin") != "telegram" or self.peer is None:
            return
        try:
            await self._deliver(event)
        except FloodWaitError as e:
            log.warning("telegram flood wait %ss; skipping %s", e.seconds, event["type"])

    async def _deliver(self, event):
        kind = event["type"]
        if kind == "read":
            tg_ids = [(self.db.get_message(i) or {}).get("meta", {}).get("tg_id") for i in event["ids"]]
            if any(tg_ids):
                await self._online(True)
                await self.client.send_read_acknowledge(self.peer, max_id=max(i for i in tg_ids if i))
        elif kind == "typing":
            self._start_action(types.SendMessageTypingAction())
        elif kind == "recording":
            self._start_action(types.SendMessageRecordAudioAction())
        elif kind == "typing_stop":
            await self._stop_action()
            self._schedule_offline()
        elif kind == "message":
            msg = event["message"]
            if event.get("update") or msg["role"] == "user" or (event.get("voice") and msg["role"] == "companion") \
                    or msg["meta"].get("download"):
                return  # spoken bubbles go out as voice notes with a caption, files as documents (below)
            text = format_message(msg, self.db, self.cfg)
            if (offer := msg["meta"].get("offer")) and not offer.get("answer"):
                text += f" Reply /lang {offer['practice_lang']}"  # user accounts can't send buttons (W5)
            await self.client.send_message(self.peer, text, parse_mode="html")
        elif kind == "audio":
            await self._stop_action()
            await self.client.send_file(self.peer, event["path"], voice_note=True, parse_mode="html",
                                        caption=format_message(event["message"], self.db, self.cfg)[:1024])
        elif kind == "file":  # an Anki deck (P4.6)
            await self.client.send_file(self.peer, event["path"], caption=event["message"]["text"],
                                        attributes=[types.DocumentAttributeFilename(event["filename"])], force_document=True)
        elif kind == "audio_failed":
            await self._stop_action()
            await self.client.send_message(self.peer, format_message(event["message"], self.db, self.cfg),
                                           parse_mode="html")
        elif kind == "error":
            await self.client.send_message(self.peer, event["text"])

    def _start_action(self, action):
        if self.action_task:
            self.action_task.cancel()
        self.action_task = asyncio.create_task(self._keep_action(action))

    async def _keep_action(self, action):
        while True:
            await self.client(functions.messages.SetTypingRequest(self.peer, action))
            await asyncio.sleep(ACTION_REFRESH_S)

    async def _stop_action(self):
        if self.action_task and not self.action_task.done():
            self.action_task.cancel()
            await self.client(functions.messages.SetTypingRequest(self.peer, types.SendMessageCancelAction()))
        self.action_task = None

    async def _online(self, online):
        if self.offline_task:
            self.offline_task.cancel()
            self.offline_task = None
        await self.client(functions.account.UpdateStatusRequest(offline=not online))

    def _schedule_offline(self):
        async def later():
            await asyncio.sleep(OFFLINE_AFTER_S)
            self.offline_task = None
            await self.client(functions.account.UpdateStatusRequest(offline=True))
        if self.offline_task:
            self.offline_task.cancel()
        self.offline_task = asyncio.create_task(later())
