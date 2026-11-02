"""Telegram channel: long polling (outbound only), answers only the user's Telegram id.

Messages pass through Telegram's servers (accepted trade-off, DECISIONS D10).
Replies go to Telegram only for turns that started there; the web app shows everything.
"""
import asyncio
import contextlib
import html
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)
BOT_FILE_LIMIT = 20 * 2**20  # the Bot API downloads files up to 20 MB
TOO_BIG = "📚 That's too big for Telegram ({limit} MB at most): put it in Library/_inbox in the vault"

BOT_COMMANDS = [BotCommand(command="lang", description="practice language: en | ru | zh"),
                BotCommand(command="corrections", description="corrections: on | end | off"),
                BotCommand(command="voice", description="voice replies: mirror | always | never"),
                BotCommand(command="books", description="your library"),
                BotCommand(command="reading", description="the book you're reading: /reading <n> [chapter]"),
                BotCommand(command="anki", description="Anki deck of a book's new words: /anki <n>")]


class TelegramChannel:
    name = "telegram"

    def __init__(self, engine, db, cfg, voice, bot=None, library=None):
        self.engine, self.db, self.cfg, self.voice, self.library = engine, db, cfg, voice, library
        self.allowed = int(cfg["secrets"]["telegram_user_id"])
        self.bot = bot or Bot(cfg["secrets"]["telegram_token"])
        self.dp = Dispatcher()
        self.dp.message.register(self.on_message)
        self.dp.callback_query.register(self.on_button)
        self.task = None
        self.reminders = None  # set in main.py (W8.1)

    async def start(self):
        try:
            await self.bot.set_my_commands(BOT_COMMANDS)  # the "/" menu in his Telegram
        except Exception as e:
            log.warning("telegram set_my_commands failed: %s", e)
        self.task = asyncio.create_task(self.dp.start_polling(self.bot, handle_signals=False))
        log.info("telegram polling started")

    async def stop(self):
        if self.task:
            await self.dp.stop_polling()
        await self.bot.session.close()

    async def on_message(self, m: Message):
        if not m.from_user or m.from_user.id != self.allowed:
            log.warning("ignored telegram message from user %s", m.from_user and m.from_user.id)
            return
        if m.text == "/start":
            await m.answer("✓")
            return
        if m.text:
            await self.engine.receive("telegram", m.text)
        elif m.voice or m.audio:
            media = m.voice or m.audio
            async with self.engine.incoming():
                buf = await self.bot.download(media.file_id)
                await self.voice.receive_note("telegram", buf.read(), "voice.ogg")
        elif self.library and m.document and not m.animation:  # a book or file for the library (P4.1)
            if (m.document.file_size or 0) > BOT_FILE_LIMIT:
                await self.library.say("telegram", TOO_BIG.format(limit=BOT_FILE_LIMIT // 2**20))
            else:
                buf = await self.bot.download(m.document.file_id)
                await self.library.add(buf.read(), m.document.file_name or "file.txt", "telegram", "telegram",
                                       meta={"caption": m.caption} if m.caption else None)
            if m.caption:
                await self.engine.receive("telegram", m.caption)
        elif self.library and m.photo:  # SB5: a screenshot or photo for his second brain
            buf = await self.bot.download(m.photo[-1].file_id)  # the largest size
            await self.library.add(buf.read(), f"foto-{m.message_id}.jpg", "telegram", "telegram",
                                   meta={"caption": m.caption} if m.caption else None)
            if m.caption:
                await self.engine.receive("telegram", m.caption)

    async def on_button(self, cb: CallbackQuery):
        """A tap on "Switch" / "Not now" under a language offer (W5)."""
        if not cb.from_user or cb.from_user.id != self.allowed:
            log.warning("ignored telegram button from user %s", cb.from_user and cb.from_user.id)
            return
        kind, _, rest = (cb.data or "").partition(":")
        msg_id, _, answer = rest.partition(":")
        if kind == "rem" and msg_id.isdigit() and self.reminders:  # "Done" / "Snooze" under a reminder (W8.1)
            r = await (self.reminders.done(int(msg_id)) if answer == "done" else self.reminders.snooze(int(msg_id)))
            if r is None and cb.message:
                with contextlib.suppress(TelegramBadRequest):
                    await cb.message.edit_reply_markup(reply_markup=None)
            await cb.answer("✓" if r else "")
            return
        answered = kind == "offer" and msg_id.isdigit() and \
            await self.engine.answer_offer(int(msg_id), answer == "yes", "telegram")
        if not answered and cb.message:  # answered elsewhere already: just take the buttons away
            with contextlib.suppress(TelegramBadRequest):
                await cb.message.edit_reply_markup(reply_markup=None)
        await cb.answer()

    async def deliver(self, event):
        if event.get("origin") != "telegram":
            return
        kind = event["type"]
        if kind == "typing":
            await self.bot.send_chat_action(self.allowed, ChatAction.TYPING)
        elif kind == "recording":
            await self.bot.send_chat_action(self.allowed, ChatAction.RECORD_VOICE)
        elif kind == "message":
            msg = event["message"]
            if event.get("update"):  # an answered offer or reminder: its buttons go away
                answered = msg["meta"].get("offer", {}).get("answer") or msg["meta"].get("reminder", {}).get("answer")
                if (tg_id := msg["meta"].get("tg_id")) and answered:
                    with contextlib.suppress(TelegramBadRequest):
                        await self.bot.edit_message_reply_markup(chat_id=self.allowed, message_id=tg_id,
                                                                 reply_markup=None)
                return
            if msg["role"] == "user" or (event.get("voice") and msg["role"] == "companion") or msg["meta"].get("download"):
                return  # voice turns send the bubble as a voice note with caption, files as documents (below)
            buttons = offer_buttons(msg) or reminder_buttons(msg)
            sent = await self.bot.send_message(self.allowed, self.format(msg), parse_mode=ParseMode.HTML,
                                               reply_markup=buttons)
            if buttons:
                self.db.set_meta(msg["id"], tg_id=sent.message_id)
        elif kind == "audio":
            await self.bot.send_voice(self.allowed, FSInputFile(event["path"]),
                                      caption=self.format(event["message"])[:1024], parse_mode=ParseMode.HTML)
        elif kind == "file":  # an Anki deck (P4.6)
            await self.bot.send_document(self.allowed, FSInputFile(event["path"], filename=event["filename"]),
                                         caption=html.escape(event["message"]["text"]), parse_mode=ParseMode.HTML)
        elif kind == "audio_failed":
            await self.bot.send_message(self.allowed, self.format(event["message"]), parse_mode=ParseMode.HTML)
        elif kind == "error":
            await self.bot.send_message(self.allowed, event["text"])

    def format(self, msg):
        return format_message(msg, self.db, self.cfg)


def offer_buttons(msg):
    """Inline "Switch" / "Not now" under an open language offer (W5), else None."""
    offer = (msg.get("meta") or {}).get("offer")
    if not offer or offer.get("answer"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Switch", callback_data=f"offer:{msg['id']}:yes"),
        InlineKeyboardButton(text="Not now", callback_data=f"offer:{msg['id']}:no")]])


def reminder_buttons(msg):
    """"Done" / "Snooze 1 h" under a delivered reminder (W8.1), else None."""
    rem = (msg.get("meta") or {}).get("reminder")
    if not rem or rem.get("answer"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Done", callback_data=f"rem:{rem['id']}:done"),
        InlineKeyboardButton(text="Snooze 1 h", callback_data=f"rem:{rem['id']}:snooze")]])


def format_message(msg, db, cfg):
    """Telegram HTML for a bubble: corrections as struck-through → bold, pinyin under Chinese."""
    meta = msg.get("meta") or {}
    if msg["role"] == "correction" and meta.get("corrections"):
        return "✏️ " + "\n".join(
            f"<s>{html.escape(c['original'])}</s> → <b>{html.escape(c['better'])}</b>\n<i>{html.escape(c['why'])}</i>"
            for c in meta["corrections"])
    text = html.escape(msg["text"])
    if meta.get("pinyin") and db.get_setting("pinyin", cfg["language"]["pinyin"]):
        text += f"\n<i>{html.escape(meta['pinyin'])}</i>"
    return text
