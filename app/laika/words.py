"""One tokenizer per language (DECISIONS D18): the library's keyword index and, from P4.4, the vocabulary.

SQLite's FTS5 can't split Chinese into words (`unicode61` keeps a run of Han characters as one token)
and its `porter` stemmer only knows English, so texts are indexed as lemmas joined by spaces, and
queries go through the same function. Each run of one script gets its own tokenizer, so mixed text
("what does собака mean?") works too:
- Chinese: jieba with part-of-speech tags (names and places are names, not vocabulary);
- Russian: pymorphy3 normal forms (Name/Surn/Patr/Geox/Orgn tags mark names);
- English and Spanish: simplemma.
The libraries load on first use; jieba takes about 1 s and ~110 MB.
"""
import functools
import re

WORD, NAME, NUM = "word", "name", "num"
_RUNS = re.compile(r"(?P<han>[㐀-䶿一-鿿]+)|(?P<cyr>[Ѐ-ӿ]+(?:[-'][Ѐ-ӿ]+)*)"
                   r"|(?P<lat>[A-Za-zÀ-ɏ]+(?:['’-][A-Za-zÀ-ɏ]+)*)|(?P<num>\d+(?:[.,]\d+)*)")
_RU_NAMES = {"Name", "Surn", "Patr", "Geox", "Orgn", "Trad"}
_ZH_NAMES = ("nr", "ns", "nt", "nz")
_ZH_SKIP = ("x", "w", "eng")
# Small words that would only add noise to a keyword query (the vector search covers meaning).
_QUERY_STOP = set("""the a an and or but of to in on at for with from by is are was were be been it this that these
those i you he she we they me him her us them my your his our their what which who how why when where do does did
not no yes so if as about into than then there here have has had can could would should will just very really
el la los las un una y o pero de del a en con por para es son era fue ser estar este esta eso que qué como cómo
yo tú él ella nosotros ellos me te se lo le mi tu su muy no sí
и в во не на я ты он она оно мы вы они что как а но да нет это то с со к ко у о об от по за из для же ли бы
быть был была были мой твой его её их весь этот тот так там тут где когда""".split())
_ZH_QUERY_KEEP = ("n", "v", "a", "l", "i", "j", "t", "s", "f", "m", "eng")


@functools.cache
def _jieba():
    import jieba
    import jieba.posseg
    jieba.setLogLevel(60)
    jieba.initialize()
    return jieba.posseg


@functools.cache
def _morph():
    import pymorphy3
    return pymorphy3.MorphAnalyzer(lang="ru")


@functools.cache
def _lemma_ru(word):
    p = _morph().parse(word)[0]
    name = word[:1].isupper() and bool(_RU_NAMES & set(p.tag.grammemes))
    return p.normal_form.replace("ё", "е"), name


@functools.lru_cache(maxsize=200_000)
def _lemma_latin(word, code):
    import simplemma
    return simplemma.lemmatize(word, lang=code).lower()


def tokens(text, code="en"):
    """[(surface, lemma, kind, pos)] for the words in `text`; punctuation is dropped. `code` is the
    document's language: it picks the lemmatizer for Latin-script words (Spanish or else English)."""
    latin_code = "es" if code == "es" else "en"
    out, sentence_start = [], True
    for m in _RUNS.finditer(text):
        kind = m.lastgroup
        word = m.group()
        if kind == "han":
            for w, flag in _jieba().cut(word):
                if flag.startswith(_ZH_SKIP):
                    continue
                k = NAME if flag.startswith(_ZH_NAMES) else NUM if flag.startswith("m") and _is_number(w) else WORD
                out.append((w, w, k, flag))
        elif kind == "cyr":
            lemma, name = _lemma_ru(word)
            out.append((word, lemma, NAME if name else WORD, "ru"))
        elif kind == "lat":
            lemma = _lemma_latin(word.lower(), latin_code)
            name = word[:1].isupper() and not sentence_start and not word.isupper()
            out.append((word, lemma, NAME if name else WORD, latin_code))
        else:
            out.append((word, word, NUM, "num"))
        sentence_start = bool(re.search(r"[.!?…。！？]\s*$", text[m.end():m.end() + 3]))
    return out


def search_text(text, code="en"):
    """Lemmas joined by spaces, lowercase, with "ё" as "е": what FTS5 stores and what queries match."""
    return " ".join(lemma.lower() for _, lemma, _, _ in tokens(text, code))


def query_terms(text, code="en"):
    """The words of a question worth matching: no function words, no single Latin or Cyrillic letters."""
    terms = []
    for surface, lemma, kind, pos in tokens(text, code):
        lemma = lemma.lower()
        if pos in ("ru", "en", "es", "num"):
            if len(lemma) < 2 or lemma in _QUERY_STOP:
                continue
        elif not pos.startswith(_ZH_QUERY_KEEP) or (len(lemma) == 1 and kind != NAME):
            continue
        if lemma not in terms:
            terms.append(lemma)
    return terms


def _is_number(w):
    """Numbers, and chapter markers like 第一章 / 第3节 (structure, not vocabulary)."""
    return bool(re.fullmatch(r"第?[\d一二三四五六七八九十百千万亿两零〇.,]+[章节回]?", w))
