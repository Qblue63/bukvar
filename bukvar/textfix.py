"""Поправя смесени азбуки: латински букви в кирилски текст и обратно.

Tesseract с bul+eng понякога чете „се на“ като латинското „ce Ha“,
защото буквите изглеждат еднакво. Тук решаваме по околния текст.
"""
import re

_LAT2CYR = dict(zip("ABCEHKMOPTXYaceopxyk", "АВСЕНКМОРТХУасеорхук"))
_CYR2LAT = {v: k for k, v in _LAT2CYR.items()}
# Само в посока към кирилица: „n“ е често прочетено „п“ (Р-л → „P-n“).
_LAT2CYR_EXT = _LAT2CYR
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# Известни грешки на Tesseract (bul+eng) в английски текст: „the“ се чете „Ше“.
# Д често се чете като „JI“ или „Л“: „ДА“ → „JIA“.
_CYR_CONFUSIONS = {"JI": "Л", "JIA": "ДА", "JlA": "ДА", "JIa": "Да", "Jla": "Да", "ДАГ": "ДА", "ЛА": "ДА"}
_LAT_CONFUSIONS = {"Ше": "the", "ше": "the", "Шеге": "there", "Шеп": "then", "Шеу": "they", "Шат": "that", "Шіз": "this"}


def _is_cyr(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


def _is_lat(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def dominant_script(text: str) -> str | None:
    cyr = sum(_is_cyr(c) for c in text)
    # латинските абревиатури (CDF, LERF) не говорят за езика на текста
    lat = sum(_is_lat(c) for w in _WORD.findall(text) if not (w.isascii() and w.isupper() and len(w) > 1)
              for c in w)
    if cyr > lat * 1.5:
        return "cyr"
    if lat > cyr * 1.5:
        return "lat"
    return None


def _fix_word(word: str, script: str | None, in_code: bool = False) -> str:
    cyr = sum(_is_cyr(c) for c in word)
    lat = sum(_is_lat(c) for c in word)
    if in_code:
        return word  # части от кодове като 6TS22S11 не пипаме
    if cyr and lat:  # смесена дума: уеднаквяваме към преобладаващата в нея
        target = "cyr" if cyr > lat or (cyr == lat and script == "cyr") else "lat"
    elif script and not word.isupper():  # цяла дума „от другата“ азбука
        target = script
    elif script == "cyr" and lat and len(word) <= 5:
        # Главни латински букви в кирилски текст („HE“, „KCK“, „CHEB“): превръщаме,
        # само ако всички имат кирилски двойник. Истински абревиатури като
        # CDF, LERF, PDF съдържат D, F, L, R и остават непокътнати.
        target = "cyr"
    else:
        return word
    table = _LAT2CYR_EXT if target == "cyr" else _CYR2LAT
    out = "".join(table.get(c, c) for c in word)
    # Ако не всички букви имат двойник, по-добре да оставим думата.
    ok = all(_is_cyr(c) for c in out) if target == "cyr" else all(_is_lat(c) for c in out)
    return out if ok else word


def fix_scripts(text: str, context: str | None = None) -> str:
    script = dominant_script(context if context is not None else text)

    def repl(m: re.Match) -> str:
        word = m.group(0)
        if script == "cyr" and word in _CYR_CONFUSIONS:
            return _CYR_CONFUSIONS[word]
        if script == "lat" and word in _LAT_CONFUSIONS:
            fixed = _LAT_CONFUSIONS[word]
            before = text[:m.start()].rstrip()
            if not before or before[-1] in ".!?":
                fixed = fixed.capitalize()
            return fixed
        before = text[m.start() - 1] if m.start() > 0 else ""
        after = text[m.end()] if m.end() < len(text) else ""
        in_code = before.isdigit() or after.isdigit()
        return _fix_word(word, script, in_code)

    text = _WORD.sub(repl, text)
    if script == "cyr":
        # „Р-л“ (ръководител) често се чете „P-n“
        text = re.sub(r"(?<![\w])[РP]-[nл](?![\w])", "Р-л", text)
        # „№ 14“ често се чете като „Ne 14“ / „No 14“
        text = re.sub(r"(?<![\w])(?:Ne|No|N°|N2|Nо|Nе)(\s?)(?=[\dА-ЯA-Z\[])", r"№\1", text)
    return text
