"""Memory work in the background (PLAN P3.3): only while the user is away, never in his way.

Every minute the runner checks whether there is something to do:
- the nightly "sleep" after 03:00 (once a day, after 10 quiet minutes), or when asked by
  `python -m laika.admin nightly`;
- closing episodes once he has been quiet for `idle_close_min` (or `admin consolidate`);
- embedding library chunks (P4.1) once he has been quiet for 2 minutes. Files in the vault's
  `Library/_inbox/` are picked up on the same check, whenever he isn't busy;
- chapter and book summaries of ready books (P4.3), then word glosses and the difficulty in the book
  notes (P4.4), once he has been quiet for 10 minutes.
A job runs on the background slot. The moment he writes, types or records, it is cancelled (the
model stops generating) and tried again later; writes happen only after the model calls, so a
cancelled job leaves nothing half-done.
"""
import asyncio
import datetime
import logging
import time
import zoneinfo

log = logging.getLogger(__name__)

CHECK_S = 60
WATCH_S = 1
NIGHTLY_QUIET_S = 600
RETRY_AFTER_ERROR_S = 1800
LIBRARY_QUIET_S = 120


class Jobs:
    def __init__(self, cfg, db, engine, consolidator, nightly, library=None, summaries=None, vocab=None, brain=None):
        self.cfg, self.db, self.engine, self.consolidator, self.nightly = cfg, db, engine, consolidator, nightly
        self.library, self.summaries, self.vocab, self.brain = library, summaries, vocab, brain
        self.tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
        self.failed_at = {}

    def busy(self):
        e = self.engine
        return bool(e.pending) or bool(e.turn and not e.turn.done()) or e.composing()

    def quiet_for(self):
        row = self.db.conn.execute("SELECT MAX(ts) FROM messages").fetchone()
        return time.time() - (row[0] or 0)

    def next_job(self):
        m = self.cfg["memory"]
        now = datetime.datetime.now(self.tz)
        hh, mm = map(int, m["nightly"].split(":"))
        due = (now.hour, now.minute) >= (hh, mm) and self.db.get_setting("nightly_done") != now.date().isoformat()
        if self.db.get_setting("nightly_requested") or (due and self.quiet_for() >= NIGHTLY_QUIET_S):
            return "nightly"
        if not self.consolidator.pending():
            if self.db.get_setting("consolidate_requested"):
                self.db.set_setting("consolidate_requested", False)
        elif self.db.get_setting("consolidate_requested") or self.quiet_for() >= m.get("idle_close_min", 30) * 60:
            return "consolidate"
        if self.library and self.library.pending() and self.quiet_for() >= LIBRARY_QUIET_S:
            return "library"
        if self.library and self.quiet_for() >= LIBRARY_QUIET_S and self.library.ocr_pending():
            return "ocr"  # SB3: scanned PDF pages, page by page
        if self.brain and self.quiet_for() >= LIBRARY_QUIET_S and self.brain.pending():
            return "brain"  # SB1: file saved documents (areas, tags, links)
        if getattr(self, "links", None) and self.quiet_for() >= LIBRARY_QUIET_S and self.links.pending():
            return "links"  # SB4: a saved reel or video, transcribed
        if self.summaries and self.quiet_for() >= NIGHTLY_QUIET_S and self.summaries.pending():
            return "summaries"
        if self.vocab and self.quiet_for() >= NIGHTLY_QUIET_S and self.vocab.pending():
            return "vocab"
        return None

    async def loop(self):
        while True:
            await asyncio.sleep(CHECK_S)
            try:
                if self.library and not self.busy():
                    await self.library.scan_inbox()
                job = None if self.busy() else self.next_job()
                if job and time.time() - self.failed_at.get(job, 0) > RETRY_AFTER_ERROR_S:
                    await self.run(job)
            except Exception:
                log.exception("memory job runner")

    async def _nightly(self):
        await self.nightly.run()
        if self.library:
            self.library.reconcile()  # SB0/P8: a ready document has nothing left to embed
        if getattr(self, "graph_edit", None):
            await self.graph_edit.purge()  # SB7b: 30 days in the trash, then gone for good
        if self.brain:
            await self.brain.nightly(getattr(self, "graph_edit", None))  # SB6: archive, Sunday digest, tag merges

    async def run(self, job):
        work = {"nightly": self._nightly, "consolidate": self.consolidator.close_all,
                "library": self.library and self.library.run, "summaries": self.summaries and self.summaries.run,
                "vocab": self.vocab and self.vocab.run, "brain": self.brain and self.brain.run,
                "links": getattr(self, "links", None) and self.links.run,
                "ocr": self.library and self.library.run_ocr}[job]()
        task = asyncio.create_task(work)
        log.info("memory job started: %s", job)
        while not task.done():
            await asyncio.wait({task}, timeout=WATCH_S)
            if not task.done() and self.busy():
                task.cancel()
                log.info("memory job %s paused: the user is here", job)
                return False
        if task.cancelled():
            return False
        if task.exception():
            self.failed_at[job] = time.time()
            log.error("memory job %s failed", job, exc_info=task.exception())
            return False
        if job not in ("library", "summaries", "vocab", "brain", "links", "ocr"):
            self.db.set_setting(f"{job}_requested", False)
        log.info("memory job finished: %s", job)
        return True
