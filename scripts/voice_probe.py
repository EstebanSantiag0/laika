"""Voice probe (W6): when Dasha asks to hear a phrase, is the phrase itself what gets spoken, in its language?

Same setup as style_probe.py (fresh history, background slot 1, current or old persona). For each request
the reply is split into bubbles and checked the way the app would send it:
  spoken     the whole reply when VOICE_REQUEST matches, else only her 🎤 bubble
  lang       lang.bubble_lang per bubble (what the TTS is told)
  alone      a spoken RU/ZH bubble whose speech text has no Latin letters left (the phrase alone, glosses stripped)
  exact      her first 🎤 message says exactly the phrase he asked for (punctuation aside)

Run like style_probe.py, e.g. after `git show HEAD:app/laika/persona.py | sed ... > /tmp/probe/persona_old.py`:
  docker run --rm --network laika_default -v "$PWD/app:/app" -v "$PWD/scripts:/scripts:ro" \
    -v "$PWD/vault:/vault:ro" -v "$PWD/config.yaml:/config.yaml:ro" -v /tmp/probe:/out \
    -e LAIKA_CONFIG=/config.yaml -e PYTHONPATH=/out -w /app \
    local/laika-app:0.1 python /scripts/voice_probe.py --samples 3 --configs old,new --out /out/voice.md
"""
import argparse
import asyncio
import importlib
import json
import pathlib
import re
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/scripts")
from laika import config, lang  # noqa: E402
from laika.conversation import MIC, VOICE_NOTE  # noqa: E402
from laika.llm import LLM  # noqa: E402
from laika.voice import VOICE_REQUEST, speech_text  # noqa: E402
from style_probe import bubbles, make_persona  # noqa: E402

PROMPTS = [  # (practice language, his message, what her 🎤 message should say); the first is msg 178 verbatim
    ("zh", "我很好,你呢？（你今天怎么样 new vocabulary for me, could you say that using you voice?）", "你今天怎么样"),
    ("zh", "how do you say 'thank you very much'? say it with your voice", "非常感谢|太谢谢你了|谢谢你"),
    ("zh", "我想听 '我喜欢学习中文'，用你的声音说", "我喜欢学习中文"),
    ("ru", "как сказать 'good night' по-русски? скажи это своим голосом", "спокойной ночи"),
    ("ru", "how do you say 'I'm hungry' in Russian? send me a voice note", "я (голоден|голодна|голодный|голодная|хочу есть)"),
    ("en", "how do you pronounce 'thoroughly'? say it out loud", "thoroughly"),
]


async def generate(llm, module, code, text, vault):
    persona, db = make_persona(module, code, vault)
    msg = db.add_message("user", "web", text, lang=lang.detect(text), status="received")
    asked = bool(VOICE_REQUEST.search(text))
    messages = persona.build_messages([msg], [VOICE_NOTE] if asked else [])
    guard = code == "en"
    grounding = persona.grounding(messages) if guard else None
    reply = "".join([d async for d in llm.stream_chat(messages, 1, guard=guard, grounding=grounding)])
    return reply, asked


def score(reply, code, asked, expect):
    rows, mic_used = [], False
    for b in bubbles(reply):
        mic = b.startswith(MIC)
        text = b.lstrip(MIC + "️").strip()
        spoken = asked or (mic and not mic_used)
        mic_used |= mic
        bl = lang.bubble_lang(text, code)
        rows.append({"text": text, "mic": mic, "spoken": spoken, "lang": bl, "speech": speech_text(text, bl)})
    target = [r for r in rows if r["spoken"] and r["lang"] == code]
    mic = next((r for r in rows if r["mic"]), None)
    said = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", mic["speech"])).strip().lower() if mic else ""
    return {
        "exact": bool(re.fullmatch(expect, said)),
        "asked": asked,
        "mic_bubbles": sum(r["mic"] for r in rows),
        "spoken_in_target_lang": len(target),
        "alone": code != "en" and any(not re.search("[A-Za-z]", r["speech"]) for r in target),
        "bubbles": rows,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--configs", default="old,new")
    ap.add_argument("--out", default="/tmp/voice.md")
    args = ap.parse_args()
    cfg = config.load()
    llm = LLM(cfg)
    vault = pathlib.Path("/vault")
    report, summary = [], {}
    for cfg_name in args.configs.split(","):
        module = importlib.import_module("persona_old" if cfg_name == "old" else "laika.persona")
        rows = []
        for code, text, expect in PROMPTS:
            for _ in range(args.samples):
                reply, asked = await generate(llm, module, code, text, vault)
                rows.append((code, text, reply, score(reply, code, asked, expect)))
        ruzh = [r for r in rows if r[0] != "en"]
        summary[cfg_name] = {
            "replies": len(rows),
            "asked_detected": sum(r[3]["asked"] for r in rows),
            "replies_with_mic": sum(r[3]["mic_bubbles"] > 0 for r in rows),
            "mic_is_the_phrase": f"{sum(r[3]['exact'] for r in rows)}/{len(rows)}",
            "ru_zh_phrase_alone": f"{sum(r[3]['alone'] for r in ruzh)}/{len(ruzh)}",
            "ru_zh_spoken_in_target_lang": f"{sum(r[3]['spoken_in_target_lang'] > 0 for r in ruzh)}/{len(ruzh)}",
        }
        report.append(f"## {cfg_name}\n")
        for code, text, reply, sc in rows:
            lines = "\n".join(f"- {'🎤 ' if b['mic'] else ''}[{b['lang']}{', spoken' if b['spoken'] else ''}] "
                              f"{b['text']}  → says: {b['speech']}" for b in sc["bubbles"])
            report.append(f"**[{code}] {text}** (asked: {sc['asked']}, exact: {sc['exact']}, alone: {sc['alone']})"
                          f"\n\n{lines}\n\n---\n")
        print(cfg_name, json.dumps(summary[cfg_name], ensure_ascii=False), flush=True)
    pathlib.Path(args.out).write_text("# Voice probe\n\n```json\n" + json.dumps(summary, indent=1, ensure_ascii=False)
                                      + "\n```\n\n" + "\n".join(report), encoding="utf-8")
    await llm.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
