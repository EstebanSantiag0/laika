"""Client for the `embed` service (Qwen3-Embedding-0.6B on llama-server, CPU): 1024-d, normalized.

Queries get Qwen3-Embedding's instruction prefix; stored memories don't (as the model card says).
If the service is down, callers get an exception and carry on without recall.
The last few `one()` results are kept, so recall and the library hook (P4.2) share one query embedding.
"""
from collections import OrderedDict

import httpx

from .retry import retrying

# Topic-focused on purpose: an instruction naming the user and Laika made every memory look alike (they all
# mention him). Calibration 2026-10-18 (BENCHMARKS P3.4): top match 0.46-0.57 for related messages vs
# 0.24-0.35 for unrelated ones with this wording; 0.55-0.65 vs 0.37-0.64 with the old one.
QUERY = "Instruct: Given a chat message, retrieve notes about the same topic, person or event\nQuery: "


class Embedder:
    def __init__(self, cfg):
        self.url = cfg["embed"]["url"].rstrip("/")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5))
        self.recent = OrderedDict()  # (text, query) -> embedding

    async def embed(self, texts, query=False):
        if not texts:
            return []
        inputs = [QUERY + t if query else t for t in texts]
        async def call():
            r = await self.client.post(f"{self.url}/v1/embeddings", json={"input": inputs})
            r.raise_for_status()
            return r

        r = await retrying(call, what="embed")
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]

    async def one(self, text, query=False):
        key = (text, query)
        if key in self.recent:
            self.recent.move_to_end(key)
            return self.recent[key]
        vec = (await self.embed([text], query))[0]
        self.recent[key] = vec
        if len(self.recent) > 16:
            self.recent.popitem(last=False)
        return vec
