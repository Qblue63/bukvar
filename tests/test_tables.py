"""Тест за таблици: линии, слета клетка, колона без линия, удебелено заглавие.

Пуска се с:  python tests/test_tables.py
"""
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bukvar import engine, layout  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
if not Path(FONT).exists():  # Windows
    FONT, BOLD = r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\timesbd.ttf"


def make_page() -> Image.Image:
    W, H = 2480, 1700
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 40)
    fb = ImageFont.truetype(BOLD, 64)
    d.text((500, 80), "Техническо решение за подмяна", font=fb, fill=0)   # не е таблица

    # Таблица 1: 4 колони, първият ред е слят в една клетка
    xs = [200, 360, 900, 1500, 2280]
    ys = [300, 380, 460, 540, 620]
    for y in ys:
        d.line((xs[0], y, xs[-1], y), fill=0, width=3)
    for x in xs:
        d.line((x, ys[0], x, ys[-1]), fill=0, width=3)
    d.rectangle((xs[0] + 3, ys[0] + 3, xs[-1] - 3, ys[1] - 2), fill="white")   # слят ред
    d.line((xs[0], ys[0], xs[0], ys[1]), fill=0, width=3)
    d.line((xs[-1], ys[0], xs[-1], ys[1]), fill=0, width=3)
    d.text((xs[0] + 20, ys[0] + 18), "Списък на приложенията", font=f, fill=0)
    rows = [("№", "Индекс", "Заглавие", "Описание"),
            ("1", "НП-089-15", "Правила за безопасност", "Нормативен документ"),
            ("2", "36.ТО.RL.22", "Система питателна вода", "Техническо описание")]
    for r, row in enumerate(rows, start=1):
        for c, t in enumerate(row):
            d.text((xs[c] + 20, ys[r] + 18), t, font=f, fill=0)

    # Таблица 2: само външна рамка и една вертикална линия; етикет и стойност
    # в лявата половина са разделени с празно място (колона без линия)
    x0, x1, xm = 200, 2280, 1240
    ys2 = [800, 880, 960, 1040]
    for y in ys2:
        d.line((x0, y, x1, y), fill=0, width=2)
    for x in (x0, xm, x1):
        d.line((x, ys2[0], x, ys2[-1]), fill=0, width=2)
    data = [("Номер:", "6673", "Блок:", "Блок 6"),
            ("Вид:", "временно", "Сграда:", "МЗ 6"),
            ("Срок:", "30.11.2027 г.", "Система:", "6RL70")]
    for r, (a, b, c, e) in enumerate(data):
        y = ys2[r] + 18
        d.text((x0 + 20, y), a, font=f, fill=0)
        d.text((x0 + 420, y), b, font=f, fill=0)
        d.text((xm + 20, y), c, font=f, fill=0)
        d.text((xm + 300, y), e, font=f, fill=0)
    d.text((200, 1200), "Изменението не променя експлоатацията на системата.", font=f, fill=0)
    return img


def main() -> int:
    engine.configure_tesseract()
    img = make_page()
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("✔ " if cond else "✖ ") + msg)
        ok &= bool(cond)

    r = layout.clean_for_ocr(img)
    check(len(r.tables) == 2, f"намерени 2 таблици, не заглавието (намерени: {len(r.tables)})")

    page = engine.Page(Path("tables.png"), 0)
    engine.ocr_page(page, "bul+eng", img)
    tables = [b for b in page.paragraphs if isinstance(b, engine.TableBlock)]
    print(page.text)
    check(len(tables) == 2, "две таблици в резултата")
    if len(tables) == 2:
        t1, t2 = tables
        check((t1.n_rows, t1.n_cols) == (4, 4), f"таблица 1: 4×4 (е {t1.n_rows}×{t1.n_cols})")
        merged = [c for c in t1.cells if c.colspan == 4]
        check(bool(merged) and "приложенията" in merged[0].text, "слетият първи ред е една клетка")
        grid = t1.rows_text()
        check(grid[1][0] == "№" and grid[2][0] == "1" and grid[3][0] == "2", "колона „№“ с 1 и 2")
        check("НП-089-15" in grid[2][1], "индекс в правилната клетка")
        check((t2.n_rows, t2.n_cols) == (3, 4), f"таблица 2: 3×4 с колони без линия (е {t2.n_rows}×{t2.n_cols})")
        g2 = t2.rows_text()
        check(g2[0][1] == "6673" and g2[2][3] == "6RL70", "стойностите са в отделни клетки")
    blocks = [type(b).__name__ for b in page.paragraphs]
    check(blocks[0] == "Paragraph" and blocks[-1] == "Paragraph", "заглавието е преди, а текстът след таблиците")

    with tempfile.TemporaryDirectory() as tmp:
        from docx import Document
        from openpyxl import load_workbook
        engine.export([page], Path(tmp) / "t.docx")
        engine.export([page], Path(tmp) / "t.xlsx")
        doc = Document(str(Path(tmp) / "t.docx"))
        check(len(doc.tables) == 2, f"Word: 2 таблици (са {len(doc.tables)})")
        ws = load_workbook(str(Path(tmp) / "t.xlsx")).active
        values = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
        check("НП-089-15" in values and 6673 in values, "Excel: клетките и числата")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
