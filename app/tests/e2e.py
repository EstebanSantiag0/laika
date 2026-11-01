"""End-to-end check against the running stack (real model, voice service).

Run inside the app container:  docker compose exec app python -m tests.e2e [--voice /data/bench/voice/tts-en.wav]
Needs ALLOWED_TS_LOGIN empty, or the request comes from localhost with the header set below.
"""
import argparse
import asyncio
import json
import os
import time

import httpx
import websockets

BASE = "http://localhost:8000"
HEADERS = {"Tailscale-User-Login": os.environ.get("ALLOWED_TS_LOGIN", "")}


async def collect(ws, until, timeout=180):
    """Collect events until `until(events)` is true or timeout."""
    events, t0 = [], time.time()
    while time.time() - t0 < timeout:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
        except asyncio.TimeoutError:
            if until(events):
                break
            continue
        events.append(json.loads(raw))
        if until(events):
            break
    return events


def of(events, role):
    return [e["message"] for e in events if e["type"] == "message" and e["message"]["role"] == role]


async def main(voice_file):
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=600) as http:
        await http.put("/api/settings", json={"practice_lang": "en", "corrections": "on", "voice_mode": "mirror"})
        async with websockets.connect("ws://localhost:8000/ws", additional_headers=HEADERS) as ws:
            t0 = time.time()
            await ws.send(json.dumps({"type": "message", "text": "hey!", "client_id": "t1"}))
            await asyncio.sleep(1)
            await ws.send(json.dumps({"type": "message", "text": "yesterday I go to the theater with my cousin and we watch a very sad movie", "client_id": "t2"}))
            events = await collect(ws, lambda ev: of(ev, "correction") or (time.time() - t0 > 120 and of(ev, "companion")))
            bubbles = of(events, "companion")
            fixes = of(events, "correction")
            reads = [e for e in events if e["type"] == "read"]
            print(f"text turn: {len(bubbles)} bubbles in {time.time() - t0:.1f}s, one read receipt for both: {len(reads) == 1 and len(reads[0]['ids']) == 2}")
            for b in bubbles:
                print("  companion:", b["text"])
            for f in fixes:
                for c in f["meta"].get("corrections", []):
                    print(f"  ✏️ {c['original']} → {c['better']} ({c['why']})")
            assert bubbles, "no reply"

            if voice_file:
                t0 = time.time()
                with open(voice_file, "rb") as fh:
                    r = await http.post("/api/voice", files={"file": ("note.wav", fh.read())})
                r.raise_for_status()
                print(f"voice note transcribed in {time.time() - t0:.1f}s: {r.json()['message']['text']!r}")
                events = await collect(ws, lambda ev: any(e["type"] == "audio" for e in ev) and
                                       not any(e["type"] == "recording" for e in ev[-1:]), timeout=240)
                audio = [e for e in events if e["type"] == "audio"]
                print(f"voice turn: {len(of(events, 'companion'))} bubbles, {len(audio)} voice replies, "
                      f"first audio after {time.time() - t0:.1f}s")
                assert audio, "no voice reply"
                a = await http.get(f"/api/audio/{audio[0]['message']['id']}")
                print(f"  audio file: {a.headers.get('content-type')} {len(a.content)} bytes")
    print("e2e OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="")
    asyncio.run(main(ap.parse_args().voice))
