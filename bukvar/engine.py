"""Буквар — OCR двигател.

Зарежда PDF и изображения, подготвя страниците (завъртане, изправяне),
разпознава текста с Tesseract и записва търсим PDF, DOCX и TXT.
"""
from __future__ import annotations

import atexit
import io
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter, Transformation

from .layout import clean_for_ocr
from .style import classify, font_size_pt, is_serif, measure, smooth, snap_size
from .textfix import fix_scripts

RENDER_DPI = 300
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".jp2"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS

LANG_NAMES = {
    "bul": "Български",
    "eng": "Английски",
    "rus": "Руски",
    "ukr": "Украински",
    "srp": "Сръбски",
    "mkd": "Македонски",
    "deu": "Немски",
    "fra": "Френски",
    "ita": "Италиански",
    "spa": "Испански",
    "tur": "Турски",
    "ell": "Гръцки",
    "ron": "Румънски",
}


# --------------------------------------------------------------------------
# Настройка на Tesseract (вграден в програмата или инсталиран отделно)
# --------------------------------------------------------------------------

def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def configure_tesseract() -> tuple[str | None, str | None]:
    """Намира tesseract и папката с езиковите данни. Връща (exe, tessdata)."""
    exe_name = "tesseract.exe" if os.name == "nt" else "tesseract"
    candidates = [
        _app_dir() / "tesseract" / exe_name,
        Path(sys.executable).parent / "tesseract" / exe_name,
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / exe_name,
    ]
    exe = next((str(p) for p in candidates if p.is_file()), None) or shutil.which("tesseract")
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe

    tess_candidates = [
        _app_dir() / "tessdata",
        _app_dir() / "tesseract" / "tessdata",
    ]
    if exe:
        tess_candidates.append(Path(exe).parent / "tessdata")
    tessdata = next((str(p) for p in tess_candidates if (p / "eng.traineddata").is_file()), None)
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    # Tesseract работи по-бързо, когато ние паралелизираме по страници.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    return exe, tessdata


def available_languages() -> list[str]:
    try:
        langs = pytesseract.get_languages(config="")
    except Exception:
        return []
    return [l for l in langs if l not in ("osd", "equ")]


# --------------------------------------------------------------------------
# Модел на документа
# --------------------------------------------------------------------------

@dataclass
class Run:
    """Парче текст с еднакъв стил."""
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class Paragraph:
    text: str
    size: float          # средна височина на реда в пиксели
    heading: bool = False
    order: float = 0.0   # място в страницата (за подреждане спрямо таблиците)
    # --- форматиране (за Word) ---
    runs: list = field(default_factory=list)   # list[Run]; празно = само text
    size_pt: float | None = None
    align: str = "left"           # left | center | right | justify
    indent_px: float = 0.0        # ляв отстъп от полето
    first_px: float = 0.0         # отстъп на първия ред спрямо останалите
    tabs: list = field(default_factory=list)   # [(позиция px от полето, left|center|right)]
    frame: int = -1               # номер на рамката, в която е абзацът
    border_top: bool = False      # разделителна линия над абзаца
    space_before_pt: float = 0.0
    top: int = 0
    bottom: int = 0


@dataclass
class TableCell:
    row: int
    col: int
    rowspan: int
    colspan: int
    text: str = ""
    bold: bool = False
    italic: bool = False
    align: str = "left"
    size_pt: float | None = None


@dataclass
class TableBlock:
    n_rows: int
    n_cols: int
    cells: list[TableCell]
    col_widths: list[float]   # относителни ширини на колоните
    order: float = 0.0
    left_px: int = 0           # положение и ширина в страницата (px)
    width_px: int = 0
    top: int = 0
    bottom: int = 0
    space_before_pt: float = 0.0

    def rows_text(self) -> list[list[str]]:
        grid = [["" for _ in range(self.n_cols)] for _ in range(self.n_rows)]
        for c in self.cells:
            grid[c.row][c.col] = c.text.replace("\n", " ")
        return grid

    @property
    def text(self) -> str:
        return "\n".join("\t".join(r) for r in self.rows_text())


_TMP_DIR: Path | None = None
_pdfium_lock = threading.Lock()   # pdfium не е безопасен при паралелни нишки


_tmp_lock = threading.Lock()


def _tmp_dir() -> Path:
    global _TMP_DIR
    with _tmp_lock:
        if _TMP_DIR is None:
            _TMP_DIR = Path(tempfile.mkdtemp(prefix="bukvar_"))
            atexit.register(shutil.rmtree, _TMP_DIR, True)
    return _TMP_DIR


def _render_pdf_page(path: Path, index: int, dpi: float) -> Image.Image:
    with _pdfium_lock:
        pdf = pdfium.PdfDocument(str(path))
        try:
            return pdf[index].render(scale=dpi / 72).to_pil().convert("RGB")
        finally:
            pdf.close()


def _open_image_frame(path: Path, index: int) -> Image.Image:
    with Image.open(path) as im:
        im.seek(index)
        return ImageOps.exif_transpose(im.copy()).convert("RGB")


@dataclass
class Page:
    """Една страница. Изображението се зарежда при нужда, не се пази в паметта."""
    source: Path
    index: int                      # номер на страницата в източника (от 0)
    is_pdf: bool = False
    processed_path: Path | None = None  # подготвено (завъртяно/изправено) изображение
    geometry_changed: bool = False
    text: str = ""
    paragraphs: list = field(default_factory=list)   # Paragraph | TableBlock
    text_layer_pdf: bytes | None = None   # невидим текстов слой
    confidence: float | None = None
    geom: dict = field(default_factory=dict)   # полета, шрифт и размер — за Word
    text_edited: bool = False
    done: bool = False
    error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.source.name} — стр. {self.index + 1}"

    def original_image(self, dpi: float = RENDER_DPI) -> Image.Image:
        if self.is_pdf:
            return _render_pdf_page(self.source, self.index, dpi)
        img = _open_image_frame(self.source, self.index)
        if dpi != RENDER_DPI:
            img.thumbnail((int(img.width * dpi / RENDER_DPI), int(img.height * dpi / RENDER_DPI)))
        return img

    def image(self) -> Image.Image:
        """Изображението, което се разпознава (подготвеното, ако има такова)."""
        if self.processed_path and self.processed_path.exists():
            with Image.open(self.processed_path) as im:
                return im.convert("RGB")
        return self.original_image()

    def thumbnail(self, size: int = 200) -> Image.Image:
        img = self.original_image(dpi=40) if self.is_pdf else self.image()
        img.thumbnail((size, size))
        return img

    def reset(self) -> None:
        self.processed_path = None
        self.geometry_changed = False
        self.text, self.paragraphs, self.text_layer_pdf = "", [], None
        self.confidence, self.text_edited, self.done, self.error = None, False, False, None


