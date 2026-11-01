"""Voice service: Qwen3-TTS text-to-speech (GPU, via faster-qwen3-tts CUDA graphs)
+ faster-whisper speech-to-text (CPU).

OpenAI-style endpoints:
  GET  /health
  GET  /v1/voices
  POST /v1/voices/reload
  POST /v1/audio/speech          JSON {input, voice?, language?, speed?, response_format?}
  POST /v1/audio/transcriptions  multipart: file, language?, prompt?

Reference voices live in VOICES_DIR as <name>.wav. In the default x-vector mode only the
speaker's timbre is taken from the clip, so every language keeps a native accent.
ICL mode (TTS_CLONE_MODE=icl) also needs <name>.txt with the clip's transcript.
Speech responses carry X-Audio-Seconds and X-RTF headers for benchmarking.
"""
import asyncio
import functools
import logging
import os
import pathlib
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

TTS_MODEL = os.environ.get("TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
TTS_DTYPE = os.environ.get("TTS_DTYPE", "bfloat16")
TTS_MAX_SEQ = int(os.environ.get("TTS_MAX_SEQ", "768"))  # static KV cache length (~1 min of audio)
TTS_CLONE_MODE = os.environ.get("TTS_CLONE_MODE", "xvec")  # xvec | icl
TTS_VRAM_FRACTION = float(os.environ.get("TTS_VRAM_FRACTION", "0.45"))
# The audio decoder's memory grows with clip length; decoding in short chunks caps the peak.
TTS_DECODE_CHUNK = int(os.environ.get("TTS_DECODE_CHUNK", "24"))      # frames (12.5 per second)
TTS_DECODE_CONTEXT = int(os.environ.get("TTS_DECODE_CONTEXT", "12"))  # frames of left context
STT_MODEL = os.environ.get("STT_MODEL", "large-v3-turbo")
STT_COMPUTE = os.environ.get("STT_COMPUTE", "int8")
STT_THREADS = int(os.environ.get("STT_THREADS", "4"))
STT_BEAM = int(os.environ.get("STT_BEAM", "1"))  # 1 = greedy: ~15% faster on CPU, same text in tests
VOICES_DIR = pathlib.Path(os.environ.get("VOICES_DIR", "/models/voice/refs"))
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "")

# ISO codes used by the app -> language names expected by Qwen3-TTS.
LANGS = {
    "en": "English", "ru": "Russian", "zh": "Chinese", "es": "Spanish", "ja": "Japanese",
    "ko": "Korean", "de": "German", "fr": "French", "pt": "Portuguese", "it": "Italian",
}

# ffmpeg output arguments per response format (input is always WAV on stdin).
FORMATS = {
    "mp3": (["-c:a", "libmp3lame", "-q:a", "4", "-f", "mp3"], "audio/mpeg"),
    "opus": (["-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-ar", "48000", "-f", "ogg"], "audio/ogg"),
    "m4a": (["-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", "-f", "mp4"], "audio/mp4"),
}

log = logging.getLogger("voice")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

state = {"tts": None, "stt": None, "voices": {}}
tts_lock = threading.Lock()
stt_lock = threading.Lock()


def load_tts():
    from faster_qwen3_tts import FasterQwen3TTS

    torch.cuda.set_per_process_memory_fraction(TTS_VRAM_FRACTION, 0)
    t0 = time.time()
    state["tts"] = FasterQwen3TTS.from_pretrained(
        TTS_MODEL, dtype=getattr(torch, TTS_DTYPE), attn_implementation="sdpa", max_seq_len=TTS_MAX_SEQ
    )
    decoder = state["tts"].speech_tokenizer.model.decoder
    decoder.chunked_decode = functools.partial(
        decoder.chunked_decode, chunk_size=TTS_DECODE_CHUNK, left_context_size=TTS_DECODE_CONTEXT)
    log.info("TTS %s loaded as %s in %.1fs", TTS_MODEL, TTS_DTYPE, time.time() - t0)


def load_voices():
    voices = {}
    base = state["tts"].model  # the underlying qwen-tts model builds the prompts
    for wav in sorted(VOICES_DIR.glob("*.wav")):
        if TTS_CLONE_MODE == "icl":
            txt = wav.with_suffix(".txt")
            if not txt.exists():
                log.warning("voice %s has no transcript %s, skipped", wav.name, txt.name)
                continue
            voices[wav.stem] = base.create_voice_clone_prompt(
                ref_audio=str(wav), ref_text=txt.read_text(encoding="utf-8").strip(), x_vector_only_mode=False)
        else:
            items = base.create_voice_clone_prompt(ref_audio=str(wav), ref_text="", x_vector_only_mode=True)
            voices[wav.stem] = {"ref_spk_embedding": [items[0].ref_spk_embedding]}
    state["voices"] = voices
    log.info("voices (%s mode): %s", TTS_CLONE_MODE, ", ".join(voices) or "none")
    if voices:
        t0 = time.time()
        state["tts"].generate_voice_clone(text="Hello.", language="English",
                                          voice_clone_prompt=next(iter(voices.values())))
        log.info("warm-up (CUDA graph capture) took %.1fs", time.time() - t0)


