"""Links and videos behind saved items (SB2 + SB4, docs/PLAN-second-brain.md).

SB2, a web page: fetched in the background right after it's saved (30 s, at most 5 MB), the article is
taken out of the page with trafilatura (no menus, ads or comments), and saved in the library as a
Markdown document attached to the item (`meta.item_id`), so links that rot later still have their
text. The raw HTML is kept next to it (`data/library/snapshots/<doc id>.html`). A link to a file (a
PDF, an EPUB…) is added as that file. If the page can't be read (a login wall, JavaScript only, too
little text), the item keeps the link and title, and a 📚 notice says so: he can paste the text.

SB4, a video (Instagram reel, YouTube, TikTok, Vimeo): queued, and done by the background job while
he's away (Whisper runs on the CPU: ~80 s per minute of audio). yt-dlp takes the audio and the
caption (at most 10 minutes), the voice service transcribes it, and the caption plus the transcript
become the item's library document. Videos that need a login fail with a notice; after 3 failed
tries the item keeps only the link.
"""
import asyncio
import json
import logging
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse

import httpx

from . import extract
from .consolidate import note_name

log = logging.getLogger(__name__)

TIMEOUT_S = 30
MAX_HTML = 5 * 2**20
MAX_FILE_MB = 100
MIN_TEXT = 300           # characters: less than this is a login wall or a JavaScript app
VIDEO_MAX_S = 600
VIDEO_TRIES = 3
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 "
      "(Laika, a personal assistant saving a page for its owner)")


class Unreadable(Exception):
    pass


