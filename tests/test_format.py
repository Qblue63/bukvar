"""Тест за форматирането: удебелен, курсив, подчертан текст, размер,
центриране, рамка, разделителна линия и колонтитул с табулации.

Пуска се с:  python tests/test_format.py
"""
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bukvar import engine  # noqa: E402

D = "/usr/share/fonts/truetype/liberation/"
REG, BOLD, ITAL = D + "LiberationSerif-Regular.ttf", D + "LiberationSerif-Bold.ttf", D + "LiberationSerif-Italic.ttf"
if not Path(REG).exists():  # Windows
    REG, BOLD, ITAL = (r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\timesbd.ttf",
                       r"C:\Windows\Fonts\timesi.ttf")
PT = 300 / 72   # px на пункт при 300 dpi


def make_page() -> Image.Image:
    W, H = 2480, 3508
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    title = ImageFont.truetype(BOLD, int(16 * PT))
    head = ImageFont.truetype(BOLD, int(11 * PT))
    body = ImageFont.truetype(REG, int(11 * PT))
    ital = ImageFont.truetype(ITAL, int(11 * PT))
    L, R = 240, 2240

    t = "Техническо решение за подмяна"
    tw = d.textlength(t, font=title)
    d.text(((L + R - tw) / 2, 250), t, font=title, fill=0)              # центрирано, удебелено

    u = "Подчертан ред за проверка"
    uw = d.textlength(u, font=body)
    d.text((L, 450), u, font=body, fill=0)
    d.line((L, 450 + int(11 * PT * 0.95), L + uw, 450 + int(11 * PT * 0.95)), fill=0, width=3)

    d.text((L, 600), "1. Основание за изменение на проекта", font=head, fill=0)
    y = 680
    for line in ("Допустимост на приложимата нормативна база. Изменението не променя",
                 "експлоатацията на системата и не изисква обучение на персонала."):
        d.text((L, y), line, font=body, fill=0)
        y += int(11 * PT * 1.25)
    d.text((L, y + 40), "Забележка:", font=body, fill=0)
    x = L + d.textlength("Забележка: ", font=body)
    d.text((x, y + 40), "важно уточнение в курсив", font=ital, fill=0)

    # рамка около един абзац
    d.rectangle((L - 20, 1050, R, 1160), outline=0, width=3)
    d.text((L, 1075), "Отлагане хидравлични изпитания на якост", font=body, fill=0)

    # колонтитул: линия и три части (ляво / център / дясно)
    d.line((L, 3200, R, 3200), fill=0, width=6)
    d.text((L, 3230), "ЕП-2.ТР.3602", font=head, fill=0)
    c = "4.11.2026 г."
    d.text(((L + R - d.textlength(c, font=head)) / 2, 3230), c, font=head, fill=0)
    r = "стр. 1 от 5"
    d.text((R - d.textlength(r, font=head), 3230), r, font=head, fill=0)
    return img


def main() -> int:
    engine.configure_tesseract()
    page = engine.Page(Path("format.png"), 0)
    engine.ocr_page(page, "bul+eng", make_page())
    pars = [b for b in page.paragraphs if isinstance(b, engine.Paragraph)]
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("✔ " if cond else "✖ ") + msg)
        ok &= bool(cond)

    def find(word):
        return next((p for p in pars if word in p.text), None)

    def runs_of(p, word):
        return [r for r in p.runs if word in r.text]

    for p in pars:
        print(f"{p.align:7s} {p.size_pt} | " + "".join(
            ("*" if r.bold else "") + ("/" if r.italic else "") + ("_" if r.underline else "") + r.text
            + ("*" if r.bold else "") for r in p.runs))

    t = find("Техническо")
    check(t and t.align == "center", "заглавието е центрирано")
    check(t and all(r.bold for r in t.runs), "заглавието е удебелено")
    check(t and t.size_pt and t.size_pt >= 14, f"заглавието е едро (≥14 pt, е {t and t.size_pt})")
    u = find("Подчертан")
    check(u and any(r.underline for r in u.runs), "подчертаният ред е подчертан")
    h = find("Основание")
    check(h and all(r.bold for r in h.runs), "„1. Основание…“ е удебелено")
    b = find("Допустимост")
    check(b and not any(r.bold for r in b.runs), "основният текст не е удебелен")
    check(b and "експлоатацията" in b.text, "двата реда са един абзац")
    check(b and b.size_pt == page.geom["body_pt"], "основният текст е с основния размер")
    n = find("Забележка")
    check(n and any(r.italic for r in runs_of(n, "курсив")) and not any(r.italic for r in runs_of(n, "Забележка")),
          "курсивът е само на курсивните думи")
    f = find("Отлагане")
    check(f and f.frame >= 0, "абзацът в рамката е с рамка")
    foot = find("ЕП-2")
    check(foot and foot.border_top, "колонтитулът е с линия отгоре")
    check(foot and [k for _, k in foot.tabs] == ["center", "right"], f"табулации център и дясно ({foot and foot.tabs})")
    check(page.geom.get("serif"), "шрифт със серифи (Times)")

    with tempfile.TemporaryDirectory() as tmp:
        from docx import Document
        engine.export([page], Path(tmp) / "f.docx")
        doc = Document(str(Path(tmp) / "f.docx"))
        check(doc.styles["Normal"].font.name == "Times New Roman", "Word: Times New Roman")
        check(any(r.bold for p in doc.paragraphs for r in p.runs if "Основание" in r.text), "Word: удебелен текст")
        engine.export([page], Path(tmp) / "p.docx", keep_format=False)
        plain = Document(str(Path(tmp) / "p.docx"))
        check(not any(r.bold for p in plain.paragraphs for r in p.runs), "Word без форматиране: чист текст")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
