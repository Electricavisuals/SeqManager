import os
import re
import sys
import queue
import subprocess
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QDialog, QSpinBox, QFormLayout, QPushButton, QProgressBar, QScrollArea,
    QCheckBox, QStackedWidget, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, QTimer, QEvent, Signal
from PySide6.QtGui import QPixmap, QImage, QCursor, QColor, QPainter

from detector import Sequence, Video, scan_folder
from encode_panel import EncodePanel
import config as cfg


# ── Image loading (thread-safe) ───────────────────────────────────────────────

def load_frame_qimage(path: str, max_height: int) -> QImage:
    ext = Path(path).suffix.lower()
    try:
        if ext == '.exr':
            return _load_exr_qimage(path, max_height)
        from PIL import Image
        img = Image.open(path).convert('RGB')
        w, h = img.size
        if h > max_height:
            factor = 1
            while h // (factor * 2) > max_height:
                factor *= 2
            if factor > 1:
                img = img.reduce(factor)
                w, h = img.size
            img = img.resize((int(w * max_height / h), max_height), Image.BILINEAR)
        data = img.tobytes('raw', 'RGB')
        return QImage(bytes(data), img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
    except Exception:
        return _placeholder_qimage(max_height)


def _load_exr_qimage(path: str, max_height: int) -> QImage:
    try:
        import OpenEXR, Imath, numpy as np
        f = OpenEXR.InputFile(path)
        dw = f.header()['dataWindow']
        w = dw.max.x - dw.min.x + 1
        h = dw.max.y - dw.min.y + 1
        pt = Imath.PixelType(Imath.PixelType.FLOAT)
        r = np.frombuffer(f.channel('R', pt), np.float32).reshape(h, w)
        g = np.frombuffer(f.channel('G', pt), np.float32).reshape(h, w)
        b = np.frombuffer(f.channel('B', pt), np.float32).reshape(h, w)
        f.close()
        rgb = np.clip(np.stack([r, g, b], axis=-1) ** (1 / 2.2), 0, 1)
        data = (rgb * 255).astype(np.uint8).tobytes()
        qimg = QImage(data, w, h, w * 3, QImage.Format_RGB888).copy()
        if h > max_height:
            qimg = qimg.scaledToHeight(max_height, Qt.SmoothTransformation)
        return qimg
    except Exception:
        return _placeholder_qimage(max_height)


def _placeholder_qimage(max_height: int) -> QImage:
    img = QImage(int(max_height * 16 / 9), max_height, QImage.Format_RGB888)
    img.fill(QColor('#2a2a35'))
    return img


# ── Background loader per seqüències ─────────────────────────────────────────

class FrameLoader:
    def __init__(self, seq: Sequence, max_height: int, skip_indices: set = None):
        self._seq = seq
        self._max_height = max_height
        self._skip = skip_indices or set()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        for i, path in enumerate(self._seq.frames):
            if self._stop.is_set():
                break
            if i in self._skip:
                continue
            qimg = load_frame_qimage(path, self._max_height)
            self._queue.put((i, qimg))
        self._queue.put(None)

    def drain(self, on_frame, on_done, max_per_tick: int = 3):
        processed = 0
        while processed < max_per_tick:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                on_done()
                return True
            idx, qimg = item
            on_frame(idx, QPixmap.fromImage(qimg))
            processed += 1
        return False

    def stop(self):
        self._stop.set()


class PreviewLoader(QThread):
    """Loads frame 0 of each sequence sequentially before full buffer loading starts."""
    frame_ready = Signal(object, object)  # Sequence, QImage
    all_done = Signal()

    def __init__(self, sequences: list, max_height: int, parent=None):
        super().__init__(parent)
        self._sequences = list(sequences)
        self._max_height = max_height

    def run(self):
        for seq in self._sequences:
            if seq.frames:
                qimg = load_frame_qimage(seq.frames[0], self._max_height)
                self.frame_ready.emit(seq, qimg)
        self.all_done.emit()


# ── Video thumbnail extractor (one frame via ffmpeg) ─────────────────────────

class VideoThumbWorker(QThread):
    ready = Signal(QPixmap)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def _get_duration(self, ffmpeg: str, no_window: int) -> float:
        try:
            r = subprocess.run(
                [ffmpeg, "-i", self._path],
                capture_output=True, timeout=10, creationflags=no_window
            )
            m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)',
                          r.stderr.decode('utf-8', errors='replace'))
            if m:
                h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                return h * 3600 + mn * 60 + s
        except Exception:
            pass
        return 0.0

    def run(self):
        from encoder import find_ffmpeg
        _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ffmpeg = find_ffmpeg()

        duration = self._get_duration(ffmpeg, _NO_WINDOW)
        seek = str(round(max(3.0, min(duration * 0.15, 60.0)), 1)) if duration > 0 else "5"

        attempts = [
            ["-ss", seek],
            ["-ss", "5"],
            ["-ss", "3"],
            [],
        ]
        for seek_args in attempts:
            try:
                cmd = [ffmpeg] + seek_args + [
                    "-i", self._path,
                    "-frames:v", "1",
                    "-vf", "scale=400:-1",
                    "-f", "image2pipe", "-vcodec", "png",
                    "-loglevel", "error", "-",
                ]
                r = subprocess.run(cmd, capture_output=True, timeout=15,
                                   creationflags=_NO_WINDOW)
                if r.returncode == 0 and r.stdout:
                    img = QImage()
                    img.loadFromData(r.stdout)
                    if not img.isNull():
                        self.ready.emit(QPixmap.fromImage(img))
                        return
            except Exception:
                pass


