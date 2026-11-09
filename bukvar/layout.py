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
    tables: list = None  # list[TableGrid]; таблица с една клетка = рамка
    rules: list = None   # самостоятелни линии (x0, x1, y): подчертаване/разделител


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


# --------------------------------------------------------------------------
# Таблици с линии
# --------------------------------------------------------------------------

@dataclass
class TableGrid:
    """Таблица: граници на редовете (ys) и колоните (xs) и клетките.

    Всяка клетка е (r0, c0, r1, c1) — първи/последен ред и колона включително;
    слетите клетки обхващат повече от един ред или колона.
    """
    xs: list[int]
    ys: list[int]
    cells: list[tuple[int, int, int, int]]

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.xs[0], self.ys[0], self.xs[-1], self.ys[-1]

    def cell_rect(self, cell) -> tuple[int, int, int, int]:
        r0, c0, r1, c1 = cell
        return self.xs[c0], self.ys[r0], self.xs[c1 + 1], self.ys[r1 + 1]

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.bbox
        return x0 <= x <= x1 and y0 <= y <= y1

    def cell_at(self, x: float, y: float):
        for cell in self.cells:
            x0, y0, x1, y1 = self.cell_rect(cell)
            if x0 <= x < x1 and y0 <= y < y1:
                return cell
        return None


def _cluster(values: list[float], tol: float) -> list[int]:
    values = sorted(values)
    groups: list[list[float]] = []
    for v in values:
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [int(round(sum(g) / len(g))) for g in groups]


def _line_masks(gray: np.ndarray, colored: np.ndarray, dpi: int) -> tuple[np.ndarray, np.ndarray]:
    """Хоризонтални и вертикални линии на таблици.

    Линиите често са тънки (1 px) и сиви, затова прагът е по-светъл от този
    за текста. Текстът се отсява по дебелина: линията е тънка, а ред
    удебелен текст, „слепен“ от морфологията, е дебела ивица.
    """
    lm = ((gray < 215) & ~colored).astype(np.uint8) * 255
    max_thick = max(4.0, 0.022 * dpi)

    def directional(close_k, open_k, axis, max_thick=max_thick):
        closed = cv2.morphologyEx(lm, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, close_k))
        opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, open_k))
        n, lab, st, _ = cv2.connectedComponentsWithStats((opened > 0).astype(np.uint8), 8)
        length = st[:, 2] if axis == 0 else st[:, 3]
        thick = st[:, 4] / np.maximum(1, length)
        keep = thick <= max_thick
        keep[0] = False
        return keep[lab], lab, st, keep

    # Малко „затваряне“ само за прекъсвания от скенера; по-голямо би слепило
    # буквите в тесни клетки с линиите над и под тях.
    h, _, _, _ = directional((7, 1), (int(0.3 * dpi), 1), 0)
    v, lab, st, keep = directional((1, 5), (1, int(0.14 * dpi)), 1)

    # Истинската вертикална линия свързва хоризонтални линии и отгоре, и отдолу.
    # Щрихите на буквите (Т, П, И, подчертано заглавие) докосват линия най-много от едната страна.
    reach = max(6, int(0.03 * dpi))
    hgrow = cv2.dilate(h.astype(np.uint8), np.ones((2 * reach + 1, 2 * reach + 1), np.uint8)) > 0
    for i in np.nonzero(keep)[0]:
        x, y, w, hh, _ = st[i]
        top = hgrow[y:y + 3, x:x + w].any()
        bottom = hgrow[y + hh - 3:y + hh, x:x + w].any()
        if not (top and bottom):
            keep[i] = False
    v = keep[lab]
    # за самостоятелни линии (разделители) допускаме и дебели линии; те после
    # се проверяват за непрекъснатост
    _line_masks.thick_h = directional((7, 1), (int(0.3 * dpi), 1), 0, max(8.0, 0.045 * dpi))[0]
    return h, v