def load_document(path: str | Path) -> list[Page]:
    """Бързо: само преброява страниците, без да ги изобразява."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        with _pdfium_lock:
            pdf = pdfium.PdfDocument(str(path))
            n = len(pdf)
            pdf.close()
        return [Page(path, i, is_pdf=True) for i in range(n)]
    if ext in IMAGE_EXTS:
        with Image.open(path) as im:
            n = getattr(im, "n_frames", 1)
        return [Page(path, i) for i in range(n)]
    raise ValueError(f"Неподдържан формат: {path.suffix}")


# --------------------------------------------------------------------------
# Предварителна обработка
# --------------------------------------------------------------------------

def _detect_rotation(img: Image.Image) -> int:
    """Завъртане на 0/90/180/270° по данни от Tesseract OSD."""
    try:
        small = img.copy()
        small.thumbnail((2000, 2000))
        osd = pytesseract.image_to_osd(small, config="--psm 0 -c min_characters_to_try=5")
    except Exception:
        return 0
    m = re.search(r"Rotate:\s*(\d+)", osd)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", osd)
    if m and conf and float(conf.group(1)) >= 2.0:
        return int(m.group(1)) % 360
    return 0


def _otsu(arr: np.ndarray) -> int:
    hist = np.bincount(arr.ravel(), minlength=256).astype(float)
    total = arr.size
    cum = np.cumsum(hist)
    cum_mean = np.cumsum(hist * np.arange(256))
    w0 = cum / total
    w1 = 1 - w0
    with np.errstate(divide="ignore", invalid="ignore"):
        m0 = cum_mean / cum
        m1 = (cum_mean[-1] - cum_mean) / (total - cum)
        between = w0 * w1 * (m0 - m1) ** 2
    return int(np.nanargmax(between))


def _detect_skew(img: Image.Image) -> float:
    """Ъгъл (в градуси), с който img.rotate() изправя редовете.

    Метод на проекциите: завъртаме умалено копие под различни ъгли и
    избираме този, при който сумите по редове са най-„остри“ —
    тогава редовете текст са строго хоризонтални.
    """
    g = img.convert("L")
    g.thumbnail((1000, 1000))
    arr = np.asarray(g)
    ink = (arr < _otsu(arr)).astype(np.uint8) * 255
    if ink.mean() < 0.5:  # почти празна страница
        return 0.0
    bw = Image.fromarray(ink)

    def score(angle: float) -> float:
        rows = np.asarray(bw.rotate(angle, resample=Image.BILINEAR, fillcolor=0)).sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(rows) ** 2))

    def best_of(angles) -> tuple[float, float]:
        scores = [(score(a), a) for a in angles]
        return max(scores)

    base = score(0.0)
    s1, a1 = best_of(np.arange(-10, 10.01, 0.5))
    s2, a2 = best_of(np.arange(a1 - 0.5, a1 + 0.51, 0.05))
    if s2 < base * 1.02:  # няма ясен наклон — не пипаме изображението
        return 0.0
    return round(float(a2), 2)


def preprocess(page: Page, auto_rotate: bool = True, deskew: bool = True) -> Image.Image:
    img = page.original_image()
    changed = False
    if auto_rotate:
        rot = _detect_rotation(img)
        if rot:
            img = img.rotate(-rot, expand=True, fillcolor="white")
            changed = True
    if deskew:
        angle = _detect_skew(img)
        if abs(angle) >= 0.3:
            img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor="white")
            changed = True
    page.geometry_changed = changed
    if changed:
        page.processed_path = _tmp_dir() / f"{id(page)}_{page.index}.png"
        img.save(page.processed_path, dpi=(RENDER_DPI, RENDER_DPI))
    else:
        page.processed_path = None
    return img


# --------------------------------------------------------------------------
# Разпознаване
# --------------------------------------------------------------------------

@dataclass
class _Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float
    style: object = None   # style.WordStyle

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass
class _Line:
    block: int
    par: int
    order: float
    words: list[_Word]
    frame: int = -1
    size_pt: float | None = None

    @property
    def left(self) -> int:
        return min(w.left for w in self.words)

    @property
    def right(self) -> int:
        return max(w.right for w in self.words)

    @property
    def top(self) -> int:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> int:
        return max(w.bottom for w in self.words)

    @property
    def height(self) -> float:
        real = [w.height for w in self.words if w.conf >= 0]
        return float(statistics.median(real or [w.height for w in self.words]))

    def text(self) -> tuple[str, bool]:
        """Текст на реда. Големите празнини (колони на формуляр) стават табулации."""
        words = sorted(self.words, key=lambda w: w.left)
        h = self.height
        out, tabbed = words[0].text, False
        for a, b in zip(words, words[1:]):
            gap = b.left - a.right
            if gap > 2.5 * h:
                out += "\t" + b.text
                tabbed = True
            else:
                out += " " + b.text
        return out, tabbed

    def segments(self) -> list[list[_Word]]:
        """Части на реда, разделени с табулация."""
        words = sorted(self.words, key=lambda w: w.left)
        h = self.height
        segs = [[words[0]]]
        for a, b in zip(words, words[1:]):
            if b.left - a.right > 2.5 * h:
                segs.append([b])
            else:
                segs[-1].append(b)
        return segs

    def styles(self) -> list:
        return [w.style for w in sorted(self.words, key=lambda w: w.left)]


_NUMBERED = re.compile(r"^(\d+(\.\d+)*\.?|[IVX]+\.|[а-яa-z]\)|[-•–—*])\s")
_MARKS = ("☐", "☒", "[ръкопис]")
_EXTRA_CONF = 100.0   # псевдодумите (☐ ☒ [ръкопис]) са сигурни


def _collapse_marks(text: str) -> str:
    text = re.sub(r"(\[ръкопис\][\s:.,]*){2,}", "[ръкопис] ", text).rstrip()
    return text


def _lines_from_tsv(data: dict) -> list[_Line]:
    lines: dict[tuple, _Line] = {}
    order = 0
    for i, word in enumerate(data["text"]):
        if not word or not word.strip() or data["conf"][i] < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = _Line(key[0], key[1], float(order), [])
            order += 1
        lines[key].words.append(_Word(word, data["left"][i], data["top"][i],
                                      data["width"][i], data["height"][i], data["conf"][i]))
    return list(lines.values())


def _drop_noise_words(words: list[_Word]) -> list[_Word]:
    line = _Line(0, 0, 0.0, list(words))
    kept = _drop_noise([line]) if words else []
    return kept[0].words if kept else []


def _drop_noise(lines: list[_Line]) -> list[_Line]:
    """Маха остатъци от ръкопис/печати: думи с много ниска увереност."""
    kept = []
    for ln in lines:
        def noise(w: _Word) -> bool:
            if w.conf >= 30:
                return False
            alnum = sum(ch.isalnum() for ch in w.text)
            # кодове и номера („30.ПП.00.МТ.11/7“) пазим дори при ниска увереност
            if any(ch.isdigit() for ch in w.text) and alnum >= 3:
                return False
            return alnum <= 2 or alnum < len(w.text) / 2
        ws = [w for w in ln.words if not noise(w)]
        # ред само от препинателни знаци и черти („.....“, „| |“) — шум
        if ws and any(ch.isalnum() for w in ws for ch in w.text):
            ln.words = ws
            kept.append(ln)
    return kept


def _merge_rows(lines: list[_Line]) -> list[_Line]:
    """Кратки парчета текст вдясно на същия ред (колона „ДА / НЕ“ на формуляр)
    се присъединяват към реда отляво. Дългите редове (истински колони) не пипаме."""
    def short(ln: _Line) -> bool:
        return len(ln.words) <= 3 and sum(len(w.text) for w in ln.words) <= 24

    changed = True
    while changed:
        changed = False
        for ln in sorted(lines, key=lambda l: -l.left):
            if not short(ln):
                continue
            best = None
            for other in lines:
                if other is ln or other.right > ln.left + 5:
                    continue
                overlap = min(ln.bottom, other.bottom) - max(ln.top, other.top)
                if overlap >= 0.5 * min(ln.bottom - ln.top, other.bottom - other.top):
                    if best is None or other.right > best.right:
                        best = other
            if best is not None and (best.block != ln.block or best.par != ln.par):
                best.words.extend(ln.words)
                lines.remove(ln)
                changed = True
                break
    return lines


def _insert_extras(lines: list[_Line], extras) -> None:
    for e in extras:
        word = _Word(e.text, e.left, e.top, e.width, e.height, _EXTRA_CONF)
        best, best_dist = None, None
        for ln in lines:
            pad = 0.35 * ln.height
            if ln.top - pad <= e.cy <= ln.bottom + pad:
                dist = 0 if ln.left <= e.cx <= ln.right else min(abs(e.cx - ln.left), abs(e.cx - ln.right))
                if best is None or dist < best_dist:
                    best, best_dist = ln, dist
        if best is not None and best_dist < 1200:
            best.words.append(word)
            continue
        # Самостоятелен ред: след най-близкия ред над него
        above = [ln for ln in lines if ln.bottom <= e.cy]
        if above:
            ref = min(above, key=lambda ln: (e.cy - ln.bottom) + 0.2 * abs(e.cx - (ln.left + ln.right) / 2))
            lines.append(_Line(ref.block, -1, ref.order + 0.5 + 0.001 * len(lines), [word]))
        else:
            lines.append(_Line(-1, -1, -1 + 0.001 * len(lines), [word]))


def _style_key(st) -> tuple[bool, bool, bool]:
    return (bool(st.bold), bool(st.italic), bool(st.underline)) if st is not None else (False, False, False)


def _line_runs(text: str, styles: list) -> list[Run]:
    """Разделя поправения текст на реда на парчета със стила на думите."""
    parts = re.split(r"(\s+)", text)
    words, seps = parts[0::2], parts[1::2]
    keys = [_style_key(st) for st in styles]
    if len(words) != len(keys) or not keys:
        # броят на думите се е сменил (слети знаци и пр.) — стилът на мнозинството
        if keys:
            maj = tuple(sum(k[i] for k in keys) * 2 > len(keys) for i in range(3))
        else:
            maj = (False, False, False)
        return [Run(text, *maj)]
    runs: list[Run] = []

    def add(t, key):
        if runs and (runs[-1].bold, runs[-1].italic, runs[-1].underline) == key:
            runs[-1].text += t
        else:
            runs.append(Run(t, *key))

    for i, w in enumerate(words):
        if i > 0:
            a, b = keys[i - 1], keys[i]
            add(seps[i - 1], tuple(x and y for x, y in zip(a, b)))
        add(w, keys[i])
    return runs


def _join_runs(runs: list[Run], more: list[Run]) -> None:
    """Добавя реда към абзаца: с интервал или слепвайки пренесена дума."""
    if not more:
        return
    if not runs:
        runs.extend(more)
        return
    last, first = runs[-1], more[0]
    if (last.text.endswith("-") and first.text[:1].islower() and len(last.text) > 1
            and last.text[-2].isalpha()):
        last.text = last.text[:-1]
    else:
        key = tuple(x and y for x, y in zip((last.bold, last.italic, last.underline),
                                            (first.bold, first.italic, first.underline)))
        if (last.bold, last.italic, last.underline) == key:
            last.text += " "
        else:
            runs.append(Run(" ", *key))
    for r in more:
        if runs and (runs[-1].bold, runs[-1].italic, runs[-1].underline) == (r.bold, r.italic, r.underline):
            runs[-1].text += r.text
        else:
            runs.append(Run(r.text, r.bold, r.italic, r.underline))


def _all_bold(ln: _Line) -> bool:
    sts = [w.style for w in ln.words if w.style is not None and sum(c.isalpha() for c in w.text) >= 2]
    return bool(sts) and sum(st.bold for st in sts) * 3 >= len(sts) * 2


def _paragraph_geometry(par: Paragraph, plines: list[_Line], box) -> None:
    par.top = min(ln.top for ln in plines)
    par.bottom = max(ln.bottom for ln in plines)
    sizes = [ln.size_pt for ln in plines if ln.size_pt]
    par.size_pt = snap_size(statistics.median(sizes)) if sizes else None
    if not box:
        return
    L, R = box
    Wd = max(1.0, R - L)
    mid = (L + R) / 2
    lefts = [ln.left for ln in plines]
    rights = [ln.right for ln in plines]
    if len(plines) == 1 and "\t" in par.text:
        segs = plines[0].segments()
        par.indent_px = max(0.0, segs[0][0].left - L)
        last = par.indent_px
        for seg in segs[1:]:
            sl = min(w.left for w in seg)
            sr = max(w.right for w in seg)
            if abs((sl + sr) / 2 - mid) < 0.04 * Wd:
                tab = (mid - L, "center")
            elif abs(sr - R) < 0.03 * Wd or sr > R:
                tab = (max(sr, R) - L, "right")
            else:
                tab = (sl - L, "left")
            if tab[0] > last + 5:
                par.tabs.append(tab)
                last = tab[0]
        return
    if len(plines) == 1:
        left, right = lefts[0], rights[0]
        if abs((left + right) / 2 - mid) < 0.04 * Wd and left - L > 0.08 * Wd:
            par.align = "center"
        elif abs(right - R) < 0.02 * Wd and left - L > 0.25 * Wd:
            par.align = "right"
        else:
            par.indent_px = max(0.0, left - L)
        return
    centers = [(a + b) / 2 for a, b in zip(lefts, rights)]
    if (all(abs(c - mid) < 0.04 * Wd for c in centers) and np.std(lefts) > 0.02 * Wd):
        par.align = "center"
        return
    body_left = statistics.median(lefts[1:])
    par.indent_px = max(0.0, body_left - L)
    par.first_px = lefts[0] - body_left
    full = rights[:-1]
    if len(plines) >= 3 and max(full) - min(full) < 0.012 * Wd and max(full) >= R - 0.03 * Wd:
        par.align = "justify"


def _build_paragraphs(lines: list[_Line], breaks: list[float] = (),
                      page_text: str | None = None, box=None) -> list[Paragraph]:
    """breaks: места (order) на таблиците — абзац не може да ги прескача.
    box: (ляво, дясно) поле на текста в страницата — за подравняването."""
    lines = sorted(lines, key=lambda ln: ln.order)
    if not lines:
        return []
    if page_text is None:
        page_text = " ".join(w.text for ln in lines for w in ln.words)
    # Дясната граница на текста в блока — за да познаем кратките (последни) редове
    block_right: dict[int, int] = {}
    for ln in lines:
        block_right[ln.block] = max(block_right.get(ln.block, 0), ln.right)

    paragraphs: list[Paragraph] = []
    cur_runs: list[Run] = []
    cur_heights: list[float] = []
    cur_lines: list[_Line] = []
    prev = None
    cur_order = 0.0
    prev_tabbed = False
    paragraphs_text_tail = ""

    def flush():
        nonlocal cur_runs, cur_heights, cur_lines
        text = "".join(r.text for r in cur_runs).strip()
        if text:
            par = Paragraph(text=text, size=statistics.median(cur_heights), order=cur_order,
                            runs=cur_runs, frame=cur_lines[0].frame)
            _paragraph_geometry(par, cur_lines, box)
            paragraphs.append(par)
        cur_runs, cur_heights, cur_lines = [], [], []

    for ln in lines:
        text, tabbed = ln.text()
        letters = sum(ch.isalpha() for ch in text)
        # кратките редове („ДА ☐ HE ☒“, „Р-л c-p YC:“) нямат достатъчно букви —
        # азбуката се определя по цялата страница
        text = fix_scripts(text, None if letters >= 20 else page_text)
        text = _collapse_marks(text)
        runs = _line_runs(text, ln.styles())
        h = ln.height
        new_par = prev is None
        if prev is not None:
            ph = prev.height
            prev_text = paragraphs_text_tail
            continues = (abs(ln.left - prev.left) < 2 * ph and 0 <= ln.top - prev.bottom < 0.9 * ph
                         and prev.right >= block_right[prev.block] - 4 * ph
                         and prev_text[-1:] not in ".:;!?" and not _NUMBERED.match(text)
                         and not tabbed and not prev_tabbed)
            # ред, който започва с малка буква, продължава изречението отгоре
            lower_cont = (text[:1].islower() and prev_text[-1:] not in ".:;!?"
                          and not tabbed and not prev_tabbed
                          and not any(m in prev_text for m in _MARKS)
                          and 0 <= ln.top - prev.bottom < 1.8 * ph)
            table_between = any(prev.order < b < ln.order for b in breaks)
            style_change = _all_bold(prev) != _all_bold(ln)
            # отместен/центриран ред, следван от ред, започващ другаде — нов абзац
            shifted = (box is not None and abs(ln.left - prev.left) > 3 * ph
                       and prev.left > box[0] + 0.08 * (box[1] - box[0]) and not lower_cont)
            if table_between or ln.frame != prev.frame or shifted:
                new_par = True
            elif lower_cont or (continues and not style_change and (ln.block != prev.block or ln.par != prev.par)):
                pass
            elif (ln.block != prev.block or ln.par != prev.par
                    or tabbed or prev_tabbed
                    or style_change
                    or any(m in prev_text for m in _MARKS)
                    or _NUMBERED.match(text)
                    or ln.top - prev.bottom > 1.2 * max(h, ph)
                    or prev.right < block_right[prev.block] - 4 * ph
                    or abs(h - ph) > 0.35 * max(h, ph)):
                new_par = True
        if new_par:
            flush()
            cur_order = ln.order
        _join_runs(cur_runs, runs)
        cur_heights.append(h)
        cur_lines.append(ln)
        prev, prev_tabbed = ln, tabbed
        paragraphs_text_tail = text
    flush()

    body = statistics.median(p.size for p in paragraphs) if paragraphs else 0
    for p in paragraphs:
        p.heading = body > 0 and p.size >= body * 1.45 and len(p.text) < 160
    return paragraphs


def _cell_text(words: list[_Word], page_text: str, rect=None) -> str:
    """Текст на клетка: думите по редове, редовете слети в едно изречение."""
    if not words:
        return ""
    words = sorted(words, key=lambda w: (w.top + w.height / 2))
    rows: list[list[_Word]] = []
    for w in words:
        cy = w.top + w.height / 2
        for row in rows:
            top = min(x.top for x in row)
            bottom = max(x.bottom for x in row)
            if top - 0.2 * w.height <= cy <= bottom + 0.2 * w.height:
                row.append(w)
                break
        else:
            rows.append([w])
    out = ""
    prev_row = None
    for row in rows:
        t = " ".join(w.text for w in sorted(row, key=lambda w: w.left))
        # Ред, който свършва далеч преди десния край на клетката (заглавие,
        # „МЕТОДИКА“ над подзаглавие), остава на отделен ред.
        early = (rect is not None and prev_row is not None
                 and rect[2] - max(w.right for w in prev_row) > 0.25 * (rect[2] - rect[0])
                 and not t[:1].islower())
        if out.endswith("-") and t[:1].islower() and len(out) > 1 and out[-2].isalpha():
            out = out[:-1] + t
        elif early:
            out = f"{out}\n{t}"
        else:
            out = f"{out} {t}" if out else t
        prev_row = row
    letters = sum(ch.isalpha() for ch in out)
    out = fix_scripts(out, None if letters >= 20 else page_text)
    # заглавие на колона „№“ често се чете като „Ло“, „No“, „Хо“
    if out in ("Ло", "No", "Ne", "Хо", "Nо", "Мо", "Nе", "N", "Ns", "Ле"):
        out = "№"
    return _collapse_marks(out)


def _split_borderless_columns(grid, words: list[_Word], dpi: int) -> None:
    """Колони без линия: празнина, която минава през всички редове на колоната
    (напр. „Номер:      6673“). Добавя нова граница в grid.xs."""
    min_gap = 0.09 * dpi    # ~2 интервала между думи
    changed = True
    while changed:
        changed = False
        for c in range(len(grid.xs) - 1):
            cells = [cell for cell in grid.cells if cell[1] == c and cell[3] == c]
            if len(cells) < 2:
                continue
            x0, x1 = grid.xs[c], grid.xs[c + 1]
            occupied = np.zeros(int(x1 - x0) + 1, bool)
            per_cell = []
            for cell in cells:
                cx0, cy0, cx1, cy1 = grid.cell_rect(cell)
                ws = [w for w in words if cx0 <= w.left + w.width / 2 < cx1 and cy0 <= w.top + w.height / 2 < cy1]
                per_cell.append(ws)
                for w in ws:
                    occupied[max(0, w.left - x0):max(0, w.right - x0) + 1] = True
            # всички празни участъци вътре в колоната
            free = ~occupied
            best = None
            k = 0
            margin = int(0.05 * dpi)
            while k < len(free):
                if free[k]:
                    j = k
                    while j < len(free) and free[j]:
                        j += 1
                    if k > margin and j < len(free) - margin and j - k >= min_gap:
                        mid = x0 + (k + j) // 2
                        both = sum(1 for ws in per_cell
                                   if any(w.right < mid for w in ws) and any(w.left > mid for w in ws))
                        if both >= max(2, 0.6 * len(cells)) and (best is None or j - k > best[1]):
                            best = (mid, j - k)
                    k = j
                else:
                    k += 1
            if best is None:
                continue
            mid = int(best[0])
            new_cells = []
            for (r0, c0, r1, c1) in grid.cells:
                if c0 > c:
                    new_cells.append((r0, c0 + 1, r1, c1 + 1))
                elif c0 == c and c1 == c:
                    new_cells.append((r0, c, r1, c))
                    new_cells.append((r0, c + 1, r1, c + 1))
                elif c0 <= c <= c1:
                    new_cells.append((r0, c0, r1, c1 + 1))
                else:
                    new_cells.append((r0, c0, r1, c1))
            grid.xs.insert(c + 1, mid)
            grid.cells = sorted(new_cells)
            changed = True
            break


def _cell_format(cell: TableCell, words: list[_Word], rect) -> None:
    styled = [w for w in words if w.style is not None and sum(c.isalpha() for c in w.text) >= 2]
    if styled:
        n = sum(len(w.text) for w in styled)
        cell.bold = sum(len(w.text) for w in styled if w.style.bold) * 5 >= n * 3
        cell.italic = sum(len(w.text) for w in styled if w.style.italic) * 5 >= n * 3
        ems = [w.style.em for w in styled if w.style.em]
        if ems:
            cell.size_pt = snap_size(font_size_pt(statistics.median(ems) * 0.70, RENDER_DPI))
    if words:
        x0, _, x1, _ = rect
        left = min(w.left for w in words)
        right = max(w.right for w in words)
        cw = max(1, x1 - x0)
        if abs((left + right) / 2 - (x0 + x1) / 2) < 0.08 * cw and left - x0 > 0.15 * cw:
            cell.align = "center"
        elif x1 - right < 0.08 * cw and left - x0 > 0.4 * cw:
            cell.align = "right"


def _build_tables(grids, lines: list[_Line], extras, page_text: str, dpi: int,
                  cell_words: list[list[_Word]] | None = None):
    """Изважда думите в таблиците от редовете и прави TableBlock-ове."""
    if not grids:
        return [], lines, list(extras)
    table_words: list[list[_Word]] = [[] for _ in grids]

    def which(x: float, y: float) -> int | None:
        for k, g in enumerate(grids):
            if g.contains(x, y):
                return k
        return None

    rest_lines = []
    for ln in lines:
        keep = []
        for w in ln.words:
            k = which(w.left + w.width / 2, w.top + w.height / 2)
            if k is None:
                keep.append(w)
            elif not cell_words:
                table_words[k].append(w)
            # иначе думата е разпозната по-добре клетка по клетка
        if keep:
            ln.words = keep
            rest_lines.append(ln)
    if cell_words:
        for k, ws in enumerate(cell_words):
            # В клетката кратките думи („1“, „№“, „ТН.“) са нормални — махаме
            # само чисти препинателни знаци с ниска увереност.
            table_words[k].extend(w for w in ws
                                  if any(ch.isalnum() for ch in w.text) or w.conf >= 50)
    rest_extras = []
    for e in extras:
        k = which(e.cx, e.cy)
        if k is None:
            rest_extras.append(e)
        else:
            table_words[k].append(_Word(e.text, e.left, e.top, e.width, e.height, _EXTRA_CONF))

    blocks = []
    for g, words in zip(grids, table_words):
        _split_borderless_columns(g, words, dpi)
        by_cell: dict[tuple, list[_Word]] = {cell: [] for cell in g.cells}
        for w in words:
            cell = g.cell_at(w.left + w.width / 2, w.top + w.height / 2)
            if cell is not None:
                by_cell[cell].append(w)
        cells = []
        for (r0, c0, r1, c1) in g.cells:
            ws = by_cell[(r0, c0, r1, c1)]
            cell = TableCell(r0, c0, r1 - r0 + 1, c1 - c0 + 1,
                             _cell_text(ws, page_text, g.cell_rect((r0, c0, r1, c1))))
            _cell_format(cell, ws, g.cell_rect((r0, c0, r1, c1)))
            cells.append(cell)
        total = g.xs[-1] - g.xs[0]
        widths = [(g.xs[j + 1] - g.xs[j]) / total for j in range(len(g.xs) - 1)]
        # място в текста: след последния ред над таблицата
        top = g.ys[0]
        above = [ln.order for ln in rest_lines if ln.bottom <= top + 5]
        order = (max(above) if above else -1.0) + 0.5
        blocks.append(TableBlock(len(g.ys) - 1, len(g.xs) - 1, cells, widths, order,
                                 left_px=g.xs[0], width_px=g.xs[-1] - g.xs[0],
                                 top=g.ys[0], bottom=g.ys[-1]))
    # две таблици на едно и също място — запазваме реда им отгоре надолу
    for k, b in enumerate(blocks):
        b.order += 0.001 * k
    return blocks, rest_lines, rest_extras


def _parse_tsv(tsv: str) -> dict:
    cols = ["block_num", "par_num", "line_num", "left", "top", "width", "height", "conf", "text"]
    data: dict[str, list] = {c: [] for c in cols}
    rows = tsv.splitlines()
    if not rows:
        return data
    header = rows[0].split("\t")
    pos = {name: header.index(name) for name in cols}
    for row in rows[1:]:
        parts = row.split("\t")
        if len(parts) < len(header):
            parts += [""] * (len(header) - len(parts))
        for c in cols:
            v = parts[pos[c]]
            data[c].append(v if c == "text" else float(v or -1) if c == "conf" else int(v or 0))
    return data


def _run_tesseract(img: Image.Image, langs: str, psm: int = 3,
                   want_pdf: bool = False) -> tuple[dict, bytes | None]:
    with tempfile.TemporaryDirectory(prefix="bukvar_") as tmp:
        src = Path(tmp) / "page.png"
        base = Path(tmp) / "out"
        img.save(src, dpi=(RENDER_DPI, RENDER_DPI))
        cmd = [pytesseract.pytesseract.tesseract_cmd, str(src), str(base),
               "-l", langs, "--psm", str(psm), "--dpi", str(RENDER_DPI),
               "-c", "tessedit_create_tsv=1", "-c", "tessedit_create_txt=0"]
        if want_pdf:
            cmd += ["-c", "textonly_pdf=1", "-c", "tessedit_create_pdf=1"]
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        res = subprocess.run(cmd, capture_output=True, creationflags=flags)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode("utf-8", "replace").strip() or "Tesseract грешка")
        data = _parse_tsv(base.with_suffix(".tsv").read_text(encoding="utf-8"))
        pdf = base.with_suffix(".pdf").read_bytes() if want_pdf else None
    return data, pdf


def _ocr_table_cells(img: Image.Image, grids, langs: str) -> list[list[_Word]]:
    """Разпознава клетките на таблиците поотделно.

    Цялата страница наведнъж кара Tesseract да бърка тесните колони, а
    отделно извикване за всяка клетка е бавно. Затова изрязваме клетките,
    нареждаме ги една под друга с широки празни ивици и ги разпознаваме с
    едно извикване. После връщаме думите на местата им в страницата."""
    gray = img.convert("L")
    arr = np.asarray(gray)
    inset = 3
    gap = int(0.15 * RENDER_DPI)
    pad = int(0.1 * RENDER_DPI)
    pieces = []   # (table index, crop x0, crop y0, y в общото изображение, височина)
    y = pad
    width = 0
    for k, g in enumerate(grids):
        for cell in g.cells:
            x0, y0, x1, y1 = g.cell_rect(cell)
            x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue
            if (arr[y0:y1, x0:x1] < 160).sum() < 20:
                continue   # празна клетка
            pieces.append((k, x0, y0, x1, y1, y))
            y += (y1 - y0) + gap
            width = max(width, x1 - x0)
    words: list[list[_Word]] = [[] for _ in grids]
    if not pieces:
        return words
    canvas = Image.new("L", (width + 2 * pad, y + pad), 255)
    for (_, x0, y0, x1, y1, yy) in pieces:
        canvas.paste(gray.crop((x0, y0, x1, y1)), (pad, yy))
    data, _ = _run_tesseract(canvas, langs, psm=4)
    found = set()
    for i, t in enumerate(data["text"]):
        if not t or not t.strip() or data["conf"][i] < 0:
            continue
        cy = data["top"][i] + data["height"][i] / 2
        for n, (k, x0, y0, x1, y1, yy) in enumerate(pieces):
            if yy - gap / 2 <= cy < yy + (y1 - y0) + gap / 2:
                words[k].append(_Word(t, data["left"][i] - pad + x0, data["top"][i] - yy + y0,
                                      data["width"][i], data["height"][i], data["conf"][i]))
                found.add(n)
                break

    # Втори проход за клетки с мастило, но без разпознат текст — обикновено
    # единичен знак („1“, „№“), който Tesseract пропуска в голямо празно поле.
    missing = []
    for n, (k, x0, y0, x1, y1, yy) in enumerate(pieces):
        if n in found:
            continue
        ink = np.argwhere(arr[y0:y1, x0:x1] < 160)
        if len(ink) < 20:
            continue
        (iy0, ix0), (iy1, ix1) = ink.min(axis=0), ink.max(axis=0)
        missing.append((k, x0 + ix0, y0 + iy0, x0 + ix1 + 1, y0 + iy1 + 1))
    if missing:
        line_h = max(b[4] - b[2] for b in missing) + 2 * pad
        sep = int(0.3 * RENDER_DPI)
        total_w = sum(b[3] - b[1] for b in missing) + sep * (len(missing) + 1)
        strip = Image.new("L", (total_w, line_h), 255)
        spans = []
        x = sep
        for (k, bx0, by0, bx1, by1) in missing:
            yo = (line_h - (by1 - by0)) // 2
            strip.paste(gray.crop((bx0, by0, bx1, by1)), (x, yo))
            spans.append((k, bx0, by0, x, x + (bx1 - bx0), yo))
            x += (bx1 - bx0) + sep
        data, _ = _run_tesseract(strip, langs, psm=7)
        for i, t in enumerate(data["text"]):
            if not t or not t.strip() or data["conf"][i] < 0:
                continue
            cx = data["left"][i] + data["width"][i] / 2
            for (k, bx0, by0, sx0, sx1, yo) in spans:
                if sx0 - sep / 2 <= cx < sx1 + sep / 2:
                    words[k].append(_Word(t, data["left"][i] - sx0 + bx0, data["top"][i] - yo + by0,
                                          data["width"][i], data["height"][i], data["conf"][i]))
                    break
    return words


def _measure_styles(lines: list[_Line], cell_words: list[list[_Word]], gray: np.ndarray,
                    thr: float) -> None:
    """Стил на всяка дума (удебелен/курсив/размер) и размер на шрифта на редовете."""
    # Размерът се мери по редове (в таблицата — по редове на таблицата),
    # а стилът се изглажда по редове на текста и по клетки.
    size_groups: list[list[_Word]] = [ln.words for ln in lines]
    style_groups: list[list[_Word]] = [ln.words for ln in lines]
    for ws in cell_words:
        ws = sorted(ws, key=lambda w: w.top + w.height / 2)
        rows: list[list[_Word]] = []
        for w in ws:
            if rows and abs((w.top + w.height / 2) - (rows[-1][-1].top + rows[-1][-1].height / 2)) <= 0.5 * w.height:
                rows[-1].append(w)
            else:
                rows.append([w])
        for row in rows:
            row.sort(key=lambda w: w.left)
            size_groups.append(row)
            cur = [row[0]]
            for a, b in zip(row, row[1:]):
                if b.left - a.right > 1.5 * max(a.height, b.height):   # друга клетка
                    style_groups.append(cur)
                    cur = []
                cur.append(b)
            style_groups.append(cur)
    all_styles, all_ems = [], []
    group_caps = []
    for g in size_groups:
        caps = []
        for w in g:
            if w.conf >= _EXTRA_CONF and w.text in _MARKS:
                continue
            w.style = measure(gray, w.left, w.top, w.width, w.height, w.text, thr)
            if w.style.cap:
                caps.append(w.style.cap)
        group_caps.append(statistics.median(caps) if caps else None)
    known = [c for c in group_caps if c]
    page_cap = statistics.median(known) if known else 0.1 * RENDER_DPI
    for g, cap in zip(size_groups, group_caps):
        em = (cap or page_cap) / 0.70
        for w in g:
            if w.style is not None:
                w.style.em = em
                all_styles.append(w.style)
                all_ems.append(em)
    classify(all_styles, all_ems)
    for g in style_groups:
        ordered = [w for w in sorted(g, key=lambda w: w.left) if w.style is not None]
        lens = [len(w.text) for w in ordered]
        # удебеляването обикновено е на цели редове; курсивът — често на фрази
        for attr, hi, lo in (("bold", 0.5, 0.3), ("italic", 0.85, 0.15)):
            flags = smooth([getattr(w.style, attr) for w in ordered], lens, hi, lo)
            for w, f in zip(ordered, flags):
                setattr(w.style, attr, f)
    for ln, cap in zip(lines, group_caps):
        ln.size_pt = font_size_pt(cap or page_cap, RENDER_DPI)


def _mark_underlines(rules, words: list[_Word]) -> list:
    """Подчертава думите над линията. Връща линиите, които не са подчертаване."""
    separators = []
    for (x0, x1, y) in rules:
        hit = False
        for w in words:
            if w.style is None:
                continue
            overlap = min(x1, w.right) - max(x0, w.left)
            if overlap >= 0.5 * w.width and -0.35 * w.height <= y - w.bottom <= 0.6 * w.height:
                w.style.underline = True
                hit = True
        if not hit:
            separators.append((x0, x1, y))
    return separators


def _text_box(lines: list[_Line], page_width: int) -> tuple[float, float]:
    long_lines = [ln for ln in lines if len(ln.words) >= 3]
    if not long_lines:
        long_lines = lines
    if not long_lines:
        return 0.1 * page_width, 0.9 * page_width
    lefts = [ln.left for ln in long_lines]
    # дясното поле: най-десният ред с поне две думи (колонтитул, подравнен вдясно
    # ред), но не в самия край на листа (там са само бележки на ръка)
    limit = page_width - 0.3 * RENDER_DPI
    wordy = [ln.right for ln in lines
             if sum(1 for w in ln.words if sum(c.isalnum() for c in w.text) >= 2) >= 2 and ln.right <= limit]
    right = max(wordy) if wordy else 0
    return float(np.percentile(lefts, 5)), float(max(right, np.percentile([ln.right for ln in long_lines], 95)))


def ocr_page(page: Page, langs: str, img: Image.Image | None = None,
             clean_forms: bool = True, detect_tables: bool = True) -> None:
    """Едно минаване на Tesseract дава и текстовия слой (PDF), и позициите (TSV)."""
    img = img or page.image()
    cleaned = clean_for_ocr(img, RENDER_DPI, forms=clean_forms, tables=detect_tables)
    ocr_img, extras, grids = cleaned.image, cleaned.extras, cleaned.tables or []
    table_grids = [g for g in grids if len(g.cells) > 1]
    frames = [g for g in grids if len(g.cells) == 1]   # рамка около текст
    data, page.text_layer_pdf = _run_tesseract(ocr_img, langs, psm=3, want_pdf=True)
    cell_words = _ocr_table_cells(ocr_img, table_grids, langs) if table_grids else []

    lines = _drop_noise(_lines_from_tsv(data))
    confs = [w.conf for ln in lines for w in ln.words]
    page_text = " ".join(w.text for ln in lines for w in ln.words)

    gray = np.asarray(ocr_img.convert("L"))
    thr = min(float(_otsu(gray)), 200.0)
    _measure_styles(lines, cell_words, gray, thr)
    separators = _mark_underlines(cleaned.rules or [], [w for ln in lines for w in ln.words])

    tables, lines, extras = _build_tables(table_grids, lines, extras, page_text, RENDER_DPI, cell_words)
    for ln in lines:
        cx, cy = (ln.left + ln.right) / 2, (ln.top + ln.bottom) / 2
        for k, f in enumerate(frames):
            if f.contains(cx, cy):
                ln.frame = k
                break
    lines = _merge_rows(lines)
    _insert_extras(lines, extras)
    box = _text_box(lines, img.width)
    paragraphs = _build_paragraphs(lines, [t.order for t in tables], page_text, box)
    blocks = sorted(paragraphs + tables, key=lambda b: b.order)

    # разстояние преди абзаца и разделителни линии
    body = [p.size_pt for p in paragraphs if p.size_pt for _ in range(len(p.text) // 20 + 1)]
    body_pt = snap_size(statistics.median(body)) if body else 11.0
    # Размер на ±1 pt от основния — това е грешка на мерането, а не друг шрифт
    for p in paragraphs:
        words = [w for w in p.text.split() if sum(c.isalpha() for c in w) >= 2 and w not in _MARKS]
        if p.size_pt is None or abs(p.size_pt - body_pt) <= 1.0:
            p.size_pt = body_pt
        elif p.size_pt < body_pt and len(words) < 3:
            p.size_pt = body_pt      # твърде малко думи за надеждно мерене
    for t in tables:
        for c in t.cells:
            if c.size_pt and abs(c.size_pt - body_pt) <= 1.0:
                c.size_pt = body_pt
    prev_bottom = None
    for b in blocks:
        if prev_bottom is not None:
            gap = b.top - prev_bottom
            normal = 0.45 * body_pt / 72 * RENDER_DPI
            b.space_before_pt = round(max(0.0, min(30.0, (gap - normal) / RENDER_DPI * 72)) * 2) / 2
        prev_bottom = max(prev_bottom or 0, b.bottom)
    width = box[1] - box[0]
    for (x0, x1, y) in separators:
        if x1 - x0 < 0.5 * width:
            continue
        below = [b for b in blocks if isinstance(b, Paragraph) and 0 <= b.top - y < 0.4 * RENDER_DPI]
        if below:
            min(below, key=lambda b: b.top).border_top = True

    serif_boxes = [(w.left, w.top, w.width, w.height) for ln in lines for w in ln.words
                   if w.style is not None and not w.style.bold and len(w.text) >= 4]
    page.geom = {"width": img.width, "height": img.height, "left": box[0], "right": box[1],
                 "top": min((b.top for b in blocks), default=int(0.08 * img.height)),
                 "serif": is_serif(gray, serif_boxes, thr), "body_pt": snap_size(body_pt)}
    page.paragraphs = blocks
    page.text = "\n\n".join(b.text for b in page.paragraphs)
    page.confidence = round(sum(confs) / len(confs), 1) if confs else None
    page.text_edited = False
    page.done = True


def default_workers() -> int:
    """Колко страници едновременно: поне 2, но не повече от ядрата (до 4).

    Tesseract работи с една нишка на страница, затова паралелните страници
    са най-добрият начин да се използват всички ядра."""
    return min(4, max(2, os.cpu_count() or 2))


def process_page(page: Page, langs: str, auto_rotate: bool = True, deskew: bool = True,
                 clean_forms: bool = True, detect_tables: bool = True) -> None:
    """Разпознава една страница. Грешката се записва в page.error, не се хвърля."""
    page.reset()
    try:
        img = preprocess(page, auto_rotate, deskew)
        ocr_page(page, langs, img, clean_forms, detect_tables)
        page.error = None
    except Exception as exc:  # една лоша страница не спира останалите
        page.error = str(exc) or exc.__class__.__name__
        page.done = False


def process_pages(pages: list[Page], langs: str, auto_rotate: bool = True,
                  deskew: bool = True, workers: int | None = None,
                  clean_forms: bool = True, detect_tables: bool = True,
                  on_page_done: Callable[[int], None] | None = None,
                  should_stop: Callable[[], bool] | None = None) -> None:
    """Обработва страниците паралелно. on_page_done(i) се вика за всяка готова,
    в реда, в който завършват."""
    workers = workers or min(len(pages), default_workers()) or 1

    def job(i: int) -> int:
        if should_stop and should_stop():
            return -1
        process_page(pages[i], langs, auto_rotate, deskew, clean_forms, detect_tables)
        return i

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(job, i) for i in range(len(pages))]
        for f in as_completed(futures):
            i = f.result()
            if i >= 0 and on_page_done:
                on_page_done(i)


# --------------------------------------------------------------------------
# Запис
# --------------------------------------------------------------------------

def _image_page_pdf(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PDF", resolution=RENDER_DPI, quality=80)
    return buf.getvalue()


def export_pdf(pages: Iterable[Page], out_path: str | Path) -> None:
    """Търсим PDF: оригиналната страница + невидим текстов слой."""
    writer = PdfWriter()
    source_cache: dict[Path, PdfReader] = {}
    for page in pages:
        if page.is_pdf and not page.geometry_changed:
            reader = source_cache.setdefault(page.source, PdfReader(str(page.source)))
            writer.add_page(reader.pages[page.index])
        else:
            writer.add_page(PdfReader(io.BytesIO(_image_page_pdf(page.image()))).pages[0])
        target = writer.pages[-1]
        if page.text_layer_pdf:
            layer = PdfReader(io.BytesIO(page.text_layer_pdf)).pages[0]
            tw, th = float(target.mediabox.width), float(target.mediabox.height)
            lw, lh = float(layer.mediabox.width), float(layer.mediabox.height)
            rot = (target.get("/Rotate") or 0) % 360
            if rot in (90, 270):
                tw, th = th, tw
            sx, sy = tw / lw, th / lh
            op = Transformation().scale(sx, sy)
            if rot:
                # Слоят е изчислен върху завъртяната (видимата) страница;
                # връщаме го в координатите на неротирания mediabox.
                W, H = float(target.mediabox.width), float(target.mediabox.height)
                shift = {90: (W, 0), 180: (W, H), 270: (0, H)}[rot]
                op = op.rotate(rot).translate(*shift)
            ox, oy = float(target.mediabox.left), float(target.mediabox.bottom)
            if ox or oy:
                op = op.translate(ox, oy)
            target.merge_transformed_page(layer, op)
    writer.add_metadata({"/Producer": "Буквар OCR", "/Creator": "Буквар OCR"})
    with open(out_path, "wb") as fh:
        writer.write(fh)


def _page_paragraphs(page: Page) -> list:
    if page.text_edited or not page.paragraphs:
        chunks = [c.strip() for c in re.split(r"\n\s*\n", page.text) if c.strip()]
        blocks = []
        for c in chunks:
            rows = c.split("\n")
            # ръчно редактирана таблица: редове с табулации
            if len(rows) > 1 and all("\t" in r for r in rows):
                grid = [r.split("\t") for r in rows]
                n_cols = max(len(r) for r in grid)
                cells = [TableCell(i, j, 1, 1, r[j] if j < len(r) else "")
                         for i, r in enumerate(grid) for j in range(n_cols)]
                blocks.append(TableBlock(len(grid), n_cols, cells, [1 / n_cols] * n_cols))
            else:
                blocks.append(Paragraph(c.replace("\n", " "), 0))
        return blocks
    return page.paragraphs


def _px(px: float):
    from docx.shared import Emu
    return Emu(int(px / RENDER_DPI * 914400))


_ALIGN = {"left": 0, "center": 1, "right": 2, "justify": 3}


def _set_borders(pf_element, sides: dict) -> None:
    """Рамка на абзац: sides = {"top": размер в 1/8 pt, ...}."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pPr = pf_element.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    for side in ("top", "left", "bottom", "right"):
        if side in sides:
            el = OxmlElement(f"w:{side}")
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), str(sides[side]))
            el.set(qn("w:space"), "4")
            el.set(qn("w:color"), "000000")
            bdr.append(el)
    pPr.append(bdr)


