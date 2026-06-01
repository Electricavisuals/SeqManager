import sys
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QLabel, QComboBox, QSpinBox, QCheckBox, QPushButton,
    QProgressBar, QScrollArea, QFrame,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor

from detector import Sequence, scan_folder
from encoder import PRESETS, EncodeTask, EncodeWorker, ffmpeg_available, output_stem
import config as cfg


_STYLE = """
QWidget {
    background-color: #1a1a22;
    color: #e8e8f0;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
}
QComboBox, QSpinBox {
    background-color: #2a2a35;
    color: #e8e8f0;
    border: 1px solid #3a3a48;
    border-radius: 2px;
    padding: 3px 8px;
    selection-background-color: #3a6e87;
    min-width: 160px;
}
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background-color: #1e1e28;
    color: #e8e8f0;
    border: 1px solid #3a3a48;
    selection-background-color: #3a5a70;
}
QCheckBox { color: #a8d8ea; spacing: 6px; }
QCheckBox::indicator {
    width: 12px; height: 12px;
    border: 1px solid #3a6e87;
    border-radius: 2px;
    background: #1a1a22;
}
QCheckBox::indicator:checked { background: #3a6e87; }
QCheckBox::indicator:disabled { border-color: #303040; background: #202028; }
QPushButton {
    background-color: #3a6e87; color: #e8f4f8;
    border: 1px solid #2a5a72; border-radius: 3px;
    padding: 6px 24px; font-weight: bold;
    min-width: 100px;
}
QPushButton:hover { background-color: #4a7e97; }
QPushButton:disabled { background-color: #222830; color: #404858; border-color: #2a3040; }
QProgressBar { background: #2a2a38; border: none; border-radius: 2px; }
QProgressBar::chunk { background: #3a6e87; border-radius: 2px; }
QScrollBar:vertical { background: #16161e; width: 4px; border: none; }
QScrollBar::handle:vertical { background: #3a3a50; border-radius: 2px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { height: 0; }
"""

_BTN_ABORT = (
    "QPushButton { background-color: #7a2828; color: #f8d0d0;"
    " border: 1px solid #5a1818; border-radius: 3px;"
    " padding: 6px 24px; font-weight: bold; min-width: 100px; }"
    "QPushButton:hover { background-color: #8a3838; }"
)
_BAR_DONE_OK  = "QProgressBar { background: #2a2a38; border: none; border-radius: 2px; } QProgressBar::chunk { background: #3a8a50; border-radius: 2px; }"
_BAR_DONE_ERR = "QProgressBar { background: #2a2a38; border: none; border-radius: 2px; } QProgressBar::chunk { background: #8a3030; border-radius: 2px; }"


class _SeqItem(QWidget):
    def __init__(self, seq: Sequence, parent=None):
        super().__init__(parent)
        self._seq = seq

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(6)

        self._check = QCheckBox()
        self._check.setChecked(True)
        row.addWidget(self._check, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)

        subfolder = Path(seq.folder).name
        display = seq.name.rstrip('_.- ') or "(unnamed)"
        name_lbl = QLabel(f"{subfolder}/  {display}{seq.extension}")
        name_lbl.setStyleSheet("color: #c8e8f8; font-size: 10px; background: transparent;")
        col.addWidget(name_lbl)

        res = f"  {seq.resolution[0]}×{seq.resolution[1]}" if seq.resolution else ""
        detail = QLabel(f"{len(seq.frames)} frames{res}")
        detail.setStyleSheet("color: #7a9ab0; font-size: 10px; background: transparent;")
        col.addWidget(detail)

        if seq.gaps:
            gap_lbl = QLabel(f"⚠  {len(seq.gaps)} gap{'s' if len(seq.gaps) > 1 else ''}")
            gap_lbl.setStyleSheet("color: #c8a040; font-size: 9px; background: transparent;")
            col.addWidget(gap_lbl)

        row.addLayout(col)
        row.addStretch()

        self.setStyleSheet("QWidget { border-bottom: 1px solid #21212c; background: transparent; }")

    @property
    def is_checked(self) -> bool:
        return self._check.isChecked()

    @property
    def seq(self) -> Sequence:
        return self._seq

    def set_controls_enabled(self, enabled: bool):
        self._check.setEnabled(enabled)


