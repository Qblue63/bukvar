"""Буквар — графичен интерфейс (PySide6)."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread, Signal, QUrl
from PySide6.QtGui import (QAction, QDesktopServices, QFont, QIcon, QImage, QKeySequence,
                           QPainter, QPixmap)
from PySide6.QtWidgets import (QApplication, QFileDialog, QGraphicsPixmapItem, QGraphicsScene,
                               QGraphicsView, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QSplitter,
                               QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import __version__, engine

APP_NAME = "Буквар"
FILE_FILTER = ("Всички поддържани (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp *.jp2);;"
               "PDF (*.pdf);;Изображения (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp *.jp2)")


def pil_to_qimage(img: Image.Image) -> QImage:
    img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name


# --------------------------------------------------------------------------
# Работа във фонов режим
# --------------------------------------------------------------------------

class OcrWorker(QObject):
    page_done = Signal(int)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, pages, indexes, langs, auto_rotate, deskew, forms=True):
        super().__init__()
        self.pages, self.indexes = pages, indexes
        self.langs, self.auto_rotate, self.deskew, self.forms = langs, auto_rotate, deskew, forms
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            subset = [self.pages[i] for i in self.indexes]
            for p in subset:
                p.reset()
            engine.process_pages(subset, self.langs, self.auto_rotate, self.deskew,
                                 clean_forms=self.forms,
                                 on_page_done=lambda k: self.page_done.emit(self.indexes[k]),
                                 should_stop=self._stop.is_set)
        except Exception as exc:  # pragma: no cover - показва се на потребителя
            self.failed.emit(str(exc))
        self.finished.emit()


class ThumbWorker(QObject):
    ready = Signal(int, QImage)
    done = Signal()

    def __init__(self, pages, start):
        super().__init__()
        self.pages, self.start = pages, start
        self._stop = threading.Event()

    def run(self) -> None:
        for i in range(self.start, len(self.pages)):
            if self._stop.is_set():
                break
            try:
                self.ready.emit(i, pil_to_qimage(self.pages[i].thumbnail(180)))
            except Exception:
                pass
        self.done.emit()


# --------------------------------------------------------------------------
# Изглед на страницата с мащабиране
# --------------------------------------------------------------------------

class PageView(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(Qt.gray)
        self._item: QGraphicsPixmapItem | None = None
        self._fit = True

    def show_image(self, qimg: QImage | None) -> None:
        self.scene().clear()
        self._item = None
        if qimg is None:
            return
        self._item = self.scene().addPixmap(QPixmap.fromImage(qimg))
        self.scene().setSceneRect(self._item.boundingRect())
        self._fit = True
        self.fit()

    def fit(self) -> None:
        if self._item:
            self.fitInView(self._item, Qt.KeepAspectRatio)
            self._fit = True

    def zoom(self, factor: float) -> None:
        self._fit = False
        self.scale(factor, factor)

    def wheelEvent(self, ev):
        # Ctrl + колелце = мащаб; само колелце = превъртане
        if ev.modifiers() & Qt.ControlModifier:
            self.zoom(1.15 if ev.angleDelta().y() > 0 else 1 / 1.15)
        else:
            super().wheelEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._fit:
            self.fit()


# --------------------------------------------------------------------------
# Главен прозорец
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Bukvar", "Bukvar")
        self.pages: list[engine.Page] = []
        self.current = -1
        self._loading_text = False
        self._ocr_thread: QThread | None = None
        self._ocr_worker: OcrWorker | None = None
        self._thumb_threads: list[tuple[QThread, ThumbWorker]] = []

        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        icon = resource("bukvar.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        exe, tessdata = engine.configure_tesseract()
        self.languages = engine.available_languages() if exe else []

        self._build_ui()
        self._build_toolbar()
        self.resize(self.settings.value("size", QSize(1280, 820)))
        self._update_actions()
        if not exe:
            QMessageBox.critical(self, APP_NAME, "Не е намерен двигателят Tesseract. "
                                 "Преинсталирайте програмата.")

    # ---------- изграждане ----------

    def _build_ui(self) -> None:
        self.page_list = QListWidget()
        self.page_list.setViewMode(QListWidget.IconMode)
        self.page_list.setFlow(QListWidget.TopToBottom)
        self.page_list.setWrapping(False)
        self.page_list.setIconSize(QSize(140, 180))
        self.page_list.setMovement(QListWidget.Static)
        self.page_list.setSpacing(6)
        self.page_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.page_list.setMinimumWidth(180)
        self.page_list.currentRowChanged.connect(self.show_page)

        self.view = PageView()

        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Segoe UI" if os.name == "nt" else "DejaVu Sans", 11))
        self.editor.setPlaceholderText("Отворете файл и натиснете „Разпознай“.\n\n"
                                       "Можете и да пуснете файлове с мишката в прозореца.")
        self.editor.textChanged.connect(self._text_changed)
        self.page_info = QLabel()
        self.page_info.setStyleSheet("color: palette(mid); padding: 2px 4px;")
        right = QWidget()
        lay = QVBoxLayout(right)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.page_info)
        lay.addWidget(self.editor)

        split = QSplitter()
        split.addWidget(self.page_list)
        split.addWidget(self.view)
        split.addWidget(right)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 2)
        split.setSizes([200, 620, 460])
        self.setCentralWidget(split)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Готов")

    def _build_toolbar(self) -> None:
        tb = QToolBar("Основни")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        st = self.style()
        self.act_open = QAction(st.standardIcon(st.StandardPixmap.SP_DialogOpenButton), "Отвори…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.open_files)
        tb.addAction(self.act_open)

        self.act_ocr = QAction(st.standardIcon(st.StandardPixmap.SP_MediaPlay), "Разпознай", self)
        self.act_ocr.setShortcut("F5")
        self.act_ocr.triggered.connect(self.recognize_all)
        tb.addAction(self.act_ocr)

        self.act_stop = QAction(st.standardIcon(st.StandardPixmap.SP_MediaStop), "Спри", self)
        self.act_stop.triggered.connect(self.stop_ocr)
        tb.addAction(self.act_stop)

        # Езици
        self.lang_button = QToolButton()
        self.lang_button.setPopupMode(QToolButton.InstantPopup)
        self.lang_menu = QMenu(self)
        saved = (self.settings.value("langs", "bul+eng") or "bul+eng").split("+")
        self.lang_actions: dict[str, QAction] = {}
        priority = {"bul": 0, "eng": 1}
        ordered = sorted(self.languages, key=lambda c: (priority.get(c, 2), engine.LANG_NAMES.get(c, c)))
        for code in ordered:
            a = QAction(f"{engine.LANG_NAMES.get(code, code)} ({code})", self, checkable=True)
            a.setChecked(code in saved)
            a.toggled.connect(self._langs_changed)
            self.lang_menu.addAction(a)
            self.lang_actions[code] = a
        self.lang_button.setMenu(self.lang_menu)
        tb.addSeparator()
        tb.addWidget(QLabel(" Езици: "))
        tb.addWidget(self.lang_button)
        self._langs_changed()

        # Настройки
        self.opt_menu = QMenu(self)
        self.act_rotate = QAction("Автоматично завъртане", self, checkable=True)
        self.act_rotate.setChecked(self.settings.value("auto_rotate", True, type=bool))
        self.act_deskew = QAction("Изправяне на наклона", self, checkable=True)
        self.act_deskew.setChecked(self.settings.value("deskew", True, type=bool))
        self.act_forms = QAction("Формуляри: квадратчета ☐/☒ и ръкопис", self, checkable=True)
        self.act_forms.setChecked(self.settings.value("forms", True, type=bool))
        self.act_forms.setToolTip("Разпознава квадратчетата за отметка, маха подписите и "
                                  "ръкописа (синьо мастило) и редовете от точки.")
        self.opt_menu.addActions([self.act_rotate, self.act_deskew, self.act_forms])
        self.opt_menu.setToolTipsVisible(True)
        opt_btn = QToolButton()
        opt_btn.setText("Настройки")
        opt_btn.setPopupMode(QToolButton.InstantPopup)
        opt_btn.setMenu(self.opt_menu)
        tb.addWidget(opt_btn)
        tb.addSeparator()

        # Запис
        save_menu = QMenu(self)
        self.act_save_pdf = save_menu.addAction("Търсим PDF…", lambda: self.save(".pdf"))
        self.act_save_docx = save_menu.addAction("Word документ (DOCX)…", lambda: self.save(".docx"))
        self.act_save_txt = save_menu.addAction("Текстов файл (TXT)…", lambda: self.save(".txt"))
        self.save_button = QToolButton()
        self.save_button.setText("Запиши като")
        self.save_button.setIcon(st.standardIcon(st.StandardPixmap.SP_DialogSaveButton))
        self.save_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.save_button.setPopupMode(QToolButton.InstantPopup)
        self.save_button.setMenu(save_menu)
        tb.addWidget(self.save_button)

        self.act_copy = QAction("Копирай текста", self)
        self.act_copy.setShortcut("Ctrl+Shift+C")
        self.act_copy.triggered.connect(self.copy_all)
        tb.addAction(self.act_copy)

        self.act_clear = QAction("Затвори", self)
        self.act_clear.triggered.connect(self.clear)
        tb.addAction(self.act_clear)

        tb.addSeparator()
        zoom_in = QAction("+", self, triggered=lambda: self.view.zoom(1.25))
        zoom_in.setShortcut(QKeySequence.ZoomIn)
        zoom_out = QAction("−", self, triggered=lambda: self.view.zoom(0.8))
        zoom_out.setShortcut(QKeySequence.ZoomOut)
        fit = QAction("Цяла страница", self, triggered=self.view.fit)
        tb.addActions([zoom_out, zoom_in, fit])

        about = QAction("За програмата", self, triggered=self.about)
        tb.addAction(about)

    # ---------- езици ----------

    def selected_langs(self) -> str:
        codes = [c for c, a in self.lang_actions.items() if a.isChecked()]
        return "+".join(codes)

    def _langs_changed(self) -> None:
        langs = self.selected_langs()
        names = [engine.LANG_NAMES.get(c, c) for c in langs.split("+") if c]
        self.lang_button.setText(", ".join(names) or "изберете")
        if langs:
            self.settings.setValue("langs", langs)
        if hasattr(self, "act_clear"):
            self._update_actions()

    # ---------- файлове ----------

    def open_files(self) -> None:
        start = self.settings.value("last_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(self, "Отваряне на файлове", start, FILE_FILTER)
        if files:
            self.settings.setValue("last_dir", str(Path(files[0]).parent))
            self.add_files(files)

    def add_files(self, files) -> None:
        first_new = len(self.pages)
        errors = []
        for f in files:
            try:
                self.pages.extend(engine.load_document(f))
            except Exception as exc:
                errors.append(f"{Path(f).name}: {exc}")
        for i in range(first_new, len(self.pages)):
            item = QListWidgetItem(self._item_label(i))
            item.setTextAlignment(Qt.AlignHCenter)
            item.setSizeHint(QSize(160, 215))
            self.page_list.addItem(item)
        if errors:
            QMessageBox.warning(self, APP_NAME, "Някои файлове не можаха да се отворят:\n\n" + "\n".join(errors))
        if len(self.pages) > first_new:
            self._start_thumbnails(first_new)
            self.page_list.setCurrentRow(first_new)
            self.statusBar().showMessage(f"Заредени {len(self.pages) - first_new} страници. "
                                         "Натиснете „Разпознай“ (F5).")
        self._update_actions()

    def _start_thumbnails(self, start: int) -> None:
        thread, worker = QThread(self), ThumbWorker(self.pages, start)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ready.connect(self._set_thumb)
        thread.finished.connect(thread.deleteLater)
        self._thumb_threads.append((thread, worker))
        worker.done.connect(thread.quit)
        thread.start()

    def _set_thumb(self, i: int, qimg: QImage) -> None:
        item = self.page_list.item(i)
        if item:
            item.setIcon(QIcon(QPixmap.fromImage(qimg)))

    def _item_label(self, i: int) -> str:
        p = self.pages[i]
        mark = ""
        if p.error:
            mark = "  ✖"
        elif p.done:
            mark = f"  ✔ {p.confidence:.0f}%" if p.confidence is not None else "  ✔"
        return f"Стр. {i + 1}{mark}"

    def clear(self) -> None:
        if self._busy():
            return
        self.pages.clear()
        self.page_list.clear()
        self.current = -1
        self.view.show_image(None)
        self._set_editor_text("")
        self.page_info.clear()
        self._update_actions()
        self.statusBar().showMessage("Готов")

    # ---------- изглед ----------

    def show_page(self, i: int) -> None:
        self.current = i
        if i < 0 or i >= len(self.pages):
            return
        p = self.pages[i]
        try:
            self.view.show_image(pil_to_qimage(p.image()))
        except Exception as exc:
            self.view.show_image(None)
            self.statusBar().showMessage(f"Грешка при показване: {exc}")
        self._set_editor_text(p.text)
        self.editor.setReadOnly(not p.done)
        info = p.label
        if p.error:
            info += f" — грешка: {p.error}"
        elif p.done:
            info += f" — разпознато, увереност {p.confidence or 0:.0f}%"
            if p.text_edited:
                info += " (редактирано)"
        else:
            info += " — не е разпозната"
        self.page_info.setText(info)

    def _set_editor_text(self, text: str) -> None:
        self._loading_text = True
        self.editor.setPlainText(text)
        self._loading_text = False

    def _text_changed(self) -> None:
        if self._loading_text or not (0 <= self.current < len(self.pages)):
            return
        p = self.pages[self.current]
        p.text = self.editor.toPlainText()
        p.text_edited = True

    # ---------- разпознаване ----------

    def _busy(self) -> bool:
        return self._ocr_thread is not None

    def recognize_all(self) -> None:
        rows = sorted({ix.row() for ix in self.page_list.selectedIndexes()})
        if len(rows) <= 1:
            rows = list(range(len(self.pages)))
        if any(self.pages[i].text_edited for i in rows):
            if QMessageBox.question(self, APP_NAME, "Има ръчни поправки в текста. "
                                    "Повторното разпознаване ще ги изтрие. Продължаване?") != QMessageBox.Yes:
                return
        self.start_ocr(rows)

    def start_ocr(self, indexes: list[int]) -> None:
        if self._busy() or not indexes:
            return
        langs = self.selected_langs()
        if not langs:
            QMessageBox.information(self, APP_NAME, "Изберете поне един език.")
            return
        self.settings.setValue("auto_rotate", self.act_rotate.isChecked())
        self.settings.setValue("deskew", self.act_deskew.isChecked())
        self.settings.setValue("forms", self.act_forms.isChecked())
        self._done_count, self._total = 0, len(indexes)
        self.progress.setRange(0, len(indexes))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.statusBar().showMessage("Разпознаване…")

        self._ocr_thread = QThread(self)
        self._ocr_worker = OcrWorker(self.pages, indexes, langs,
                                     self.act_rotate.isChecked(), self.act_deskew.isChecked(),
                                     self.act_forms.isChecked())
        self._ocr_worker.moveToThread(self._ocr_thread)
        self._ocr_thread.started.connect(self._ocr_worker.run)
        self._ocr_worker.page_done.connect(self._page_done)
        self._ocr_worker.failed.connect(lambda m: QMessageBox.critical(self, APP_NAME, m))
        self._ocr_worker.finished.connect(self._ocr_finished)
        self._ocr_thread.start()
        self._update_actions()

    def _page_done(self, i: int) -> None:
        self._done_count += 1
        self.progress.setValue(self._done_count)
        self.page_list.item(i).setText(self._item_label(i))
        self.statusBar().showMessage(f"Разпознаване… {self._done_count} от {self._total}")
        if i == self.current:
            self.show_page(i)

    def _ocr_finished(self) -> None:
        self._ocr_thread.quit()
        self._ocr_thread.wait()
        self._ocr_thread = None
        self._ocr_worker = None
        self.progress.setVisible(False)
        errors = sum(1 for p in self.pages if p.error)
        msg = f"Готово: {self._done_count} страници"
        if errors:
            msg += f", {errors} с грешка"
        self.statusBar().showMessage(msg)
        if 0 <= self.current < len(self.pages):
            self.show_page(self.current)
        self._update_actions()

    def stop_ocr(self) -> None:
        if self._ocr_worker:
            self._ocr_worker.stop()
            self.statusBar().showMessage("Спиране след текущите страници…")

    # ---------- запис ----------

    def save(self, ext: str) -> None:
        done = [p for p in self.pages if p.done]
        if not done:
            return
        if len(done) < len(self.pages):
            if QMessageBox.question(self, APP_NAME, f"Разпознати са {len(done)} от {len(self.pages)} страници. "
                                    "Да се запишат само разпознатите?") != QMessageBox.Yes:
                return
        if ext == ".pdf" and any(p.text_edited for p in done):
            QMessageBox.information(self, APP_NAME, "Ръчните поправки се записват в DOCX и TXT. "
                                    "В PDF невидимият текстов слой остава от разпознаването.")
        default_dir = Path(self.settings.value("save_dir", str(done[0].source.parent)))
        default = default_dir / f"{done[0].source.stem}.ocr{ext}"
        names = {".pdf": "PDF (*.pdf)", ".docx": "Word (*.docx)", ".txt": "Текст (*.txt)"}
        path, _ = QFileDialog.getSaveFileName(self, "Запис", str(default), names[ext])
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            engine.export(done, path)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, f"Записът не успя:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self.settings.setValue("save_dir", str(Path(path).parent))
        self.statusBar().showMessage(f"Записано: {path}")
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(f"Файлът е записан:\n{path}")
        open_btn = box.addButton("Отвори", QMessageBox.AcceptRole)
        box.addButton("Затвори", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def copy_all(self) -> None:
        text = "\n\n".join(p.text for p in self.pages if p.done)
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("Текстът е копиран в клипборда")

    # ---------- общи ----------

    def _update_actions(self) -> None:
        busy = self._busy()
        has = bool(self.pages)
        any_done = any(p.done for p in self.pages)
        self.act_open.setEnabled(not busy)
        self.act_ocr.setEnabled(has and not busy and bool(self.selected_langs()))
        self.act_stop.setEnabled(busy)
        self.save_button.setEnabled(any_done and not busy)
        self.act_copy.setEnabled(any_done)
        self.act_clear.setEnabled(has and not busy)
        self.lang_button.setEnabled(not busy)

    def about(self) -> None:
        QMessageBox.about(self, APP_NAME,
                          f"<h3>{APP_NAME} {__version__}</h3>"
                          "<p>Безплатна програма за разпознаване на текст от сканирани документи и снимки.</p>"
                          "<p>Използва двигателя <a href='https://github.com/tesseract-ocr/tesseract'>Tesseract</a> "
                          "(лиценз Apache 2.0) и Qt for Python (LGPL).</p>")

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        files = []
        for url in ev.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                files += sorted(str(f) for f in p.iterdir() if f.suffix.lower() in engine.SUPPORTED_EXTS)
            elif p.suffix.lower() in engine.SUPPORTED_EXTS:
                files.append(str(p))
        if files and not self._busy():
            self.add_files(files)

    def closeEvent(self, ev):
        if self._busy():
            self.stop_ocr()
            self._ocr_thread.quit()
            self._ocr_thread.wait(10000)
        for thread, worker in self._thumb_threads:
            worker._stop.set()
            try:
                thread.quit()
                thread.wait(2000)
            except RuntimeError:
                pass
        self.settings.setValue("size", self.size())
        super().closeEvent(ev)


def main() -> int:
    if os.name == "nt":
        try:  # собствена иконка в лентата на задачите
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Bukvar.OCR")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    files = [a for a in sys.argv[1:] if Path(a).suffix.lower() in engine.SUPPORTED_EXTS and Path(a).exists()]
    if files:
        win.add_files(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
