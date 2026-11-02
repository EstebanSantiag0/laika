"""Obsidian vault notes rendered from the memory store (PLAN §7G, DECISIONS D14).

Laika writes only between the Laika markers; anything the user writes outside them is never
touched. Fact lines end with an Obsidian block id (^f123) so his edits there map back to the row
(P3.5). Notes are in English; the journal is her first-person diary. The vault has its own local
git history (committed nightly) so a bad rewrite can be rolled back.
"""
import datetime
import json
import logging
import re
import subprocess

log = logging.getLogger(__name__)

START, END = "<!-- laika:start -->", "<!-- laika:end -->"
BLOCK_ID = re.compile(r"\s\^f(\d+)\s*$")
CHECKED = re.compile(r"^\s*-\s*\[[xX]\]")
GITIGNORE = (".laika/backup/\n.laika/rendered.json\n.obsidian/\n.trash/\n*.tmp~\n"
             "# P4.1: files dropped for the library are stored in data/library/originals, not in git\n"
             "Library/_inbox/\n*.epub\n*.pdf\n*.fb2\n*.zip\n*.docx\n")
from .config import DEFAULT_USER

RENDERED = ".laika/rendered.json"  # note -> {fact id: line} as Laika wrote it (to see what the user changed)


def fact_line(fact, checkbox=False):
    when = ""
    if fact.get("status") in ("superseded", "past") and fact.get("valid_to"):
        when = f"(until {fact['valid_to']}) "
    elif fact.get("valid_from") and fact.get("category") == "event":
        when = f"({fact['valid_from']}) "
    return f"- {'[ ] ' if checkbox else ''}{when}{fact['text']} ^f{fact['id']}"


def fact_ids(text):
    """{fact id: line text without the block id} for every fact line in a Laika section."""
    out = {}
    for line in text.splitlines():
        if m := BLOCK_ID.search(line):
            out[int(m.group(1))] = re.sub(r"^\s*-\s*(\[[ xX]\]\s*)?", "", line[:m.start()]).strip()
    return out


def checked_ids(text):
    """Fact ids whose Inbox checkbox the user ticked."""
    return {int(m.group(1)) for line in text.splitlines() if CHECKED.match(line) and (m := BLOCK_ID.search(line))}


def shown_text(line_text):
    """A fact line's text without the '(until …)' / '(date)' prefix Laika adds."""
    return re.sub(r"^\((until )?[^()]{4,20}\)\s+", "", line_text).strip()


class Vault:
    def __init__(self, root, user=DEFAULT_USER):
        self.root = root
        self.user = user  # the user's folder: <name>/Profile.md and <name>/Now.md
        self.profile = f"{user}/Profile.md"

    def path(self, rel):
        return self.root / rel

    def read(self, rel):
        p = self.path(rel)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def section(self, rel):
        """The Laika section of a note (as Laika wrote it, or as the user edited it), or None."""
        text = self.read(rel)
        if text is None or START not in text or END not in text:
            return None
        return text.split(START, 1)[1].split(END, 1)[0].strip("\n")

    def write(self, rel, body, title=None, kind="note", aliases=()):
        """Replace the Laika section of a note, creating the note if needed. True if it changed."""
        today = datetime.date.today().isoformat()
        section = f"{START}\n{body.strip()}\n{END}"
        old = self.read(rel)
        if old is None:
            front = ["---", f"type: {kind}", f"created: {today}", f"updated: {today}"]
            if aliases:
                front.append("aliases: [" + ", ".join(f'"{a}"' for a in aliases) + "]")
            new = "\n".join(front + ["---", ""] + ([f"# {title}", ""] if title else []) + [section, ""])
        elif START in old and END in old:
            head, rest = old.split(START, 1)
            new = head + section + rest.split(END, 1)[1]
        else:
            new = old.rstrip("\n") + "\n\n" + section + "\n"
        if new == old:
            return False
        new = re.sub(r"(?m)^updated: .*$", f"updated: {today}", new, count=1) if new.startswith("---") else new
        path = self.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp~")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(path)
        self.set_rendered(rel, fact_ids(body))
        return True

    def rendered(self):
        p = self.path(RENDERED)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            return {}

    def rendered_lines(self, rel):
        """{fact id: the line as Laika last wrote it, or as he last edited it}. Before 2026-10-20 only the ids
        were kept: their lines are None until the note is written again."""
        entry = self.rendered().get(rel, {})
        if isinstance(entry, list):
            return dict.fromkeys(entry)
        return {int(k): v for k, v in entry.items()}

    def set_rendered(self, rel, lines):
        data = self.rendered()
        data[rel] = {str(k): v for k, v in sorted(lines.items())}
        p = self.path(RENDERED)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=0), encoding="utf-8")

    def drop_fact(self, fact_id):
        """SB7b: take a fact's line out of every note Laika wrote it into, and out of rendered.json at the same
        time, so the watcher doesn't read it as his deletion (that would tombstone it). Returns the notes."""
        touched = []
        for rel in list(self.rendered()):
            lines = self.rendered_lines(rel)
            if fact_id not in lines:
                continue
            text = self.read(rel)
            if text is not None:
                kept = [ln for ln in text.split("\n") if not ((m := BLOCK_ID.search(ln)) and int(m.group(1)) == fact_id)]
                path = self.path(rel)
                tmp = path.with_name(path.name + ".tmp~")
                tmp.write_text("\n".join(kept), encoding="utf-8")
                tmp.replace(path)
            lines.pop(fact_id, None)
            self.set_rendered(rel, lines)
            touched.append(rel)
        return touched

    def trash_note(self, rel):
        """SB7b: move a note to `.trash/` (ignored by the watcher and by git). Returns its trash path or None."""
        src = self.path(rel)
        if not rel or not src.exists():
            return None
        dest_rel = f".trash/{rel}"
        dest = self.path(dest_rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dest)
        data = self.rendered()
        if rel in data:
            data.pop(rel)
            p = self.path(RENDERED)
            p.write_text(json.dumps(data, indent=0), encoding="utf-8")
        return dest_rel

    def untrash_note(self, rel):
        src = self.path(f".trash/{rel}")
        if not rel or not src.exists():
            return False
        dest = self.path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dest)
        section = self.section(rel)
        if section is not None:
            self.set_rendered(rel, fact_ids(section))
        return True

    def prompt_text(self, rel):
        """A note as prompt text: no frontmatter, HTML comments, block ids or the title line."""
        text = self.read(rel)
        if not text:
            return ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            text = text[end + 4:] if end != -1 else text
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        lines = [BLOCK_ID.sub("", line) for line in text.splitlines() if not line.startswith("# ")]
        return "\n".join(lines).strip()

    def commit(self, message):
        """Commit the vault to its own local git history (never pushed anywhere)."""
        def git(*args):
            return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
        if not (self.root / ".git").exists():
            git("init", "-q")
            (self.root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        git("add", "-A")
        r = git("-c", "user.name=Laika", "-c", "user.email=laika@localhost", "commit", "-q", "-m", message)
        if r.returncode not in (0, 1):  # 1 = nothing to commit
            log.warning("vault commit failed: %s", r.stderr.strip())
        return r.returncode == 0
