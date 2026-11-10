"""Буквар — графичен интерфейс (PySide6)."""
from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread, Signal, QUrl
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QDesktopServices, QFont, QIcon,
                           QImage, QKeySequence, QPainter, QPixmap)
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

class OcrQueue(QObject):
    """Опашка за разпознаване: всяка страница е отделна задача, няколко
    страници се разпознават едновременно. Страници могат да се добавят и
    докато други се разпознават."""
    page_started = Signal(int)
    page_finished = Signal(int)
    page_cancelled = Signal(int)
    idle = Signal()

    def __init__(self, workers: int):
        super().__init__()
        self.workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr")
        self._futures: dict[int, Future] = {}
        self._lock = threading.Lock()

    def set_workers(self, n: int) -> None:
        """Новият брой важи за следващите задачи (текущите довършват)."""
        if n == self.workers:
            return
        self.workers = n
        old = self._pool
        self._pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="ocr")
        if not self.busy():
            old.shutdown(wait=False)
        else:
            self._old_pool = old   # затваря се, когато опашката се изпразни

    def busy(self) -> bool:
        with self._lock:
            return bool(self._futures)

    def pending(self, i: int) -> bool:
        with self._lock:
            return i in self._futures

    def submit(self, i: int, page, params: dict) -> bool:
        with self._lock:
            if i in self._futures:
                return False      # вече е в опашката
            fut = self._pool.submit(self._run, i, page, params)
            self._futures[i] = fut
        fut.add_done_callback(lambda f, i=i: self._finished(i, f))
        return True

    def _run(self, i: int, page, params: dict) -> None:
        self.page_started.emit(i)
        engine.process_page(page, **params)

    def _finished(self, i: int, fut: Future) -> None:
        with self._lock:
            self._futures.pop(i, None)
            empty = not self._futures
        if fut.cancelled():
            self.page_cancelled.emit(i)
        else:
            self.page_finished.emit(i)
        if empty:
            old = getattr(self, "_old_pool", None)
            if old is not None:
                old.shutdown(wait=False)
                self._old_pool = None
            self.idle.emit()

    def cancel(self, indexes=None) -> int:
        """Маха чакащите задачи (започналите довършват). Връща колко са махнати."""
        with self._lock:
            items = [(i, f) for i, f in self._futures.items() if indexes is None or i in indexes]
        return sum(1 for _, f in items if f.cancel())

    def shutdown(self) -> None:
        self.cancel()
        self._pool.shutdown(wait=True)


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
        self._running: set[int] = set()     # страници, които се разпознават в момента
        self._batch_total = 0
        self._batch_done = 0
        self._thumb_threads: list[tuple[QThread, ThumbWorker]] = []

        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        icon = resource("bukvar.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        exe, tessdata = engine.configure_tesseract()
        self.languages = engine.available_languages() if exe else []

        workers = self.settings.value("workers", engine.default_workers(), type=int)
        self.queue = OcrQueue(max(1, workers))
        self.queue.page_started.connect(self._page_started)
        self.queue.page_finished.connect(self._page_done)
        self.queue.page_cancelled.connect(self._page_cancelled)
        self.queue.idle.connect(self._ocr_finished)

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
        self.page_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.page_list.customContextMenuRequested.connect(self._page_menu)
        self.page_list.setToolTip("Десен бутон върху страница — разпознаване само на нея")

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
        self.act_ocr.setToolTip("Разпознава избраните страници, а ако е избрана една — всички "
                                "нерозпознати (F5).\nДесен бутон върху страница — само тя.")
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
        self.act_tables = QAction("Таблици", self, checkable=True)
        self.act_tables.setChecked(self.settings.value("tables", True, type=bool))
        self.act_tables.setToolTip("Открива таблиците с линии и ги записва като истински "
                                   "таблици в Word и Excel.")
        self.act_format = QAction("Запази форматирането в Word", self, checkable=True)
        self.act_format.setChecked(self.settings.value("keep_format", True, type=bool))
        self.act_format.setToolTip("Шрифт, размер, удебелен/курсив/подчертан текст, подравняване, "
                                   "отстъпи, рамки и полета като в оригинала. Изключено — чист текст.")
        self.act_format.toggled.connect(lambda v: self.settings.setValue("keep_format", v))
        self.opt_menu.addActions([self.act_rotate, self.act_deskew, self.act_forms, self.act_tables])
        self.opt_menu.addSeparator()
        self.opt_menu.addAction(self.act_format)
        self.opt_menu.addSeparator()
        workers_menu = self.opt_menu.addMenu("Едновременно страници")
        group = QActionGroup(self)
        cores = os.cpu_count() or 2
        for n in sorted({1, 2, 3, 4, 6, 8, self.queue.workers}):
            a = QAction(f"{n}" + ("  (препоръчително)" if n == engine.default_workers() else ""),
                        self, checkable=True)
            a.setChecked(n == self.queue.workers)
            a.setToolTip(f"Компютърът има {cores} ядра. Повече страници едновременно "
                         "искат и повече памет (~300 MB на страница).")
            a.triggered.connect(lambda _=False, n=n: self._set_workers(n))
            group.addAction(a)
            workers_menu.addAction(a)
        workers_menu.setToolTipsVisible(True)
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
        self.act_save_xlsx = save_menu.addAction("Таблиците в Excel (XLSX)…", lambda: self.save(".xlsx"))
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
        if i in self._running:
            mark = "  ⟳ разпознава се"
        elif self.queue.pending(i):
            mark = "  ⏳ в опашка"
        elif p.error:
            mark = "  ✖ грешка"
        elif p.done:
            mark = f"  ✔ {p.confidence:.0f}%" if p.confidence is not None else "  ✔"
        else:
            mark = ""
        return f"Стр. {i + 1}{mark}"

    def _refresh_item(self, i: int) -> None:
        item = self.page_list.item(i)
        if item is None:
            return
        item.setText(self._item_label(i))
        p = self.pages[i]
        if p.error and i not in self._running and not self.queue.pending(i):
            item.setForeground(QBrush(QColor(190, 30, 30)))
            item.setToolTip(f"Грешка: {p.error}\nДесен бутон → „Разпознай отново“")
        else:
            item.setData(Qt.ForegroundRole, None)
            item.setToolTip(p.label)

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
        self.editor.setReadOnly(not p.done or self.queue.pending(i))
        info = p.label
        if i in self._running:
            info += " — разпознава се…"
        elif self.queue.pending(i):
            info += " — в опашка"
        elif p.error:
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
        return self.queue.busy()

    def _selected_rows(self) -> list[int]:
        return sorted({ix.row() for ix in self.page_list.selectedIndexes()})

    def recognize_all(self) -> None:
        """F5: избраните страници; ако е избрана една — всички нерозпознати."""
        rows = self._selected_rows()
        if len(rows) <= 1:
            rows = [i for i, p in enumerate(self.pages) if not p.done and not self.queue.pending(i)]
            if not rows:
                if not self.pages or self._busy():
                    return
                if QMessageBox.question(self, APP_NAME, "Всички страници вече са разпознати. "
                                        "Да се разпознаят ли отново?") != QMessageBox.Yes:
                    return
                rows = list(range(len(self.pages)))
        self.recognize(rows)

    def recognize(self, rows: list[int]) -> None:
        rows = [i for i in rows if 0 <= i < len(self.pages) and not self.queue.pending(i)]
        if not rows:
            return
        if any(self.pages[i].text_edited for i in rows):
            if QMessageBox.question(self, APP_NAME, "Има ръчни поправки в текста. "
                                    "Повторното разпознаване ще ги изтрие. Продължаване?") != QMessageBox.Yes:
                return
        self.start_ocr(rows)

    def start_ocr(self, indexes: list[int]) -> None:
        langs = self.selected_langs()
        if not langs:
            QMessageBox.information(self, APP_NAME, "Изберете поне един език.")
            return
        self.settings.setValue("auto_rotate", self.act_rotate.isChecked())
        self.settings.setValue("deskew", self.act_deskew.isChecked())
        self.settings.setValue("forms", self.act_forms.isChecked())
        self.settings.setValue("tables", self.act_tables.isChecked())
        params = dict(langs=langs, auto_rotate=self.act_rotate.isChecked(),
                      deskew=self.act_deskew.isChecked(), clean_forms=self.act_forms.isChecked(),
                      detect_tables=self.act_tables.isChecked())
        if not self._busy():
            self._batch_total = self._batch_done = 0
        added = 0
        for i in indexes:
            if self.queue.submit(i, self.pages[i], params):
                added += 1
                self._refresh_item(i)
        if not added:
            return
        self._batch_total += added
        self.progress.setRange(0, self._batch_total)
        self.progress.setValue(self._batch_done)
        self.progress.setVisible(True)
        self._status_running()
        if self.current in indexes:
            self.show_page(self.current)
        self._update_actions()

    def _status_running(self) -> None:
        self.statusBar().showMessage(
            f"Разпознаване… {self._batch_done} от {self._batch_total} "
            f"(едновременно: {self.queue.workers})")

    def _page_started(self, i: int) -> None:
        self._running.add(i)
        self._refresh_item(i)
        if i == self.current:
            self.show_page(i)

    def _page_done(self, i: int) -> None:
        self._running.discard(i)
        self._batch_done += 1
        self.progress.setValue(self._batch_done)
        self._refresh_item(i)
        self._status_running()
        if i == self.current:
            self.show_page(i)
        self._update_actions()

    def _page_cancelled(self, i: int) -> None:
        self._running.discard(i)
        self._batch_total = max(self._batch_done, self._batch_total - 1)
        self.progress.setRange(0, max(1, self._batch_total))
        self._refresh_item(i)
        if i == self.current:
            self.show_page(i)

    def _ocr_finished(self) -> None:
        self.progress.setVisible(False)
        errors = sum(1 for p in self.pages if p.error)
        msg = f"Готово: {self._batch_done} страници"
        if errors:
            msg += f", {errors} с грешка — десен бутон → „Разпознай отново неуспешните“"
        self.statusBar().showMessage(msg)
        if 0 <= self.current < len(self.pages):
            self.show_page(self.current)
        self._update_actions()

    def stop_ocr(self) -> None:
        n = self.queue.cancel()
        if self._running:
            self.statusBar().showMessage(f"Махнати от опашката: {n}. "
                                         "Започналите страници се довършват…")

    def _set_workers(self, n: int) -> None:
        self.queue.set_workers(n)
        self.settings.setValue("workers", n)
        if self._busy():
            self.statusBar().showMessage(f"Едновременно страници: {n} (за следващите в опашката)")

    def _page_menu(self, pos) -> None:
        item = self.page_list.itemAt(pos)
        if item is not None and not item.isSelected():
            self.page_list.setCurrentItem(item)       # десен бутон избира страницата
        rows = self._selected_rows()
        menu = QMenu(self)
        if rows:
            label = (f"Разпознай страница {rows[0] + 1}" if len(rows) == 1
                     else f"Разпознай избраните страници ({len(rows)})")
            if all(self.pages[i].done or self.pages[i].error for i in rows):
                label = label.replace("Разпознай", "Разпознай отново")
            a = menu.addAction(label, lambda: self.recognize(rows))
            a.setEnabled(any(not self.queue.pending(i) for i in rows) and bool(self.selected_langs()))
            menu.setDefaultAction(a)
        todo = [i for i, p in enumerate(self.pages) if not p.done and not p.error and not self.queue.pending(i)]
        failed = [i for i, p in enumerate(self.pages) if p.error and not self.queue.pending(i)]
        free = [i for i in range(len(self.pages)) if not self.queue.pending(i)]
        menu.addAction(f"Разпознай нерозпознатите ({len(todo)})", lambda: self.recognize(todo)).setEnabled(bool(todo))
        if failed:
            menu.addAction(f"Разпознай отново неуспешните ({len(failed)})", lambda: self.recognize(failed))
        menu.addAction("Разпознай всички страници", lambda: self.recognize(free)).setEnabled(bool(free))
        queued = [i for i in rows if self.queue.pending(i) and i not in self._running]
        if queued:
            menu.addSeparator()
            menu.addAction("Махни от опашката", lambda: self.queue.cancel(set(queued)))
        done_rows = [i for i in rows if self.pages[i].done]
        if done_rows:
            menu.addSeparator()
            sub = menu.addMenu("Запиши " + ("страницата" if len(done_rows) == 1 else f"избраните ({len(done_rows)})"))
            sub.addAction("Търсим PDF…", lambda: self.save(".pdf", done_rows))
            sub.addAction("Word документ (DOCX)…", lambda: self.save(".docx", done_rows))
            sub.addAction("Таблиците в Excel (XLSX)…", lambda: self.save(".xlsx", done_rows))
            sub.addAction("Текстов файл (TXT)…", lambda: self.save(".txt", done_rows))
            menu.addAction("Копирай текста", lambda: QApplication.clipboard().setText(
                "\n\n".join(self.pages[i].text for i in done_rows)))
        menu.exec(self.page_list.viewport().mapToGlobal(pos))

    # ---------- запис ----------

    def save(self, ext: str, rows: list[int] | None = None) -> None:
        if rows is not None:
            done = [self.pages[i] for i in rows if self.pages[i].done]
        else:
            done = [p for p in self.pages if p.done]
        if not done:
            return
        if rows is None and len(done) < len(self.pages):
            if QMessageBox.question(self, APP_NAME, f"Разпознати са {len(done)} от {len(self.pages)} страници. "
                                    "Да се запишат само разпознатите?") != QMessageBox.Yes:
                return
        if ext == ".pdf" and any(p.text_edited for p in done):
            QMessageBox.information(self, APP_NAME, "Ръчните поправки се записват в DOCX и TXT. "
                                    "В PDF невидимият текстов слой остава от разпознаването.")
        default_dir = Path(self.settings.value("save_dir", str(done[0].source.parent)))
        suffix = ""
        if rows is not None and len(done) == 1:
            suffix = f".стр{done[0].index + 1}"
        default = default_dir / f"{done[0].source.stem}{suffix}.ocr{ext}"
        names = {".pdf": "PDF (*.pdf)", ".docx": "Word (*.docx)", ".xlsx": "Excel (*.xlsx)",
                 ".txt": "Текст (*.txt)"}
        path, _ = QFileDialog.getSaveFileName(self, "Запис", str(default), names[ext])
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            engine.export(done, path, keep_format=self.act_format.isChecked())
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
        self.act_open.setEnabled(True)            # може да се добавят файлове и по време на работа
        self.act_ocr.setEnabled(has and bool(self.selected_langs()))
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
        if files:
            self.add_files(files)

    def closeEvent(self, ev):
        if self._busy():
            self.statusBar().showMessage("Довършване на започналите страници…")
            QApplication.processEvents()
        self.queue.shutdown()
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
