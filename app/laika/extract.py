"""Text out of books and files (PLAN P4.1, DECISIONS D18): bytes + filename → title, author, sections.

Pure functions with no database access, so library.py runs them in a worker thread.
- EPUB: container.xml → OPF spine. Chapter titles come from the nav (EPUB3) or the NCX (EPUB2) by
  href; spine items without a TOC entry (a chapter split over several files) join the previous one.
- PDF (pypdfium2): the outline's top level gives page ranges, else 10 pages per section. Words broken
  at line ends are joined. A PDF with almost no text is a scan, and there is no OCR.
- FB2, also inside a .zip; DOCX (Heading styles); HTML; TXT and MD (Markdown headings, "Chapter 3",
  "Глава 3", "第三章", roman numerals). Text without headings is cut into parts of ~3,000 tokens.
Project Gutenberg's header, footer and licence are dropped. Long titles keep their main part
("A Modest Proposal / For preventing…" → "A Modest Proposal"); the full one stays in `meta`.
Ruby annotations (`rt`/`rp`, furigana or pinyin over characters) are dropped, so 漢字 stays 漢字.
The language is detected on samples; traditional Chinese is converted to simplified (opencc), so
the keyword index and, later, the vocabulary see one script.
"""
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from . import lang

KINDS = {".epub": "epub", ".pdf": "pdf", ".fb2": "fb2", ".zip": "zip", ".docx": "docx", ".txt": "txt",
         ".md": "md", ".markdown": "md", ".html": "html", ".htm": "html", ".xhtml": "html"}
PDF_PAGES_PER_SECTION = 10
PDF_MIN_CHARS_PER_PAGE = 50
PART_TOKENS = 3000
SECTION_MAX_TOKENS = 12000  # longer "chapters" (a junk TOC, a huge PDF outline range) are cut into parts
TITLE_MAX = 60
MAX_UNZIPPED = 300 * 2**20  # an EPUB or zip that inflates past this is refused
TRADITIONAL_SHARE = 0.01


class Unreadable(Exception):
    """The file can't become text; the message is shown to the user."""


@dataclass
class Doc:
    title: str
    author: str = ""
    kind: str = ""
    lang: str | None = None
    sections: list = field(default_factory=list)  # [(title or None, text)]
    meta: dict = field(default_factory=dict)


def kind_of(filename):
    return KINDS.get(posixpath.splitext(filename.lower())[1])


def extract(data, filename):
    """Doc for a file, or Unreadable. Sections are non-empty; the language is set."""
    kind = kind_of(filename)
    stem = _clean_title(posixpath.splitext(posixpath.basename(filename))[0])
    if kind == "epub":
        doc = _epub(data)
    elif kind == "pdf":
        doc = _pdf(data)
    elif kind == "fb2":
        doc = _fb2(data)
    elif kind == "zip":
        doc = _fb2(_fb2_from_zip(data))
    elif kind == "docx":
        doc = _docx(data)
    elif kind == "html":
        doc = _html(decode(data))
    elif kind in ("txt", "md"):
        doc = _plain(decode(data), markdown=kind == "md")
    else:
        raise Unreadable("I can't read that kind of file yet (EPUB, PDF, FB2, DOCX, TXT, MD and HTML work)")
    doc.kind = doc.kind or kind
    doc.title = doc.title or stem or "Untitled"
    if len(doc.title) > TITLE_MAX:
        doc.meta["full_title"] = doc.title
        doc.title = short_title(doc.title)
    doc.sections = split_long_sections(drop_boilerplate([(t, _tidy(s)) for t, s in doc.sections]))
    if not doc.sections:
        raise Unreadable("I found no text in it")
    return finish(doc)


_PG_START = re.compile(r"^.*?\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG[^\n]*\n", re.S | re.I)
_PG_END = re.compile(r"\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG.*$", re.S | re.I)
_PG_LICENCE = re.compile(r"project gutenberg.*licen[cs]e|^\s*the full project gutenberg", re.I)


def drop_boilerplate(sections):
    """Empty sections, Project Gutenberg's licence chapter, and its START/END header and footer."""
    out = []
    for title, text in sections:
        if title and _PG_LICENCE.search(title):
            continue
        text = _PG_END.sub("", _PG_START.sub("", text)).strip()
        if text:
            out.append((title, text))
    return out


