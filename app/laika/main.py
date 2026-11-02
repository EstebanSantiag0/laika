"""FastAPI entry point: wires storage, model client, persona, conversation engine and channels."""
import asyncio
import datetime
import logging
import zoneinfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .anki import AnkiExport
from .api import Api
from .api_brain import BrainApi, install_errors
from .areas import Areas
from .brain import Brain
from .books import Books
from .channels.web import WebChannel
from .compaction import Compactor
from .consolidate import Consolidator
from .conversation import Engine
from .db import DB
from .edits import Edits
from .edits import run as watch_vault
from .embed import Embedder
from .graph_edit import GraphEdit
from .jobs import Jobs
from .library import Library
from .links import Links
from .llm import LLM
from .memory import Memory
from .memory_commands import MemoryCommands
from .nightly import Nightly
from .persona import Persona
from .recall import Recall
from .reminders import Reminders
from .summaries import Summaries
from .vocab import Vocab
from .tutor import Tutor
from .vault import Vault
from .voice import Voice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("laika")

cfg = config.load()
if not (cfg.get("user") or {}).get("pronouns"):
    log.warning("config.yaml has no user name/pronouns yet: run `python3 scripts/setup.py` (using %s, %s)",
                config.user_name(cfg), config.user_pronouns(cfg))
config.DATA_DIR.mkdir(parents=True, exist_ok=True)
db = DB(config.DATA_DIR / "laika.db")
memory = Memory(db)  # Phase 3 "Organic Knowledge" store (DECISIONS D14)
llm = LLM(cfg)
persona = Persona(cfg, db, config.VAULT_DIR)
persona.ensure_files()
engine = Engine(cfg, db, llm, persona)
tutor = Tutor(cfg, db, llm, engine)
engine.after_turn.append(tutor.after_turn)
engine.annotate = tutor.annotate
voice = Voice(cfg, db, engine)
engine.voice_policy = voice.wants_voice
engine.voice_allowed = voice.allowed
engine.on_bubble.append(voice.on_bubble)
tutor.before_send = voice.drained
engine.on_trim.append(Compactor(cfg, db, llm, engine).on_trim)
vault = Vault(config.VAULT_DIR, config.user_name(cfg))  # <name>/Profile.md, <name>/Now.md
embedder = Embedder(cfg)
areas = Areas(cfg, db)  # W8.3: life areas from config.yaml
consolidator = Consolidator(cfg, db, llm, memory, embedder, vault, areas)
library = Library(cfg, db, embedder, vault, engine)  # Phase 4: books and files (DECISIONS D18)
engine.long_text = library.shorten_paste
vocab = Vocab(cfg, db, library, persona, llm)  # P4.4: what he knows, book difficulty, glosses
tutor.on_checked = vocab.from_his_messages
library.note_extra = vocab.note_lines
brain = Brain(cfg, db, llm, memory, embedder, vault, engine, areas, library)  # SB1: his second brain
library.on_document.append(brain.on_document)
links = Links(cfg, db, library, brain, engine)  # SB2/SB4: the page or the video behind a saved link
brain.fetch_url = links.fetch
jobs = Jobs(cfg, db, engine, consolidator, Nightly(cfg, db, llm, memory, vault, consolidator, persona, areas), library,
            Summaries(cfg, db, llm, library, embedder), vocab, brain)  # P4.3/P4.4: summaries and glosses while he's away
