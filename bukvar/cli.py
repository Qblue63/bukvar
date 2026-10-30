"""Буквар — команден ред за пакетна обработка.

Примери:
  bukvar-cli скан.pdf                          → скан.ocr.pdf до оригинала
  bukvar-cli папка -r -f pdf,docx -o изход     → всички файлове в папката
  bukvar-cli снимка.jpg -l bul+eng+rus -f txt
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, engine


def _collect(inputs: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            files += sorted(f for f in it if f.is_file() and f.suffix.lower() in engine.SUPPORTED_EXTS
                            and ".ocr." not in f.name)
        elif p.is_file():
            files.append(p)
        else:
            print(f"Пропускам: {item} (не съществува)", file=sys.stderr)
    return files


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="bukvar-cli", description="Буквар OCR — разпознаване на текст от PDF и изображения.")
    ap.add_argument("inputs", nargs="*", help="файлове или папки")
    ap.add_argument("-o", "--out", help="папка за резултатите (по подразбиране: до оригинала)")
    ap.add_argument("-f", "--formats", default="pdf", help="pdf,docx,txt (по подразбиране: pdf)")
    ap.add_argument("-l", "--lang", default="bul+eng", help="езици, напр. bul+eng (по подразбиране)")
    ap.add_argument("-r", "--recursive", action="store_true", help="обхожда и подпапките")
    ap.add_argument("--no-rotate", action="store_true", help="без автоматично завъртане")
    ap.add_argument("--no-deskew", action="store_true", help="без изправяне на наклона")
    ap.add_argument("--no-forms", action="store_true",
                    help="без обработка на формуляри (квадратчета ☐/☒, ръкопис, редове от точки)")
    ap.add_argument("-j", "--jobs", type=int, default=None, help="брой паралелни страници")
    ap.add_argument("--list-langs", action="store_true", help="показва наличните езици")
    ap.add_argument("--version", action="version", version=f"Буквар {__version__}")
    args = ap.parse_args(argv)

    exe, _ = engine.configure_tesseract()
    if not exe:
        print("Не е намерен Tesseract.", file=sys.stderr)
        return 2
    if args.list_langs:
        for code in engine.available_languages():
            print(f"{code}\t{engine.LANG_NAMES.get(code, '')}")
        return 0

    if not args.inputs:
        ap.error("посочете поне един файл или папка")

    formats = [f.strip().lower().lstrip(".") for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f".{f}" not in engine.EXPORTERS]
    if bad:
        ap.error(f"неподдържан формат: {', '.join(bad)}")

    files = _collect(args.inputs, args.recursive)
    if not files:
        print("Няма файлове за обработка.", file=sys.stderr)
        return 1

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    for n, f in enumerate(files, 1):
        t = time.time()
        try:
            pages = engine.load_document(f)
            done = [0]

            def tick(_i: int) -> None:
                done[0] += 1
                print(f"\r[{n}/{len(files)}] {f.name}: страница {done[0]}/{len(pages)}", end="", flush=True)

            engine.process_pages(pages, args.lang, not args.no_rotate, not args.no_deskew,
                                 workers=args.jobs, clean_forms=not args.no_forms,
                                 on_page_done=tick)
            target_dir = out_dir or f.parent
            for fmt in formats:
                engine.export(pages, target_dir / f"{f.stem}.ocr.{fmt}")
            errs = [p for p in pages if p.error]
            note = f", {len(errs)} страници с грешка" if errs else ""
            print(f"\r[{n}/{len(files)}] {f.name}: {len(pages)} стр. за {time.time() - t:.1f} с{note}        ")
            failed += bool(errs)
        except Exception as exc:
            failed += 1
            print(f"\r[{n}/{len(files)}] {f.name}: ГРЕШКА — {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