def split_long_sections(sections):
    """A section over SECTION_MAX_TOKENS becomes "<title> (part i)" sections of ~PART_TOKENS*2 tokens."""
    out = []
    for title, text in sections:
        if estimate_tokens(text) <= SECTION_MAX_TOKENS:
            out.append((_short_heading(title), text))
            continue
        parts = _parts(text, PART_TOKENS * 2)
        head = _short_heading(title)
        out += [(f"{head} (part {i})" if head else f"part {i}", body) for i, (_, body) in enumerate(parts, 1)]
    return out


def short_title(title):
    """The main part of a long title: up to the first " / ", ": ", "; " or " — ", else cut at a word."""
    for sep in (" / ", ": ", "; ", " — ", ". "):
        head = title.split(sep, 1)[0].strip()
        if 3 <= len(head) <= TITLE_MAX and head != title:
            return head
    return title[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def _short_heading(title):
    if not title or len(title) <= TITLE_MAX:
        return title
    cut = title[:TITLE_MAX]
    if " " in cut[TITLE_MAX // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:，。；：") + "…"


def finish(doc):
    """Language by majority over samples; traditional Chinese becomes simplified."""
    text = "\n".join(s for _, s in doc.sections)
    samples = [text[i:i + 2000] for i in range(0, len(text), max(2000, len(text) // 20))][:20]
    votes = {}
    for s in samples:
        if code := lang.detect(s):
            votes[code] = votes.get(code, 0) + 1
    doc.lang = max(votes, key=votes.get) if votes else None
    if doc.lang == "zh":
        sample = "".join(samples)
        han = [ch for ch in sample if "一" <= ch <= "鿿"]
        changed = sum(a != b for a, b in zip(sample, t2s(sample)) if "一" <= a <= "鿿")
        if han and changed / len(han) > TRADITIONAL_SHARE:
            doc.meta["traditional"] = True
            doc.title, doc.author = t2s(doc.title), t2s(doc.author)
            doc.sections = [(t and t2s(t), t2s(s)) for t, s in doc.sections]
    return doc


def t2s(text):
    return _opencc().convert(text)


_OPENCC = None


def _opencc():
    global _OPENCC
    if _OPENCC is None:
        from opencc import OpenCC
        _OPENCC = OpenCC("t2s")
    return _OPENCC


def decode(data):
    """Bytes of a text file as str: UTF-8 (with or without BOM), UTF-16 with BOM, else detected."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes
    best = from_bytes(data).best()
    if best is None:
        raise Unreadable("I couldn't tell how the text is encoded")
    return str(best)


# HTML ------------------------------------------------------------------------------------------------
_BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "section", "article", "blockquote", "pre", "dd", "dt",
          "figcaption", "hr", "table", "ul", "ol", "header", "footer", "aside", "body"}
_SKIP = {"rt", "rp", "script", "style", "head", "svg", "math", "nav"}
_HEADING = re.compile(r"h([1-6])$")


class _Blocks(HTMLParser):
    """Text of an HTML page as [(heading level or 0, text)], one entry per block."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buf, self.level, self.skip, self.title, self.in_title = [], [], 0, 0, "", False

    def flush(self):
        text = re.sub(r"[ \t\r\n ]+", " ", "".join(self.buf)).strip()
        if text:
            self.blocks.append((self.level, text))
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in _SKIP:
            self.skip += 1
        elif m := _HEADING.match(tag):
            self.flush()
            self.level = int(m.group(1))
        elif tag in _BLOCK:
            self.flush()

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self.flush()

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif _HEADING.match(tag):
            self.flush()
            self.level = 0
        elif tag in _BLOCK:
            self.flush()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self.flush()


def html_blocks(markup):
    p = _Blocks()
    p.feed(markup)
    p.close()
    return p.blocks, re.sub(r"\s+", " ", p.title).strip()


def _html(markup):
    blocks, title = html_blocks(markup)
    return Doc(title=_clean_title(title), sections=by_headings(blocks))


def by_headings(blocks, parent=None):
    """[(level, text)] → sections split at the top heading level used at least twice (else one section).
    Lower headings stay in the text as lines, unless a section is too long and has its own chapters: then
    it is split by them too ("Каштанка — Глава первая", P4.2). Text before the first heading is an
    untitled section."""
    levels = sorted({lv for lv, _ in blocks if lv})
    top = next((lv for lv in levels if sum(b[0] == lv for b in blocks) >= 2), None)
    if top is None:
        return _parts("\n".join(t for _, t in blocks)) if parent is None else [(parent, "\n".join(t for _, t in blocks))]
    groups, title, inner = [], parent, []
    for lv, text in blocks:
        if lv and lv <= top:
            if inner or title != parent:
                groups.append((title, inner))
            title, inner = (f"{parent} — {text}" if parent else text), []
        else:
            inner.append((lv, text))
    groups.append((title, inner))
    sections = []
    for title, inner in groups:
        text = "\n".join(t for _, t in inner)
        if estimate_tokens(text) > SECTION_MAX_TOKENS and sum(1 for lv, _ in inner if lv) >= 2:
            sections += by_headings(inner, parent=title)
        else:
            sections.append((title, text))
    return sections


# EPUB ------------------------------------------------------------------------------------------------
def _zip(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Unreadable("the file looks damaged (not a valid zip)") from None
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        raise Unreadable("it unpacks to more than 300 MB")
    return z


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    return [e for e in el.iter() if _local(e.tag) == name]


def _epub(data):
    z = _zip(data)
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in _find(container, "rootfile"))
        opf = ET.fromstring(z.read(opf_path))
    except (KeyError, StopIteration, ET.ParseError):
        raise Unreadable("the EPUB has no readable table of contents (container or OPF missing)") from None
    base = posixpath.dirname(opf_path)
    title = next((e.text for e in _find(opf, "title") if e.text), "")
    author = next((e.text for e in _find(opf, "creator") if e.text), "")
    manifest = {e.get("id"): e for e in _find(opf, "item")}
    spine_el = next(iter(_find(opf, "spine")), None)
    spine = [manifest[i.get("idref")] for i in _find(opf, "itemref") if i.get("idref") in manifest] \
        if spine_el is not None else []
    toc = {}
    nav = next((e for e in manifest.values() if "nav" in (e.get("properties") or "").split()), None)
    ncx = manifest.get(spine_el.get("toc")) if spine_el is not None and spine_el.get("toc") else \
        next((e for e in manifest.values() if e.get("media-type") == "application/x-dtbncx+xml"), None)
    try:
        if nav is not None:
            toc = _nav_toc(z, posixpath.join(base, unquote(nav.get("href"))))
        if not toc and ncx is not None:
            toc = _ncx_toc(z, posixpath.join(base, unquote(ncx.get("href"))))
    except (KeyError, ET.ParseError):
        toc = {}
    sections = []
    for item in spine:
        path = posixpath.normpath(posixpath.join(base, unquote(item.get("href") or "")))
        if "html" not in (item.get("media-type") or "") and not path.endswith(("html", "htm")):
            continue
        try:
            blocks, _ = html_blocks(z.read(path).decode("utf-8", "replace"))
        except KeyError:
            continue
        text = "\n".join(t for _, t in blocks)
        if toc:
            if path in toc or not sections:
                sections.append([toc.get(path), text])
            else:
                sections[-1][1] += "\n" + text
        else:
            heading = next((t for lv, t in blocks if lv), None)
            sections.append([heading, text])
    return Doc(title=_clean_title(title), author=author.strip(), sections=[tuple(s) for s in sections])


def _nav_toc(z, path):
    """{chapter file: title} from the EPUB3 nav's top-level toc entries."""
    root = ET.fromstring(z.read(path))
    navs = [n for n in _find(root, "nav") if "toc" in " ".join(v for k, v in n.attrib.items() if k.endswith("type"))]
    if not navs:
        return {}
    ol = next((e for e in navs[0] if _local(e.tag) == "ol"), None)
    toc = {}
    for li in (e for e in (ol if ol is not None else []) if _local(e.tag) == "li"):
        a = next(iter(_find(li, "a")), None)
        if a is not None and a.get("href"):
            _add_toc(toc, path, a.get("href"), "".join(a.itertext()))
    return toc


def _ncx_toc(z, path):
    root = ET.fromstring(z.read(path))
    nav_map = next(iter(_find(root, "navMap")), None)
    toc = {}
    for point in (e for e in (nav_map if nav_map is not None else []) if _local(e.tag) == "navPoint"):
        label = next(iter(_find(point, "text")), None)
        content = next(iter(_find(point, "content")), None)
        if content is not None and content.get("src"):
            _add_toc(toc, path, content.get("src"), "".join(label.itertext()) if label is not None else "")
    return toc


def _add_toc(toc, toc_path, href, title):
    target = posixpath.normpath(posixpath.join(posixpath.dirname(toc_path), unquote(href.split("#", 1)[0])))
    title = re.sub(r"\s+", " ", title).strip()
    if target not in toc and title:
        toc[target] = title


# PDF -------------------------------------------------------------------------------------------------
def _pdf(data):
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        raise Unreadable("the PDF looks damaged or is password-protected") from None
    try:
        meta = pdf.get_metadata_dict()
        pages = []
        for i in range(len(pdf)):
            page = pdf[i]
            textpage = page.get_textpage()
            pages.append(textpage.get_text_bounded())
            textpage.close()
            page.close()
        if sum(len(p.strip()) for p in pages) < PDF_MIN_CHARS_PER_PAGE * max(1, len(pages)):
            raise Unreadable("it's a scanned PDF (pictures of pages) and I can't do OCR")
        starts = []
        for item in pdf.get_toc():
            dest = item.get_dest()
            index = dest.get_index() if dest else None
            if item.level == 0 and index is not None and 0 <= index < len(pages):
                starts.append((index, re.sub(r"\s+", " ", item.get_title() or "").strip() or None))
    finally:
        pdf.close()
    starts = sorted({i: t for i, t in reversed(starts)}.items())
    if not starts:
        n = PDF_PAGES_PER_SECTION
        starts = [(i, f"pages {i + 1}–{min(i + n, len(pages))}") for i in range(0, len(pages), n)]
    elif starts[0][0] > 0:
        starts.insert(0, (0, None))
    sections = []
    for k, (first, title) in enumerate(starts):
        last = starts[k + 1][0] if k + 1 < len(starts) else len(pages)
        sections.append((title, "\n\n".join(_unwrap(p) for p in pages[first:last])))
    return Doc(title=_clean_title(meta.get("Title", "")), author=(meta.get("Author") or "").strip(),
               sections=sections)


def _unwrap(page):
    """Join a PDF page's wrapped lines into paragraphs; "exam-\\nple" becomes "example"."""
    # pdfium already joins a word broken at a line end and leaves \x02 where the hyphen was.
    page = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", page.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [ln.strip() for ln in page.split("\n")]
    width = sorted(len(ln) for ln in lines if ln)[len([ln for ln in lines if ln]) // 2] if any(lines) else 0
    out = ""
    for ln in lines:
        if not ln:
            out = out.rstrip(" ") + "\n\n" if out and not out.endswith("\n\n") else out
            continue
        if out.endswith("-") and len(out) > 1 and out[-2].isalpha() and ln[:1].islower():
            out = out[:-1] + ln
        elif not out or out.endswith("\n"):
            out += ln
        else:
            out += ("" if _cjk(out[-1]) and _cjk(ln[0]) else " ") + ln
        if re.search(r"[.!?:;»”\"。！？]$", ln) and len(ln) < 0.8 * width:
            out += "\n"
    return out.strip()


def _cjk(ch):
    return "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"


# FB2 -------------------------------------------------------------------------------------------------
def _fb2_from_zip(data):
    z = _zip(data)
    names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
    if not names:
        raise Unreadable("the zip has no .fb2 book inside (send other formats unzipped)")
    return z.read(names[0])


def _fb2(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise Unreadable("the FB2 file looks damaged") from None
    info = next(iter(_find(root, "title-info")), None)
    title, author = "", ""
    if info is not None:
        title = " ".join("".join(e.itertext()) for e in _find(info, "book-title"))
        a = next(iter(_find(info, "author")), None)
        if a is not None:
            author = " ".join(("".join(e.itertext())).strip() for e in a
                              if _local(e.tag) in ("first-name", "middle-name", "last-name"))
    bodies = [e for e in root if _local(e.tag) == "body"]
    body = next((b for b in bodies if b.get("name") not in ("notes", "comments")), bodies[0] if bodies else None)
    if body is None:
        return Doc(title=_clean_title(title), author=author)
    sections, loose = [], []
    for el in body:
        tag = _local(el.tag)
        if tag == "section":
            head = next((e for e in el if _local(e.tag) == "title"), None)
            heading = " ".join(_fb2_lines(head)) if head is not None else None
            lines = [ln for e in el if e is not head for ln in _fb2_lines(e)]
            sections.append((heading, "\n".join(lines)))
        elif tag not in ("title", "image", "epigraph"):
            loose += _fb2_lines(el)
    if loose:
        sections.insert(0, (None, "\n".join(loose)))
    if len(sections) == 1 and sections[0][0] is None:
        sections = _parts(sections[0][1])
    return Doc(title=_clean_title(title), author=author.strip(), sections=sections)


def _fb2_lines(el):
    """Paragraph lines of an FB2 element (nested sections and their titles included)."""
    tag = _local(el.tag)
    if tag in ("p", "v", "subtitle", "text-author"):
        text = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
        return [text] if text else []
    if tag in ("binary", "image"):
        return []
    return [ln for child in el for ln in _fb2_lines(child)]


# DOCX ------------------------------------------------------------------------------------------------
def _docx(data):
    import docx
    try:
        d = docx.Document(io.BytesIO(data))
    except Exception:
        raise Unreadable("the Word file looks damaged (or it's an old .doc: save it as .docx)") from None
    blocks = []
    for p in d.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name if p.style is not None else "") or ""
        m = re.match(r"(?:Heading|Заголовок|标题)\s*(\d)", style)
        blocks.append((int(m.group(1)) if m else 1 if style == "Title" else 0, text))
    props = d.core_properties
    return Doc(title=_clean_title(props.title or ""), author=(props.author or "").strip(),
               sections=by_headings([b for b in blocks if not (b[0] == 1 and b[1] == props.title)]))


# TXT / MD --------------------------------------------------------------------------------------------
_ROMAN = r"(?=[MDCLXVI])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
_NUMBER_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                 "sixteen|seventeen|eighteen|nineteen|twenty|first|second|third|fourth|fifth|sixth|seventh|"
                 "eighth|ninth|tenth|last|первая|вторая|третья|четвертая|четвёртая|пятая|шестая|седьмая|"
                 "восьмая|девятая|десятая|последняя")
_CHAPTER = re.compile(
    r"^\s*(?:(?:chapter|part|book|capítulo|глава|часть|книга)\s+(?:\d+|(?-i:" + _ROMAN + r")|" + _NUMBER_WORDS +
    r")\b.{0,60}|第[0-9一二三四五六七八九十百千零〇两]+[章回节卷部篇].{0,30}|(?-i:" + _ROMAN + r")\.?)\s*$", re.I)
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _plain(text, markdown=False):
    blocks, title = [], ""
    for para in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        para = para.strip()
        if not para:
            continue
        first, _, rest = para.partition("\n")
        if markdown and (m := _MD_HEADING.match(first)):
            blocks.append((len(m.group(1)), m.group(2)))
        elif _CHAPTER.match(first) and len(first) <= 80:
            blocks.append((1, first.strip()))
        else:
            blocks.append((0, re.sub(r"[ \t]*\n[ \t]*", "\n", para)))
            continue
        if rest.strip():
            blocks.append((0, rest.strip()))
    if markdown:  # a single "# Title" at the top is the document's title, not a chapter
        h1 = [b for b in blocks if b[0] == 1]
        if len(h1) == 1 and blocks and blocks[0][0] == 1:
            title, blocks = h1[0][1], blocks[1:]
    return Doc(title=_clean_title(title), sections=by_headings(blocks))


def _parts(text, size_tokens=PART_TOKENS):
    """Text without headings: parts of about `size_tokens` tokens, cut between paragraphs."""
    paras = []
    for p in (p for p in re.split(r"\n+", text) if p.strip()):  # a giant paragraph is cut at sentence ends
        paras += re.findall(r".*?(?:[.!?…。！？]+[\"'»”’)）」』]*\s*|$)", p, re.S)[:-1] \
            if estimate_tokens(p) > size_tokens else [p]
    paras = [p.strip() for p in paras if p.strip()]
    if estimate_tokens(text) <= size_tokens * 1.5:
        return [(None, "\n".join(paras))]
    parts, cur, size = [], [], 0
    for p in paras:
        t = estimate_tokens(p)
        if cur and size + t > size_tokens:
            parts.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += t
    if cur:
        parts.append(cur)
    return [(f"part {i}", "\n".join(ps)) for i, ps in enumerate(parts, 1)]


# helpers ---------------------------------------------------------------------------------------------
def estimate_tokens(text):
    """Model tokens without a tokenizer: 1 per CJK character, Cyrillic letters / 2.8, the rest / 3.8."""
    cjk, cyr, _ = lang.counts(text)
    return int(cjk + cyr / 2.8 + (len(text) - cjk - cyr) / 3.8) + 1


def _tidy(text):
    text = re.sub(r"[ \t ]+", " ", text or "")
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_title(title):
    title = re.sub(r"\s+", " ", title or "").strip()
    return "" if title.lower() in ("untitled", "unknown", "microsoft word") else title
