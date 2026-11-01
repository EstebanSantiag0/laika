"""The user's name is a setting (config.yaml `user.name`, default "Dasha"): prompts, transcripts, the
vault folder and the self entity all follow it.

Run: docker run --rm -v "$PWD/app:/app:ro" -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 \
       python -m tests.test_user
"""
import pathlib
import re
import tempfile

from laika import consolidate, nightly, reminders
from laika.config import DEFAULT_USER, personal, user_name
from laika.consolidate import rounds
from laika.db import DB
from laika.persona import Persona
from laika.vault import Vault
from tests.test_engine import CFG


def main():
    bare = {k: v for k, v in CFG.items() if k != "user"}
    assert DEFAULT_USER == "Dasha" and user_name(bare) == "Dasha" and user_name(CFG) == CFG["user"]["name"]
    assert user_name({"user": {"name": ""}}) == "Dasha", "an empty name falls back to the default"
    assert personal("Hi {user}, {user_about}.", {"user": {"name": "Kim", "about": "a cook"}}) == "Hi Kim, a cook."
    for template in (consolidate.PROMPT, reminders.EXTRACT, nightly.PORTRAIT):
        assert "Dasha" in personal(template, bare)
        text = personal(template, {"user": {"name": "Kim"}})
        assert "Kim" in text and "{user" not in text and "Dasha" not in text
    root = pathlib.Path(tempfile.mkdtemp())
    persona = Persona(bare, DB(":memory:"), root)
    persona.ensure_files()
    written = persona.persona_path.read_text()
    assert "You are Dasha's friend" in written and "{user}" not in written
    prompt = persona.system_prompt()
    assert "Dasha's native language" in prompt and "{user}" not in prompt
    assert Vault(root).profile == "Dasha/Profile.md" and Vault(root, "Kim").profile == "Kim/Profile.md"
    seg = [{"id": 1, "ts": 0, "role": "user", "text": "hola", "meta": {}},
           {"id": 2, "ts": 1, "role": "companion", "text": "¡hola!", "meta": {}}]
    assert rounds(seg)[0][3] == "Dasha: hola\nLaika: ¡hola!" and rounds(seg, "Kim")[0][3].startswith("Kim: ")
    # pronouns (W8): templates are written with he/him/his; "he" leaves them byte for byte
    from laika.config import pronouns, user_pronouns
    assert user_pronouns(bare) == "they" and user_pronouns({"user": {"pronouns": "She"}}) == "she"
    s = "Does he like it? He wasn't sure; tell him his level, he taught himself and he always writes fast"
    assert pronouns(s, "he") == s
    assert pronouns(s, "she") == ("Does she like it? She wasn't sure; tell her her level, she taught herself and she "
                                 "always writes fast")
    assert pronouns(s, "they") == ("Do they like it? They weren't sure; tell them their level, they taught themself "
                                  "and they always write fast")
    assert pronouns("what he's going through, how he likes to talk", "they") == \
        "what they're going through, how they like to talk"
    they = {"user": {"name": "Kim", "pronouns": "they"}}
    assert "how much it's about who they are" in personal(consolidate.PROMPT, they)
    for template in (consolidate.PROMPT, reminders.EXTRACT, nightly.PORTRAIT):
        assert not re.search(r"\b(he|him|his|himself)\b", personal(template, they), re.I), "a he-form was left"
    # gender: one line in the language rules only when set
    p2 = Persona({**bare, "user": {"name": "Kim", "pronouns": "she", "gender": "woman"}}, DB(":memory:"), root)
    assert "Kim is a woman: in languages with grammatical gender" in p2.system_prompt()
    assert "is a woman: in languages" not in prompt and "is a man: in languages" not in prompt, "no gender: no line"
    print("test_user: all passed")


if __name__ == "__main__":
    main()