def _outline_level(paragraph, level: int) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    el = OxmlElement("w:outlineLvl")
    el.set(qn("w:val"), str(level))
    paragraph._p.get_or_add_pPr().append(el)


def _docx_paragraph(doc, b: Paragraph, body_pt: float):
    from docx.enum.text import WD_TAB_ALIGNMENT
    from docx.shared import Pt

    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.alignment = _ALIGN.get(b.align, 0)
    if b.indent_px > 0.01 * RENDER_DPI:
        pf.left_indent = _px(b.indent_px)
    if abs(b.first_px) > 0.03 * RENDER_DPI:
        pf.first_line_indent = _px(b.first_px)
    if b.space_before_pt:
        pf.space_before = Pt(b.space_before_pt)
    kinds = {"left": WD_TAB_ALIGNMENT.LEFT, "center": WD_TAB_ALIGNMENT.CENTER, "right": WD_TAB_ALIGNMENT.RIGHT}
    for pos, kind in b.tabs:
        pf.tab_stops.add_tab_stop(_px(pos - b.indent_px if kind == "left" else pos - b.indent_px), kinds[kind])
    if b.frame >= 0:
        _set_borders(p._p, {"top": 6, "left": 6, "bottom": 6, "right": 6})
    elif b.border_top:
        _set_borders(p._p, {"top": 12})
    if b.heading:
        _outline_level(p, 0)
        pf.keep_with_next = True
    size = Pt(b.size_pt) if b.size_pt and b.size_pt != body_pt else None
    for r in (b.runs or [Run(b.text)]):
        run = p.add_run(r.text)
        run.bold = r.bold or None
        run.italic = r.italic or None
        run.underline = r.underline or None
        if size:
            run.font.size = size
    return p