# ── Sequence item with encode checkbox ───────────────────────────────────────

class SequenceItem(QWidget):
    check_changed = Signal()

    def __init__(self, seq: Sequence, on_hover, parent=None):
        super().__init__(parent)
        self._seq = seq
        self._on_hover = on_hover
        self.setMouseTracking(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        res = f"  {seq.resolution[0]}x{seq.resolution[1]}" if seq.resolution else ""
        subfolder = Path(seq.folder).name
        # Detect if an output video already exists
        _stem = seq.name.rstrip('_.- ') or Path(seq.folder).name
        self._output_exists = any(
            (Path(seq.folder) / f"{_stem}{ext}").exists()
            for ext in ('.mp4', '.mov', '.mkv', '.avi')
        )

        done_tag = "  ✓" if self._output_exists else ""
        self._label = QLabel(
            f"{subfolder}/\n{seq.name}{seq.extension}\n{len(seq.frames)} frames{res}{done_tag}"
        )
        self._label.setContentsMargins(12, 6, 4, 6)
        self._label.setMouseTracking(True)
        row.addWidget(self._label, 1)

        self._check = QCheckBox()
        self._check.setChecked(False)
        self._check.stateChanged.connect(self.check_changed)

        check_wrap = QWidget()
        check_wrap.setFixedWidth(28)
        cw = QHBoxLayout(check_wrap)
        cw.setContentsMargins(0, 0, 0, 0)
        cw.setSpacing(0)
        cw.addWidget(self._check, 0, Qt.AlignCenter)
        row.addWidget(check_wrap)

        layout.addLayout(row)

        self._bar = QProgressBar()
        self._bar.setFixedHeight(3)
        self._bar.setRange(0, len(seq.frames))
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet(
            "QProgressBar { background: #2a2a38; border: none; }"
            "QProgressBar::chunk { background: #8a3030; }"
        )
        layout.addWidget(self._bar)

        self._set_style(False)

    @property
    def is_checked(self) -> bool:
        return self._check.isChecked()

    @property
    def seq(self) -> Sequence:
        return self._seq

    def _set_style(self, active: bool):
        if active:
            self._label.setStyleSheet(
                "color: #a8d8ea; background-color: #1e2e3a;"
                "border-left: 2px solid #3a6e87; padding-left: 10px;"
            )
        else:
            color = "#5a8a5a" if self._output_exists else "#9a9aaa"
            self._label.setStyleSheet(
                f"color: {color}; background-color: transparent;"
                "border-left: 2px solid transparent; padding-left: 10px;"
            )

    def set_active(self, active: bool):
        self._set_style(active)

    def set_progress(self, loaded: int, total: int):
        self._bar.setValue(loaded)

    def set_done(self):
        self._bar.setValue(self._bar.maximum())
        self._bar.setStyleSheet(
            "QProgressBar { background: #2a2a38; border: none; }"
            "QProgressBar::chunk { background: #3a8a50; }"
        )

    def enterEvent(self, event):
        self._on_hover(self._seq)
        super().enterEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)


# ── Video item ────────────────────────────────────────────────────────────────

