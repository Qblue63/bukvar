"""Създава тестови „сканове“ с известен текст."""
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfReader, PdfWriter

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
OUT.mkdir(parents=True, exist_ok=True)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"

TITLE = "Договор за наем на жилище"
BODY = [
    "Днес, 26 октомври 2026 г., в град София, между страните се сключи "
    "настоящият договор за наем на апартамент, находящ се на улица "
    "„Шипка“ № 14, етаж трети.",
    "Наемателят се задължава да плаща месечен наем в размер на 850 лева "
    "до пето число на всеки месец по банков път.",
    "The tenant shall keep the property in good condition and return it "
    "at the end of the term, normal wear and tear excepted.",
    "Щъркелите ловуваха жаби край язовир „Искър“ — юли, юни, ъгъл, ябълка, "
    "жълто, цвете, чаша, шапка, щастие, хубаво, ѝ.",
]

def wrap(draw, text, font, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines

def page(scale=1.0):
    W, H = int(2480 * scale), int(3508 * scale)  # A4 при 300 dpi
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    ft = ImageFont.truetype(BOLD, int(72 * scale))
    fb = ImageFont.truetype(FONT, int(44 * scale))
    x, y = int(220 * scale), int(260 * scale)
    d.text((x, y), TITLE, font=ft, fill=0)
    y += int(170 * scale)
    for para in BODY:
        for line in wrap(d, para, fb, W - 2 * x):
            d.text((x, y), line, font=fb, fill=0)
            y += int(66 * scale)
        y += int(50 * scale)
    return img

def scanned(img, angle):
    random.seed(1)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=245)
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    px = img.load()
    for _ in range(4000):  # прах по скенера
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = random.choice((0, 90))
    return img

clean = page()
clean.save(OUT / "clean.png")
scanned(clean, 3.2).save(OUT / "skewed.jpg", quality=80)
scanned(clean, 90).convert("RGB").save(OUT / "rotated90.pdf", resolution=300)
# PDF, в който страницата е завъртяна чрез атрибута /Rotate
clean.rotate(90, expand=True).convert("RGB").save(OUT / "_tmp.pdf", resolution=300)
w = PdfWriter()
w.add_page(PdfReader(OUT / "_tmp.pdf").pages[0])
w.pages[0].rotate(90)
w.write(OUT / "rotate_attr.pdf")
(OUT / "_tmp.pdf").unlink()
# Многостраничен TIFF
clean.save(OUT / "multi.tif", save_all=True, append_images=[scanned(clean, -1.5)], compression="tiff_lzw")
(OUT / "expected.txt").write_text(TITLE + "\n\n" + "\n\n".join(BODY), encoding="utf-8")
print("ok", sorted(p.name for p in OUT.iterdir()))
