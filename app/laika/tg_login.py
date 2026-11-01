"""One-time login for Laika's own Telegram account (DECISIONS D13), run inside the app container:

  python -m laika.tg_login send [+34...]   Telegram sends a login code to Laika's account
  python -m laika.tg_login code 12345      finish the login (asks for the 2FA password if set)
  python -m laika.tg_login status          show which account is logged in

TG_API_ID and TG_API_HASH (from my.telegram.org) and optionally TG_PHONE come from .env.
The session ends up in /data/telegram-user.session (mode 600): it is a password, never print,
copy or commit it. Then set TELEGRAM_MODE=user in .env and restart the app.
"""
import asyncio
import getpass
import json
import os
import sys

from telethon.errors import SessionPasswordNeededError

from . import config
from .channels.telegram_user import SESSION, make_client

PENDING = config.DATA_DIR / ".tg_login_pending.json"  # phone + code hash between "send" and "code"


async def main(args):
    cfg = config.load()
    if not (cfg["secrets"]["tg_api_id"] and cfg["secrets"]["tg_api_hash"]):
        sys.exit("TG_API_ID / TG_API_HASH are missing in .env")
    client = make_client(cfg)
    await client.connect()
    try:
        if args[0] == "send":
            phone = args[1] if len(args) > 1 else cfg["secrets"]["tg_phone"]
            if not phone:
                sys.exit("give Laika's phone number: tg_login send +34... (or TG_PHONE in .env)")
            sent = await client.send_code_request(phone)
            PENDING.write_text(json.dumps({"phone": phone, "hash": sent.phone_code_hash}))
            os.chmod(PENDING, 0o600)
            print("Code sent to Laika's Telegram app. Now run: python -m laika.tg_login code <code>")
        elif args[0] == "code" and len(args) == 2:
            pending = json.loads(PENDING.read_text())
            try:
                await client.sign_in(pending["phone"], args[1], phone_code_hash=pending["hash"])
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("Laika's two-step verification password: "))
            PENDING.unlink(missing_ok=True)
            me = await client.get_me()
            print(f"Logged in as {me.first_name} (id {me.id}). Set TELEGRAM_MODE=user in .env and restart the app.")
        elif args[0] == "status":
            me = await client.get_me() if await client.is_user_authorized() else None
            print(f"logged in as {me.first_name} (id {me.id})" if me else "not logged in")
        else:
            sys.exit(__doc__)
    finally:
        await client.disconnect()
        session = SESSION.with_suffix(".session")
        if session.exists():
            os.chmod(session, 0o600)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1:]))
