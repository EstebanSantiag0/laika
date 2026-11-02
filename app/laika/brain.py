"""Second brain (SB1, docs/PLAN-second-brain.md, DECISIONS D22/D23): what he saves, filed by Laika.

Items are ideas, things "for later" (no time, not reminders) and saved things (a link, a file, a reel,
a piece of information kept for its content). Likes and dislikes are not items: they are facts about
him with a `polarity` (D22), so they live with the rest of his memory.

Capture:
- In the chat, a cue ("guarda esta idea", "para después", "save this", "сохрани", "收藏"…) or a link
  triggers one JSON call on the background slot: is it a save, which kind, a title and text, up to 2
  areas, up to 3 tags, and up to 3 links to existing dots (entities, facts, other items) chosen only from
  retrieved candidates, each with a short reason. The turn note tells Laika what was saved, so her
  confirmation matches the app; she never claims a save the note doesn't confirm.
- Every library document (PWA 📎, Telegram file, inbox, long paste) becomes a `saved` item; it is filed
  (areas, tags, links) by a background job once its text is extracted.
Each item is indexed with the memories (kind `brain`), so Laika can bring it up when it fits, and gets
a vault note in `Brain/<Kind>/`. Links are rows in `graph_links` (also used by SB7b for his own links
and for hidden automatic links).
"""
import asyncio
import datetime
import json
import logging
import re
import time
import urllib.parse
import zoneinfo

from .config import personal, user_name
from .consolidate import note_name
from .memory_commands import FORGET, _content

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    format TEXT NOT NULL DEFAULT 'text',
    doc_id INTEGER,
    url TEXT,
    source_message_id INTEGER,
    channel TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    filed INTEGER NOT NULL DEFAULT 0,
    title_locked INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    resurface_at REAL,
    resurface_step INTEGER NOT NULL DEFAULT 0,
    note_path TEXT,
    trashed_at REAL
);
CREATE INDEX IF NOT EXISTS brain_items_kind ON brain_items(kind, status);
CREATE INDEX IF NOT EXISTS brain_items_doc ON brain_items(doc_id);
CREATE TABLE IF NOT EXISTS graph_links (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'auto',
    action TEXT NOT NULL DEFAULT 'add',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    trashed_at REAL
);
CREATE INDEX IF NOT EXISTS graph_links_source ON graph_links(source);
CREATE INDEX IF NOT EXISTS graph_links_target ON graph_links(target);
"""
KINDS = ("idea", "for_later", "saved")
FOLDERS = {"idea": "Ideas", "for_later": "For later", "saved": "Saved"}
STATUSES = ("active", "done", "archived")
FORMATS = ("text", "link", "video", "pdf", "book", "image")
RESURFACE_DAYS = (3, 10, 30)  # SB6: an idea or "for later" item comes back after these days
MAX_LINKS = 3
MAX_TAGS = 3
LINK_COS = 0.45      # candidates for links: doc-to-doc cosine (the model picks among them)
RECALL_COS = 0.50    # a saved item comes up in conversation (query-to-doc cosine)
ASKED_COS = 0.35     # ...lower when he asks what he saved
OFFER_AGAIN_S = 6 * 3600
CONTEXT_MSGS = 6

URL = re.compile(r"https?://[^\s<>\"']+", re.I)
VIDEO = re.compile(r"(?:instagram\.com/(?:reels?|p|tv)/|youtube\.com/(?:watch|shorts)|youtu\.be/|tiktok\.com/|"
                   r"vimeo\.com/\d)", re.I)
CUE = re.compile(
    r"\bsave\s+(?:this|that|it)\b|\bfor\s+later\b|\bnote\s+(?:this|that)\s+down\b|\bwrite\s+(?:this|that)\s+down\b|"
    r"\bbookmark\b|\bkeep\s+(?:this|that)\b|\b(?:an?\s+)?idea\s*:|\bmy\s+(?:likes|dislikes)\b|"
    r"\bguard(?:a|ar|alo|ame|emos)\b|\bapunt(?:a|ar|alo|ame)\b|\banot(?:a|ar|alo|ame)\b|"
    r"\bpara\s+(?:despu[eé]s|luego|m[aá]s\s+tarde)\b|\bidea\s*:|\btengo\s+una\s+idea\b|\bmis\s+gustos\b|"
    r"сохрани|запиши|на\s+потом|\bидея\s*:|"
    r"收藏|记下|保存|以后再看|想法[:：]", re.I)
ASKED = re.compile(r"\bwhat\s+(?:did\s+i|have\s+i)\s+saved?\b|\bmy\s+(?:ideas|saved)\b|"
                   r"\bqu[eé]\s+(?:tengo\s+)?guard(?:ado|é)\b|\bmis\s+ideas\b|\blo\s+que\s+guard[eé]\b|"
                   r"что\s+я\s+сохранил|我收藏", re.I)
NEGATION = re.compile(r"(?:^|\W)(?:no|not|нет|не)(?:\W|$)", re.I)
CORRECT = re.compile(r"\b(?:es|it'?s|it\s+is|это|make\s+it|ponlo\s+como)\s+(?:(?:una?|an?)\s+)?"
                     r"(?P<kind>idea|para\s+despu[eé]s|for\s+later|на\s+потом|para\s+guardar|saved?)", re.I)
CORRECT_KIND = {"idea": "idea", "para despues": "for_later", "para después": "for_later", "for later": "for_later",
                "на потом": "for_later", "para guardar": "saved", "save": "saved", "saved": "saved"}
CORRECT_WINDOW_S = 15 * 60
CAPTION_WINDOW_S = 120
COMMENT_WAIT_S = 120   # "comment": how long to wait for a file's text before she reacts anyway
COMMENT_CHARS = 1500   # how much of its text she gets
RELATED_COS = 0.76         # SB6: "this goes with the idea you saved on 10-12" (the graph's `similar` bar)
RESURFACE_HOURS = (9, 22)  # she brings something back only in the day
ARCHIVE_LATER_DAYS = 60    # a "for later" item that went through every resurfacing untouched is archived
DIGEST_MIN_AGE_DAYS = 14   # Sunday's "still want these?" covers "for later" items at least this old
TAG_COS = 0.85             # nightly: tags this close are checked by the model and merged if they're the same
MAX_TAG_MERGES = 10
TAG_SAME = """Two tags from someone's notes app. Say whether they name the same topic, so one can replace \
the other (spelling, plural, language or synonym), or different topics."""
FORGET_COS = 0.6