def _docx_table(doc, block: TableBlock, avail_width, left_margin_px: float | None = None,
                body_pt: float = 11.0, spacer: bool = True) -> None:
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    t = doc.add_table(rows=block.n_rows, cols=block.n_cols)
    t.style = "Table Grid"
    t.autofit = False
    total = int(avail_width)
    if block.width_px and left_margin_px is not None:
        total = min(int(avail_width * 1.08), int(_px(block.width_px)))
        # отстъп на таблицата спрямо полето
        ind = OxmlElement("w:tblInd")
        ind.set(qn("w:w"), str(int((block.left_px - left_margin_px) / RENDER_DPI * 1440)))
        ind.set(qn("w:type"), "dxa")
        t._tbl.tblPr.append(ind)
    else:
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
    widths = [int(total * w) for w in block.col_widths]
    # ширините на колоните (tblGrid), за да ги спазват Word и LibreOffice
    grid = t._tbl.tblGrid
    for gc, w in zip(grid.findall(qn("w:gridCol")), widths):
        gc.set(qn("w:w"), str(int(w / 635)))   # EMU → twips
    for row in t.rows:
        for j, cell in enumerate(row.cells):
            cell.width = widths[j]
    for c in block.cells:
        cell = t.cell(c.row, c.col)
        if c.rowspan > 1 or c.colspan > 1:
            cell = cell.merge(t.cell(c.row + c.rowspan - 1, c.col + c.colspan - 1))
            cell.width = sum(widths[c.col:c.col + c.colspan])
        cell.text = c.text
        for par in cell.paragraphs:
            par.paragraph_format.space_after = Pt(0)
            par.paragraph_format.alignment = _ALIGN.get(c.align, 0)
            for run in par.runs:
                run.font.size = Pt(c.size_pt or min(body_pt, 10))
                run.bold = c.bold or None
                run.italic = c.italic or None
    # Таблицата да не се „залепва“ за следващата таблица
    if spacer:
        sp = doc.add_paragraph()
        sp.paragraph_format.space_after = Pt(0)
        sp.paragraph_format.line_spacing = Pt(4)