class VideoItem(QWidget):
    check_changed = Signal()

    def __init__(self, video: Video, on_hover, parent=None):
        super().__init__(parent)
        self._video = video
        self._on_hover = on_hover
        self.setMouseTracking(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(56, 36)
        self._icon_lbl.setAlignment(Qt.AlignCenter)
        self._icon_lbl.setStyleSheet("background: #0e0e14; border: none;")
        row.addWidget(self._icon_lbl)

        subfolder = Path(video.folder).name
        self._label = QLabel(f"{subfolder}/\n{video.name}")
        self._label.setContentsMargins(4, 6, 4, 6)
        self._label.setMouseTracking(True)
        self._label.setCursor(Qt.PointingHandCursor)
        row.addWidget(self._label, 1)

        self._check = QCheckBox()
        self._check.setChecked(False)
        self._check.stateChanged.connect(self.check_changed)
        check_wrap = QWidget()
        check_wrap.setFixedWidth(28)
        cw = QHBoxLayout(check_wrap)
        cw.setContentsMargins(0, 0, 0, 0)
        cw.setSpacing(0)
        cw.addWidget(self._check, 0, Qt.AlignCenter)
        row.addWidget(check_wrap)

        layout.addLayout(row)
        self._set_style(False)

    @property
    def is_checked(self) -> bool:
        return self._check.isChecked()

    def _set_style(self, active: bool):
        if active:
            self._label.setStyleSheet(
                "color: #ffe060; background-color: #252218;"
                "border-left: 2px solid #c8a030; padding-left: 6px;"
            )
        else:
            self._label.setStyleSheet(
                "color: #9a9aaa; background-color: transparent;"
                "border-left: 2px solid transparent; padding-left: 6px;"
            )

    def set_active(self, active: bool):
        self._set_style(active)

    def set_thumb(self, pix: QPixmap):
        self._icon_lbl.setPixmap(
            pix.scaled(56, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._label.geometry().contains(event.position().toPoint()):
                os.startfile(self._video.path)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self._on_hover(self._video)
        super().enterEvent(event)


# ── Thumbnail panel ───────────────────────────────────────────────────────────

class ThumbnailPanel(QWidget):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._seq = None
        self._cache = {}
        self._frame_idx = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setMinimumSize(400, 200)
        self._img_label.setStyleSheet("background-color: #0e0e14;")
        layout.addWidget(self._img_label)

        self._info = QLabel()
        self._info.setAlignment(Qt.AlignCenter)
        self._info.setFixedHeight(18)
        self._info.setStyleSheet("color: #a8d8ea; font-size: 10px; padding: 0 8px;")
        layout.addWidget(self._info)

        self._autoplay_timer = QTimer(self)
        self._autoplay_timer.timeout.connect(self._advance)

        self._ease_steps = [180, 130, 95, 70, 55]

        self._scrub_cooldown = QTimer(self)
        self._scrub_cooldown.setSingleShot(True)
        self._scrub_cooldown.setInterval(80)
        self._scrub_cooldown.timeout.connect(self._start_ease_in)

        self._seek_steps = [33, 40, 50, 65, 85, 115, 150, 210]
        self._seeking = False
        self._target_idx = 0

        self._paused = False
        self._video_mode = False

    def switch_sequence(self, seq: Sequence, cache: dict):
        self._stop_video_extractor()
        self._video_mode = False
        self._seq = seq
        self._cache = cache
        self._frame_idx = 0
        self._paused = False
        self._seeking = False
        self._scrub_cooldown.stop()
        if 0 in cache:
            self._show_frame(0)
            self._ensure_autoplay()
        else:
            self._info.setText("loading...")

    def switch_video(self, video: Video, pix: QPixmap = None):
        self._autoplay_timer.stop()
        self._scrub_cooldown.stop()
        self._seq = None
        self._video_mode = True
        if pix and not pix.isNull():
            lw = max(self._img_label.width(), 400)
            lh = max(self._img_label.height(), 300)
            self._img_label.setPixmap(
                pix.scaled(lw, lh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self._img_label.setPixmap(QPixmap())
        self._info.setText(f"  {video.name}  —  click to open")

    def _stop_video_extractor(self):
        self._video_mode = False

    def on_frame_added(self, idx: int):
        if idx == 0 and self._frame_idx == 0:
            self._show_frame(0)
            self._ensure_autoplay()

    def show_frame_at_x(self, x: int, window_width: int):
        if not self._seq or self._paused:
            return
        total = len(self._seq.frames)
        target = int((x / max(window_width, 1)) * total)
        self._target_idx = max(0, min(target, total - 1))

        if self._autoplay_timer.isActive():
            self._autoplay_timer.stop()
            self._scrub_cooldown.stop()
            if not self._seeking:
                self._seeking = True
                self._do_seek_step()
        elif not self._seeking:
            if self._target_idx in self._cache:
                self._scrub_cooldown.start()
                self._show_frame(self._target_idx)

    def _do_seek_step(self):
        if not self._seq or not self._seeking:
            return
        current = self._frame_idx
        target = self._target_idx
        distance = abs(target - current)

        if distance == 0:
            self._seeking = False
            self._start_ease_in()
            return

        step = 1 if target > current else -1
        next_idx = current + step
        if next_idx in self._cache:
            self._show_frame(next_idx)

        n = len(self._seek_steps)
        interval = self._seek_steps[n - distance] if distance < n else self._seek_steps[0]
        QTimer.singleShot(interval, self._do_seek_step)

    def toggle_pause(self):
        self._paused = not self._paused
        if self._paused:
            self._autoplay_timer.stop()
            n = self._seq.frame_numbers[self._frame_idx] if self._seq else 0
            total = len(self._seq.frames) if self._seq else 0
            self._info.setText(f"⏸  frame {n}   {self._frame_idx + 1} / {total}")
        else:
            self._ensure_autoplay()

    def _ensure_autoplay(self):
        if self._paused or not self._seq or not self._cache:
            return
        fps = self._config.get('fps', 24)
        self._autoplay_timer.setInterval(max(1, int(1000 / fps)))
        if not self._autoplay_timer.isActive():
            self._autoplay_timer.start()

    def _start_ease_in(self):
        if self._paused or not self._seq:
            return
        self._ease_idx = 0
        self._do_ease_step()

    def _do_ease_step(self):
        if self._paused or not self._seq:
            return
        self._advance()
        self._ease_idx += 1
        if self._ease_idx < len(self._ease_steps):
            QTimer.singleShot(self._ease_steps[self._ease_idx], self._do_ease_step)
        else:
            self._ensure_autoplay()

    def start_autoplay(self):
        self._ensure_autoplay()

    def stop_autoplay(self):
        pass

    def clear(self):
        self._autoplay_timer.stop()
        self._scrub_cooldown.stop()
        self._seq = None
        self._cache = {}
        self._frame_idx = 0
        self._img_label.setPixmap(QPixmap())
        self._info.setText("")

    def _show_frame(self, idx: int):
        if not self._seq or idx not in self._cache:
            return
        self._img_label.setPixmap(self._cache[idx])
        self._frame_idx = idx
        if not self._paused:
            n = self._seq.frame_numbers[idx]
            total = len(self._seq.frames)
            loaded = len(self._cache)
            suffix = "" if loaded >= total else f"  ({loaded}/{total})"
            self._info.setText(f"frame {n}   {idx + 1} / {total}{suffix}")

    def _advance(self):
        if self._paused or not self._seq:
            return
        next_idx = (self._frame_idx + 1) % len(self._seq.frames)
        if next_idx in self._cache:
            self._show_frame(next_idx)


# ── Config dialog ─────────────────────────────────────────────────────────────

class ConfigDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Dialog)
        self._config = config

        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.setInterval(1000)
        self._leave_timer.timeout.connect(self._close)

        self.setStyleSheet("""
            QDialog, QWidget {
                background-color: #1a1a22;
                color: #e8e8f0;
                font-family: 'IBM Plex Mono', monospace;
                font-size: 11px;
                border: none;
            }
            QLabel { color: #a8d8ea; }
            QSpinBox {
                background-color: #2a2a35;
                color: #e8e8f0;
                border: 1px solid #3a3a48;
                border-radius: 2px;
                padding: 3px 8px;
            }
            QPushButton {
                background-color: #3a6e87;
                color: #e8f4f8;
                border: 1px solid #2a5a72;
                border-radius: 3px;
                padding: 5px 14px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #4a7e97; }
            QLabel a { color: #5ab8e8; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title = QLabel("SeqManager")
        title.setStyleSheet("color: #e8e8f0; font-size: 14px; font-weight: bold;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(8)

        self._fps = QSpinBox()
        self._fps.setRange(1, 120)
        self._fps.setValue(config.get('fps', 24))
        form.addRow("Playback FPS:", self._fps)

        self._height = QSpinBox()
        self._height.setRange(100, 800)
        self._height.setValue(config.get('max_height', 200))
        form.addRow("Max height (px):", self._height)

        layout.addLayout(form)

        sep = QLabel("─" * 32)
        sep.setStyleSheet("color: #2a2a38; font-size: 9px;")
        layout.addWidget(sep)

        credits = QLabel(
            'by <b>Albert Callejo</b><br>'
            '<a href="https://www.electricavisuals.com" style="color:#a8e6ff;">electricavisuals.com</a><br>'
            '<a href="https://www.artstation.com/albertcallejo" style="color:#a8e6ff;">artstation.com/albertcallejo</a>'
        )
        credits.setOpenExternalLinks(True)
        credits.setStyleSheet("color: #b0d8f0; font-size: 10px;")
        layout.addWidget(credits)

        btn = QPushButton("Apply")
        btn.clicked.connect(self._apply)
        layout.addWidget(btn, alignment=Qt.AlignRight)

    def leaveEvent(self, event):
        self._leave_timer.start()
        super().leaveEvent(event)

    def enterEvent(self, event):
        self._leave_timer.stop()
        super().enterEvent(event)

    def showEvent(self, event):
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)

    def _close(self):
        QApplication.instance().removeEventFilter(self)
        self.reject()

    def _apply(self):
        self._config['fps'] = self._fps.value()
        self._config['max_height'] = self._height.value()
        cfg.save(self._config)
        QApplication.instance().removeEventFilter(self)
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._close()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            try:
                pos = event.globalPosition().toPoint()
                if not self.geometry().contains(pos):
                    self._close()
            except Exception:
                pass
        return False


# ── Main window ───────────────────────────────────────────────────────────────

class SeqWindow(QWidget):
    def __init__(self, sequences: list, config: dict, folder_path: str = ""):
        super().__init__()
        self._config = config
        self._sequences = [m for m in sequences if isinstance(m, Sequence)]
        self._videos = [m for m in sequences if isinstance(m, Video)]
        self._items = []
        self._caches = {}
        self._loaders = {}
        self._video_thumbs: dict = {}
        self._video_workers: list = []
        self._drain_timer = QTimer()
        self._drain_timer.setInterval(33)
        self._drain_timer.timeout.connect(self._drain_all)
        self._current_key = None
        self._current_seq = None
        self._config_dlg = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setStyleSheet("""
            QWidget {
                background-color: #1a1a22;
                color: #e8e8f0;
                font-family: 'IBM Plex Mono', monospace;
                font-size: 11px;
            }
            QCheckBox { spacing: 4px; }
            QCheckBox::indicator {
                width: 11px; height: 11px;
                border: 1px solid #3a5a70;
                border-radius: 2px;
                background: #1a1a22;
            }
            QCheckBox::indicator:checked { background: #3a6e87; }
            QComboBox {
                background-color: #2a2a35;
                border: 1px solid #3a3a48;
                border-radius: 2px;
                padding: 3px 8px;
                min-width: 140px;
            }
            QComboBox::drop-down { border: none; width: 16px; }
            QComboBox QAbstractItemView {
                background-color: #1e1e28;
                border: 1px solid #3a3a48;
                selection-background-color: #3a5a70;
            }
            QSpinBox {
                background-color: #2a2a35;
                border: 1px solid #3a3a48;
                border-radius: 2px;
                padding: 3px 8px;
            }
        """)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        # ── Left panel ──────────────────────────────────────────
        left = QWidget()
        left.setFixedWidth(210)
        left.setStyleSheet("background-color: #16161e; border-right: 1px solid #2a2a38;")
        left.setMouseTracking(True)

        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        folder_name = Path(folder_path).name if folder_path else "sequences"
        header_widget = QWidget()
        header_widget.setStyleSheet("border-bottom: 1px solid #2a2a38;")
        header_row = QHBoxLayout(header_widget)
        header_row.setContentsMargins(0, 0, 4, 0)
        header_row.setSpacing(0)

        folder_lbl = QLabel(f"  {folder_name}")
        folder_lbl.setStyleSheet(
            "color: #c8e8f8; padding: 8px 10px; font-size: 11px; font-weight: bold; border: none;"
        )
        header_row.addWidget(folder_lbl)
        header_row.addStretch()

        self._exit_btn = QPushButton("EXIT")
        self._exit_btn.setFixedSize(34, 16)
        self._exit_btn.setCursor(Qt.PointingHandCursor)
        self._exit_btn.clicked.connect(self.close)
        self._exit_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #d45151; border: none;"
            " font-size: 8px; font-weight: bold; padding: 0; min-width: 0; }"
            "QPushButton:hover { color: #ff6161; }"
        )
        header_row.addWidget(self._exit_btn, alignment=Qt.AlignVCenter)

        left_layout.addWidget(header_widget)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { background: #16161e; width: 4px; border: none; }"
            "QScrollBar::handle:vertical { background: #3a3a50; border-radius: 2px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )

        items_widget = QWidget()
        items_widget.setMouseTracking(True)
        items_layout = QVBoxLayout(items_widget)
        items_layout.setContentsMargins(0, 0, 0, 0)
        items_layout.setSpacing(0)

        for seq in self._sequences:
            item = SequenceItem(seq, self._on_hover)
            item.setMouseTracking(True)
            item.check_changed.connect(self._update_encode_btn)
            items_layout.addWidget(item)
            self._items.append(item)

        for video in self._videos:
            item = VideoItem(video, self._on_hover)
            item.setMouseTracking(True)
            items_layout.addWidget(item)
            self._items.append(item)
            item.check_changed.connect(self._update_encode_btn)
            worker = VideoThumbWorker(video.path, self)
            worker.ready.connect(lambda pix, v=video: self._on_video_thumb(v, pix))
            worker.start()
            self._video_workers.append(worker)

        items_layout.addStretch()
        scroll.setWidget(items_widget)
        left_layout.addWidget(scroll)

        # Footer: ⚙ CONFIG  |  ENCODE (N) →
        footer = QWidget()
        footer.setStyleSheet("background: #16161e; border-top: 1px solid #2a2a38;")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(0)

        gear = QLabel("CONFIG")
        gear.setStyleSheet(
            "color: #c0ecff; padding: 5px 8px; font-size: 10px; border: none;"
        )
        gear.setCursor(Qt.PointingHandCursor)
        gear.mousePressEvent = lambda e: self._open_config()
        footer_row.addWidget(gear)
        footer_row.addStretch()

        self._delete_lbl = QLabel("DELETE")
        self._delete_lbl.setStyleSheet(
            "color: #909098; padding: 5px 8px; font-size: 10px; border: none;"
        )
        self._delete_lbl.setCursor(Qt.PointingHandCursor)
        self._delete_lbl.mousePressEvent = lambda e: self._on_delete_clicked()
        footer_row.addWidget(self._delete_lbl)
        footer_row.addStretch()

        self._encode_lbl = QLabel("ENCODE")
        self._encode_lbl.setStyleSheet(
            "color: #707888; padding: 5px 8px; font-size: 10px; border: none;"
        )
        self._encode_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._encode_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._encode_lbl.setCursor(Qt.PointingHandCursor)
        self._encode_lbl.mousePressEvent = lambda e: self._enter_encode_mode()
        footer_row.addWidget(self._encode_lbl)

        left_layout.addWidget(footer)

        outer.addWidget(left)

        # ── Right panel: stacked (thumbnail | encode) ───────────────
        self._thumb = ThumbnailPanel(config)
        self._thumb.setMouseTracking(True)

        self._encode_panel = EncodePanel(config)
        self._encode_panel.back_requested.connect(self._exit_encode_mode)

        self._right_stack = QStackedWidget()
        self._right_stack.addWidget(self._thumb)        # index 0
        self._right_stack.addWidget(self._encode_panel) # index 1
        outer.addWidget(self._right_stack)

        # ── Positioning ──────────────────────────────────────────────
        screen = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()

        max_h = int(screen.height() * 0.85)
        if self.height() > max_h:
            self.resize(self.width(), max_h)

        pos = QCursor.pos()
        x = pos.x() + 14
        if x + self.width() > screen.right():
            x = pos.x() - self.width() - 14
        y = pos.y() - self.height() // 3
        y = max(screen.top(), min(y, screen.bottom() - self.height()))
        self.move(max(screen.left(), x), y)

        QApplication.instance().installEventFilter(self)

        # 1) Load frame 0 of each sequence quickly, then start full buffers
        self._preview_loader = PreviewLoader(
            self._sequences, config.get('max_height', 200), self
        )
        self._preview_loader.frame_ready.connect(self._on_preview_frame)
        self._preview_loader.all_done.connect(self._start_all_loaders)
        self._preview_loader.start()

        all_media = self._sequences + self._videos
        if all_media:
            self._on_hover(all_media[0])

        self._update_encode_btn()

    def _on_preview_frame(self, seq: Sequence, qimg):
        key = self._seq_key(seq)
        pix = QPixmap.fromImage(qimg)
        if key not in self._caches:
            self._caches[key] = {}
        self._caches[key][0] = pix
        item = self._item_for_seq_key(key)
        if item:
            item.set_progress(1, len(seq.frames))
        if key == self._current_key:
            self._thumb.on_frame_added(0)

    def _start_all_loaders(self):
        for seq in self._sequences:
            key = self._seq_key(seq)
            skip = {0} if 0 in self._caches.get(key, {}) else set()
            self._start_loader(seq, skip_indices=skip)

    # ── Encode mode ───────────────────────────────────────────────────────────

    def _update_encode_btn(self):
        n_seq = sum(1 for it in self._items if isinstance(it, SequenceItem) and it.is_checked)
        n_del = n_seq + sum(1 for it in self._items if isinstance(it, VideoItem) and it.is_checked)
        if n_seq > 0:
            self._encode_lbl.setStyleSheet(
                "color: #00d8ff; padding: 5px 8px; font-size: 10px; border: none;"
            )
        else:
            self._encode_lbl.setStyleSheet(
                "color: #707888; padding: 5px 8px; font-size: 10px; border: none;"
            )
        if n_del > 0:
            self._delete_lbl.setText(f"DELETE ({n_del})")
            self._delete_lbl.setStyleSheet(
                "color: #ff3c3c; padding: 5px 8px; font-size: 10px; border: none;"
            )
        else:
            self._delete_lbl.setText("DELETE")
            self._delete_lbl.setStyleSheet(
                "color: #909098; padding: 5px 8px; font-size: 10px; border: none;"
            )

    def _confirm_delete(self, entries: list) -> bool:
        # entries: list of (display_name, color) tuples
        dlg = QDialog(self, Qt.FramelessWindowHint | Qt.Dialog)
        dlg.setStyleSheet(
            "QDialog { background: #1a1a22; border: 1px solid #5a2828; }"
            "QLabel { font-family: 'IBM Plex Mono', monospace; font-size: 12px; }"
            "QPushButton { font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: bold;"
            " border-radius: 3px; padding: 5px 16px; min-width: 70px; border: 1px solid; }"
        )
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(6)

        for name, color in entries:
            row = QHBoxLayout()
            row.setSpacing(6)
            del_lbl = QLabel("Delete")
            del_lbl.setStyleSheet("color: #f07070; font-weight: bold;")
            row.addWidget(del_lbl)
            name_lbl = QLabel(name)
            name_lbl.setStyleSheet(f"color: {color};")
            row.addWidget(name_lbl)
            row.addStretch()
            layout.addLayout(row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(
            "QPushButton { background: #2a2a35; color: #a8a8c0; border-color: #3a3a50; }"
            "QPushButton:hover { background: #32323e; }"
        )
        cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel)

        delete = QPushButton("DELETE")
        delete.setStyleSheet(
            "QPushButton { background: #7a2020; color: #f0c0c0; border-color: #5a1818; }"
            "QPushButton:hover { background: #8a3030; }"
        )
        delete.clicked.connect(dlg.accept)
        btn_row.addWidget(delete)

        layout.addSpacing(6)
        layout.addLayout(btn_row)
        dlg.adjustSize()
        return dlg.exec() == QDialog.Accepted

    def _on_delete_clicked(self):
        checked_seq = [it for it in self._items if isinstance(it, SequenceItem) and it.is_checked]
        checked_vid = [it for it in self._items if isinstance(it, VideoItem) and it.is_checked]
        if not checked_seq and not checked_vid:
            return

        entries = []
        for it in checked_seq:
            color = "#5a8a5a" if it._output_exists else "#9a9aaa"
            entries.append((f"{it.seq.name}{it.seq.extension}", color))
        for it in checked_vid:
            entries.append((it._video.name, "#9a9aaa"))

        if not self._confirm_delete(entries):
            return

        for item in checked_seq:
            for frame_path in item.seq.frames:
                try:
                    Path(frame_path).unlink(missing_ok=True)
                except OSError:
                    pass
            key = self._seq_key(item.seq)
            self._sequences = [s for s in self._sequences if self._seq_key(s) != key]
            if key in self._caches:
                del self._caches[key]
            if key in self._loaders:
                self._loaders[key].stop()
                del self._loaders[key]
            self._items.remove(item)
            item.setParent(None)
            item.deleteLater()

        for item in checked_vid:
            try:
                Path(item._video.path).unlink(missing_ok=True)
            except OSError:
                pass
            self._videos = [v for v in self._videos if v.path != item._video.path]
            self._video_thumbs.pop(item._video.path, None)
            self._items.remove(item)
            item.setParent(None)
            item.deleteLater()

        self._current_key = None
        self._current_seq = None
        self._thumb.clear()

        remaining = [it for it in self._items if isinstance(it, SequenceItem)]
        if remaining:
            self._on_hover(remaining[0].seq)
        else:
            remaining_vid = [it for it in self._items if isinstance(it, VideoItem)]
            if remaining_vid:
                self._on_hover(remaining_vid[0]._video)

        self._update_encode_btn()

    def _enter_encode_mode(self):
        checked = [it.seq for it in self._items if isinstance(it, SequenceItem) and it.is_checked]
        if not checked:
            return
        self._drain_timer.stop()
        self._thumb._autoplay_timer.stop()
        self._encode_panel.prepare(checked)
        self._right_stack.setCurrentIndex(1)

    def _exit_encode_mode(self):
        self._right_stack.setCurrentIndex(0)
        pending = any(
            len(self._caches.get(self._seq_key(s), {})) < len(s.frames)
            for s in self._sequences
        )
        if pending and not self._drain_timer.isActive():
            self._drain_timer.start()
        if self._current_seq:
            self._thumb.switch_sequence(
                self._current_seq,
                self._caches.get(self._seq_key(self._current_seq), {}),
            )

    # ── Thumbnail loading ─────────────────────────────────────────────────────

    def _seq_key(self, seq: Sequence) -> str:
        return seq.name + seq.extension

    def _start_loader(self, seq: Sequence, skip_indices: set = None):
        key = self._seq_key(seq)
        existing = self._caches.get(key, {})
        if not existing:
            self._caches[key] = {}
        loader = FrameLoader(seq, self._config.get('max_height', 200), skip_indices)
        loader.start()
        self._loaders[key] = loader
        if not self._drain_timer.isActive():
            self._drain_timer.start()

    def _drain_all(self):
        all_done = True
        for key, loader in list(self._loaders.items()):
            seq = next((s for s in self._sequences if self._seq_key(s) == key), None)
            if seq is None:
                continue

            def on_frame(idx, pix, k=key, s=seq):
                self._caches[k][idx] = pix
                item = self._item_for_seq_key(k)
                if item:
                    item.set_progress(len(self._caches[k]), len(s.frames))
                if k == self._current_key:
                    self._thumb.on_frame_added(idx)

            def on_done(k=key):
                item = self._item_for_seq_key(k)
                if item:
                    item.set_done()

            finished = loader.drain(on_frame, on_done)
            if not finished:
                all_done = False

        if all_done:
            self._drain_timer.stop()

    def _item_for_seq_key(self, key: str):
        for item in self._items:
            if isinstance(item, SequenceItem) and self._seq_key(item._seq) == key:
                return item
        return None

    def _item_for_video(self, video: Video):
        for item in self._items:
            if isinstance(item, VideoItem) and item._video.path == video.path:
                return item
        return None

    def _on_video_thumb(self, video: Video, pix: QPixmap):
        self._video_thumbs[video.path] = pix
        item = self._item_for_video(video)
        if item:
            item.set_thumb(pix)
        if self._current_key == "video:" + video.path:
            self._thumb.switch_video(video, pix)

    def _on_hover(self, media):
        if isinstance(media, Video):
            key = "video:" + media.path
            if key == self._current_key:
                return
            self._current_key = key
            self._current_seq = None
            for item in self._items:
                if isinstance(item, SequenceItem):
                    item.set_active(False)
                else:
                    item.set_active(item._video.path == media.path)
            self._thumb.switch_video(media, self._video_thumbs.get(media.path))
        else:
            key = self._seq_key(media)
            if key == self._current_key:
                return
            self._current_key = key
            self._current_seq = media
            for item in self._items:
                if isinstance(item, SequenceItem):
                    item.set_active(self._seq_key(item._seq) == key)
                else:
                    item.set_active(False)
            self._thumb.switch_sequence(media, self._caches.get(key, {}))

    # ── Config dialog ─────────────────────────────────────────────────────────

    def _open_config(self):
        if self._config_dlg:
            return
        self._config_dlg = ConfigDialog(self._config, self)
        self._config_dlg.finished.connect(self._on_config_closed)
        self._config_dlg.show()

    def _on_config_closed(self):
        self._config_dlg = None

    # ── Events ────────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.MouseButtonPress:
            try:
                pos = event.globalPosition().toPoint()
                if self.geometry().contains(pos):
                    local = self.mapFromGlobal(pos)
                    if (self._right_stack.geometry().contains(local)
                            and not self._thumb._video_mode
                            and self._right_stack.currentIndex() == 0):
                        self._thumb.toggle_pause()
            except Exception:
                pass
        elif t == QEvent.MouseMove:
            try:
                pos = event.globalPosition().toPoint()
                if self.geometry().contains(pos):
                    if self._current_seq and self._right_stack.currentIndex() == 0:
                        local_x = self.mapFromGlobal(pos).x()
                        self._thumb.show_frame_at_x(local_x, self.width())
            except Exception:
                pass
        return False

    def closeEvent(self, event):
        self._drain_timer.stop()
        for loader in self._loaders.values():
            loader.stop()
        self._encode_panel.cleanup()
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(event)
        QApplication.instance().quit()


# ── Entry point ───────────────────────────────────────────────────────────────

def run_viewer(folder_path: str, config: dict = None, app=None):
    if config is None:
        config = cfg.load()
    media = scan_folder(folder_path)
    if not media:
        print(f"No sequences or videos found in: {folder_path}")
        return
    if app is None:
        app = QApplication.instance() or QApplication(sys.argv)
    win = SeqWindow(media, config, folder_path)
    win.show()
    app.exec()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python viewer.py <folder>")
        sys.exit(1)
    run_viewer(sys.argv[1])