recall = Recall(cfg, db, memory, embedder, persona)
books = Books(cfg, db, library, embedder, persona)  # P4.2: passages from his books, /books, /reading
engine.before_turn += [MemoryCommands(cfg, db, memory, embedder, engine).before_turn, recall.for_turn, books.before_turn]
engine.commands.update({"books": books.cmd_books, "reading": books.cmd_reading})
books.extra_info = lambda doc: [line] if (line := vocab.difficulty_line(doc)) else []
engine.before_turn.append(vocab.before_turn)  # P4.5: a word from the book he's reading, now and then
engine.after_turn.append(vocab.after_turn)
anki = AnkiExport(db, library, vocab, engine)  # P4.6
engine.commands["anki"] = anki.command
engine.after_turn.append(recall.after_turn)
reminders = Reminders(cfg, db, llm, engine)  # W8.1: "remind me" becomes a reminder sent at that time
reminders.areas = areas
engine.before_turn.append(reminders.before_turn)
engine.before_turn.append(brain.before_turn)  # SB1: "guarda esta idea", links, saved things that fit
edits = Edits(db, memory, embedder, vault)  # P3.5: his edits in Obsidian win
edits.brain = brain  # SB7b: his edits to Brain/ notes too
graph_edit = GraphEdit(cfg, db, memory, embedder, vault, brain, reminders, areas, library, consolidator)  # SB7b
jobs.graph_edit = graph_edit  # the nightly job empties the 30-day trash
brain.edit = graph_edit  # a file or link sent again while in the trash comes back
library.restore_trashed = brain.restore_doc
jobs.links = links  # SB4: reels and videos are transcribed while he's away
web = WebChannel(engine, db, cfg, voice, library, vocab)
engine.channels.append(web)

telegram = None
secrets = cfg["secrets"]
if secrets["telegram_mode"] == "user" and secrets["tg_api_id"] and secrets["telegram_user_id"]:
    from .channels.telegram_user import TelegramUserChannel  # Laika's own account (DECISIONS D13)

    telegram = TelegramUserChannel(engine, db, cfg, voice, library=library)
    engine.channels.append(telegram)
elif secrets["telegram_token"] and secrets["telegram_user_id"]:
    from .channels.telegram import TelegramChannel

    telegram = TelegramChannel(engine, db, cfg, voice, library=library)
    engine.channels.append(telegram)
else:
    log.info("telegram disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID not set)")
if telegram:
    telegram.reminders = reminders


def backup_if_missing():
    """The nightly job backs up the database; this covers days when it hasn't run yet."""
    dest = config.VAULT_DIR / ".laika" / "backup"
    today = datetime.datetime.now(zoneinfo.ZoneInfo(cfg["memory"]["tz"])).strftime("laika-%Y-%m-%d.db")
    try:
        if not (dest / today).exists():
            log.info("database backup: %s", memory.backup(dest))
    except Exception:
        log.exception("database backup failed")


@asynccontextmanager
async def lifespan(_app):
    if telegram:
        await telegram.start()
    await llm.raw_ready()  # logs whether exact replay is on (DECISIONS D15); retried at the first turn if not
    backup_if_missing()
    await library.resume()  # extraction a restart interrupted
    await brain.backfill_library()  # SB1: documents from before the second brain become saved items
    runner = asyncio.create_task(jobs.loop())  # episodes after quiet time, nightly sleep (P3.3)
    watcher = asyncio.create_task(watch_vault(edits))
    scheduler = asyncio.create_task(reminders.loop())  # W8.1: also sends what was due while the server was down
    yield
    runner.cancel()
    watcher.cancel()
    scheduler.cancel()
    if telegram:
        await telegram.stop()
    await llm.client.aclose()
    await voice.client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def tailscale_only(request: Request, call_next):
    """Behind `tailscale serve`, only the user's tailnet login may use the app (health is open)."""
    allowed = cfg["secrets"]["allowed_ts_login"]
    if allowed and request.url.path != "/health" and request.headers.get("tailscale-user-login") != allowed:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


@app.get("/health")
async def health():
    return {"ok": True, "llm": await llm.healthy(), "telegram": telegram is not None}


app.include_router(web.router)
graph_api = Api(cfg, db, memory, reminders, areas, brain, graph_edit)
app.include_router(graph_api.router)  # W8.2: graph + reminders for laika-app (docs/API.md)
app.include_router(BrainApi(cfg, db, memory, embedder, brain, library, graph_edit, graph_api).router)  # SB7/SB7b


install_errors(app)  # {"error": ...} everywhere (docs/API.md)


@app.get("/")
async def index():
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def service_worker():
    return FileResponse(config.WEB_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


class _Revalidate(StaticFiles):
    """Static files without Cache-Control get cached by heuristics: a phone kept an old app.js next to a new index.html."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", _Revalidate(directory=config.WEB_DIR), name="web")
