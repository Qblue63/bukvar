"""Стил на думите по изображението: удебелен, курсив, размер на шрифта.

Tesseract дава само текста и правоъгълника на всяка дума. Стила
измерваме сами върху изображението:

* **удебелен** — средна дебелина на щриха (2 × площ / периметър на мастилото),
  отнесена към размера на шрифта; удебеленият текст е с ~1,5 пъти по-дебел щрих;
* **курсив** — наклонът, при който вертикалните щрихи стават най-„остри“
  (проекцията по колони е най-контрастна);
* **размер** — височината на главните букви и буквите с горни елементи,
  превърната в пунктове.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

import cv2
import numpy as np

# Букви без долни елементи, но с главна или горен елемент — по тях мерим
# височината на главните букви (≈ 0,69 em за Times/Arial).
_ASC = set("АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЪЬЮЯABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789бbdfhklt")
# долни елементи или знаци над главните букви (Й, кавички) — не стават за мерене
_DESC = set("ДЦЩдрруфцщgjpqyQ,;()[]{}|/ЙЁйё\"“”„'‘’")
_CAP_EM = 0.70
_TALL = set("ЙЁйё\"“”„'‘’")
_LOW = set("ДЦЩдрруфцщgjpqy,;")
_SLANTS = (0.0, 0.1, 0.18, 0.26, 0.34)


@dataclass
class WordStyle:
    stroke: float = 0.0     # дебелина на щриха, px
    slant: float = 0.0      # tg на наклона (0 = прав, ~0.2 = курсив)
    cap: float | None = None  # височина на главните букви, px (ако думата е подходяща)
    bold: bool = False
    italic: bool = False
    underline: bool = False
    em: float = 0.0         # размер на шрифта (em) в px, определен за реда/клетката


def _shear_score(ink: np.ndarray, t: float) -> float:
    h, w = ink.shape
    if t:
        m = np.float32([[1, t, -t * h], [0, 1, 0]])
        ink = cv2.warpAffine(ink, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    col = ink.sum(axis=0, dtype=np.float64)
    s = col.sum()
    return float((col ** 2).sum() / (s * s)) if s else 0.0


def measure(gray: np.ndarray, left: int, top: int, width: int, height: int, text: str,
            thr: float) -> WordStyle:
    H, W = gray.shape
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(W, left + width), min(H, top + height)
    st = WordStyle()
    if x1 - x0 < 4 or y1 - y0 < 4:
        return st
    ink = (gray[y0:y1, x0:x1] < thr).astype(np.uint8)
    area = int(ink.sum())
    if area < 15:
        return st
    contours, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    perim = sum(cv2.arcLength(c, True) for c in contours)
    st.stroke = 2.0 * area / perim if perim else 0.0
    letters = [c for c in text if c.isalnum()]
    if len(letters) >= 3:
        scores = [(_shear_score(ink, t), t) for t in _SLANTS]
        base = scores[0][0]
        best, t = max(scores)
        st.slant = t if best > base * 1.04 else 0.0
    if letters:
        # височина на думата спрямо em според буквите в нея
        tall = any(c in _ASC or c in _TALL for c in text)
        low = any(c in _LOW for c in text)
        ratio = {(True, False): _CAP_EM, (True, True): 0.92,
                 (False, False): 0.47, (False, True): 0.69}[(tall, low)]
        st.cap = float(y1 - y0) / ratio * _CAP_EM      # приравнено към „главни букви“
    return st


def font_size_pt(cap_px: float, dpi: int) -> float:
    em_px = cap_px / _CAP_EM
    return em_px / dpi * 72.0


# размерите от падащото меню на Word
_SIZES = (6, 7, 8, 9, 10, 10.5, 11, 12, 14, 16, 18, 20, 22, 24, 26, 28, 36, 48, 72)


def snap_size(pt: float) -> float:
    return min(_SIZES, key=lambda s: abs(s - pt))


def classify(styles: list[WordStyle], sizes_px: list[float]) -> None:
    """Решава кои думи са удебелени/курсив спрямо останалите на страницата.

    sizes_px: размер на шрифта (em, px) за всяка дума — за да сравняваме
    дебелина на щриха при еднакъв размер."""
    # Сканирането и изглаждането добавят ~еднакъв брой пиксели към всеки щрих,
    # независимо от размера на шрифта; без тази поправка дребният текст
    # изглежда „по-удебелен“ от едрия.
    blur = 2.5

    def rel_of(s, e):
        return max(0.05, s.stroke - blur) / e

    rel = [rel_of(s, e) for s, e in zip(styles, sizes_px) if s.stroke and e]
    if len(rel) < 5:
        return
    # Обикновеният текст е мнозинството: долната част на разпределението
    base = float(np.percentile(rel, 35))
    for s, e in zip(styles, sizes_px):
        if s.stroke and e:
            s.bold = rel_of(s, e) > base * 1.45
        s.italic = s.slant >= 0.1


def smooth(flags: list[bool], lengths: list[int], hi: float = 0.5, lo: float = 0.3) -> list[bool]:
    """Изглажда стила в реда.

    Форматирането обикновено е на цели редове (заглавие, колонтитул): ако
    ≥ 70 % от буквите в реда са удебелени — целият ред е удебелен, ако ≤ 30 % —
    никой. Иначе само кратка дума („на“, „в“) със стил, различен от двете
    съседни, се смята за грешка на мерането."""
    total = sum(lengths)
    if total:
        share = sum(n for f, n in zip(flags, lengths) if f) / total
        if share >= hi:
            return [True] * len(flags)
        if share <= lo:
            return [False] * len(flags)
    out = list(flags)
    for i in range(len(flags)):
        if lengths[i] <= 3:
            left = flags[i - 1] if i > 0 else None
            right = flags[i + 1] if i + 1 < len(flags) else None
            neighbours = [x for x in (left, right) if x is not None]
            if neighbours and all(x != flags[i] for x in neighbours):
                out[i] = neighbours[0]
    return out


def median_or(values, default):
    values = [v for v in values if v]
    return statistics.median(values) if values else default


def _runs(b: np.ndarray) -> np.ndarray:
    d = np.diff(np.pad(b.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    starts = np.argwhere(d == 1)
    ends = np.argwhere(d == -1)
    return ends[:, 1] - starts[:, 1]


def is_serif(gray: np.ndarray, boxes: list[tuple[int, int, int, int]], thr: float) -> bool:
    """Шрифт със серифи (Times) или без (Arial)?

    При Times хоризонталните елементи са много по-тънки от вертикалните щрихи
    (висок контраст); при Arial дебелината е почти еднаква."""
    stems, bars = [], []
    for (x, y, w, h) in boxes[:120]:
        crop = gray[y:y + h, x:x + w] < thr
        if crop.size == 0:
            continue
        stems.extend(_runs(crop).tolist())
        bars.extend(_runs(crop.T).tolist())
    stems = np.array([v for v in stems if 1 < v < 40])
    bars = np.array([v for v in bars if 1 < v < 40])
    if len(stems) < 200 or len(bars) < 200:
        return True
    return np.bincount(bars).argmax() / np.bincount(stems).argmax() <= 0.7
