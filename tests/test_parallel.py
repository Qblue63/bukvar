"""Тест за паралелната обработка: страници с различен размер, обработвани
едновременно, трябва да дадат същия резултат като една по една.

(Във версия 0.4.0 споделено междинно състояние смесваше данните на
страниците и случайна страница пропадаше.)

Пуска се с:  python tests/test_parallel.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bukvar import engine  # noqa: E402
import test_format  # noqa: E402
import test_tables  # noqa: E402


def make_pages():
    imgs = []
    for k, maker in enumerate((test_tables.make_page, test_format.make_page) * 2):
        img = maker()
        # различни размери — както при сканове от една и съща страница
        img = img.resize((img.width - 3 * k, img.height + 5 * k))
        imgs.append(img)
    return imgs


def run(imgs, workers):
    pages = [engine.Page(Path(f"p{k}.png"), k) for k in range(len(imgs))]
    from concurrent.futures import ThreadPoolExecutor

    def job(k):
        try:
            engine.ocr_page(pages[k], "bul+eng", imgs[k])
        except Exception as exc:
            pages[k].error = str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(job, range(len(imgs))))
    return pages


def main() -> int:
    engine.configure_tesseract()
    imgs = make_pages()
    ref = run(imgs, 1)
    ok = True
    for attempt in range(2):
        par = run(imgs, 4)
        errors = [p.error for p in par if p.error]
        same = all(a.text == b.text for a, b in zip(ref, par))
        print(("✔ " if not errors else "✖ ") + f"паралелно, опит {attempt + 1}: без грешки {errors[:1]}")
        print(("✔ " if same else "✖ ") + f"паралелно, опит {attempt + 1}: същият текст като последователно")
        ok &= not errors and same
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
