"""Client for llama-server, with llama.cpp extras (slots, prompt cache).

The live conversation uses raw `/completion` with prompts from `render.py`, so past turns are
replayed byte-exact and stay in the prompt cache (DECISIONS D15). In English it also runs a slop
guard (D16): each sentence is checked before it is released, and on a match the reply is rewound
to where the pattern starts and continues with that first token banned at that spot only.
Background jobs use the OpenAI-compatible chat endpoint (JSON schemas).
"""
import json
import logging
import re

import httpx

from . import render, slop

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?…]+(?=\s)|(?<=\S)(?=\n)")
MARKDOWN_TOKENS = ("**", " **", "*", " *")  # banned outright in chat: she texts, she doesn't format
MAX_REWINDS = 6  # per reply; each costs ~0.3-0.5 s, hidden in her "typing" time


class LLM:
    def __init__(self, cfg):
        c = cfg["llm"]
        self.url = c["url"].rstrip("/")
        self.sampling = c.get("sampling", {})
        self.template_kwargs = c.get("chat_template_kwargs") or {}
        self.max_tokens = c.get("max_tokens", 400)
        self.exact_replay = c.get("exact_replay", True)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        self.raw_ok = None   # None = not checked yet; set by the first live turn
        self.ban_ids = []    # markdown token ids, resolved with the template check

    def _body(self, messages, slot, **overrides):
        body = {"messages": messages, "max_tokens": self.max_tokens, "cache_prompt": True,
                "id_slot": slot, **self.sampling, **overrides}
        if self.template_kwargs:
            body["chat_template_kwargs"] = self.template_kwargs
        return body

    async def stream_chat(self, messages, slot, guard=False, grounding=None):
        """Yield text deltas. Closing the generator early aborts generation on the server.

        `guard` turns on the English slop guard; `grounding` (what he wrote plus her memory notes)
        lets it also stop callbacks to things he never said."""
        if await self.raw_ready():
            async for piece in self._stream_raw(render.render(messages), slot, guard, grounding):
                yield piece
            return
        body = self._body(messages, slot, stream=True)
        async with self.client.stream("POST", f"{self.url}/v1/chat/completions", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("timings"):
                    _log_timings(chunk["timings"])
                for choice in chunk.get("choices", []):
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        yield delta

    async def raw_ready(self):
        """Exact replay is on when the server's template renders like render.py (checked once)."""
        if not self.exact_replay:
            return False
        if self.raw_ok is None:
            try:
                r = await self.client.post(f"{self.url}/apply-template", json={
                    "messages": render.SAMPLE, "chat_template_kwargs": self.template_kwargs})
                r.raise_for_status()
                self.raw_ok = render.matches_template(r.json()["prompt"])
                if self.raw_ok:
                    for s in MARKDOWN_TOKENS:
                        if len(ids := await self.tokenize(s)) == 1:
                            self.ban_ids.append(ids[0])
                    log.info("exact replay on: raw /completion for the conversation (banned markdown tokens %s)",
                             self.ban_ids)
                else:
                    log.warning("the chat template differs from render.py: exact replay off, using chat completions")
            except httpx.HTTPError as e:
                log.warning("template check failed (%s); using chat completions for now", e)
                return False
        return self.raw_ok

    def _raw_body(self, prompt, slot, **overrides):
        return {"prompt": prompt, "n_predict": self.max_tokens, "cache_prompt": True, "id_slot": slot,
                **self.sampling, "logit_bias": [[t, False] for t in self.ban_ids], **overrides}

    async def _stream_raw(self, prompt, slot, guard, grounding=None):
        """Stream a reply; with `guard`, release whole sentences only after the slop check."""

        def check(sentence):
            return slop.find(sentence) or (grounding is not None and slop.ungrounded_callback(sentence, grounding))
        text, pending, rewinds = "", "", 0  # text: already yielded; pending: generated, not yet checked
        banned_at = {}  # reply position -> tokens banned there, so two phrasings can't ping-pong
        while True:
            restart = False
            async with self.client.stream("POST", f"{self.url}/completion",
                                          json=self._raw_body(prompt + text + pending, slot, stream=True)) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = json.loads(line[6:])
                    piece = chunk.get("content") or ""
                    if not guard:
                        if piece:
                            text += piece
                            yield piece
                    else:
                        pending += piece
                        while (m := _SENTENCE_END.search(pending, len(pending) - len(pending.lstrip()))):
                            sentence = pending[:m.end()]
                            if rewinds < MAX_REWINDS and (hit := check(sentence)):
                                pending = await self._rewind(prompt + text, slot, sentence, hit, banned_at)
                                rewinds, restart = rewinds + 1, True
                                break
                            text, pending = text + sentence, pending[m.end():]
                            yield sentence
                    if chunk.get("stop"):
                        if chunk.get("timings"):
                            _log_timings(chunk["timings"], rewinds)
                        break
                    if restart:
                        break
            if restart:
                continue
            if pending and rewinds < MAX_REWINDS and (hit := check(pending)):
                pending = await self._rewind(prompt + text, slot, pending, hit, banned_at)
                rewinds += 1
                continue
            if pending:
                yield pending
            return

    async def _rewind(self, before, slot, sentence, hit, banned_at):
        """Cut `sentence` where the pattern starts and write one token there without that word's first token."""
        start, name = hit
        keep = sentence[:start]
        lead = " " if keep.endswith(" ") else ""
        keep = keep.rstrip(" ")
        comma = keep.endswith(",")  # "tomorrow, or…": rewind before the comma and ban both there
        keep = keep[:-1] if comma else keep
        word = re.match(r"[\w']+|\S", sentence[start:]).group()
        banned = banned_at.setdefault(len(before + keep), [])
        banned.extend((await self.tokenize(lead + word))[:1])
        if comma:
            banned.extend((await self.tokenize(","))[:1])
        log.info("slop guard: %s at %r", name, sentence[start:start + 50])
        r = await self.client.post(f"{self.url}/completion", json=self._raw_body(
            before + keep, slot, n_predict=1,
            logit_bias=[[t, False] for t in self.ban_ids + banned]))
        r.raise_for_status()
        return keep + (r.json().get("content") or "")

    async def tokenize(self, text):
        r = await self.client.post(f"{self.url}/tokenize", json={"content": text})
        r.raise_for_status()
        return r.json()["tokens"]

    async def warm(self, messages, slot):
        """Pre-process a stable prompt prefix on `slot` so the next turn only reads the new message."""
        if await self.raw_ready():
            r = await self.client.post(f"{self.url}/completion", json=self._raw_body(
                render.render(messages, open_reply=False), slot, n_predict=0))
            r.raise_for_status()
            _log_timings(r.json().get("timings") or {})
        else:
            await self.chat(messages, slot, max_tokens=1)

    async def chat(self, messages, slot, json_schema=None, **overrides):
        """One-shot completion. With json_schema the reply is constrained to that schema and parsed."""
        if json_schema is not None:
            overrides["response_format"] = {"type": "json_schema", "json_schema": {"schema": json_schema}}
        r = await self.client.post(f"{self.url}/v1/chat/completions", json=self._body(messages, slot, **overrides))
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return json.loads(text) if json_schema is not None else text

    async def healthy(self):
        try:
            r = await self.client.get(f"{self.url}/health", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False


def _log_timings(t, rewinds=0):
    if t:
        log.info("llm: prompt %d tok (cached %s) %.0f tok/s, gen %d tok %.1f tok/s%s",
                 t.get("prompt_n", 0), t.get("cache_n", "?"), t.get("prompt_per_second", 0),
                 t.get("predicted_n", 0), t.get("predicted_per_second", 0),
                 f", {rewinds} slop rewind(s)" if rewinds else "")