def export_docx(pages: Iterable[Page], out_path: str | Path, keep_format: bool = True) -> None:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    pages = list(pages)
    geom = next((p.geom for p in pages if p.geom), {}) if keep_format else {}
    doc = Document()
    sec = doc.sections[0]
    if geom:
        # размер на страницата и полета като в оригинала
        sec.page_width, sec.page_height = _px(geom["width"]), _px(geom["height"])
        sec.left_margin = _px(max(0.3 * RENDER_DPI, geom["left"]))
        sec.right_margin = _px(max(0.3 * RENDER_DPI, geom["width"] - geom["right"]))
        top = min(max(geom.get("top", 0.6 * RENDER_DPI) - 0.1 * RENDER_DPI, 0.4 * RENDER_DPI), 1.2 * RENDER_DPI)
        sec.top_margin = _px(top)
        sec.bottom_margin = Cm(1.5)
    else:
        sec.page_width, sec.page_height = Cm(21), Cm(29.7)      # A4
        sec.left_margin = sec.right_margin = Cm(2)
        sec.top_margin = sec.bottom_margin = Cm(2)
    avail = sec.page_width - sec.left_margin - sec.right_margin
    font = "Times New Roman" if geom.get("serif", False) else "Calibri" if not geom else "Arial"
    body_pt = float(geom.get("body_pt", 11))
    normal = doc.styles["Normal"]
    normal.font.name = font
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), font)
    normal.font.size = Pt(body_pt)
    normal.paragraph_format.space_after = Pt(0 if geom else 3)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = 1.0
    for n, page in enumerate(pages):
        left_px = page.geom.get("left") if page.geom else None
        formatted = keep_format and bool(page.geom) and not page.text_edited
        blocks = _page_paragraphs(page)
        for k, b in enumerate(blocks):
            new_page = n > 0 and k == 0
            if isinstance(b, TableBlock):
                if new_page:
                    # Таблица в началото на страницата: малък абзац с „нова страница преди“
                    br = doc.add_paragraph()
                    br.paragraph_format.page_break_before = True
                    br.paragraph_format.line_spacing = Pt(1)
                nxt = blocks[k + 1] if k + 1 < len(blocks) else None
                _docx_table(doc, b, avail, left_px if formatted else None, body_pt,
                            spacer=isinstance(nxt, TableBlock))
                continue
            if formatted:
                para = _docx_paragraph(doc, b, body_pt)
            elif b.heading:
                para = doc.add_paragraph(style="Heading 2")
                para.add_run(b.text)
            else:
                para = doc.add_paragraph(b.text)
            if new_page:
                # „Нова страница преди абзаца“ вместо отделен разделител: ако
                # предишната страница е пълна, разделителят би останал сам на празна страница.
                para.paragraph_format.page_break_before = True
    doc.core_properties.author = ""
    doc.save(str(out_path))


