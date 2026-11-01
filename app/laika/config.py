"""Load config.yaml (settings) and secrets from the environment."""
import os
import pathlib
import re

import yaml

CONFIG_PATH = pathlib.Path(os.environ.get("LAIKA_CONFIG", "/app/config.yaml"))
DATA_DIR = pathlib.Path(os.environ.get("LAIKA_DATA", "/data"))
VAULT_DIR = pathlib.Path(os.environ.get("LAIKA_VAULT", "/vault"))
WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
DEFAULT_USER = "Dasha"  # the person Laika talks with, when config.yaml has no `user.name`
DEFAULT_ABOUT = "a Spanish speaker who practices English, Russian and Chinese with her"


def user_name(cfg):
    """The user's name (config.yaml `user.name`): used in prompts, transcripts and the vault folder."""
    return ((cfg or {}).get("user") or {}).get("name") or DEFAULT_USER


def user_pronouns(cfg):
    """`he`, `she` or `they` (config.yaml `user.pronouns`); `they` when unset."""
    p = str(((cfg or {}).get("user") or {}).get("pronouns") or DEFAULT_PRONOUNS).strip().lower()
    return p if p in PRONOUNS else DEFAULT_PRONOUNS


def personal(text, cfg):
    """Fill `{user}` (the name) and `{user_about}` (a short description) in a prompt template, and put
    its pronouns in the user's (templates are written with he/him/his). Done before any `.format()`,
    so templates keep their other placeholders and inserted text (facts, book passages) is untouched."""
    about = ((cfg or {}).get("user") or {}).get("about") or DEFAULT_ABOUT
    text = text.replace("{user_about}", about).replace("{user}", user_name(cfg))
    return pronouns(text, user_pronouns(cfg))


# Pronouns: the prompt templates refer to the user as "he"; for "she" and "they" they are rewritten
# here, with verb agreement for "they" ("he is" → "they are", "he writes" → "they write").
DEFAULT_PRONOUNS = "they"
PRONOUNS = {
    "he": {},
    "she": {"he": "she", "him": "her", "his": "her", "himself": "herself", "he's": "she's", "he'd": "she'd",
            "he'll": "she'll"},
    "they": {"he": "they", "him": "them", "his": "their", "himself": "themself", "he's": "they're",
             "he'd": "they'd", "he'll": "they'll"},
}
_THEY_VERBS = {"is": "are", "was": "were", "has": "have", "does": "do", "doesn't": "don't", "isn't": "aren't",
               "wasn't": "weren't", "hasn't": "haven't", "goes": "go"}
_ADVERBS = {"always", "never", "often", "sometimes", "usually", "really", "just", "still", "also", "already",
            "clearly", "actually", "probably", "only", "even", "perhaps", "now", "then", "simply", "rarely"}
_NOT_VERBS = {"thus", "as", "this", "us", "plus", "yes", "its", "his", "has", "is", "was", "does"}
_WORD_RE = re.compile(r"([A-Za-z]+(?:'[a-z]+)?)")
_QUESTION_RE = re.compile(r"\b(is|was|has|does|doesn't|isn't|wasn't|hasn't|Is|Was|Has|Does)(\s+)he\b")


def _de_s(verb):
    """Third person singular → plural: writes → write, tries → try, watches → watch."""
    if verb.endswith("ies") and len(verb) > 4:
        return verb[:-3] + "y"
    if verb.endswith(("sses", "shes", "ches", "xes", "zes", "oes")):
        return verb[:-2]
    if verb.endswith("s") and not verb.endswith("ss"):
        return verb[:-1]
    return verb


def pronouns(text, which):
    """Rewrite he/him/his in a template for `which` (he: unchanged, byte for byte)."""
    table = PRONOUNS.get(which) or {}
    if not table:
        return text
    if which == "they":  # "does he" → "do they"
        text = _QUESTION_RE.sub(lambda m: (_THEY_VERBS.get(m.group(1).lower(), m.group(1)).capitalize()
                                           if m.group(1)[0].isupper() else _THEY_VERBS.get(m.group(1), m.group(1)))
                                + m.group(2) + "they", text)

    parts = _WORD_RE.split(text)  # odd indexes are words
    i = 1
    while i < len(parts):
        word = parts[i]
        new = table.get(word.lower())
        if new:
            parts[i] = new.capitalize() if word[0].isupper() else new
            if which == "they" and word.lower() == "he":  # agree the verb, maybe after one adverb
                j = i + 2
                if j < len(parts) and parts[j].lower() in _ADVERBS and re.fullmatch(r"\s+", parts[j - 1] or ""):
                    j += 2
                if j < len(parts) and re.fullmatch(r"\s+", parts[j - 1] or ""):
                    verb, low = parts[j], parts[j].lower()
                    if low in _THEY_VERBS:
                        parts[j] = _THEY_VERBS[low]
                    elif low.endswith("s") and low not in _NOT_VERBS and low not in _ADVERBS and verb.isalpha():
                        parts[j] = _de_s(verb)
        i += 2
    return "".join(parts)


def load():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["secrets"] = {
        "allowed_ts_login": os.environ.get("ALLOWED_TS_LOGIN", ""),
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_user_id": os.environ.get("TELEGRAM_ALLOWED_USER_ID", ""),
        # W4 (D13): Laika's own Telegram account; "bot" keeps the old bot adapter
        "telegram_mode": os.environ.get("TELEGRAM_MODE", "bot"),
        "tg_api_id": os.environ.get("TG_API_ID", ""),
        "tg_api_hash": os.environ.get("TG_API_HASH", ""),
        "tg_phone": os.environ.get("TG_PHONE", ""),
    }
    return cfg
