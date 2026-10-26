"""Поправя смесени азбуки: латински букви в кирилски текст и обратно.

Tesseract с bul+eng понякога чете „се на“ като латинското „ce Ha“,
защото буквите изглеждат еднакво. Тук решаваме по околния текст.
"""
import re

_LAT2CYR = dict(zip("ABCEHKMOPTXYaceopxyk", "АВСЕНКМОРТХУасеорхук"))
_CYR2LAT = {v: k for k, v in _LAT2CYR.items()}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# Известни грешки на Tesseract (bul+eng) в английски текст: „the“ се чете „Ше“.
_LAT_CONFUSIONS = {"Ше": "the", "ше": "the", "Шеге": "there", "Шеп": "then", "Шеу": "they", "Шат": "that", "Шіз": "this"}


def _is_cyr(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


def _is_lat(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def dominant_script(text: str) -> str | None:
    cyr = sum(_is_cyr(c) for c in text)
    lat = sum(_is_lat(c) for c in text)
    if cyr > lat * 1.5:
        return "cyr"
    if lat > cyr * 1.5:
        return "lat"
    return None


def _fix_word(word: str, script: str | None) -> str:
    cyr = sum(_is_cyr(c) for c in word)
    lat = sum(_is_lat(c) for c in word)
    if cyr and lat:  # смесена дума: уеднаквяваме към преобладаващата в нея
        target = "cyr" if cyr > lat or (cyr == lat and script == "cyr") else "lat"
    elif script and not word.isupper():  # цяла дума „от другата“ азбука
        target = script
    else:
        return word  # абревиатури (PC, OK) не пипаме
    table = _LAT2CYR if target == "cyr" else _CYR2LAT
    out = "".join(table.get(c, c) for c in word)
    # Ако не всички букви имат двойник, по-добре да оставим думата.
    ok = all(_is_cyr(c) for c in out) if target == "cyr" else all(_is_lat(c) for c in out)
    return out if ok else word


def fix_scripts(text: str, context: str | None = None) -> str:
    script = dominant_script(context if context is not None else text)

    def repl(m: re.Match) -> str:
        word = m.group(0)
        if script == "lat" and word in _LAT_CONFUSIONS:
            fixed = _LAT_CONFUSIONS[word]
            before = text[:m.start()].rstrip()
            if not before or before[-1] in ".!?":
                fixed = fixed.capitalize()
            return fixed
        return _fix_word(word, script)

    text = _WORD.sub(repl, text)
    if script == "cyr":
        # „№ 14“ често се чете като „Ne 14“ / „No 14“
        text = re.sub(r"(?<![\w])(?:Ne|No|N°|N2)(\s?)(?=\d)", r"№\1", text)
    return text
