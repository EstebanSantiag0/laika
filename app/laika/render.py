"""Raw Gemma 4 prompts for the live conversation slot, replayed byte-exact (DECISIONS D15).

llama-server's chat template drops the empty thought block from past replies, but the model
generated each reply right after that block, so the prompt cache broke at every past reply and
each turn re-read the previous exchange. Rendering the prompt here keeps every past turn identical
to what the model read and wrote; a turn only reads the new message.
"""
THOUGHT = "<|channel>thought\n<channel|>"  # what follows "<|turn>model\n" when thinking is off
ROLES = {"system": "system", "user": "user", "assistant": "model"}
SAMPLE = [{"role": "system", "content": "S"}, {"role": "user", "content": "a"},
          {"role": "assistant", "content": "b\n\nc"}, {"role": "user", "content": "d"}]


def render(messages, open_reply=True, replay_thought=True):
    """Prompt text for `messages`; with `open_reply` it ends where the model starts writing."""
    parts = []
    for m in messages:
        role = ROLES[m["role"]]
        thought = THOUGHT if role == "model" and replay_thought else ""
        parts.append(f"<|turn>{role}\n{thought}{m['content']}<turn|>\n")
    if open_reply:
        parts.append("<|turn>model\n" + THOUGHT)
    return "".join(parts)


def matches_template(template_text):
    """True when the server's chat template renders SAMPLE exactly like `render` (minus replayed thoughts)."""
    return template_text == render(SAMPLE, replay_thought=False)
