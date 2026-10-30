"""Подготовка на страницата за разпознаване: формуляри, ръкопис, точки.

Tesseract се справя добре с обикновен печатен текст, но на формуляри:
  * чете квадратчетата ☐ ☒ като „| HEX] [_]“ и обърква съседните думи;
  * опитва да разчете ръкописа и подписите и връща безсмислици;
  * пропуска цели части от страницата, ако в тях има редове от точки
    („……“ за попълване).

Тук намираме тези елементи, изтриваме ги от копието, което се подава на
Tesseract, и връщаме „псевдодуми“ (☐, ☒, [ръкопис]) с координати, които
после се вмъкват в текста на правилното място.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

CHECKED = "☒"
UNCHECKED = "☐"
HANDWRITING = "[ръкопис]"


@dataclass
class Extra:
    """Псевдодума, която не идва от Tesseract."""
    text: str
    left: int
    top: int
    width: int
    height: int

    @property
    def cx(self) -> float:
        return self.left + self.width / 2

    @property
    def cy(self) -> float:
        return self.top + self.height / 2


@dataclass
class CleanResult:
    image: Image.Image
    extras: list[Extra]
    stats: dict


def _colored_mask(rgb: np.ndarray) -> np.ndarray:
    """Цветно мастило (син химикал, червен печат), но не жълтеникава хартия."""
    a = rgb.astype(np.int16)
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    chroma = mx - mn
    blue = (a[..., 2] - np.maximum(a[..., 0], a[..., 1]) > 25) & (mn < 200)
    strong = (chroma > 60) & (mn < 190)
    return blue | strong


def _find_checkboxes(dark: np.ndarray, ink: np.ndarray, dpi: int) -> list[tuple[int, int, int, int, bool]]:
    """Квадратчета: (x, y, w, h, отметнато)."""
    s_min, s_max = int(0.06 * dpi), int(0.26 * dpi)       # ~1.5–6.5 мм
    n, lab, st, _ = cv2.connectedComponentsWithStats(dark.astype(np.uint8), 8)
    boxes = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if not (s_min <= w <= s_max and s_min <= h <= s_max):
            continue
        if not 0.75 <= w / h <= 1.33:
            continue
        comp = lab[y:y + h, x:x + w] == i
        t = max(2, int(min(w, h) * 0.15))                 # дебелина на рамката, която проверяваме
        sides = [comp[:t, :].any(axis=0).mean(), comp[-t:, :].any(axis=0).mean(),
                 comp[:, :t].any(axis=1).mean(), comp[:, -t:].any(axis=1).mean()]
        if min(sides) < 0.8:
            continue
        # Тънка рамка: в средните редове лявата третина е почти празна.
        # Удебелени букви (П, Н, И) имат дебели вертикални щрихи (≥0.2 от ширината).
        mid = comp[h // 3: 2 * h // 3]
        thickness = mid[:, : max(1, w // 3)].sum(axis=1).mean() / w
        if thickness > 0.14:
            continue
        # вътрешността не бива да е плътна (това би било ■ или буква)
        m = max(3, int(min(w, h) * 0.22))
        inner_comp = comp[m:-m, m:-m]
        inner_ink = ink[y + m:y + h - m, x + m:x + w - m]
        if inner_comp.size == 0:
            continue
        if inner_comp.mean() > 0.6:
            continue
        # отметка: черна (част от компонента) или синя (отделна)
        checked = inner_ink.mean() > 0.05
        boxes.append((int(x), int(y), int(w), int(h), bool(checked)))
    return boxes


def _dotted_leaders(dark: np.ndarray, dpi: int) -> np.ndarray:
    """Маска на редове от точки („…………“), които Tesseract не обича."""
    n, lab, st, cen = cv2.connectedComponentsWithStats(dark.astype(np.uint8), 8)
    max_dot = max(4, int(0.05 * dpi))
    min_area = max(4, int((0.01 * dpi) ** 2))          # по-малките са прах от скенера
    specks = [i for i in range(1, n) if st[i, 4] < min_area]
    solid = dark.copy()
    if specks:
        solid[np.isin(lab, specks)] = False
    small = [i for i in range(1, n)
             if st[i, 2] <= max_dot and st[i, 3] <= max_dot and min_area <= st[i, 4] <= max_dot * max_dot]
    if not small:
        return np.zeros_like(dark, dtype=bool)
    small.sort(key=lambda i: (cen[i, 1], cen[i, 0]))
    # Групираме по ред (точки на една височина)
    rows: list[list[int]] = []
    tol = max(3, int(0.02 * dpi))
    for i in small:
        for row in rows:
            if abs(cen[row[-1], 1] - cen[i, 1]) <= tol:
                row.append(i)
                break
        else:
            rows.append([i])
    gap = int(0.09 * dpi)   # точките в „……“ са през ~1/3 em (≈ 17 px при 300 dpi)

    def clear_between(a: int, b: int) -> bool:
        """Между две точки няма друго мастило (иначе са запетаи между думи)."""
        x0 = st[a, 0] + st[a, 2]
        x1 = st[b, 0]
        if x1 <= x0:
            return True
        y0 = min(st[a, 1], st[b, 1]) - 1
        y1 = max(st[a, 1] + st[a, 3], st[b, 1] + st[b, 3]) + 1
        return not solid[max(0, y0):y1, x0:x1].any()

    def accept(run: list[int]) -> bool:
        if len(run) < 8:
            return False
        gaps = np.diff([cen[i, 0] for i in run])
        med = float(np.median(gaps))
        regular = np.abs(gaps - med) <= 0.3 * med     # равномерни разстояния
        return med > 0 and regular.mean() >= 0.8

    keep = []
    for row in rows:
        row.sort(key=lambda i: cen[i, 0])
        run = [row[0]]
        for a, b in zip(row, row[1:]):
            if cen[b, 0] - cen[a, 0] <= gap and clear_between(a, b):
                run.append(b)
            else:
                if accept(run):
                    keep += run
                run = [b]
        if accept(run):
            keep += run
    if not keep:
        return np.zeros_like(dark, dtype=bool)
    return np.isin(lab, keep)


def _handwriting_regions(colored: np.ndarray, skip: list[tuple[int, int, int, int]], dpi: int):
    """Групи от цветно мастило (подписи, ръкописни бележки)."""
    mask = colored.astype(np.uint8)
    for x, y, w, h in skip:                 # отметки в квадратчета не са ръкопис
        pad = int(w * 0.6)
        mask[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad] = 0
    if mask.sum() < 50:
        return []
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (int(0.12 * dpi), int(0.05 * dpi)))
    merged = cv2.dilate(mask, k)
    n, lab, st, _ = cv2.connectedComponentsWithStats(merged, 8)
    regions = []
    min_w, min_h = int(0.12 * dpi), int(0.06 * dpi)
    for i in range(1, n):
        x, y, w, h, _ = st[i]
        real = mask[y:y + h, x:x + w].sum()
        if (w >= min_w or h >= min_h) and real >= 0.0004 * dpi * dpi:
            regions.append((int(x), int(y), int(w), int(h)))
    return regions


def clean_for_ocr(img: Image.Image, dpi: int = 300, remove_handwriting: bool = True) -> CleanResult:
    rgb = np.asarray(img.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    colored = _colored_mask(rgb) if remove_handwriting else np.zeros(gray.shape, bool)
    # праг за „мастило“: Otsu, но не по-светъл от 200
    thr, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    thr = min(float(thr), 200.0)
    ink = gray < thr
    dark = ink & ~colored

    out = gray.copy()
    extras: list[Extra] = []

    boxes = _find_checkboxes(dark, ink, dpi)
    for x, y, w, h, checked in boxes:
        pad = max(2, int(w * 0.12))
        out[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad] = 255
        extras.append(Extra(CHECKED if checked else UNCHECKED, x, y, w, h))

    hw = []
    if remove_handwriting and colored.any():
        hw = _handwriting_regions(colored, [(b[0], b[1], b[2], b[3]) for b in boxes], dpi)
        # изтриваме цялото цветно мастило (и тънкия ореол около него)
        grown = cv2.dilate(colored.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        out[grown] = 255
        # Около мастилото остава светъл „ореол“ и кухи точки — изчистваме всичко,
        # което не е ясно черен печат.
        zone = cv2.dilate(colored.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
        out[zone & (gray > min(thr, 140))] = 255
        for x, y, w, h in hw:
            extras.append(Extra(HANDWRITING, x, y, w, h))

    # Точките търсим след изтриването на ръкописа — така хващаме и тези под подписите.
    dots = _dotted_leaders(dark, dpi) | _dotted_leaders(out < thr, dpi)
    out[dots] = 255

    stats = {"checkboxes": len(boxes), "checked": sum(b[4] for b in boxes),
             "dots": int(dots.sum() > 0), "handwriting": len(hw)}
    return CleanResult(Image.fromarray(out), extras, stats)
