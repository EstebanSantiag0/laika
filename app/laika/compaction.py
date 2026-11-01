"""Keeps long conversations within the prompt budget.

When the engine trims the history window, the dropped messages are folded into a short running
summary (shown in the system prompt), and the new prompt prefix is pre-processed on the
conversation slot so the user's next message doesn't wait for it.
"""
import logging

from .config import personal, user_name

log = logging.getLogger(__name__)

PROMPT = """You keep the memory of an ongoing chat between {user} and his AI friend. Merge the earlier \
summary (if any) with the new part of the chat below into one summary in English: at most 10 short \
bullet points, under 250 words. Keep facts about {user} and the people in his life, what he is doing \
and feeling, plans, the topics and opinions you discussed, running jokes, and which language he was \
practicing. Drop small talk and anything already covered."""


class Compactor:
    def __init__(self, cfg, db, llm, engine):
        self.cfg, self.db, self.llm, self.engine = cfg, db, llm, engine

    async def on_trim(self, old_start, new_start):
        dropped = [m for m in self.db.conversation_since(old_start) if m["id"] < new_start]
        if dropped:
            transcript = "\n".join(f"{user_name(self.cfg) if m['role'] == 'user' else 'Friend'}: {m['text']}" for m in dropped)
            earlier = self.db.get_setting("running_summary", "")
            user = (f"Earlier summary:\n{earlier}\n\n" if earlier else "") + f"New part of the chat:\n{transcript}"
            summary = await self.llm.chat([{"role": "system", "content": personal(PROMPT, self.cfg)}, {"role": "user", "content": user}],
                                          self.cfg["llm"]["background_slot"], temperature=0.3, max_tokens=400)
            self.db.set_setting("running_summary", summary.strip())
            log.info("history compacted: %d messages folded into the running summary", len(dropped))
        await self.warm()

    async def warm(self):
        """Pre-process the new stable prefix on the conversation slot (one generated token)."""
        if self.engine.turn and not self.engine.turn.done():
            return  # a turn is running; it will rebuild the cache itself
        await self.llm.warm(self.engine.persona.prefix_messages(), self.cfg["llm"]["conversation_slot"])
        log.info("prompt cache warmed after compaction")