class Links:
    def __init__(self, cfg, db, library, brain, engine):
        self.cfg, self.db, self.library, self.brain, self.engine = cfg, db, library, brain, engine
        self.voice_url = cfg["voice"]["url"].rstrip("/")
        self.snapshots = library.dir / "snapshots"
        self.tasks = set()
        self.transport = None  # tests put an httpx.MockTransport here
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(brain_items)")}
        if "fetch_tries" not in cols:
            db.conn.execute("ALTER TABLE brain_items ADD COLUMN fetch_tries INTEGER NOT NULL DEFAULT 0")
            db.conn.execute("ALTER TABLE brain_items ADD COLUMN fetch_error TEXT")

    async def fetch(self, item, url):
        """Brain hook: a page is read now (in the background); a video waits for the job."""
        if item["format"] == "video":
            log.info("links: video item %s queued", item["id"])
            return
        task = asyncio.create_task(self._page(item["id"], url))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # SB2: pages ---------------------------------------------------------------------------------------
    async def _page(self, item_id, url):
        item = self.brain.get(item_id)
        channel = (item or {}).get("channel") or "web"
        channel = channel if channel in ("web", "telegram") else "web"
        try:
            await self.read_page(item_id, url, channel)
        except Unreadable as e:
            self._failed(item_id, str(e))
            await self.library.say(channel, f"📚 I couldn't read the page «{_short(url)}» ({e}). The link is "
                                             f"saved; paste the text if you want it kept too")
        except Exception as e:
            log.exception("links: page %s for item %s failed", url, item_id)
            self._failed(item_id, f"{type(e).__name__}: {e}")
            await self.library.say(channel, f"📚 I couldn't read the page «{_short(url)}» (something went wrong "
                                             f"on my side). The link is saved")

    async def read_page(self, item_id, url, channel="web"):
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S, connect=10), follow_redirects=True,
                                     headers={"User-Agent": UA, "Accept-Language": "es,en;q=0.8"},
                                     transport=self.transport) as client:
            async with client.stream("GET", url) as r:
                if r.status_code in (401, 403):
                    raise Unreadable("it needs a login")
                if r.status_code >= 400:
                    raise Unreadable(f"the site answered {r.status_code}")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                limit = MAX_HTML if "html" in ctype or not ctype else MAX_FILE_MB * 2**20
                data = b""
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise Unreadable("the page is too big")
                final = str(r.url)
        name = _filename(final, r.headers.get("content-disposition", ""), ctype)
        if "html" not in ctype and extract.kind_of(name) not in (None, "html"):  # a file: add it as it is
            doc, reply = await self.library.add(data, name, "link", channel, quiet=True,
                                                meta={"item_id": item_id, "url": url})
            if doc is None:
                raise Unreadable(reply.removeprefix("📚 ") if reply else "an unknown kind of file")
            return doc
        html = data.decode(r.encoding or "utf-8", errors="replace")
        text, meta = await asyncio.to_thread(_article, html, final)
        if len(text) < MIN_TEXT:
            raise Unreadable("too little text: it may need a login or JavaScript")
        title = (meta.get("title") or _short(final)).strip()
        head = [f"# {title}", ""]
        info = [f"- Source: {final}"] + ([f"- Site: {meta['sitename']}"] if meta.get("sitename") else []) + \
            ([f"- Author: {meta['author']}"] if meta.get("author") else []) + \
            ([f"- Date: {meta['date']}"] if meta.get("date") else []) + \
            [f"- Saved: {time.strftime('%Y-%m-%d')}", ""]
        md = "\n".join(head + info) + "\n" + text
        doc, reply = await self.library.add(md.encode("utf-8"), f"{note_name(title)[:80]}.md", "link", channel,
                                            quiet=True, meta={"item_id": item_id, "url": url, "final_url": final,
                                                              "site": meta.get("sitename"), "date": meta.get("date")})
        if doc is None:
            raise Unreadable(reply or "the library refused it")
        self.snapshots.mkdir(parents=True, exist_ok=True)
        (self.snapshots / f"{doc['id']}.html").write_bytes(data)
        self.db.conn.execute("UPDATE brain_items SET fetch_error = NULL WHERE id = ?", (item_id,))
        log.info("links: page for item %s saved as document %s (%d characters)", item_id, doc["id"], len(text))
        return doc

    def _failed(self, item_id, error):
        self.db.conn.execute("UPDATE brain_items SET fetch_tries = fetch_tries + 1, fetch_error = ? WHERE id = ?",
                             (error[:300], item_id))

    # SB4: videos (background job) ------------------------------------------------------------------------------
    def _next_video(self):
        row = self.db.conn.execute(
            "SELECT id, url, channel FROM brain_items WHERE format = 'video' AND url IS NOT NULL AND doc_id IS NULL "
            "AND trashed_at IS NULL AND fetch_tries < ? ORDER BY id LIMIT 1", (VIDEO_TRIES,)).fetchone()
        return dict(row) if row else None

    def pending(self):
        return self._next_video() is not None

    async def run(self):
        while (v := self._next_video()) is not None:
            channel = v["channel"] if v["channel"] in ("web", "telegram") else "web"
            try:
                await self.read_video(v["id"], v["url"], channel)
            except Unreadable as e:
                self._failed(v["id"], str(e))
                tries = self.db.conn.execute("SELECT fetch_tries FROM brain_items WHERE id = ?", (v["id"],)).fetchone()[0]
                if tries >= VIDEO_TRIES or "login" in str(e):
                    self.db.conn.execute("UPDATE brain_items SET fetch_tries = ? WHERE id = ?", (VIDEO_TRIES, v["id"]))
                    await self.library.say(channel, f"📚 I couldn't watch the video «{_short(v['url'])}» ({e}). "
                                                    f"The link is saved")

    async def read_video(self, item_id, url, channel="web"):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="laika-video-"))
        try:
            info = await asyncio.to_thread(_download_audio, url, tmp)
            audio = next((p for p in tmp.iterdir() if p.suffix not in (".json", ".part")), None)
            if audio is None:
                raise Unreadable("no audio came")
            wav = tmp / "audio.wav"
            await asyncio.to_thread(subprocess.run, ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i",
                                                     str(audio), "-ac", "1", "-ar", "16000", str(wav)], check=True,
                                    timeout=300)
            async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10), transport=self.transport) as client:
                r = await client.post(f"{self.voice_url}/v1/audio/transcriptions",
                                      files={"file": ("audio.wav", wav.read_bytes())}, data={"language": "", "prompt": ""})
                r.raise_for_status()
                stt = r.json()
            caption = (info.get("description") or "").strip()
            title = (info.get("title") or "").strip()
            if not title or title.lower().startswith("video by"):
                title = (caption.split("\n")[0][:80] or f"Video de {info.get('uploader') or 'Instagram'}").strip()
            transcript = (stt.get("text") or "").strip()
            if not transcript and not caption:
                raise Unreadable("it has no speech and no caption")
            lines = [f"# {title}", "", f"- Source: {url}"]
            if info.get("uploader"):
                lines.append(f"- By: {info['uploader']}")
            if info.get("duration"):
                lines.append(f"- Length: {int(info['duration']) // 60}:{int(info['duration']) % 60:02d}")
            lines += [f"- Saved: {time.strftime('%Y-%m-%d')}", ""]
            if caption:
                lines += ["## Caption", "", caption, ""]
            if transcript:
                lines += ["## Transcript" + (f" ({stt.get('language')})" if stt.get("language") else ""), "", transcript]
            doc, reply = await self.library.add("\n".join(lines).encode("utf-8"), f"{note_name(title)[:80]}.md", "video",
                                                channel, quiet=True, meta={"item_id": item_id, "url": url,
                                                                           "uploader": info.get("uploader"),
                                                                           "duration": info.get("duration")})
            if doc is None:
                raise Unreadable(reply or "the library refused it")
            self.db.conn.execute("UPDATE brain_items SET fetch_error = NULL WHERE id = ?", (item_id,))
            log.info("links: video for item %s transcribed into document %s (%d characters)", item_id, doc["id"],
                     len(transcript))
            return doc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _download_audio(url, folder):
    """The best audio and the info JSON with yt-dlp. Raises Unreadable for a login wall, a long video or an
    unsupported site."""
    import yt_dlp

    def short_enough(info, *, incomplete):
        d = info.get("duration")
        return f"longer than {VIDEO_MAX_S // 60} minutes" if d and d > VIDEO_MAX_S else None

    opts = {"format": "bestaudio/best", "outtmpl": str(folder / "media.%(ext)s"), "quiet": True, "no_warnings": True,
            "noprogress": True,
            "noplaylist": True, "match_filter": short_enough, "socket_timeout": 30, "retries": 2}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "login" in msg or "cookies" in msg or "private" in msg or "rate-limit" in msg:
            raise Unreadable("it needs a login") from None
        if "unsupported url" in msg:
            raise Unreadable("this site isn't supported") from None
        raise Unreadable(re.sub(r"^error:\s*", "", str(e), flags=re.I)[:120]) from None
    if not info:
        raise Unreadable(f"longer than {VIDEO_MAX_S // 60} minutes")
    if info.get("duration") and info["duration"] > VIDEO_MAX_S:
        raise Unreadable(f"longer than {VIDEO_MAX_S // 60} minutes")
    (folder / "info.json").write_text(json.dumps({k: info.get(k) for k in ("title", "description", "uploader",
                                                                            "duration")}), encoding="utf-8")
    return info


def _article(html, url):
    """(text, metadata) of the page's main content, as Markdown, via trafilatura."""
    import trafilatura
    text = trafilatura.extract(html, url=url, output_format="markdown", include_comments=False, include_tables=True,
                               include_links=False, favor_recall=True) or ""
    meta = {}
    try:
        m = trafilatura.extract_metadata(html, default_url=url)
        if m:
            meta = {"title": m.title, "author": m.author, "sitename": m.sitename, "date": m.date}
    except Exception:  # metadata is a bonus
        pass
    if not meta.get("title"):
        hit = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        meta["title"] = re.sub(r"\s+", " ", hit.group(1)).strip() if hit else None
    return text.strip(), meta


def _filename(url, disposition, ctype):
    hit = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition or "", re.I)
    if hit:
        return urllib.parse.unquote(hit.group(1))
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) or "page"
    if "." not in name:
        ext = {"application/pdf": ".pdf", "application/epub+zip": ".epub", "text/plain": ".txt",
               "text/markdown": ".md"}.get(ctype, ".html")
        name += ext
    return name


def _short(url):
    p = urllib.parse.urlsplit(url)
    s = p.netloc.removeprefix("www.") + p.path
    return s if len(s) <= 60 else s[:59] + "…"