class SeqManagerWindow(QWidget):
    def __init__(self, sequences: list, config: dict, folder_path: str):
        super().__init__()
        self._config = config
        self._folder = folder_path
        self._seq_items: list[_SeqItem] = []
        self._worker = None
        self._tasks = []
        self._task_count = 0
        self._success_count = 0
        self._fail_count = 0
        self._task_start = 0.0
        self._has_exr = any(s.extension == '.exr' for s in sequences)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(_STYLE)

        self._build_ui(sequences)
        self._position()
        self._update_btn()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self, sequences: list):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        outer.addWidget(self._make_left(sequences))
        outer.addWidget(self._make_right())

    def _make_left(self, sequences: list) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(230)
        panel.setStyleSheet("background-color: #16161e; border-right: 1px solid #2a2a38;")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QWidget()
        header.setStyleSheet("border-bottom: 1px solid #2a2a38;")
        hrow = QHBoxLayout(header)
        hrow.setContentsMargins(0, 0, 4, 0)
        hrow.setSpacing(0)

        folder_lbl = QLabel(f"  {Path(self._folder).name}")
        folder_lbl.setStyleSheet(
            "color: #c8e8f8; padding: 8px 10px; font-size: 11px; font-weight: bold; border: none;"
        )
        hrow.addWidget(folder_lbl)
        hrow.addStretch()

        exit_btn = QPushButton("EXIT")
        exit_btn.setFixedSize(34, 16)
        exit_btn.setCursor(Qt.PointingHandCursor)
        exit_btn.clicked.connect(self.close)
        exit_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #d45151; border: none;"
            " font-size: 8px; font-weight: bold; padding: 0; min-width: 0; }"
            "QPushButton:hover { color: #ff6161; }"
        )
        hrow.addWidget(exit_btn, alignment=Qt.AlignVCenter)
        layout.addWidget(header)

        # Sequence list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        clist = QVBoxLayout(container)
        clist.setContentsMargins(0, 0, 0, 0)
        clist.setSpacing(0)

        for seq in sequences:
            item = _SeqItem(seq)
            item._check.stateChanged.connect(self._update_btn)
            clist.addWidget(item)
            self._seq_items.append(item)

        clist.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)
        return panel

    def _make_right(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(330)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Options form
        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._preset_combo = QComboBox()
        for p in PRESETS:
            self._preset_combo.addItem(p["name"])
        self._preset_combo.setCurrentIndex(self._config.get("preset_index", 0))
        form.addRow("Preset:", self._preset_combo)

        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 120)
        self._fps_spin.setValue(self._config.get("fps", 24))
        form.addRow("FPS:", self._fps_spin)

        layout.addLayout(form)

        self._gamma_check = QCheckBox("Gamma correction  (EXR → SDR, needs zscale)")
        self._gamma_check.setChecked(self._config.get("gamma_fix", False))
        self._gamma_check.setVisible(self._has_exr)
        layout.addWidget(self._gamma_check)

        # Divider
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("QFrame { background: #2a2a38; border: none; max-height: 1px; }")
        layout.addWidget(sep)

        # Status
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("color: #7a9ab0; font-size: 10px;")
        self._status_lbl.setWordWrap(True)
        layout.addWidget(self._status_lbl)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Stats line
        self._stats_lbl = QLabel()
        self._stats_lbl.setStyleSheet("color: #9ab8c8; font-size: 10px;")
        self._stats_lbl.setVisible(False)
        layout.addWidget(self._stats_lbl)

        # Log area
        log_scroll = QScrollArea()
        log_scroll.setWidgetResizable(True)
        log_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        log_scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #252530; background: #131318; }"
            "QScrollBar:vertical { background: #131318; width: 3px; }"
            "QScrollBar::handle:vertical { background: #2a2a40; }"
        )
        log_scroll.setFixedHeight(90)

        self._log_inner = QWidget()
        self._log_inner.setStyleSheet("background: #131318;")
        self._log_layout = QVBoxLayout(self._log_inner)
        self._log_layout.setContentsMargins(6, 4, 6, 4)
        self._log_layout.setSpacing(3)
        self._log_layout.addStretch()
        log_scroll.setWidget(self._log_inner)
        layout.addWidget(log_scroll)
        self._log_scroll = log_scroll

        # Encode button
        self._encode_btn = QPushButton("ENCODE")
        self._encode_btn.setFixedHeight(30)
        self._encode_btn.clicked.connect(self._on_encode_clicked)
        layout.addWidget(self._encode_btn, alignment=Qt.AlignCenter)

        if not ffmpeg_available():
            self._status_lbl.setText("⚠  ffmpeg not found in PATH")
            self._status_lbl.setStyleSheet("color: #c84040; font-size: 10px;")
            self._encode_btn.setEnabled(False)

        return panel

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _position(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        pos = QCursor.pos()
        x = pos.x() + 14
        if x + self.width() > screen.right():
            x = pos.x() - self.width() - 14
        y = pos.y() - self.height() // 3
        y = max(screen.top(), min(y, screen.bottom() - self.height()))
        self.move(max(screen.left(), x), y)

    def _checked_sequences(self) -> list:
        return [it.seq for it in self._seq_items if it.is_checked]

    def _set_controls_enabled(self, enabled: bool):
        self._preset_combo.setEnabled(enabled)
        self._fps_spin.setEnabled(enabled)
        self._gamma_check.setEnabled(enabled)
        for it in self._seq_items:
            it.set_controls_enabled(enabled)

    def _update_btn(self):
        if self._worker is not None:
            return
        if not ffmpeg_available():
            return
        checked = self._checked_sequences()
        self._encode_btn.setEnabled(bool(checked))
        if checked:
            n = len(checked)
            self._status_lbl.setText(f"{n} sequence{'s' if n > 1 else ''} selected")
            self._status_lbl.setStyleSheet("color: #7a9ab0; font-size: 10px;")
        else:
            self._status_lbl.setText("Select at least one sequence")
            self._status_lbl.setStyleSheet("color: #606070; font-size: 10px;")

    def _log_clear(self):
        while self._log_layout.count() > 1:
            item = self._log_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _log_add(self, text: str, color: str):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {color}; font-size: 10px; background: transparent;")
        lbl.setWordWrap(True)
        self._log_layout.insertWidget(self._log_layout.count() - 1, lbl)
        QApplication.processEvents()
        self._log_scroll.verticalScrollBar().setValue(
            self._log_scroll.verticalScrollBar().maximum()
        )

    # ── Encode ────────────────────────────────────────────────────────────────

    def _on_encode_clicked(self):
        if self._worker is not None:
            self._worker.abort()
            self._encode_btn.setEnabled(False)
            self._status_lbl.setText("Aborting...")
            return

        checked = self._checked_sequences()
        if not checked:
            return

        self._config["fps"] = self._fps_spin.value()
        self._config["preset_index"] = self._preset_combo.currentIndex()
        self._config["gamma_fix"] = self._gamma_check.isChecked()
        cfg.save(self._config)

        preset = PRESETS[self._preset_combo.currentIndex()]
        fps = self._fps_spin.value()
        gamma_fix = self._gamma_check.isChecked()

        self._tasks = [
            EncodeTask(
                seq=seq,
                out_path=Path(seq.folder) / f"{output_stem(seq)}{preset['ext']}",
                fps=fps,
                preset=preset,
                gamma_fix=gamma_fix,
            )
            for seq in checked
        ]
        self._task_count = len(self._tasks)
        self._success_count = 0
        self._fail_count = 0

        self._log_clear()
        self._set_controls_enabled(False)
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._progress.setStyleSheet("")
        self._stats_lbl.setVisible(True)
        self._stats_lbl.setText("")
        self._encode_btn.setText("ABORT")
        self._encode_btn.setStyleSheet(_BTN_ABORT)
        self._encode_btn.setEnabled(True)

        self._worker = EncodeWorker(self._tasks)
        self._worker.task_started.connect(self._on_task_started)
        self._worker.task_progress.connect(self._on_task_progress)
        self._worker.task_done.connect(self._on_task_done)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.start()

    def _on_task_started(self, idx: int):
        self._task_start = time.time()
        name = output_stem(self._tasks[idx].seq)
        self._status_lbl.setText(f"Encoding  {idx + 1}/{self._task_count}:  {name}")
        self._status_lbl.setStyleSheet("color: #a8d8ea; font-size: 10px;")
        self._progress.setValue(0)
        self._stats_lbl.setText("")
        self._log_add(f"⏳  {name}  encoding...", "#7a9ab0")

    def _on_task_progress(self, current: int, total: int, speed: str):
        if total <= 0:
            return
        pct = int(current / total * 100)
        self._progress.setValue(pct)
        elapsed = time.time() - self._task_start
        if current > 0:
            eta = elapsed / current * (total - current)
            eta_s = f"  ETA {int(eta)}s"
        else:
            eta_s = ""
        sp = f"  •  speed: {speed}" if speed else ""
        self._stats_lbl.setText(f"frame {current}/{total}  {pct}%{eta_s}{sp}")

    def _on_task_done(self, idx: int, ok: bool, msg: str):
        name = output_stem(self._tasks[idx].seq)
        if ok:
            self._success_count += 1
            self._log_add(f"✓  {name}  —  {msg}", "#50b870")
        else:
            self._fail_count += 1
            self._log_add(f"✗  {name}  —  {msg}", "#c05050")

    def _on_all_done(self):
        self._worker = None
        self._progress.setValue(100)
        self._progress.setStyleSheet(_BAR_DONE_OK if self._fail_count == 0 else _BAR_DONE_ERR)
        self._stats_lbl.setVisible(False)

        if self._fail_count == 0:
            self._status_lbl.setText(f"Done!  {self._success_count}/{self._task_count} encoded")
            self._status_lbl.setStyleSheet("color: #50b870; font-size: 10px;")
        else:
            self._status_lbl.setText(
                f"Finished with errors.  {self._success_count} ok  /  {self._fail_count} failed"
            )
            self._status_lbl.setStyleSheet("color: #c08040; font-size: 10px;")

        self._encode_btn.setText("ENCODE")
        self._encode_btn.setStyleSheet("")
        self._set_controls_enabled(True)
        self._update_btn()

    # ── Events ────────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()

    def closeEvent(self, event):
        if self._worker:
            self._worker.abort()
            self._worker.wait(3000)
        super().closeEvent(event)
        QApplication.instance().quit()


# ── Entry point ───────────────────────────────────────────────────────────────

def run_encoder(folder_path: str):
    config = cfg.load()
    sequences = scan_folder(folder_path)

    app = QApplication.instance() or QApplication(sys.argv)

    if not sequences:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            None, "SeqManager",
            f"No image sequences found in:\n{folder_path}"
        )
        return

    win = SeqManagerWindow(sequences, config, folder_path)
    win.show()
    app.exec()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ui.py <folder>")
        sys.exit(1)
    run_encoder(sys.argv[1])
