"""P3.1 before/after numbers: RAM, swap, container memory, and llama-server speed on a fixed prompt.

Run on the host: python3 scripts/ram_speed.py   (uses slot 1, temperature 0; ~1 minute)
"""
import json
import random
import subprocess
import urllib.request

URL = "http://127.0.0.1:8081/completion"


def post(body):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=900))


def meminfo():
    info = dict(line.split(":", 1) for line in open("/proc/meminfo"))
    mb = lambda k: int(info[k].split()[0]) // 1024  # noqa: E731
    return {"available_mb": mb("MemAvailable"), "swap_used_mb": mb("SwapTotal") - mb("SwapFree"),
            "anon_mb": mb("AnonPages"), "file_cache_mb": mb("Cached")}


def containers():
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],
                         capture_output=True, text=True).stdout
    return {line.split()[0]: line.split()[1] for line in out.splitlines() if "laika-" in line or "immich_server" in line}


filler = " ".join(f"Line {i}: the quick brown fox jumps over the lazy dog near the river bank." for i in range(120))
nonce = random.randrange(10**9)  # a fresh prompt every run, so no cache (slot or RAM) can serve it
prompt = (f"<|turn>system\nYou are a friendly assistant (run {nonce}).<turn|>\n<|turn>user\n{filler}\n\nTell me a "
          f"short story about a cat in four sentences.<turn|>\n<|turn>model\n<|channel>thought\n<channel|>")
t = post({"prompt": prompt, "n_predict": 120, "id_slot": 1, "cache_prompt": True, "temperature": 0})["timings"]
print(json.dumps({"memory": meminfo(), "containers": containers(),
                  "prompt_tok": t["prompt_n"], "prompt_tok_s": round(t["prompt_per_second"]),
                  "gen_tok": t["predicted_n"], "gen_tok_s": round(t["predicted_per_second"], 1)}, indent=1))
