"""Recreate VoiceStudio's "The Neighbor" designed voice (OmniVoice, seed 42, 32 steps, guidance 2.0).

This is the reference clip behind Laika's voice (DECISIONS D12). Run it once in a throwaway container:

  docker run --rm --gpus all -e HF_HOME=/models/hf-cache \
    -v ~/laika/models/hf-cache:/models/hf-cache \
    -v ~/laika/scripts:/scripts:ro -v ~/laika/models/voice/refs:/out \
    --entrypoint sh local/laika-voice:0.2 -c \
    'pip install -q omnivoice==0.2.1 transformers==5.15.1 && python /scripts/make_neighbor_voice.py'

Then: cp models/voice/refs/neighbor-seed42.wav models/voice/refs/neighbor.wav and
curl -X POST 127.0.0.1:8082/v1/voices/reload. Seed 43 is a second take of the same description.
"""
import numpy as np, soundfile as sf, torch
from omnivoice import OmniVoice

TEXT = ("Oh hey, I didn't expect to run into you here! How have you been? "
        "We should really catch up properly one of these days.")
INSTRUCT = "female, young adult, moderate pitch, american accent"
model = OmniVoice.from_pretrained("k2-fsa/OmniVoice", device_map="cuda:0", dtype=torch.bfloat16)
for seed in (42, 43):
    torch.manual_seed(seed)
    audio = model.generate(text=TEXT, language="English", instruct=INSTRUCT, num_step=32,
                           guidance_scale=2.0, speed=1.0, denoise=True, postprocess_output=True)
    a = np.asarray(audio[0], dtype=np.float32)
    print(f"seed {seed}: {len(a)/24000:.1f}s, finite={np.isfinite(a).all()}, peak={np.abs(a).max():.2f}")
    sf.write(f"/out/neighbor-seed{seed}.wav", a, 24000)
