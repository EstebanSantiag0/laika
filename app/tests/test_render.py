"""Exact-replay renderer and slop guard, against a fake llama-server (httpx MockTransport).

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_render
"""
import asyncio
import json

import httpx

from laika import render, slop
from laika.llm import LLM

CFG = {"llm": {"url": "http://llm", "sampling": {"temperature": 1.0}, "max_tokens": 100,
               "chat_template_kwargs": {"enable_thinking": False}}}
TEMPLATE = ("<|turn>system\nS<turn|>\n<|turn>user\na<turn|>\n<|turn>model\nb\n\nc<turn|>\n<|turn>user\nd<turn|>\n"
            "<|turn>model\n<|channel>thought\n<channel|>")  # what llama-server's /apply-template returns for SAMPLE


def test_render():
    assert render.matches_template(TEMPLATE)
    replay = render.render(render.SAMPLE)
    assert "<|turn>model\n<|channel>thought\n<channel|>b\n\nc<turn|>" in replay, replay
    assert replay.endswith("<|turn>model\n<|channel>thought\n<channel|>")
    assert not render.render(render.SAMPLE, open_reply=False).endswith("<|turn>model\n<|channel>thought\n<channel|>")
    print("ok render")


def test_slop_patterns():
    hits = {
        "That's such a poetic way to put it.": "praise_idea",
        "\n\nIt's like you're trying to give me a memory.": "restate_its_like",
        " It's not just words; it's a new way of thinking.": "not_just",
        "We could build a history rather than just chatting.": "rather_than_just",
        "Do you want to dive into something deep, or talk about something random?": "either_or_question",
        "There's something profound about it.": "praise_word",
        "Whether you're building the future or just navigating it, you'll be fine.": "whether_or",
        "Which part was the hardest? The grammar or the listening?": "either_or_question",
        'Does this mean you\'re building a space for my "brain"?': "scare_quotes",
        "Being called out like that is such a visceral feeling.": "praise_word",
        'I guess it depends on how you define "you."': "scare_quotes",
        "It's one thing to be a student, it's another to feel like a tool.": "one_thing_another",
    }
    for sentence, name in hits.items():
        hit = slop.find(sentence)
        assert hit and hit[1] == name, (sentence, hit)
        assert sentence[hit[0]].isalnum() or sentence[hit[0]] in '"“', (sentence, hit)
    for sentence in ["Hmm, I'm not sure I buy that.", "What happened?", "That's huge!", "I like it too, honestly.",
                     "Which part went best?", "It's 3am here, go to sleep 😄", "Not just yet.",
                     "I read it, or at least most of it.", 'He said "no" and left.']:
        assert slop.find(sentence) is None, (sentence, slop.find(sentence))
    q = "Do you have to deal with them tomorrow, or can you take some time to breathe?"
    assert slop.find(q) == (q.index(" or ") + 1, "either_or_question"), slop.find(q)  # rewind at "or"
    assert slop.ungrounded_callback("We could talk about that book you were reading.", "hi") is not None
    invented = "I was thinking about that book you mentioned last time. Did you finish it?"
    assert slop.ungrounded_callback(invented, "what should we talk about?") == (0, "ungrounded_callback")
    assert slop.ungrounded_callback(invented, "I'm reading a great book about Stoics") is None
    assert slop.ungrounded_callback(" Wasn't your exam this week?", "my exam") is None  # no callback cue
    print("ok slop_patterns")


def fake_server(script):
    """Continuations keyed by the end of the prompt; records every request body."""
    requests = []

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, body))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": TEMPLATE})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [len(body["content"])]})  # fake ids: string length
        prompt = body["prompt"]
        if body.get("n_predict") == 1:
            return httpx.Response(200, json={"content": " Honestly", "stop": True})  # real tokens carry the space
        text = next(cont for end, cont in script if prompt.endswith(end))
        chunks = [text[i:i + 7] for i in range(0, len(text), 7)]
        lines = "".join(f"data: {json.dumps({'content': c, 'stop': False})}\n\n" for c in chunks)
        lines += f"data: {json.dumps({'content': '', 'stop': True, 'timings': {'prompt_n': 3}})}\n\n"
        return httpx.Response(200, content=lines.encode())
    return requests, handler


async def collect(llm, messages, guard):
    return "".join([d async for d in llm.stream_chat(messages, 0, guard=guard)])


async def test_guard_rewinds():
    script = [
        ("Honestly", ", I think you're right.\n\nTell me more about the teacher"),
        ("<channel|>", "Haha. That's such a poetic way to put it. I think you're right.\n\nTell me more"),
    ]
    requests, handler = fake_server(script)
    llm = LLM(CFG)
    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    out = await collect(llm, messages, guard=True)
    assert out == "Haha. Honestly, I think you're right.\n\nTell me more about the teacher", repr(out)
    rewind = next(b for path, b in requests if path == "/completion" and b.get("n_predict") == 1)
    assert rewind["prompt"].endswith("<channel|>Haha."), rewind["prompt"][-30:]
    assert [len(" That's"), False] in rewind["logit_bias"], rewind["logit_bias"]  # first token of " That's", at that spot only
    assert all(path != "/v1/chat/completions" for path, _ in requests), "fell back to chat completions"
    first = next(b for path, b in requests if path == "/completion")
    assert first["prompt"] == render.render(messages) and first["id_slot"] == 0
    print("ok guard_rewinds")


async def test_no_guard_passthrough():
    requests, handler = fake_server([("<channel|>", "Привет! Это не просто слова, а чувства.")])
    llm = LLM(CFG)
    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await collect(llm, [{"role": "user", "content": "hi"}], guard=False)
    assert out == "Привет! Это не просто слова, а чувства.", out
    assert not any(b.get("n_predict") == 1 for _, b in requests)
    print("ok no_guard_passthrough")


async def main():
    test_render()
    test_slop_patterns()
    await test_guard_rewinds()
    await test_no_guard_passthrough()
    print("all render tests passed")


if __name__ == "__main__":
    asyncio.run(main())
