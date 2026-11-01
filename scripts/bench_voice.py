#!/usr/bin/env python3
"""P0.2 voice benchmark against the running voice service (127.0.0.1:8082).

Speaks one sentence per language (EN/RU/ZH), records speed (real-time factor) and VRAM, saves
the audio under data/bench/voice/ for listening, then transcribes each clip back with Whisper.
Stdlib only. Usage: scripts/bench_voice.py [--voice NAME] [--stt-model-note TEXT]
"""
import argparse
import json
import pathlib
import time
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8082"
SENTENCES = {
    "en": "Hey, I was just thinking about what you said yesterday. Do you really believe we can choose who we are?",
    "ru": "Привет! Я как раз думал о том, что ты сказал вчера. Ты правда веришь, что мы можем выбирать, кем быть?",
    "zh": "嘿，我刚才在想你昨天说的话。你真的相信我们可以选择成为什么样的人吗？",
}


def get(path):
    with urllib.request.urlopen(URL + path, timeout=30) as r:
        return json.load(r)


def speak(text, lang, voice):
    body = json.dumps({"input": text, "language": lang, "voice": voice, "response_format": "wav"}).encode()
    req = urllib.request.Request(URL + "/v1/audio/speech", data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        data = r.read()
        return data, float(r.headers["X-Audio-Seconds"]), float(r.headers["X-RTF"]), time.time() - t0


def transcribe(path, lang):
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\n{lang}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode() + path.read_bytes() + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(URL + "/v1/audio/transcriptions", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="")
    args = ap.parse_args()
    out = ROOT / "data" / "bench" / "voice"
    out.mkdir(parents=True, exist_ok=True)
    results = {"date": time.strftime("%Y-%m-%d %H:%M"), "health_before": get("/health"), "tts": {}, "stt": {}}
    for lang, text in SENTENCES.items():
        speak(text[:20], lang, args.voice)  # warm-up per language
        data, seconds, rtf, wall = speak(text, lang, args.voice)
        path = out / f"tts-{lang}.wav"
        path.write_bytes(data)
        results["tts"][lang] = {"audio_s": seconds, "rtf": rtf, "wall_s": round(wall, 2)}
        print(f"TTS {lang}: {seconds:.1f}s audio, RTF {rtf:.2f}, wall {wall:.1f}s -> {path}")
    for lang in SENTENCES:
        r = transcribe(out / f"tts-{lang}.wav", lang)
        results["stt"][lang] = r
        print(f"STT {lang}: {r['elapsed']}s for {r['duration']}s audio (logprob {r['avg_logprob']}): {r['text']}")
    results["health_after"] = get("/health")
    print("VRAM reserved/peak MB:", results["health_after"]["vram_reserved_mb"], results["health_after"]["vram_peak_mb"])
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
