#!/usr/bin/env python3
"""Turn data/bench/*.json (from bench.py) into:
  - vault/Laika/Model test.md  (answers side by side, for Dasha to judge)
  - a Markdown speed table on stdout (paste into docs/BENCHMARKS.md)
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TITLES = {"small_talk": "Small talk", "philosophy": "Philosophy", "persona_check": "Persona check",
          "learner": "Learner sentence (chat reply)", "tutor": "Learner sentence (tutor JSON)"}
LANGS = {"en": "English", "ru": "Russian", "zh": "Chinese"}


def main():
    results = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "data" / "bench").glob("*.json"))]
    print("| Candidate | Model | -ngl | ctx | extra | gen tok/s | prompt tok/s | VRAM llm MB | RAM avail after MB | load s |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['name']} | {r['model']} | {r['ngl']} | {r['ctx']} | {r['extra'] or '-'} | {r.get('gen_tps', '-')} "
              f"| {r.get('prompt_tps', '-')} | {r.get('vram_llm_mb', '-')} | {r.get('ram_available_mb_after', '-')} "
              f"| {r.get('load_seconds', '-')} |")

    lines = ["---", "type: model-test", f"updated: {results[-1]['date'] if results else ''}", "---", "",
             "# Model test", "",
             "Same prompts for every model. Read the answers and tell Claude which one feels best as a",
             "friend and language partner, and which one corrects you best.", "",
             "| Candidate | Model | Speed (tokens/s) |", "|---|---|---|"]
    lines += [f"| {r['name']} | {r['model']} | {r.get('gen_tps', '-')} |" for r in results]
    keys = []
    for r in results:
        for k in r.get("answers", {}):
            if k not in keys:
                keys.append(k)
    for key in keys:
        kind, lang = key.split("/")
        lines += ["", f"## {TITLES.get(kind, kind)} · {LANGS.get(lang, lang)}", ""]
        for r in results:
            a = r.get("answers", {}).get(key)
            if not a:
                continue
            lines += [f"### {r['name']} · {r['model']} ({a['gen_tps']} tok/s, {a['seconds']} s)", "", a["text"].strip(), ""]
    out = ROOT / "vault" / "Laika" / "Model test.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
