"""Cheap language detection by script and stopwords. Good enough to tell EN/ES/RU/ZH apart."""
import re

NAMES = {"en": "English", "ru": "Russian", "zh": "Chinese", "es": "Spanish"}
NATIVE_NAMES = {"en": "English", "ru": "Русский", "zh": "中文", "es": "Español"}
PRACTICE = ("en", "ru", "zh")
# A gloss in parentheses with Latin letters: "кошка (cat)", "你好 (Nǐ hǎo - hi)", "你好（nǐ hǎo）".
GLOSS = re.compile(r"\s*[(（](?=[^()（）]*[A-Za-z])[^()（）]*[)）]")

_WORD = re.compile(r"[a-záéíóúñü']+")
_ES = {"que", "de", "el", "la", "los", "las", "y", "en", "es", "por", "para", "con", "una", "un", "pero",
       "muy", "como", "qué", "cómo", "yo", "tú", "mi", "me", "te", "lo", "se", "no", "sí", "esto", "eso",
       "está", "estoy", "tengo", "quiero", "porque", "hola", "gracias"}
_EN = {"the", "and", "is", "you", "i", "to", "of", "a", "in", "that", "it", "for", "with", "my", "me",
       "are", "was", "what", "how", "do", "this", "but", "have", "be", "not", "can", "so", "hi", "thanks"}


def counts(text):
    cjk = sum("一" <= ch <= "鿿" or "㐀" <= ch <= "䶿" for ch in text)
    cyr = sum("Ѐ" <= ch <= "ӿ" for ch in text)
    latin = sum(ch.isalpha() and ch.isascii() or ch in "áéíóúñü" for ch in text.lower())
    return cjk, cyr, latin


def detect(text):
    """Return 'zh', 'ru', 'en', 'es' or None."""
    cjk, cyr, latin = counts(text)
    # One CJK character carries roughly as much as three Latin letters.
    if cjk and cjk * 3 >= max(cyr, latin):
        return "zh"
    if cyr and cyr >= latin:
        return "ru"
    if not latin:
        return None
    words = _WORD.findall(text.lower())
    # Next to Chinese, accented letters are pinyin tone marks (shénme), not Spanish.
    accents = 0 if cjk else sum(ch in "ñ¿¡áéíóú" for ch in text.lower())
    es = sum(w in _ES for w in words) + 2 * accents
    en = sum(w in _EN for w in words)
    return "es" if es > en else "en"


def bubble_lang(text, practice):
    """Language of one of her messages. While he practices Russian or Chinese, a message with two or
    more letters of that script is Russian or Chinese, even when English glosses or pinyin outweigh
    them: the voice must read it in that language (W6). Otherwise the glosses don't count."""
    cjk, cyr, _ = counts(text)
    if practice == "zh" and cjk >= 2 or practice == "ru" and cyr >= 2:
        return practice
    return detect(GLOSS.sub("", text)) or detect(text)