def find_tables(gray: np.ndarray, colored: np.ndarray, dpi: int = 300) -> tuple[list[TableGrid], np.ndarray]:
    """Намира таблици с линии. Връща таблиците и маската на линиите им."""
    H, W = gray.shape
    hmask, vmask = _line_masks(gray, colored, dpi)
    lines = (hmask | vmask).astype(np.uint8)
    region = cv2.dilate(lines, np.ones((9, 9), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(region, 8)
    tol = max(4, int(0.03 * dpi))
    tables: list[TableGrid] = []
    used = np.zeros(gray.shape, dtype=bool)

    for i in range(1, n):
        x, y, w, h, _ = st[i]
        if w < 0.1 * W or h < 0.08 * dpi:
            continue
        sub = lab[y:y + h, x:x + w] == i
        hs = hmask[y:y + h, x:x + w] & sub
        vs = vmask[y:y + h, x:x + w] & sub

        # Позиции на линиите
        hn, hl, hst, hcen = cv2.connectedComponentsWithStats(hs.astype(np.uint8), 8)
        vn, vl, vst, vcen = cv2.connectedComponentsWithStats(vs.astype(np.uint8), 8)
        min_gap = int(0.06 * dpi)    # по-близки линии са една (двойна/дебела) линия
        ys = _cluster([y + hcen[j, 1] for j in range(1, hn) if hst[j, 2] >= 0.2 * w or hst[j, 2] >= dpi], min_gap)
        xs = _cluster([x + vcen[j, 0] for j in range(1, vn)], min_gap)
        if len(ys) < 2 or len(xs) < 2:
            continue      # таблица = поне две хоризонтални и две вертикални линии
        # Външните граници: ако няма вертикална линия в края, ползваме края на хоризонталните
        if xs[0] - x > min_gap:
            xs.insert(0, int(x))
        if x + w - xs[-1] > min_gap:
            xs.append(int(x + w))
        if ys[0] - y > min_gap:
            ys.insert(0, int(y))
        if y + h - ys[-1] > min_gap:
            ys.append(int(y + h))
        ys = _cluster(ys, min_gap)
        xs = _cluster(xs, min_gap)
        nr, nc = len(ys) - 1, len(xs) - 1
        if nr < 1 or nc < 1:
            continue
        if nr * nc == 1 and h > 0.4 * H:
            continue    # рамка около цялата страница — не е таблица
        # Ред от таблица е по-висок от ред текст (иначе това са ръбовете на
        # удебелени букви в заглавие).
        if max(b - a for a, b in zip(ys, ys[1:])) < 0.13 * dpi:
            continue
        # Горната и долната граница са непрекъснати линии по цялата ширина.
        def coverage(yc):
            band = hmask[max(0, yc - tol):yc + tol + 1, xs[0]:xs[-1]]
            return band.any(axis=0).mean() if band.size else 0.0
        if coverage(ys[0]) < 0.7 or coverage(ys[-1]) < 0.7:
            continue

        def vcover(xc, y0, y1):
            pad = max(2, (y1 - y0) // 6)
            seg = vmask[y0 + pad:y1 - pad, max(0, xc - tol):xc + tol + 1]
            return seg.any(axis=1).mean() if seg.size else 0.0

        def hcover(yc, x0, x1):
            pad = max(2, (x1 - x0) // 8)
            seg = hmask[max(0, yc - tol):yc + tol + 1, x0 + pad:x1 - pad]
            return seg.any(axis=0).mean() if seg.size else 0.0

        # Слети клетки: липсва линия между съседни клетки
        parent = list(range(nr * nc))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            parent[find(a)] = find(b)

        for r in range(nr):
            for c in range(nc - 1):
                if vcover(xs[c + 1], ys[r], ys[r + 1]) < 0.5:
                    union(r * nc + c, r * nc + c + 1)
        for r in range(nr - 1):
            for c in range(nc):
                if hcover(ys[r + 1], xs[c], xs[c + 1]) < 0.5:
                    union(r * nc + c, (r + 1) * nc + c)
        groups: dict[int, list[tuple[int, int]]] = {}
        for r in range(nr):
            for c in range(nc):
                groups.setdefault(find(r * nc + c), []).append((r, c))
        cells = []
        for g in groups.values():
            rs = [r for r, _ in g]
            cs = [c for _, c in g]
            cells.append((min(rs), min(cs), max(rs), max(cs)))
        cells.sort()
        tables.append(TableGrid(xs, ys, cells))
        # Трием само отсечките, които лежат на границите на мрежата. Основите на
        # буквите в един ред също образуват тънка хоризонтална ивица, но тя е вътре в клетката.
        on_h = np.zeros(hn, bool)
        for j in range(1, hn):
            if min(abs(y + hcen[j, 1] - yy) for yy in ys) <= tol:
                on_h[j] = True
        on_v = np.zeros(vn, bool)
        for j in range(1, vn):
            if min(abs(x + vcen[j, 0] - xx) for xx in xs) <= tol:
                on_v[j] = True
        used[y:y + h, x:x + w] |= on_h[hl] | on_v[vl]

    # Самостоятелни хоризонтални линии: подчертаване или разделителна линия.
    # Истинската линия е непрекъсната още в суровото изображение — така не
    # бъркаме основите на буквите в един ред, които морфологията „слепва“.
    raw = (gray < 215) & ~colored
    rules = []
    rule_px = np.zeros(gray.shape, dtype=bool)
    rn, rl, rst, _ = cv2.connectedComponentsWithStats((_line_masks.thick_h & ~used).astype(np.uint8), 8)
    for j in range(1, rn):
        x, y, w, h, _ = rst[j]
        if w < 0.3 * dpi:
            continue
        best = 0
        for yy in range(y, y + h):
            row = raw[yy, x:x + w]
            d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
            starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
            if len(starts):
                best = max(best, int((ends - starts).max()))
        if best >= 0.9 * w:
            rules.append((int(x), int(x + w), int(y + h // 2)))
            rule_px[y:y + h, x:x + w] |= rl[y:y + h, x:x + w] == j

    # Линиите на таблиците трием с малък ореол; подчертаването — само самата
    # линия (с 1 px), за да не отрежем долните краища на буквите над нея.
    line_mask = cv2.dilate(used.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    # Трием само самата линия. Ако тя е малко под редa и я докосват само долните
    # елементи на буквите („Д“, „р“) — под тях я оставяме, за да не ги срежем.
    # Ако е точно под реда (докосва повечето букви) — трием я цялата, освен
    # където буква я пресича (мастило и над, и под линията).
    rule_grown = cv2.dilate(rule_px.astype(np.uint8), np.ones((3, 1), np.uint8)) > 0
    if rule_px.any():
        n_r, lab_r = cv2.connectedComponents(rule_grown.astype(np.uint8), 8)
        for j in range(1, n_r):
            comp = lab_r == j
            cols = np.nonzero(comp.any(axis=0))[0]
            tops, bottoms = {}, {}
            for xx in cols:
                yy = np.nonzero(comp[:, xx])[0]
                tops[xx], bottoms[xx] = yy.min(), yy.max()
            touched = [xx for xx in cols if tops[xx] > 0 and raw[tops[xx] - 1, xx]]
            only_descenders = len(touched) < 0.35 * len(cols)
            for xx in cols:
                t, b = tops[xx], bottoms[xx]
                above = t > 0 and raw[t - 1, xx]
                below = b + 1 < raw.shape[0] and raw[b + 1, xx]
                if (only_descenders and above) or (above and below):
                    rule_grown[t:b + 1, xx] = False
    line_mask |= rule_grown
    tables.sort(key=lambda t: (t.ys[0], t.xs[0]))
    find_tables.last_rules = rules
    return tables, line_mask


def clean_for_ocr(img: Image.Image, dpi: int = 300, remove_handwriting: bool = True,
                  forms: bool = True, tables: bool = True) -> CleanResult:
    rgb = np.asarray(img.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    remove_handwriting = remove_handwriting and forms
    colored = _colored_mask(rgb) if remove_handwriting else np.zeros(gray.shape, bool)
    # праг за „мастило“: Otsu, но не по-светъл от 200
    thr, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    thr = min(float(thr), 200.0)
    ink = gray < thr
    dark = ink & ~colored

    out = gray.copy()
    extras: list[Extra] = []

    grids: list[TableGrid] = []
    rules: list = []
    if tables:
        grids, line_mask = find_tables(gray, colored, dpi)
        rules = find_tables.last_rules
        out[line_mask] = 255           # линиите пречат на Tesseract
        dark = dark & ~line_mask
        ink = ink & ~line_mask

    if not forms:
        return CleanResult(Image.fromarray(out), extras, {"tables": len(grids)}, grids, rules)

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
             "dots": int(dots.sum() > 0), "handwriting": len(hw), "tables": len(grids)}
    return CleanResult(Image.fromarray(out), extras, stats, grids, rules)