def export_xlsx(pages: Iterable[Page], out_path: str | Path) -> None:
    """Всички таблици в Excel: една след друга, с празен ред между тях.
    Ако няма таблици, записва текста по абзаци."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Таблици"
    thin = Side(style="thin", color="808080")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    row0 = 1
    max_cols = 1
    found = False
    for page in pages:
        for b in _page_paragraphs(page):
            if not isinstance(b, TableBlock):
                continue
            found = True
            ws.cell(row=row0, column=1, value=f"{page.label}").font = Font(italic=True, color="808080")
            row0 += 1
            for c in b.cells:
                cell = ws.cell(row=row0 + c.row, column=1 + c.col, value=_xlsx_value(c.text))
                cell.alignment = wrap
                if c.rowspan > 1 or c.colspan > 1:
                    ws.merge_cells(start_row=row0 + c.row, start_column=1 + c.col,
                                   end_row=row0 + c.row + c.rowspan - 1, end_column=c.col + c.colspan)
            for r in range(b.n_rows):
                for j in range(b.n_cols):
                    ws.cell(row=row0 + r, column=1 + j).border = border
            max_cols = max(max_cols, b.n_cols)
            row0 += b.n_rows + 1
    if not found:
        ws.title = "Текст"
        for page in pages:
            for b in _page_paragraphs(page):
                ws.cell(row=row0, column=1, value=b.text).alignment = wrap
                row0 += 1
        ws.column_dimensions["A"].width = 100
    else:
        from openpyxl.utils import get_column_letter
        for j in range(1, max_cols + 1):
            ws.column_dimensions[get_column_letter(j)].width = 28
    wb.save(str(out_path))


def _xlsx_value(text: str):
    """Числата стават числа (1 234,56 → 1234.56), останалото остава текст."""
    t = text.strip()
    if re.fullmatch(r"-?\d{1,3}([ \u00a0]\d{3})*([.,]\d+)?|-?\d+([.,]\d+)?", t):
        num = t.replace(" ", "").replace("\u00a0", "").replace(",", ".")
        try:
            return int(num) if re.fullmatch(r"-?\d+", num) and not (len(num) > 1 and num.startswith("0")) else float(num) if "." in num else num
        except ValueError:
            return t
    return t


def export_txt(pages: Iterable[Page], out_path: str | Path) -> None:
    text = "\n\n\f".join(p.text for p in pages)
    Path(out_path).write_text(text, encoding="utf-8")


EXPORTERS = {".pdf": export_pdf, ".docx": export_docx, ".xlsx": export_xlsx, ".txt": export_txt}


def export(pages: Iterable[Page], out_path: str | Path, keep_format: bool = True) -> None:
    ext = Path(out_path).suffix.lower()
    if ext not in EXPORTERS:
        raise ValueError(f"Неподдържан изходен формат: {ext}")
    if ext == ".docx":
        export_docx(pages, out_path, keep_format)
    else:
        EXPORTERS[ext](pages, out_path)
