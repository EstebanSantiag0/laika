"""Style probe (W2): how AI-ish are Laika's replies? Same prompts, fresh history, background slot.

Configs:
  old      the persona/rules before W2 (pass the old persona.py with --old), chat completions
  new      current persona, raw /completion, slop guard off (what the prompt alone achieves)
  guarded  current persona, raw /completion, slop guard on (what Dasha gets in English)

Run (current code mounted; the old persona from git as a top-level module; nothing is mounted *into*
app/, because single-file mounts leave empty placeholder files behind on the host):
  mkdir -p /tmp/probe && git show 58554f1:app/companion/persona.py \
    | sed 's/from \. import lang/from laika import lang/' > /tmp/probe/persona_old.py
  docker run --rm --network laika_default -v "$PWD/app:/app" -v "$PWD/scripts:/scripts:ro" \
    -v "$PWD/vault:/vault:ro" -v "$PWD/config.yaml:/config.yaml:ro" -v /tmp/probe:/out \
    -e LAIKA_CONFIG=/config.yaml -e PYTHONPATH=/out -w /app \
    local/laika-app:0.1 python /scripts/style_probe.py --samples 2 --out /out/probe.md
"""
import argparse
import asyncio
import importlib
import json
import pathlib
import re
import sys
import tempfile
import time

sys.path.insert(0, "/app")
from laika import config, lang, render, slop  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.llm import LLM  # noqa: E402

PROMPTS = [
    ("en", "I had a strange day today, can I tell you about it?"),
    ("en", "Do you think we can be free if all our choices have causes?"),
    ("en", "I'm working on a memory system for you, so you can remember our conversations"),
    ("en", "I passed my Russian exam today!!"),
    ("en", "my boss yelled at me in front of everyone today"),
    ("en", "Your voice is beautiful, you know?"),
    ("en", "Can you help me with something?"),
    ("en", "I think passion is the missing ingredient for competence"),
    ("en", "I taught myself harmonica after my flute teacher used me as a stepping stone"),
    ("en", "what should we talk about?"),
    ("ru", "Привет! Как дела? Я сегодня много работал"),
    ("zh", "你好！我今天很累"),
]
SPLIT = re.compile(r"\n\s*\n")


def bubbles(text, max_bubbles=4):
    parts = [p.strip() for p in SPLIT.split(text) if p.strip()]
    return parts[:max_bubbles - 1] + ["\n\n".join(parts[max_bubbles - 1:])] if len(parts) > max_bubbles else parts


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?…])\s+|\n+", text) if s.strip()]


def score(text, code):
    bs = bubbles(text)
    hits = [h[1] for s in sentences(text) if (h := slop.find(s))] if code == "en" else []
    return {
        "bubbles": len(bs),
        "chars_per_bubble": sum(len(b) for b in bs) / max(1, len(bs)),
        "ends_with_question": bool(bs) and bs[-1].rstrip().endswith(("?", "？")),
        "questions": text.count("?") + text.count("？"),
        "slop": hits,
        "markdown": bool(re.search(r"\*\*|(?<!\w)\*\w|\w\*(?!\w)", text)),
    }


def make_persona(module, code, vault):
    db = DB(":memory:")
    db.set_setting("practice_lang", code)
    for c, lv in (("en", "C2"), ("ru", "A1"), ("zh", "A1 (HSK 1)")):
        db.set_setting(f"level_{c}", lv)
    return module.Persona(config.load(), db, vault), db


async def generate(llm, cfg_name, module, code, text, vault):
    persona, db = make_persona(module, code, vault)
    msg = db.add_message("user", "web", text, lang=lang.detect(text), status="received")
    messages = persona.build_messages([msg])
    if cfg_name == "old":
        return await llm.chat(messages, 1, max_tokens=llm.max_tokens)
    guard = cfg_name == "guarded" and code == "en"
    grounding = persona.grounding(messages) if guard else None
    return "".join([d async for d in llm.stream_chat(messages, 1, guard=guard, grounding=grounding)])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=2)
    ap.add_argument("--configs", default="old,new,guarded")
    ap.add_argument("--out", default="/tmp/probe.md")
    args = ap.parse_args()
    cfg = config.load()
    cfg["llm"]["url"] = "http://llm:8080"
    llm = LLM(cfg)
    vault = pathlib.Path("/vault")
    rewinds = {"n": 0}
    original_rewind = llm._rewind

    async def counting_rewind(*a):
        rewinds["n"] += 1
        return await original_rewind(*a)
    llm._rewind = counting_rewind
    report, summary = [], {}
    for cfg_name in args.configs.split(","):
        module = importlib.import_module("persona_old" if cfg_name == "old" else "laika.persona")
        rows, rewinds["n"], t0 = [], 0, time.time()
        for code, text in PROMPTS:
            for _ in range(args.samples):
                reply = await generate(llm, cfg_name, module, code, text, vault)
                rows.append((code, text, reply, score(reply, code)))
        en = [r for r in rows if r[0] == "en"]
        summary[cfg_name] = {
            "replies": len(rows),
            "en_slop_hits": sum(len(r[3]["slop"]) for r in en),
            "en_replies_with_slop": sum(bool(r[3]["slop"]) for r in en),
            "slop_kinds": sorted({k for r in en for k in r[3]["slop"]}),
            "ends_with_question_%": round(100 * sum(r[3]["ends_with_question"] for r in rows) / len(rows)),
            "avg_bubbles": round(sum(r[3]["bubbles"] for r in rows) / len(rows), 2),
            "avg_chars_per_bubble": round(sum(r[3]["chars_per_bubble"] for r in rows) / len(rows)),
            "markdown_replies": sum(r[3]["markdown"] for r in rows),
            "slop_rewinds": rewinds["n"],
            "seconds": round(time.time() - t0),
        }
        report.append(f"## {cfg_name}\n")
        for code, text, reply, sc in rows:
            report.append(f"**[{code}] {text}**\n\n{reply}\n\n`{json.dumps(sc, ensure_ascii=False)}`\n\n---\n")
        print(cfg_name, json.dumps(summary[cfg_name], ensure_ascii=False), flush=True)
    pathlib.Path(args.out).write_text("# Style probe\n\n```json\n" + json.dumps(summary, indent=1, ensure_ascii=False)
                                      + "\n```\n\n" + "\n".join(report), encoding="utf-8")
    await llm.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
