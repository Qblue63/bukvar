"""Пуска двигателя върху тестовите файлове и мери точността (CER)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bukvar import engine  # noqa: E402

SAMPLES = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

def cer(ref: str, hyp: str) -> float:
    ref, hyp = " ".join(ref.split()), " ".join(hyp.split())
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        cur = [i]
        for j, hc in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / max(1, len(ref))

exe, tessdata = engine.configure_tesseract()
print("tesseract:", exe, "| tessdata:", tessdata, "| езици:", engine.available_languages())
expected = (SAMPLES / "expected.txt").read_text(encoding="utf-8")

for name in ["clean.png", "skewed.jpg", "rotated90.pdf", "rotate_attr.pdf", "multi.tif"]:
    t = time.time()
    pages = engine.load_document(SAMPLES / name)
    engine.process_pages(pages, "bul+eng")
    stem = Path(name).stem
    for ext in (".pdf", ".docx", ".txt"):
        engine.export(pages, OUT / f"{stem}{ext}")
    scores = [f"{cer(expected, p.text) * 100:.2f}%" for p in pages]
    print(f"{name:16s} стр={len(pages)} завъртяна={[p.geometry_changed for p in pages]} "
          f"CER={scores} conf={[p.confidence for p in pages]} {time.time() - t:.1f}s")
    for p in pages:
        if p.error:
            print("   ГРЕШКА:", p.error[:300])

print("\n--- clean.png ---\n" + (OUT / "clean.txt").read_text(encoding="utf-8"))
