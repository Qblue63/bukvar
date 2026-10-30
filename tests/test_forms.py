"""Тест за формуляри: квадратчета, редове от точки и синьо мастило.

Пуска се с:  python tests/test_forms.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bukvar import engine, layout  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
if not Path(FONT).exists():  # Windows
    FONT = r"C:\Windows\Fonts\times.ttf"


def make_form() -> Image.Image:
    W, H = 2480, 1400
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 44)
    rows = [
        ("1.2.1 Система за безопасност (СБ)", [False, True]),
        ("1.2.2 Система нормална експлоатация", [True, False]),
        ("1.4. Влияе ли изменението върху други КСК", [False, True]),
    ]
    y = 150
    for label, states in rows:
        d.text((200, y), label, font=f, fill=0)
        x = 1900
        for word, checked in zip(("ДА", "НЕ"), states):
            d.text((x, y), word, font=f, fill=0)
            bx = x + int(d.textlength(word, font=f)) + 15
            d.rectangle((bx, y + 6, bx + 50, y + 56), outline=0, width=3)
            if checked:
                d.line((bx, y + 6, bx + 50, y + 56), fill=0, width=3)
                d.line((bx, y + 56, bx + 50, y + 6), fill=0, width=3)
            x = bx + 110
        y += 110
    d.text((200, y), "Коментар:", font=f, fill=0)
    x0 = 200 + int(d.textlength("Коментар:", font=f))
    for x in range(x0 + 10, 2200, 17):          # ред от точки
        d.ellipse((x, y + 40, x + 5, y + 45), fill=0)
    # синя ръкописна бележка върху точките
    pts = [(900 + i * 40, y + 20 + (i % 3) * 18) for i in range(20)]
    d.line(pts, fill=(30, 60, 200), width=6)
    y += 130
    d.text((200, y), "Етап 2: Оценка на влиянието върху СОАИ", font=f, fill=0)
    return img


def main() -> int:
    engine.configure_tesseract()
    img = make_form()
    r = layout.clean_for_ocr(img)
    boxes = [e.text for e in r.extras if e.text in (layout.CHECKED, layout.UNCHECKED)]
    hw = [e for e in r.extras if e.text == layout.HANDWRITING]
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("✔ " if cond else "✖ ") + msg)
        ok &= bool(cond)

    check(len(boxes) == 6, f"намерени 6 квадратчета (намерени: {len(boxes)})")
    check(boxes.count(layout.CHECKED) == 3, f"3 отметнати (намерени: {boxes.count(layout.CHECKED)})")
    check(len(hw) >= 1, "ръкописът е открит")

    page = engine.Page(Path("form.png"), 0)
    engine.ocr_page(page, "bul+eng", img)
    text = page.text
    print(text)
    check("ДА ☐ НЕ ☒" in text, "ред 1: ДА ☐ НЕ ☒")
    check("ДА ☒ НЕ ☐" in text, "ред 2: ДА ☒ НЕ ☐")
    check("КСК" in text, "„КСК“ на кирилица")
    check("Етап 2" in text, "текстът след реда с точки не е изгубен")
    check("[ръкопис]" in text, "ръкописът е отбелязан в текста")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
