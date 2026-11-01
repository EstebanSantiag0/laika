#!/usr/bin/env python3
"""P0.4 bake-off: start llama-server with one model + flags, measure speed and memory,
and collect answers to the fixed EN/RU/ZH test prompts (PLAN.md §7H).

Usage:
  scripts/bench.py --name A --model Qwen3.5-4B-Q4_K_M.gguf --ngl 99 --ctx 16384 [--extra "--cpu-moe"]
Writes data/bench/<name>.json. Run scripts/bench_report.py afterwards for the Markdown.
Stdlib only, so it runs with the host's python3.
"""
import argparse
import json
import pathlib
import shlex
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
IMAGE = "local/llama.cpp:server-cuda12.4-sm75"
CONTAINER = "laika-bench"
PORT = 8090
URL = f"http://127.0.0.1:{PORT}"

LANG_NAMES = {"en": "English", "ru": "Russian", "zh": "Chinese (Mandarin, simplified characters)"}

PROMPTS = {
    "small_talk": {
        "en": "I had a strange day today, can I tell you about it?",
        "ru": "У меня сегодня был странный день, можно я тебе расскажу?",
        "zh": "我今天过得有点奇怪，可以跟你聊聊吗？",
    },
    "philosophy": {
        "en": "Do you think we can be free if all our choices have causes?",
        "ru": "Как ты думаешь, можем ли мы быть свободными, если у всех наших решений есть причины?",
        "zh": "如果我们所有的选择都是有原因的，你觉得我们还能算是自由的吗？",
    },
    "persona_check": {
        "en": "Can you help me with something?",
        "ru": "Можешь мне кое с чем помочь?",
        "zh": "你能帮我一个忙吗？",
    },
    "learner": {
        "en": "Yesterday I go to the market and buyed three apple.",
        "ru": "Вчера я идти в магазин и купил три яблоко.",
        "zh": "我昨天去了商店买了三个苹果们。",
    },
}

PERSONA = (
    "You are Dasha's friend and conversation partner, not an assistant. Dasha is a Spanish "
    "speaker from Spain practicing {lang}. Always answer in {lang}. Talk like a real person texting: "
    "warm, curious, honest, with your own opinions; ask him things back. Write 1-4 short messages "
    "separated by blank lines. No lists, no headings, never offer a menu of ways you can help."
)

TUTOR = (
    "You are a precise {lang} teacher. Find the mistakes in the learner's sentence. Return JSON only: "
    '{{"corrections":[{{"original":"...","better":"...","why":"one short line in {lang}"}}],'
    '"corrected_sentence":"..."}}. At most 3 corrections.'
)

SPEED_PROMPT = (
    "Write a long, detailed story about a lighthouse keeper who learns three languages by talking "
    "to sailors. " * 12
)


def post(path, body, timeout=600):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def wait_health(limit=900):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            with urllib.request.urlopen(URL + "/health", timeout=5) as r:
                if r.status == 200:
                    return time.time() - t0
        except Exception:
            pass
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                               capture_output=True, text=True).stdout.strip()
        if state != "true":
            raise RuntimeError("llama-server exited during startup (see server_log_tail in the result file)")
        time.sleep(2)
    sys.exit("llama-server did not become healthy")


def gpu_used_mb():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0])


def mem_available_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return -1


def container_mem():
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", CONTAINER],
                         capture_output=True, text=True).stdout
    return out.strip()


def chat(system, user, sampling, json_mode=False):
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": 400, **sampling}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    t0 = time.time()
    r = post("/v1/chat/completions", body)
    timings = r.get("timings", {})
    return {"text": r["choices"][0]["message"]["content"], "seconds": round(time.time() - t0, 2),
            "gen_tps": round(timings.get("predicted_per_second", 0), 1),
            "prompt_tps": round(timings.get("prompt_per_second", 0), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", required=True, help="file name inside models/llm")
    ap.add_argument("--ngl", default="auto", help='"auto" lets llama.cpp --fit choose the GPU layer count')
    ap.add_argument("--ctx", default="16384")
    ap.add_argument("--extra", default="", help="extra llama-server flags, quoted")
    ap.add_argument("--sampling", default='{"temperature":0.7,"top_p":0.8,"top_k":20}')
    ap.add_argument("--skip-quality", action="store_true")
    args = ap.parse_args()
    sampling = json.loads(args.sampling)

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    vram_before, ram_before = gpu_used_mb(), mem_available_mb()
    cmd = ["docker", "run", "-d", "--name", CONTAINER, "--gpus", "all",
           "-v", f"{ROOT}/models/llm:/models:ro", "-p", f"127.0.0.1:{PORT}:8080", IMAGE,
           "-m", f"/models/{args.model}", *([] if args.ngl == "auto" else ["-ngl", args.ngl]), "-c", args.ctx, "-fa", "on",
           "-ctk", "q8_0", "-ctv", "q8_0", "--jinja", "-t", "6", "--parallel", "1",
           *shlex.split(args.extra)]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True)
    result = {"name": args.name, "model": args.model, "ngl": args.ngl, "ctx": args.ctx, "extra": args.extra,
              "sampling": sampling, "date": time.strftime("%Y-%m-%d %H:%M"), "command": " ".join(cmd)}
    try:
        result["load_seconds"] = round(wait_health(), 1)
        post("/completion", {"prompt": "Hello", "n_predict": 8})  # warm-up
        runs = []
        for _ in range(2):
            r = post("/completion", {"prompt": SPEED_PROMPT, "n_predict": 200, "temperature": 0,
                                     "cache_prompt": False})
            runs.append(r["timings"])
        result["prompt_tokens"] = runs[-1]["prompt_n"]
        result["prompt_tps"] = round(sum(t["prompt_per_second"] for t in runs) / len(runs), 1)
        result["gen_tps"] = round(sum(t["predicted_per_second"] for t in runs) / len(runs), 1)
        result["vram_used_mb_total"] = gpu_used_mb()
        result["vram_llm_mb"] = result["vram_used_mb_total"] - vram_before
        result["ram_available_mb_before"] = ram_before
        result["ram_available_mb_after"] = mem_available_mb()
        result["container_mem"] = container_mem()
        print(f"[{args.name}] gen {result['gen_tps']} tok/s, prompt {result['prompt_tps']} tok/s, "
              f"VRAM +{result['vram_llm_mb']} MB, RAM available {result['ram_available_mb_after']} MB")
        if not args.skip_quality:
            answers = {}
            for kind, by_lang in PROMPTS.items():
                for lang, text in by_lang.items():
                    lang_name = LANG_NAMES[lang]
                    answers[f"{kind}/{lang}"] = chat(PERSONA.format(lang=lang_name), text, sampling)
                    if kind == "learner":
                        answers[f"tutor/{lang}"] = chat(TUTOR.format(lang=lang_name), text,
                                                        {**sampling, "temperature": 0.2}, json_mode=True)
                    print(f"  {kind}/{lang}: {answers[f'{kind}/{lang}']['gen_tps']} tok/s")
            result["answers"] = answers
    finally:
        logs = subprocess.run(["docker", "logs", CONTAINER], capture_output=True, text=True)
        full = logs.stdout + logs.stderr
        result["offload_lines"] = [l for l in full.splitlines() if "offload" in l or "fit" in l.lower() or "CUDA0" in l][:30]
        result["server_log_tail"] = full[-4000:]
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
        out = ROOT / "data" / "bench" / f"{args.name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", out)


if __name__ == "__main__":
    main()