def get_stt():
    if state["stt"] is None:
        from faster_whisper import WhisperModel

        t0 = time.time()
        state["stt"] = WhisperModel(STT_MODEL, device="cpu", compute_type=STT_COMPUTE, cpu_threads=STT_THREADS)
        log.info("STT %s (%s) loaded in %.1fs", STT_MODEL, STT_COMPUTE, time.time() - t0)
    return state["stt"]


def synthesize(text, voice, language):
    with tts_lock:
        wavs, sr = state["tts"].generate_voice_clone(
            text=text, language=LANGS.get(language, "English"), voice_clone_prompt=state["voices"][voice]
        )
    audio = np.asarray(wavs[0], dtype=np.float32)
    if not np.isfinite(audio).all():
        raise RuntimeError("TTS produced NaN/Inf samples (try TTS_DTYPE=float32)")
    return audio, sr


def encode(audio, sr, fmt, speed):
    with tempfile.NamedTemporaryFile(suffix=".wav") as src:
        sf.write(src.name, audio, sr)
        if fmt == "wav" and speed == 1.0:
            return pathlib.Path(src.name).read_bytes(), "audio/wav"
        args, mime = FORMATS.get(fmt, (["-f", "wav"], "audio/wav"))
        filt = ["-filter:a", f"atempo={speed}"] if speed != 1.0 else []
        with tempfile.NamedTemporaryFile(suffix="." + fmt) as dst:
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-i", src.name, *filt, *args, dst.name], check=True
            )
            return pathlib.Path(dst.name).read_bytes(), mime


@asynccontextmanager
async def lifespan(_app):
    await asyncio.to_thread(load_tts)
    await asyncio.to_thread(load_voices)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    if state["tts"] is None:
        raise HTTPException(503, "loading")
    return {
        "ok": True,
        "tts_model": TTS_MODEL,
        "dtype": TTS_DTYPE,
        "voices": list(state["voices"]),
        "stt_loaded": state["stt"] is not None,
        "vram_reserved_mb": round(torch.cuda.memory_reserved(0) / 2**20),
        "vram_peak_mb": round(torch.cuda.max_memory_reserved(0) / 2**20),
    }


@app.get("/v1/voices")
def voices():
    return {"voices": list(state["voices"]), "default": DEFAULT_VOICE or next(iter(state["voices"]), "")}


@app.post("/v1/voices/reload")
async def reload_voices():
    await asyncio.to_thread(load_voices)
    return voices()


class SpeechRequest(BaseModel):
    input: str
    voice: str = ""
    language: str = "auto"
    speed: float = 1.0
    response_format: str = "wav"


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    voice = req.voice or DEFAULT_VOICE or next(iter(state["voices"]), "")
    if voice not in state["voices"]:
        raise HTTPException(400, f"unknown voice {voice!r}; available: {list(state['voices'])}")
    if not 0.5 <= req.speed <= 2.0:
        raise HTTPException(400, "speed must be between 0.5 and 2.0")
    t0 = time.time()
    audio, sr = await asyncio.to_thread(synthesize, req.input, voice, req.language)
    elapsed = time.time() - t0
    seconds = len(audio) / sr
    data, mime = await asyncio.to_thread(encode, audio, sr, req.response_format, req.speed)
    log.info("tts %s/%s %d chars -> %.2fs audio in %.2fs (RTF %.2f)",
             voice, req.language, len(req.input), seconds, elapsed, elapsed / max(seconds, 1e-6))
    return Response(data, media_type=mime, headers={
        "X-Audio-Seconds": f"{seconds:.2f}", "X-RTF": f"{elapsed / max(seconds, 1e-6):.3f}"})


def transcribe(path, language, prompt):
    model = get_stt()
    with stt_lock:
        t0 = time.time()
        segments, info = model.transcribe(
            path, language=language or None, initial_prompt=prompt or None, vad_filter=True, beam_size=STT_BEAM
        )
        segments = list(segments)
        text = "".join(s.text for s in segments).strip()
    avg = sum(s.avg_logprob for s in segments) / len(segments) if segments else 0.0
    return {"text": text, "language": info.language, "duration": round(info.duration, 2),
            "avg_logprob": round(avg, 3), "elapsed": round(time.time() - t0, 2)}


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), language: str = Form(""), prompt: str = Form("")):
    suffix = pathlib.Path(file.filename or "audio").suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp.flush()
        result = await asyncio.to_thread(transcribe, tmp.name, language, prompt)
    log.info("stt %s %.2fs audio in %.2fs", result["language"], result["duration"], result["elapsed"])
    return result