CLASSIFY = """You file what {user} saves in his "second brain", an app where his friend Laika keeps \
things for him. Now it is {now}. Read his last message, with the chat before it for context.
- is_save: true only if he asks to save, keep or note something for later, shares a link to keep, \
or states an idea he wants kept ("idea: …", "apunta esta idea"). False for ordinary chat and \
questions, and false for "remind me…" (reminders are handled elsewhere).
- kind:
  - "idea": something he could make, do or try; his own idea.
  - "for_later": something to read, watch, buy, check or do some day, with no set time.
  - "saved": a link, document or piece of information kept for its content.
  - "like" / "dislike": he asks to keep that he likes / dislikes something.
- title: at most 8 words, in the language he wrote in.
- text: what to keep, one or two self-contained sentences in his language (replace "this" or \
"eso" with what it refers to). For a link: what it is, if he said.
- areas: the parts of his life it belongs to, at most 2 of: {areas}. Empty if none fits.
- tags: up to 3 short topic tags, lowercase, words joined with "-", in his language.
- links: up to 3 ids from the candidate list below that this item is clearly about or useful for \
(his project, a goal, a person, an earlier idea), each with a reason of at most 8 words in his \
language ("podría servir para el robot"). Only ids from the list. Empty if nothing clearly fits.

Candidates (id: what it is):
{candidates}"""
FILE_DOC = """You file a document {user} saved in his "second brain". Below are its title and its \
beginning.
- title: a short title (at most 10 words), in the document's language.
- text: what it is, in one or two sentences, in Spanish.
- areas: the parts of his life it belongs to, at most 2 of: {areas}. Empty if none fits.
- tags: up to 3 short topic tags, lowercase, words joined with "-".
- links: up to 3 ids from the candidate list below that this document is clearly useful for, each \
with a reason of at most 8 words in Spanish. Only ids from the list. Empty if nothing clearly fits.

Candidates (id: what it is):
{candidates}"""


