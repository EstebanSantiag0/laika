"""Maintenance commands, run inside the app container: python -m laika.admin <command>

fold-history   fold the whole visible history into the running summary and warm the new prefix.
               W2 used it so her old AI-ish replies stop priming the new texting style; she keeps
               the content (summary), not the verbatim wording.
warm           pre-process the current prompt prefix on the conversation slot (after llm restarts),
               so the user's next message doesn't wait for it.
consolidate    ask the running app to close all pending episodes now (at its next check, ~1 min).
nightly        ask the running app to run the nightly sleep now (portrait, memory snapshot, backup).
               Both go through the app's own job runner, so two processes never write memory at once.
"""
import asyncio
import sys

from . import config
from .compaction import Compactor
from .db import DB
from .llm import LLM
from .persona import Persona


async def fold_history():
    cfg = config.load()
    db = DB(config.DATA_DIR / "laika.db")
    llm = LLM(cfg)
    persona = Persona(cfg, db, config.VAULT_DIR)
    engine = type("Idle", (), {"turn": None, "persona": persona})()  # Compactor.warm only needs these
    start = db.get_setting("history_start_id", 0)
    new_start = (db.conn.execute("SELECT max(id) FROM messages").fetchone()[0] or 0) + 1
    db.set_setting("history_start_id", new_start)
    await Compactor(cfg, db, llm, engine).on_trim(start, new_start)
    print(f"history folded: start {start} -> {new_start}; running summary:\n{db.get_setting('running_summary')}")
    await llm.client.aclose()


async def warm():
    cfg = config.load()
    db = DB(config.DATA_DIR / "laika.db")
    llm = LLM(cfg)
    persona = Persona(cfg, db, config.VAULT_DIR)
    await llm.warm(persona.prefix_messages(), cfg["llm"]["conversation_slot"])
    print("conversation prefix warmed")
    await llm.client.aclose()


def request(job):
    async def ask():
        DB(config.DATA_DIR / "laika.db").set_setting(f"{job}_requested", True)
        print(f"{job} requested; follow it with: docker compose logs -f app | grep 'memory job'")
    return ask


COMMANDS = {"fold-history": fold_history, "warm": warm, "consolidate": request("consolidate"),
            "nightly": request("nightly")}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    asyncio.run(COMMANDS[sys.argv[1]]())
