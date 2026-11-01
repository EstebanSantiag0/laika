"""Laika's own Telegram account (W4) against a fake Telethon client: allowlist, typing → engine,
read receipts, her chat actions and voice notes. Also the language offer (W5) in both Telegram
channels: inline buttons from the bot, a "/lang" hint from her own account.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_telegram_user
"""
import asyncio
import types as pytypes

from laika.channels.telegram import TelegramChannel
from laika.channels.telegram_user import TelegramUserChannel
from tests.test_engine import CFG, make, settle

DASHA = 111


class FakeClient:
    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        self.calls.append(("request", type(request).__name__, getattr(request, "action", None)))

    async def send_read_acknowledge(self, peer, max_id):
        self.calls.append(("read", max_id))

    async def send_message(self, peer, text, parse_mode=None):
        self.calls.append(("text", text))

    async def send_file(self, peer, path, **kw):
        self.calls.append(("voice", path, kw.get("caption")))

    async def download_media(self, message, file=None):
        return b"ogg"

    def kinds(self, name):
        return [c for c in self.calls if c[0] == name]


def message_event(sender, text, msg_id, voice=False):
    msg = pytypes.SimpleNamespace(id=msg_id, message=text, voice=voice, audio=None)

    async def get_input_chat():
        return "peer"
    return pytypes.SimpleNamespace(is_private=True, sender_id=sender, message=msg, get_input_chat=get_input_chat)


def action_event(user, **flags):
    return pytypes.SimpleNamespace(user_id=user, recording=flags.get("recording", False),
                                   typing=flags.get("typing", False), cancel=flags.get("cancel", False))


async def main():
    engine, llm, rec, db = make("Hey!\n\nHow are you?")
    cfg = {**CFG, "secrets": {"telegram_user_id": str(DASHA)}}
    client = FakeClient()
    channel = TelegramUserChannel(engine, db, cfg, voice=None, client=client)
    engine.channels.append(channel)

    await channel.on_message(message_event(999, "spam from a stranger", 5))
    assert not db.history(), "a stranger's message reached the conversation"

    await channel.on_action(action_event(DASHA, typing=True))
    assert engine.composing(), "their typing update didn't reach the engine"
    await channel.on_action(action_event(DASHA, cancel=True))
    assert not engine.composing()
    await channel.on_action(action_event(DASHA, recording=True))
    assert engine.composing()
    await channel.on_action(action_event(DASHA, cancel=True))

    await channel.on_message(message_event(DASHA, "hola Laika", 41))
    for _ in range(5):  # they keep typing (Telegram repeats the update every few seconds): she waits
        await channel.on_action(action_event(DASHA, typing=True))
        await asyncio.sleep(0.2)
    assert not client.kinds("read") and not llm.prompts, "she read/answered while they were typing"
    await channel.on_message(message_event(DASHA, "how are you?", 42))
    await settle(engine)
    await asyncio.sleep(0.1)
    assert client.kinds("read") == [("read", 42)], client.calls
    assert [c[1] for c in client.kinds("text")] == ["Hey!", "How are you?"], client.calls
    actions = [c[2].__class__.__name__ for c in client.calls if c[0] == "request" and c[1] == "SetTypingRequest"]
    assert "SendMessageTypingAction" in actions and actions[-1] == "SendMessageCancelAction", actions
    assert channel.action_task is None, "the typing refresher kept running after her last bubble"
    statuses = [c for c in client.calls if c[0] == "request" and c[1] == "UpdateStatusRequest"]
    assert statuses, "she never went online while answering"
    channel.offline_task.cancel()

    client.calls.clear()  # W5: her own account can't send buttons, so the offer says how to answer
    await channel.on_message(message_event(DASHA, "你好！我今天很累，想休息一下", 43))
    await settle(engine)
    await asyncio.sleep(0.1)
    assert ("text", "Switch to 中文? Reply /lang zh") in client.calls, client.calls
    channel.offline_task.cancel()
    print("ok telegram_user")


class FakeBot:
    def __init__(self):
        self.calls, self.next_id = [], 500

    async def set_my_commands(self, commands):
        self.calls.append(("commands", [c.command for c in commands]))

    async def send_chat_action(self, chat_id, action):
        self.calls.append(("action", action))

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.next_id += 1
        buttons = [b.callback_data for row in reply_markup.inline_keyboard for b in row] if reply_markup else None
        self.calls.append(("send", text, buttons))
        return pytypes.SimpleNamespace(message_id=self.next_id)

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None):
        self.calls.append(("edit", message_id, reply_markup))


def button_event(user, data, answered):
    async def answer():
        answered.append(data)
    return pytypes.SimpleNamespace(from_user=pytypes.SimpleNamespace(id=user), data=data, message=None, answer=answer)


async def bot_offer():
    engine, llm, rec, db = make("Okay!")
    cfg = {**CFG, "secrets": {"telegram_user_id": str(DASHA), "telegram_token": ""}}
    bot = FakeBot()
    channel = TelegramChannel(engine, db, cfg, voice=None, bot=bot)
    engine.channels.append(channel)
    await engine.receive("telegram", "Привет, как у тебя дела сегодня?")
    await settle(engine)
    [(_, text, buttons)] = [c for c in bot.calls if c[0] == "send" and c[2]]
    offer = next(m for m in db.history() if m["meta"].get("offer"))
    assert text == "Switch to Русский?" and buttons == [f"offer:{offer['id']}:yes", f"offer:{offer['id']}:no"]
    assert offer["meta"]["tg_id"] == 501, offer["meta"]
    answered = []
    await channel.on_button(button_event(999, f"offer:{offer['id']}:yes", answered))  # a stranger: ignored
    assert db.get_setting("practice_lang", "en") == "en" and not answered
    await channel.on_button(button_event(DASHA, f"offer:{offer['id']}:yes", answered))
    assert db.get_setting("practice_lang") == "ru" and answered
    assert ("edit", 501, None) in bot.calls, bot.calls  # the buttons went away
    assert any(c[0] == "send" and c[1] == "✓ practice_lang = Русский" for c in bot.calls), bot.calls
    print("ok bot_offer")


async def run_all():
    await main()
    await bot_offer()


if __name__ == "__main__":
    asyncio.run(run_all())
