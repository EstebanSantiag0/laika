"""English replies that sound like an AI (slop.yaml) or recall things the user never said (DECISIONS D16)."""
import pathlib
import re

import yaml

PATTERNS = {name: re.compile(rx, re.I) for name, rx in
            yaml.safe_load((pathlib.Path(__file__).with_name("slop.yaml")).read_text(encoding="utf-8"))["patterns"].items()}


def find(sentence):
    """(start, name) of the earliest match in one sentence, or None.

    `start` skips leading spaces, blank lines and dashes, so they are kept when the reply is rewound
    to that point; it stays on an opening quotation mark (that is the token to avoid).
    """
    best = None
    for name, rx in PATTERNS.items():
        m = rx.search(sentence)
        if m:
            at = m.start("cut") if "cut" in rx.groupindex else m.start()
            if best is None or at < best[0]:
                best = (at, name)
    if best is None:
        return None
    start = best[0]
    while start < len(sentence) and (sentence[start].isspace() or sentence[start] in "-–—*_(¿¡"):
        start += 1
    return start, best[1]


# "that book you mentioned last time": a callback needs a trace of the thing in what he actually wrote
# (or in her memory notes); otherwise it is an invented memory, worse than forgetting (RESEARCH Part 1, #18).
CALLBACK = re.compile(r"\b(you (mentioned|told me|said|brought up|were \w+ing)|last time|the other day|remember when"
                      r"|you'?ve been \w+ing|i know how much you|that \w+ you (were|mentioned|told|said))\b", re.I)
STOPWORDS = set("""the and but that this those these with from just really very what which when where there then than
were been have has had did does your yours about into over after before also ever even still some something thing
things time last mentioned told said saying telling remember remembered thinking thought know working studying
practicing talking brought actually wonder wondering since lately other day week weeks month going doing reading
watching learning playing writing listening trying planning""".split())


def ungrounded_callback(sentence, grounding):
    """(start, name) when the sentence recalls something with no trace in `grounding`, else None."""
    if not CALLBACK.search(sentence):
        return None
    words = {w for w in re.findall(r"[a-z]{4,}", sentence.lower()) if w not in STOPWORDS}
    text = grounding.lower()
    if any(w[:5] in text for w in words):
        return None
    start = len(sentence) - len(sentence.lstrip())
    return start, "ungrounded_callback"