def _schema(areas_schema, with_kind=True):
    props = {
        "title": {"type": "string"}, "text": {"type": "string"}, "areas": areas_schema,
        "tags": {"type": "array", "maxItems": MAX_TAGS, "items": {"type": "string"}},
        "links": {"type": "array", "maxItems": MAX_LINKS, "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["id", "reason"]}}}
    req = ["title", "text", "areas", "tags", "links"]
    if with_kind:
        props = {"is_save": {"type": "boolean"}, "kind": {"enum": [*KINDS, "like", "dislike"]}, **props}
        req = ["is_save", "kind"] + req
    return {"type": "object", "properties": props, "required": req}


def clean_tags(tags):
    out = []
    for t in tags or []:
        t = re.sub(r"[\s_]+", "-", str(t).strip().lower().lstrip("#"))
        t = re.sub(r"[^\w-]", "", t).strip("-")[:40]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def _caption_kind(caption):
    """A file sent with "para después" / "idea: …" gets that kind; anything else is a saved thing."""
    low = (caption or "").lower()
    if re.search(r"para\s+despu[eé]s|para\s+luego|for\s+later|на\s+потом|以后", low):
        return "for_later"
    if re.search(r"\bidea\b|идея|想法", low):
        return "idea"
    return "saved"


_TRACKING = re.compile(r"^(?:utm_\w+|igsh|igshid|si|fbclid|gclid|ref|ref_src)$", re.I)


def normalize_url(url):
    """For duplicate checks: lowercase host, no fragment, no tracking parameters, no trailing slash."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not _TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."), path, query, ""))


def filename_like(title):
    """'My-Teacher-is-a-Martian-Mandarin…' or 'scan_0012': a file name, not a title."""
    t = (title or "").strip()
    return not t or (" " not in t and bool(re.search(r"[-_.]", t))) or t.endswith("…")


def url_format(url):
    return "video" if VIDEO.search(url or "") else "link"


def doc_format(kind):
    return {"pdf": "pdf", "epub": "book", "fb2": "book", "zip": "book", "image": "image"}.get(kind or "", "text")


class Brain:
    def __init__(self, cfg, db, llm, memory, embedder, vault, engine, areas=None, library=None):
        self.cfg, self.db, self.llm, self.mem, self.embedder = cfg, db, llm, memory, embedder
        self.vault, self.engine, self.areas, self.library = vault, engine, areas, library
        self.conn = db.conn
        self.conn.executescript(SCHEMA)
        if "note_body" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(brain_items)")}:
            self.conn.execute("ALTER TABLE brain_items ADD COLUMN note_body TEXT")  # what Laika last wrote (SB7b)
        self.tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
        self.slot = cfg["llm"]["background_slot"]
        self.fetch_url = None  # SB2/SB4: links.py reads the page or the video behind a saved link
        self.edit = None  # SB7b graph_edit.GraphEdit: a file or link sent again while in the trash is restored

    # storage ---------------------------------------------------------------------------------------
    def get(self, item_id, trashed=False):
        row = self.conn.execute("SELECT * FROM brain_items WHERE id = ?" + ("" if trashed else " AND trashed_at IS NULL"),
                                (item_id,)).fetchone()
        return self._row(row) if row else None

    def _row(self, row):
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["areas"] = self.areas.of("item", d["id"]) if self.areas else []
        return d

    def items(self, kind=None, status="active", limit=None):
        q, args = "SELECT * FROM brain_items WHERE trashed_at IS NULL", []
        if kind:
            q, args = q + " AND kind = ?", args + [kind]
        if status != "all":
            q, args = q + " AND status = ?", args + [status]
        q += " ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
        return [self._row(r) for r in self.conn.execute(q, args)]

    async def find_url(self, url):
        """(item, restored) for a link he saved before (same normalized URL), or None. One in the trash is
        brought back (SB8: sending it again means he wants it)."""
        norm = normalize_url(url)
        for row in self.conn.execute("SELECT id, url, trashed_at FROM brain_items WHERE url IS NOT NULL "
                                     "ORDER BY trashed_at IS NOT NULL, id DESC"):
            if normalize_url(row["url"]) == norm:
                if row["trashed_at"] and self.edit:
                    await self.edit.restore(f"item:{row['id']}")
                    return self.get(row["id"]), True
                if not row["trashed_at"]:
                    return self.get(row["id"]), False
        return None

    async def restore_doc(self, doc_id):
        """Library hook: a file sent again whose document is in the trash brings back its item (and the file)."""
        row = self.conn.execute("SELECT id FROM brain_items WHERE doc_id = ? AND trashed_at IS NOT NULL ORDER BY id DESC "
                                "LIMIT 1", (doc_id,)).fetchone()
        if row and self.edit:
            await self.edit.restore(f"item:{row['id']}")
            return True
        return False

    def by_doc(self, doc_id):
        row = self.conn.execute("SELECT * FROM brain_items WHERE doc_id = ? ORDER BY id LIMIT 1", (doc_id,)).fetchone()
        return self._row(row) if row else None

    async def add(self, kind, title, text="", *, fmt="text", url=None, doc_id=None, note="", areas=(), tags=(),
                  links=(), source_message_id=None, channel=None, filed=True, vec=None):
        now = time.time()
        resurface = now + RESURFACE_DAYS[0] * 86400 if kind in ("idea", "for_later") else None
        cur = self.conn.execute(
            "INSERT INTO brain_items(kind, title, text, note, tags, format, doc_id, url, source_message_id, channel, "
            "filed, created, updated, resurface_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, (title or "").strip()[:120] or "(untitled)", (text or "").strip(), note or "",
             json.dumps(clean_tags(tags)), fmt, doc_id, url, source_message_id, channel, int(filed), now, now, resurface))
        item_id = cur.lastrowid
        if self.areas:
            self.areas.set("item", item_id, list(areas)[:2])
        self.set_links(item_id, links)
        await self.reindex(item_id, vec)
        item = self.get(item_id)
        log.info("brain: saved item %s (%s, %s)", item_id, kind, fmt)
        await self.publish(item)
        return item

    async def update(self, item_id, **fields):
        allowed = {"kind", "title", "text", "note", "tags", "status", "format", "url", "doc_id", "filed",
                   "title_locked", "resurface_at", "resurface_step"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if "tags" in sets:
            sets["tags"] = json.dumps(clean_tags(sets["tags"]))
        if "title" in sets:
            sets["title"] = (sets["title"] or "").strip()[:120] or "(untitled)"
        if sets:
            sets["updated"] = time.time()
            cols = ", ".join(f"{k} = ?" for k in sets)
            self.conn.execute(f"UPDATE brain_items SET {cols} WHERE id = ?", (*sets.values(), item_id))
        if "areas" in fields and self.areas:
            self.areas.set("item", item_id, list(fields["areas"] or [])[:3])
            self.conn.execute("UPDATE brain_items SET updated = ? WHERE id = ?", (time.time(), item_id))
        if {"title", "text", "note", "tags"} & set(sets) or "areas" in fields:
            await self.reindex(item_id)
        else:
            self.render(item_id)
        item = self.get(item_id)
        await self.publish(item)
        return item

    def index_text(self, item):
        tags = " ".join(item["tags"])
        return f"{item['title']}. {item['text']} {item['note']} {tags}".strip()

    async def reindex(self, item_id, vec=None):
        item = self.get(item_id)
        if not item:
            return
        text = self.index_text(item)
        if vec is None:
            try:
                vec = (await self.embedder.embed([text]))[0]
            except Exception as e:  # the embed service is down: keyword search still finds it
                log.warning("brain: item %s not embedded (%s)", item_id, e)
        self.mem._index("brain", item_id, text, vec)
        self.render(item_id)

    async def publish(self, item):
        if item and self.engine:
            try:
                await self.engine.publish({"type": "item", "item": self.public(item), "origin": "app"})
            except Exception:
                log.exception("brain: publish failed")

    # links -------------------------------------------------------------------------------------------
    def set_links(self, item_id, links, origin="auto"):
        """Replace the item's automatic `about` links (his own links and hidden ones stay)."""
        src = f"item:{item_id}"
        self.conn.execute("DELETE FROM graph_links WHERE source = ? AND kind = 'about' AND origin = 'auto' "
                          "AND action = 'add'", (src,))
        now = time.time()
        for target, reason in links[:MAX_LINKS]:
            if target == src or self.hidden(src, target, "about"):
                continue
            self.conn.execute("INSERT INTO graph_links(source, target, kind, label, origin, action, created, updated) "
                              "VALUES (?, ?, 'about', ?, ?, 'add', ?, ?)", (src, target, (reason or "")[:80], origin, now, now))

    def hidden(self, source, target, kind):
        return self.conn.execute("SELECT 1 FROM graph_links WHERE action = 'hide' AND kind = ? AND trashed_at IS NULL "
                                 "AND ((source = ? AND target = ?) OR (source = ? AND target = ?))",
                                 (kind, source, target, target, source)).fetchone() is not None

    def links_of(self, item_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM graph_links WHERE (source = ? OR target = ?) AND action = 'add' AND trashed_at IS NULL "
            "ORDER BY id", (f"item:{item_id}", f"item:{item_id}"))]

    def node_label(self, node_id):
        kind, _, ref = node_id.partition(":")
        try:
            ref = int(ref)
        except ValueError:
            return None
        if kind == "entity" and (e := self.mem.get_entity(ref)):
            return e["name"]
        if kind == "fact" and (f := self.mem.get_fact(ref)) and f["status"] in ("active", "archived"):
            return f["text"]
        if kind == "item" and (i := self.get(ref)):
            return i["title"]
        return None

    async def candidates(self, text, vec, exclude=None):
        """{'E12': 'project: Robot de riego', 'F40': 'He needs to renew…', 'I3': 'idea: …'} for the model:
        entities he named, then the nearest facts and items, and the entities of those facts."""
        out, low = {}, text.lower()
        for e in self.mem.entities():
            names = [e["name"], *(e["aliases"] or [])]
            if e["name"].lower() == user_name(self.cfg).lower():
                continue
            if any(len(n) > 2 and re.search(rf"(?<!\w){re.escape(n.lower())}(?!\w)", low) for n in names):
                out[f"E{e['id']}"] = f"{e['kind']}: {e['name']}" + (f" — {e['summary'][:120]}" if e["summary"] else "")
        if vec is not None:
            for kind, ref, cos in self.mem.search_vec(vec, k=12, kinds=("fact", "brain")):
                if cos < LINK_COS or len(out) >= 14:
                    continue
                if kind == "fact":
                    f = self.mem.get_fact(ref)
                    if not f or f["status"] != "active" or f["category"] == "laika_self":
                        continue
                    out[f"F{ref}"] = f["text"]
                    e = self.mem.get_entity(f["entity_id"]) if f["entity_id"] else None
                    if e and e["name"].lower() != user_name(self.cfg).lower() and f"E{e['id']}" not in out:
                        out[f"E{e['id']}"] = f"{e['kind']}: {e['name']}"
                elif ref != exclude and (i := self.get(ref)):
                    out[f"I{ref}"] = f"{i['kind'].replace('_', ' ')}: {i['title']}"
        me = user_name(self.cfg).lower()
        for f in self.mem.facts():  # his active goals and their projects are always candidates
            if f["category"] != "goal" or len(out) >= 18:
                continue
            out.setdefault(f"F{f['id']}", f["text"])
            e = self.mem.get_entity(f["entity_id"]) if f["entity_id"] else None
            if e and e["name"].lower() != me:
                out.setdefault(f"E{e['id']}", f"{e['kind']}: {e['name']}")
        return out

    @staticmethod
    def resolve_links(raw, cands):
        prefix = {"E": "entity", "F": "fact", "I": "item"}
        out = []
        for link in raw or []:
            key = str(link.get("id", "")).strip().upper()
            if key in cands and key[0] in prefix:
                node = f"{prefix[key[0]]}:{key[1:]}"
                if node not in [n for n, _ in out]:
                    out.append((node, str(link.get("reason", "")).strip()))
        return out[:MAX_LINKS]

    # the chat ------------------------------------------------------------------------------------------
    async def before_turn(self, batch, batch_id=None):
        """Notes for this turn: what was just saved or corrected, and saved items that fit the talk."""
        notes = []
        text = "\n".join(m["text"] for m in batch)
        attached = [(m.get("meta") or {}).get("attachment", {}).get("item_id") for m in batch]
        if any(attached):  # a file he sent from the app with "comment": react to it, don't save it again
            return [n for i in attached if i and (n := self._attachment_note(i))]
        cap = self.db.get_setting("brain_caption") or {}
        if cap and time.time() - cap.get("ts", 0) < CAPTION_WINDOW_S and text.strip() == cap.get("text"):
            self.db.set_setting("brain_caption", None)
            item = self.get(cap["item"])
            if item:
                return [personal("he sent a file with this message; the app saved it in his second brain (\"{title}\"). "
                                 "If he asks about it, it's there", self.cfg).format(title=item["title"])]
        if (hit := FORGET.search(text)) and (what := _content(hit["what"])):
            if (gone := await self.forget(what, batch[-1]["channel"])):
                notes.append(gone)
        elif (fix := self._correction(text)) is not None:
            notes.append(fix)
        elif CUE.search(text) or URL.search(text):
            notes += await self._capture(batch, text)
        notes += await self._recall(text, skip_sources={m["id"] for m in batch})
        if not notes:  # SB6: a quiet turn can bring back one old idea or "for later" item, or Sunday's digest
            notes += self._digest_note() or self._resurface_note(text)
        return notes

    async def forget(self, what, channel):
        """"Olvida el link de X": the matching items go at once (not to the trash), with their notes, links and,
        if nothing else uses it, their library document. Matches: cosine ≥ FORGET_COS, or all its words."""
        try:
            vec = await self.embedder.one(what, query=True)
        except Exception:
            vec = None
        words = {w for w in re.findall(r"\w{3,}", what.lower())}
        hits = {ref for _, ref, cos in (self.mem.search_vec(vec, k=40, kinds=("brain",)) if vec else []) if cos >= FORGET_COS}
        for item in self.items(status="all"):
            if words and all(w in self.index_text(item).lower() for w in words):
                hits.add(item["id"])
        titles = []
        for ref in sorted(hits):
            item = self.get(ref, trashed=True)
            if item:
                titles.append(item["title"])
                await self.delete(ref)
        if not titles:
            return None
        msg = self.db.add_message("system", channel, "🧹 Forgot from your saved things: " + "; ".join(t[:60] for t in titles[:5]))
        if self.engine:
            await self.engine.publish({"type": "message", "message": msg, "origin": channel})
        return personal("he asked you to forget this ({what}); it's gone from his saved things too, so don't bring it "
                        "up", self.cfg).format(what=what)

    async def delete(self, item_id):
        """Delete an item for good (chat "forget", or the trash's purge after 30 days)."""
        item = self.get(item_id, trashed=True)
        if not item:
            return
        node = f"item:{item_id}"
        self.mem._unindex("brain", item_id)
        self.conn.execute("DELETE FROM graph_links WHERE source = ? OR target = ?", (node, node))
        self.conn.execute("DELETE FROM area_links WHERE kind = 'item' AND ref_id = ?", (item_id,))
        self.conn.execute("DELETE FROM brain_items WHERE id = ?", (item_id,))
        if item["note_path"]:
            for p in (self.vault.path(item["note_path"]), self.vault.path(f".trash/{item['note_path']}")):
                if p.exists():  # its note, and the copy the trash keeps (SB8 purge left those behind)
                    p.unlink()
        if item["doc_id"] and self.library and not self.conn.execute(
                "SELECT 1 FROM brain_items WHERE doc_id = ?", (item["doc_id"],)).fetchone():
            await self.library.remove(item["doc_id"])
        log.info("brain: item %s deleted", item_id)

    async def comment_on(self, item_id, channel="web"):
        """POST /api/items with comment=true: once the file's text is there (or after COMMENT_WAIT_S), a "📎" message
        from him starts a turn, and the note gives Laika what's in it. Runs in the background."""
        for _ in range(COMMENT_WAIT_S):
            item = self.get(item_id)
            doc = self.library.document(item["doc_id"]) if item and item["doc_id"] and self.library else None
            if not doc or doc["status"] != "queued":
                break
            await asyncio.sleep(1)
        item = self.get(item_id)
        if item and self.engine and hasattr(self.engine, "receive"):
            await self.engine.receive(channel, f"📎 {item['title']}", meta={"attachment": {"item_id": item_id}})

    def _attachment_note(self, item_id):
        item = self.get(item_id)
        if not item:
            return None
        what = {"image": "a picture", "pdf": "a PDF", "book": "a book", "link": "a link", "video": "a video"}.get(
            item["format"], "a file")
        doc = self.library.document(item["doc_id"]) if item["doc_id"] and self.library else None
        body = ""
        if doc and doc["status"] == "ocr_pending":
            body = " It's a scan: its text will be read later, while he's away."
        elif doc and (doc.get("summary") or self.library.chunks(doc["id"])):
            text = doc.get("summary") or " ".join(c["text"] for c in self.library.chunks(doc["id"]) if c["kind"] == "text")
            body = f" What's in it: «{' '.join(text.split())[:COMMENT_CHARS]}»"
        elif item["format"] == "image":
            body = (" It has no text you can read, and you can't see pictures: ask him what it is, lightly, without "
                    "apologizing about it")
        elif item["text"]:
            body = f" What it is: «{item['text'][:COMMENT_CHARS]}»"
        return personal("he just sent you {what} from his desktop and it's saved in his second brain (\"{title}\")."
                        "{body} React to it the way a friend would: something specific about it, in a few bubbles; "
                        "don't summarize it all or announce that it was saved", self.cfg).format(
            what=what, title=item["title"], body=body)

    def _correction(self, text):
        hits = list(CORRECT.finditer(text))
        if not hits or not NEGATION.search(text[:hits[-1].start()]):
            return None
        hit = hits[-1]  # "no es una idea, es para después": the last kind named is the right one
        last = self.conn.execute("SELECT id, kind FROM brain_items WHERE trashed_at IS NULL AND created > ? "
                                 "ORDER BY id DESC LIMIT 1", (time.time() - CORRECT_WINDOW_S,)).fetchone()
        kind = CORRECT_KIND.get(re.sub(r"\s+", " ", hit["kind"].lower()))
        if not last or not kind or kind == last["kind"]:
            return None
        now = time.time()
        self.conn.execute("UPDATE brain_items SET kind = ?, updated = ?, resurface_at = ? WHERE id = ?",
                          (kind, now, now + RESURFACE_DAYS[0] * 86400 if kind != "saved" else None, last["id"]))
        self.render(last["id"])
        log.info("brain: item %s changed to %s from the chat", last["id"], kind)
        return personal("the app changed what he just saved into \"{kind}\", as he said; acknowledge it in a few "
                        "words", self.cfg).format(kind=kind.replace("_", " "))

    async def _capture(self, batch, text):
        urls = URL.findall(text)
        try:
            vec = await self.embedder.one(text)
            cands = await self.candidates(text, vec)
            out = await self._classify(batch, cands)
        except Exception as e:
            log.warning("brain: capture failed: %s", e)
            if CUE.search(text):
                return [personal("he may be asking you to save something, but the app couldn't save it right now: "
                                 "tell him it didn't work this time, don't say it's saved", self.cfg)]
            return []
        if not out.get("is_save"):
            return []
        kind = out.get("kind")
        areas = self.areas.clean(out.get("areas") or []) if self.areas else []
        title, body = (out.get("title") or "").strip(), (out.get("text") or "").strip()
        if kind in ("like", "dislike"):
            return [await self._save_taste(kind, body or title, areas, batch[-1]["id"])]
        if kind not in KINDS:
            kind = "saved"
        if urls and kind == "idea" and not CUE.search(text):
            kind = "saved"
        links = self.resolve_links(out.get("links"), cands)
        url = urls[0].rstrip(".,);]") if urls else None
        if url and (found := await self.find_url(url)):  # the same link again: no second item
            old, restored = found
            return [personal("he had already saved this link (\"{title}\"){back}; tell him in a few words", self.cfg)
                    .format(title=old["title"], back=", and it was in the trash: the app brought it back" if restored
                            else "")]
        item = await self.add(kind, title or (url or body[:60]), body, fmt=url_format(url) if url else "text", url=url,
                              areas=areas, tags=out.get("tags"), links=links, source_message_id=batch[-1]["id"],
                              channel=batch[-1]["channel"])
        if url and self.fetch_url:
            await self.fetch_url(item, url)  # SB2/SB4: the page or the reel goes into the library (in the background)
        where = ", ".join(self.node_label(n) or n for n, _ in links)
        related = self._related(item, vec)
        notes = [personal("the app saved this in his second brain as {kind}: \"{title}\"{where}. Confirm it in a few "
                         "words, in passing{linked}", self.cfg).format(
            kind={"idea": "an idea", "for_later": "something for later", "saved": "a saved thing"}[kind],
            title=item["title"], where=f", next to: {where}" if where else "",
            linked=" (you may say what you put it with)" if where else "")]
        if related:
            day = datetime.datetime.fromtimestamp(related["created"], self.tz).strftime("%d %b")
            notes.append(personal("it goes with something he saved on {day}: \"{title}\"; you may say so in passing",
                                  self.cfg).format(day=day, title=related["title"]))
        return notes

    def _related(self, item, vec):
        """The closest older saved item, if it's really close (SB6)."""
        if vec is None:
            return None
        for _, ref, cos in self.mem.search_vec(vec, k=10, kinds=("brain",)):
            if ref != item["id"] and cos >= RELATED_COS and (other := self.get(ref)) and other["status"] == "active":
                return other
        return None

    # SB6: resurfacing ---------------------------------------------------------------------------------------
    def due(self, now=None, limit=3):
        """Ideas and "for later" items whose time to come back has come (what GET /api/resurface lists)."""
        rows = self.conn.execute(
            "SELECT id FROM brain_items WHERE trashed_at IS NULL AND status = 'active' AND kind IN ('idea', 'for_later') "
            "AND resurface_at IS NOT NULL AND resurface_at <= ? ORDER BY resurface_at LIMIT ?",
            (now or time.time(), limit)).fetchall()
        return [self.get(r[0]) for r in rows]

    def advance(self, item, now=None):
        """The next step after it came back: 3 → 10 → 30 days, then no more."""
        now = now or time.time()
        step = item["resurface_step"] + 1
        when = now + RESURFACE_DAYS[step] * 86400 if step < len(RESURFACE_DAYS) else None
        self.conn.execute("UPDATE brain_items SET resurface_step = ?, resurface_at = ? WHERE id = ?",
                          (step, when, item["id"]))

    def _resurface_note(self, text, now=None):
        """At most one a day, in the day, and only in a light moment (not when he asks or writes a lot)."""
        now = now or time.time()
        local = datetime.datetime.fromtimestamp(now, self.tz)
        if self.db.get_setting("brain_resurfaced") == local.date().isoformat() or \
                not RESURFACE_HOURS[0] <= local.hour < RESURFACE_HOURS[1] or "?" in text or len(text) > 300:
            return []
        due = self.due(now, limit=1)
        if not due:
            return []
        item = due[0]
        self.advance(item, now)
        self.db.set_setting("brain_resurfaced", local.date().isoformat())
        days = max(1, int((now - item["created"]) // 86400))
        what = "an idea" if item["kind"] == "idea" else "something for later"
        log.info("brain: item %s offered for resurfacing", item["id"])
        return [personal("{days} days ago he saved {what} in his second brain: \"{title}\"{text}. If it fits at the end "
                         "of your reply, ask in a few words whether it still stands or he wants to do something with "
                         "it; if he's busy with something else or it's a serious moment, leave it", self.cfg).format(
            days=days, what=what, title=item["title"], text=f" ({item['text'][:120]})" if item["text"] else "")]

    def _digest_note(self, now=None):
        """Sunday's "still want these?" (prepared by the nightly job), once, in the day."""
        digest = self.db.get_setting("brain_digest") or {}
        now = now or time.time()
        local = datetime.datetime.fromtimestamp(now, self.tz)
        if digest.get("day") != local.date().isoformat() or not RESURFACE_HOURS[0] <= local.hour < RESURFACE_HOURS[1]:
            return []
        self.db.set_setting("brain_digest", None)
        titles = [i["title"] for i in (self.get(x) for x in digest.get("items", [])) if i and i["status"] == "active"]
        if not titles:
            return []
        return [personal("these have been waiting in his \"for later\" list for a while: {titles}. If it fits, ask him "
                         "lightly whether he still wants them (he can drop them or keep them); don't list them all "
                         "if he's busy", self.cfg).format(titles="; ".join(f'"{t}"' for t in titles))]

    async def nightly(self, edit=None, now=None):
        """SB6, after the nightly sleep: archive "for later" items that came back three times untouched, prepare
        Sunday's digest, and merge tags that mean the same (undoable from the trash)."""
        now = now or time.time()
        old = now - ARCHIVE_LATER_DAYS * 86400
        archived = 0
        for r in self.conn.execute("SELECT id FROM brain_items WHERE kind = 'for_later' AND status = 'active' AND "
                                   "trashed_at IS NULL AND resurface_at IS NULL AND created < ?", (old,)).fetchall():
            await self.update(r[0], status="archived")
            archived += 1
        local = datetime.datetime.fromtimestamp(now, self.tz)
        if local.weekday() == 6:  # Sunday
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM brain_items WHERE kind = 'for_later' AND status = 'active' AND trashed_at IS NULL AND "
                "created < ? ORDER BY created LIMIT 5", (now - DIGEST_MIN_AGE_DAYS * 86400,))]
            if ids:
                self.db.set_setting("brain_digest", {"day": local.date().isoformat(), "items": ids})
        merged = await self.merge_tags(edit) if edit else 0
        if archived or merged:
            log.info("brain nightly: %d 'for later' items archived, %d tags merged", archived, merged)
        return archived, merged

    async def merge_tags(self, edit):
        rows = self.conn.execute("SELECT t.value AS tag, COUNT(*) AS n FROM brain_items b, json_each(b.tags) t "
                                 "WHERE b.trashed_at IS NULL GROUP BY t.value").fetchall()
        tags = {r["tag"]: r["n"] for r in rows}
        if len(tags) < 2:
            return 0
        names = sorted(tags)
        vecs = await self.embedder.embed([n.replace("-", " ") for n in names])
        pairs = []
        for i, a in enumerate(names):
            for j in range(i + 1, len(names)):
                cos = sum(x * y for x, y in zip(vecs[i], vecs[j]))
                if cos >= TAG_COS:
                    pairs.append((cos, a, names[j]))
        merged, gone = 0, set()
        for _, a, b in sorted(pairs, reverse=True):
            if merged >= MAX_TAG_MERGES or a in gone or b in gone:
                continue
            out = await self.llm.chat([{"role": "system", "content": TAG_SAME},
                                       {"role": "user", "content": f"1: {a}\n2: {b}"}], self.slot,
                                      json_schema={"type": "object", "properties": {"same": {"type": "boolean"}},
                                                   "required": ["same"]}, temperature=0.0, max_tokens=20)
            if not out.get("same"):
                continue
            keep, drop = (a, b) if (tags[a], len(b)) >= (tags[b], len(a)) else (b, a)
            await edit.patch(f"tag:{drop}", {"name": keep})
            gone.add(drop)
            merged += 1
            log.info("brain: tag %r merged into %r", drop, keep)
        return merged

    async def _classify(self, batch, cands):
        now = datetime.datetime.now(self.tz).strftime("%A %d %B %Y, %H:%M")
        rows = self.conn.execute(
            "SELECT role, text FROM messages WHERE role IN ('user', 'companion') AND id < ? ORDER BY id DESC LIMIT ?",
            (batch[0]["id"], CONTEXT_MSGS)).fetchall()
        name = user_name(self.cfg)
        lines = [f"{name if r['role'] == 'user' else 'Laika'}: {r['text'][:300]}" for r in reversed(rows)]
        lines += [f"{name}: {m['text'][:1500]}" for m in batch]
        system = personal(CLASSIFY, self.cfg).format(
            now=now, areas=self.areas.prompt_list() if self.areas else "(none)",
            candidates="\n".join(f"{k}: {v[:160]}" for k, v in cands.items()) or "(none)")
        schema = _schema(self.areas.schema() if self.areas else {"type": "array", "maxItems": 0, "items": {"type": "string"}})
        return await self.llm.chat([{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}],
                                   self.slot, json_schema=schema, temperature=0.0, max_tokens=300)

    async def capture_text(self, text, kind=None, areas=None, tags=None, note=""):
        """POST /api/items with {text}: filed like a chat capture (kind, title, areas, tags, links); what he
        gave wins. If the model is unreachable, the text is still saved (as an idea unless he said)."""
        try:
            vec = await self.embedder.one(text)
            cands = await self.candidates(text, vec)
            out = await self._classify([{"id": 0, "text": text, "channel": "app"}], cands)
        except Exception as e:
            log.warning("brain: filing a text from the app failed: %s", e)
            vec, cands, out = None, {}, {}
        k = kind or (out.get("kind") if out.get("kind") in KINDS else None) or "idea"
        return await self.add(k, out.get("title") or text[:60], text, note=note,  # his words stay as he wrote them (SB8)
                              areas=areas if areas is not None else (self.areas.clean(out.get("areas") or [])
                                                                     if self.areas else []),
                              tags=tags if tags is not None else out.get("tags"),
                              links=self.resolve_links(out.get("links"), cands), channel="app", vec=None)

    async def _save_taste(self, polarity, text, areas, msg_id):
        if not text:
            return personal("he asked to keep a like or dislike, but it wasn't clear what: ask him", self.cfg)
        vec = await self.embedder.one(text)
        me = self.mem.entity(user_name(self.cfg), "person")
        fid = self.mem.add_fact({"entity_id": me["id"], "text": text[0].upper() + text[1:], "category": "preference",
                                 "importance": 0.6, "confidence": 1.0, "origin": "told", "evidence": [msg_id, msg_id],
                                 "polarity": polarity}, vec)
        if self.areas:
            self.areas.set("fact", fid, areas)
        log.info("brain: saved a %s as fact %s", polarity, fid)
        return personal("the app saved this among his {what}: \"{text}\". Confirm it in a few words", self.cfg).format(
            what="likes" if polarity == "like" else "dislikes", text=text)

    async def _recall(self, text, skip_sources=()):
        asked = bool(ASKED.search(text))
        if not asked and len(text.split()) < 3:
            return []
        try:
            query = await self.embedder.one(text, query=True)
        except Exception:
            return []
        offered = self.db.get_setting("brain_offered") or {}
        now = time.time()
        offered = {k: v for k, v in offered.items() if now - v < OFFER_AGAIN_S}
        notes = []
        hits = [(ref, cos) for _, ref, cos in self.mem.search_vec(query, k=60, kinds=("brain",))]
        if asked:  # "what did I save about X?": words count too, not only meaning
            words = [ref for _, ref, _ in self.mem.search_text(ASKED.sub(" ", text), k=5, kinds=("brain",))]
            hits = [(ref, 1.0) for ref in words] + [(ref, cos) for ref, cos in hits if ref not in words]
        for ref, cos in hits:
            if cos < (ASKED_COS if asked else RECALL_COS) or (str(ref) in offered and not asked):
                continue
            item = self.get(ref)
            if not item or item["status"] != "active" or item["source_message_id"] in skip_sources:
                continue  # (not what he is saving right now)
            day = datetime.datetime.fromtimestamp(item["created"], self.tz).strftime("%d %b")
            links = [self.node_label(lk["target"]) for lk in self.links_of(ref) if lk["source"] == f"item:{ref}"]
            links = [x for x in links if x]
            what = {"idea": "an idea", "for_later": "something for later", "saved": "a saved thing"}[item["kind"]]
            notes.append(personal("something he saved in his second brain ({what}, {day}{links}): \"{title}\" {text}. "
                                  "Bring it up only if it really fits", self.cfg).format(
                what=what, day=day, links=", with: " + "; ".join(links)[:120] if links else "", title=item["title"],
                text=("— " + item["text"][:200]) if item["text"] else ""))
            offered[str(ref)] = now
            if len(notes) >= (5 if asked else 2):
                break
        self.db.set_setting("brain_offered", offered)
        return notes

    # library documents ----------------------------------------------------------------------------------
    async def on_document(self, event, doc):
        """Library hook: every document he adds is a `saved` item; filed once its text is there."""
        item = self.by_doc(doc["id"])
        meta = json.loads(doc.get("meta") or "{}")
        if event == "added" and not item and meta.get("item_id") and (owner := self.get(meta["item_id"])):
            self.conn.execute("UPDATE brain_items SET doc_id = ?, updated = ? WHERE id = ?",
                              (doc["id"], time.time(), owner["id"]))  # a link's page or a reel's transcript (SB2/SB4)
            if not owner["title_locked"] and not filename_like(doc["title"]):
                self.conn.execute("UPDATE brain_items SET title = ? WHERE id = ?", (doc["title"][:120], owner["id"]))
            self.render(owner["id"])
            await self.publish(self.get(owner["id"]))
            return
        if event == "added" and not item and doc.get("replaces") and (old := self.by_doc(doc["replaces"])):
            self.conn.execute("UPDATE brain_items SET doc_id = ?, updated = ? WHERE id = ?",
                              (doc["id"], time.time(), old["id"]))  # sent again after a failure: same item
            return
        if event == "added" and not item:
            caption = (meta.get("caption") or "").strip()
            title = doc["title"] or meta.get("filename") or "(document)"
            if caption and filename_like(title):
                title = caption.split("\n")[0][:80]
            new = await self.add(_caption_kind(caption), title, "", fmt=doc_format(doc.get("kind")), doc_id=doc["id"],
                                 url=meta.get("url"), note=caption, channel=meta.get("channel"), filed=False)
            if caption:  # the caption also comes as a chat message: it mustn't save a second item
                self.db.set_setting("brain_caption", {"text": caption, "item": new["id"], "ts": time.time()})
        elif item and event in ("indexing", "ready"):
            fmt = item["format"] if item["format"] in ("link", "video") else doc_format(doc.get("kind"))
            if not item["title_locked"] and doc["title"] and not filename_like(doc["title"]) and \
                    (item["title"] != doc["title"] or item["format"] != fmt):
                await self.update(item["id"], title=doc["title"], format=fmt)
            elif event == "ready":
                await self.publish(item)  # the app shows "ready" (and the pet eats) without polling
        elif item and event == "deleted":
            self.conn.execute("UPDATE brain_items SET doc_id = NULL, updated = ? WHERE id = ?", (time.time(), item["id"]))

    async def backfill_library(self):
        """Documents added before SB1 get their `saved` item (once)."""
        if not self.library:
            return 0
        n = 0
        for doc in self.library.documents():
            if not doc["status"].startswith("failed") and not self.by_doc(doc["id"]):
                await self.on_document("added", doc)
                n += 1
        if n:
            log.info("brain: %d library documents became saved items", n)
        return n

    def pending(self):
        """Library items whose text is there but that haven't been filed (areas, tags, links) yet."""
        return self._unfiled() is not None

    def _unfiled(self):
        if not self.library:
            return None
        for row in self.conn.execute("SELECT * FROM brain_items WHERE filed = 0 AND doc_id IS NOT NULL AND "
                                     "trashed_at IS NULL ORDER BY id"):
            doc = self.library.document(row["doc_id"])
            if doc and doc["status"] in ("indexing", "ready"):
                return self._row(row)
        return None

    async def run(self):
        """Background job (jobs.py): file library items one by one while he's away."""
        while (item := self._unfiled()) is not None:
            doc = self.library.document(item["doc_id"])
            if not self.library.chunks(doc["id"]):  # a picture without text: nothing to read (SB5)
                await self.update(item["id"], filed=1)
                continue
            start = ""
            for c in self.library.chunks(doc["id"])[:4]:
                start += c["text"] + "\n"
                if len(start) > 3000:
                    break
            text = f"Title: {doc['title']}" + (f" — {doc['author']}" if doc["author"] else "") + "\n\n" + start[:3000]
            vec = await self.embedder.one(text[:2000])
            cands = await self.candidates(text[:2000], vec, exclude=item["id"])
            system = personal(FILE_DOC, self.cfg).format(
                areas=self.areas.prompt_list() if self.areas else "(none)",
                candidates="\n".join(f"{k}: {v[:160]}" for k, v in cands.items()) or "(none)")
            schema = _schema(self.areas.schema() if self.areas else {"type": "array", "maxItems": 0,
                                                                         "items": {"type": "string"}}, with_kind=False)
            out = await self.llm.chat([{"role": "system", "content": system}, {"role": "user", "content": text}],
                                      self.slot, json_schema=schema, temperature=0.0, max_tokens=300)
            fields = {"filed": 1, "text": item["text"] or (out.get("text") or "").strip()}
            if not item["tags"]:  # what he set when he sent it wins
                fields["tags"] = out.get("tags") or []
            if not item["areas"] and self.areas:
                fields["areas"] = self.areas.clean(out.get("areas") or [])
            if not item["title_locked"] and out.get("title") and (doc["source"] == "paste" or filename_like(doc["title"])):
                fields["title"], fields["title_locked"] = out["title"], 1  # keep it: the document's own is a file name
            self.set_links(item["id"], self.resolve_links(out.get("links"), cands))
            await self.update(item["id"], **fields)
            log.info("brain: filed library item %s (doc %s)", item["id"], doc["id"])

    # vault ------------------------------------------------------------------------------------------------
    def render(self, item_id):
        item = self.get(item_id)
        if not item:
            return
        rel = item["note_path"] or f"Brain/{FOLDERS[item['kind']]}/{note_name(item['title'])}.md"
        want = f"Brain/{FOLDERS[item['kind']]}/"
        if not rel.startswith(want):  # the kind changed: move the note
            old = self.vault.path(rel)
            rel = want + rel.rsplit("/", 1)[-1]
            if old.exists():
                new = self.vault.path(rel)
                new.parent.mkdir(parents=True, exist_ok=True)
                old.replace(new)
        lines = [item["text"]] if item["text"] else []
        if item["note"]:
            lines += ["", f"> {item['note']}"]
        lines.append("")
        if item["url"]:
            lines.append(f"- Link: {item['url']}")
        if item["doc_id"] and self.library and (doc := self.library.document(item["doc_id"])) and doc.get("note_path"):
            lines.append(f"- In the library: [[{doc['note_path'][:-3]}]]")
        if item["areas"] and self.areas:
            names = {a["id"]: a["name"] for a in self.areas.list()}
            lines.append("- Areas: " + ", ".join(names.get(a, a) for a in item["areas"]))
        if item["tags"]:
            lines.append("- Tags: " + " ".join(f"#{t}" for t in item["tags"]))
        for lk in self.links_of(item_id):
            other = lk["target"] if lk["source"] == f"item:{item_id}" else lk["source"]
            label = self.node_label(other)
            if label:
                lines.append(f"- With: {label[:100]}" + (f" — {lk['label']}" if lk["label"] else ""))
        day = datetime.datetime.fromtimestamp(item["created"], self.tz).strftime("%Y-%m-%d")
        lines.append(f"- Saved: {day}" + (f" ({item['channel']})" if item["channel"] else "") +
                     ("" if item["status"] == "active" else f", {item['status']}"))
        body = "\n".join(lines).strip()
        self.vault.write(rel, body, title=item["title"], kind=f"brain-{item['kind']}")
        self.conn.execute("UPDATE brain_items SET note_path = ?, note_body = ? WHERE id = ?", (rel, body, item_id))

    async def apply_vault_edit(self, rel):
        """His edit of an item's note in Obsidian wins (SB7b): the text above the list, the "> " note and the
        "- Tags:" line. Returns 1 if something changed."""
        row = self.conn.execute("SELECT id, note_body FROM brain_items WHERE note_path = ? AND trashed_at IS NULL",
                                (rel,)).fetchone()
        section = self.vault.section(rel)
        if not row or section is None or section.strip() == (row["note_body"] or "").strip():
            return 0
        item = self.get(row["id"])
        lines = section.strip().splitlines()
        text, note, tags = [], [], None
        for ln in lines:
            if ln.startswith(">"):
                note.append(ln.lstrip("> ").strip())
            elif ln.startswith("- Tags:"):
                tags = re.findall(r"#([\w-]+)", ln)
            elif not ln.startswith("- ") and not note:
                text.append(ln)
        fields = {"text": "\n".join(text).strip(), "note": " ".join(note).strip()}
        if tags is not None:
            fields["tags"] = tags
        elif item["tags"]:
            fields["tags"] = []  # he deleted the Tags line
        changed = {k: v for k, v in fields.items() if v != item[k]}
        if not changed:
            self.conn.execute("UPDATE brain_items SET note_body = ? WHERE id = ?", (section.strip(), row["id"]))
            return 0
        await self.update(row["id"], **changed)
        log.info("brain: the user edited item %s in %s (%s)", row["id"], rel, ", ".join(changed))
        return 1

    # API shape (SB7) --------------------------------------------------------------------------------------
    def iso(self, ts):
        return datetime.datetime.fromtimestamp(ts, self.tz).isoformat(timespec="seconds") if ts else None

    def public(self, item):
        doc = None
        if item["doc_id"] and self.library and (d := self.library.document(item["doc_id"])):
            n = self.library.counts(d["id"])
            pages = n.get("pages") or {}
            doc = {"id": d["id"], "title": d["title"], "state": self.library.state(d),
                   "progress": {"chunks": n["chunks"], "sections": n["sections"], "pages": n["pages"]},
                   "pages": pages.get("total") if isinstance(pages, dict) else None}
        return {"id": item["id"], "kind": item["kind"], "title": item["title"], "text": item["text"],
                "note": item["note"], "areas": item["areas"], "tags": item["tags"], "format": item["format"],
                "url": item["url"], "doc": doc, "status": item["status"], "created_at": self.iso(item["created"]),
                "updated_at": self.iso(item["updated"]), "resurface_at": self.iso(item["resurface_at"])}
