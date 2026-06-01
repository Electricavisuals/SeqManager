import time
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QComboBox, QSpinBox, QCheckBox, QPushButton,
    QProgressBar, QScrollArea, QFrame, QApplication,
)
from PySide6.QtCore import Qt, Signal

from encoder import PRESETS, EncodeTask, EncodeWorker, output_stem, get_available_presets
import config as cfg


class EncodePanel(QWidget):
    back_requested = Signal()

    def __init__(self, app_config: dict, parent=None):
        super().__init__(parent)
        self._config = app_config
        self._sequences = []
        self._worker = None
        self._tasks = []
        self._task_labels: dict = {}
        self._task_count = 0
        self._success_count = 0
        self._fail_count = 0
        self._task_start = 0.0
        self._available_presets = PRESETS
        self._available_codecs = {p["vcodec"] for p in get_available_presets()}
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(9)

        # Header: ← back  |  ENCODE
        hdr = QHBoxLayout()
        self._back_btn = QPushButton("← back")
        self._back_btn.setFixedHeight(20)
        self._back_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #6a8aaa; border: none;"
            " font-size: 10px; padding: 0; min-width: 0; font-weight: normal; }"
            "QPushButton:hover { color: #a8d8ea; }"
            "QPushButton:disabled { color: #2e3840; }"
        )
        self._back_btn.clicked.connect(self._on_back)
        hdr.addWidget(self._back_btn)
        hdr.addStretch()
        title = QLabel("ENCODE")
        title.setStyleSheet("color: #d8f0ff; font-size: 13px; font-weight: bold;")
        hdr.addWidget(title)
        root.addLayout(hdr)

        # Options form
        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._preset_combo = QComboBox()
        for p in self._available_presets:
            self._preset_combo.addItem(p["name"])
        saved_name = self._config.get("preset_name", "")
        idx = next((i for i, p in enumerate(self._available_presets) if p["name"] == saved_name), 0)
        self._preset_combo.setCurrentIndex(idx)
        self._preset_combo.currentIndexChanged.connect(self._update_status)
        form.addRow("Preset:", self._preset_combo)

        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 120)
        self._fps_spin.setValue(self._config.get("encode_fps", 30))
        form.addRow("FPS:", self._fps_spin)

        root.addLayout(form)

        self._gamma_check = QCheckBox("Gamma correction  (EXR → SDR)")
        self._gamma_check.setChecked(self._config.get("gamma_fix", False))
        self._gamma_check.setVisible(False)
        root.addWidget(self._gamma_check)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: #2a2a38; border: none; max-height: 1px;")
        root.addWidget(sep)

        self._status_lbl = QLabel()
        self._status_lbl.setStyleSheet("color: #b8d8f0; font-size: 12px;")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        # Sequence list / log area  — shown BEFORE progress bar
        log_scroll = QScrollArea()
        log_scroll.setWidgetResizable(True)
        log_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        log_scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #232330; background: #111116; }"
            "QScrollBar:vertical { background: #111116; width: 3px; border: none; }"
            "QScrollBar::handle:vertical { background: #2a2a40; border-radius: 1px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        log_scroll.setMinimumHeight(60)
        log_scroll.setMaximumHeight(200)
        self._log_inner = QWidget()
        self._log_inner.setStyleSheet("background: #111116;")
        self._log_layout = QVBoxLayout(self._log_inner)
        self._log_layout.setContentsMargins(8, 6, 8, 6)
        self._log_layout.setSpacing(3)
        self._log_layout.addStretch()
        log_scroll.setWidget(self._log_inner)
        root.addWidget(log_scroll)
        self._log_scroll = log_scroll

        # Progress bar — below the list
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")
        self._progress.setFixedHeight(16)
        self._progress.setVisible(False)
        self._progress.setStyleSheet(self._style_bar_active())
        root.addWidget(self._progress)

        self._stats_lbl = QLabel()
        self._stats_lbl.setStyleSheet("color: #b8d0e0; font-size: 12px;")
        self._stats_lbl.setVisible(False)
        root.addWidget(self._stats_lbl)

        root.addStretch()

        self._encode_btn = QPushButton("ENCODE")
        self._encode_btn.setFixedHeight(28)
        self._encode_btn.setStyleSheet(self._style_btn_encode())
        self._encode_btn.clicked.connect(self._on_encode_clicked)
        root.addWidget(self._encode_btn, alignment=Qt.AlignCenter)

    # ── Public ────────────────────────────────────────────────────────────────

    def prepare(self, sequences: list):
        self._sequences = sequences
        has_exr = any(s.extension == '.exr' for s in sequences)
        self._gamma_check.setVisible(has_exr)
        self._progress.setVisible(False)
        self._stats_lbl.setVisible(False)
        self._encode_btn.setText("ENCODE")
        self._encode_btn.setStyleSheet(self._style_btn_encode())
        self._encode_btn.setEnabled(True)
        self._back_btn.setEnabled(True)

        # Build sequence list
        self._log_clear()
        self._task_labels = {}
        for i, seq in enumerate(sequences):
            lbl = QLabel(f"  {output_stem(seq)}")
            lbl.setStyleSheet("color: #8ab0c8; font-size: 12px; background: transparent;")
            self._task_labels[i] = lbl
            self._log_layout.insertWidget(self._log_layout.count() - 1, lbl)

        self._update_status()

    # ── Styles ────────────────────────────────────────────────────────────────

    @staticmethod
    def _style_btn_encode():
        return (
            "QPushButton { background-color: #3a6e87; color: #e8f4f8;"
            " border: 1px solid #2a5a72; border-radius: 3px;"
            " padding: 5px 24px; font-weight: bold; }"
            "QPushButton:hover { background-color: #4a7e97; }"
            "QPushButton:disabled { background-color: #222830; color: #404858; border-color: #2a3040; }"
        )

    @staticmethod
    def _style_btn_abort():
        return (
            "QPushButton { background-color: #7a2828; color: #f8d0d0;"
            " border: 1px solid #5a1818; border-radius: 3px;"
            " padding: 5px 24px; font-weight: bold; }"
            "QPushButton:hover { background-color: #8a3838; }"
            "QPushButton:disabled { background-color: #3a2020; color: #604040; }"
        )

    @staticmethod
    def _style_bar_active():
        return (
            "QProgressBar { background: #2a2a38; border: none; border-radius: 3px;"
            " color: #d8f0ff; font-size: 11px; font-family: 'IBM Plex Mono', monospace; }"
            "QProgressBar::chunk { background: #3a6e87; border-radius: 3px; }"
        )

    @staticmethod
    def _style_bar_ok():
        return (
            "QProgressBar { background: #2a2a38; border: none; border-radius: 3px;"
            " color: #d0f8e0; font-size: 11px; font-family: 'IBM Plex Mono', monospace; }"
            "QProgressBar::chunk { background: #3a8a50; border-radius: 3px; }"
        )

    @staticmethod
    def _style_bar_err():
        return (
            "QProgressBar { background: #2a2a38; border: none; border-radius: 3px;"
            " color: #f8d0d0; font-size: 11px; font-family: 'IBM Plex Mono', monospace; }"
            "QProgressBar::chunk { background: #8a3030; border-radius: 3px; }"
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _update_status(self):
        if self._worker is not None or not self._sequences:
            return
        preset = self._available_presets[self._preset_combo.currentIndex()]
        if preset["vcodec"] not in self._available_codecs:
            self._status_lbl.setText(f"⚠  codec {preset['vcodec']} not in your ffmpeg build")
            self._status_lbl.setStyleSheet("color: #f07070; font-size: 12px;")
            return
        existing = [
            output_stem(s) + preset['ext']
            for s in self._sequences
            if (Path(s.folder) / f"{output_stem(s)}{preset['ext']}").exists()
        ]
        n = len(self._sequences)
        if existing:
            names = "  ".join(existing)
            self._status_lbl.setText(f"⚠  overwrites:  {names}")
            self._status_lbl.setStyleSheet("color: #e8c860; font-size: 12px;")
        else:
            self._status_lbl.setText(f"{n} sequence{'s' if n > 1 else ''}  —  ready")
            self._status_lbl.setStyleSheet("color: #b8d8f0; font-size: 12px;")

    def _on_back(self):
        if self._worker is not None:
            self._worker.abort()
            self._worker.wait(2000)
            self._worker = None
        self.back_requested.emit()

    def _log_clear(self):
        while self._log_layout.count() > 1:
            item = self._log_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_encode_clicked(self):
        if self._worker is not None:
            self._worker.abort()
            self._encode_btn.setEnabled(False)
            self._status_lbl.setText("Aborting...")
            return

        self._config["encode_fps"] = self._fps_spin.value()
        preset = self._available_presets[self._preset_combo.currentIndex()]
        self._config["preset_name"] = preset["name"]
        self._config["gamma_fix"] = self._gamma_check.isChecked()
        cfg.save(self._config)
        fps = self._config["encode_fps"]
        gamma_fix = self._config["gamma_fix"]

        self._tasks = [
            EncodeTask(
                seq=seq,
                out_path=Path(seq.folder) / f"{output_stem(seq)}{preset['ext']}",
                fps=fps,
                preset=preset,
                gamma_fix=gamma_fix,
            )
            for seq in self._sequences
        ]
        self._task_count = len(self._tasks)
        self._success_count = 0
        self._fail_count = 0

        # Reset list labels to pending state
        for i, lbl in self._task_labels.items():
            lbl.setText(f"  {output_stem(self._sequences[i])}")
            lbl.setStyleSheet("color: #6a8aaa; font-size: 12px; background: transparent;")

        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._progress.setStyleSheet(self._style_bar_active())
        self._stats_lbl.setVisible(True)
        self._stats_lbl.setText("")
        self._encode_btn.setText("ABORT")
        self._encode_btn.setStyleSheet(self._style_btn_abort())
        self._encode_btn.setEnabled(True)
        self._back_btn.setEnabled(False)

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
        self._status_lbl.setStyleSheet("color: #d0eeff; font-size: 12px;")
        self._progress.setValue(0)
        self._stats_lbl.setText("")
        if idx in self._task_labels:
            self._task_labels[idx].setText(f"►  {name}")
            self._task_labels[idx].setStyleSheet(
                "color: #d0eeff; font-size: 12px; background: transparent;"
            )

    def _on_task_progress(self, current: int, total: int, speed: str):
        if total <= 0:
            return
        pct = int(current / total * 100)
        self._progress.setValue(pct)
        elapsed = time.time() - self._task_start
        eta_s = f"  ETA {int(elapsed / current * (total - current))}s" if current > 0 else ""
        sp = f"  •  {speed}" if speed else ""
        self._stats_lbl.setText(f"frame {current}/{total}  {pct}%{eta_s}{sp}")

    def _on_task_done(self, idx: int, ok: bool, msg: str):
        name = output_stem(self._tasks[idx].seq)
        if ok:
            self._success_count += 1
            if idx in self._task_labels:
                self._task_labels[idx].setText(f"✓  {name}  —  {msg}")
                self._task_labels[idx].setStyleSheet(
                    "color: #60e888; font-size: 12px; background: transparent;"
                )
        else:
            self._fail_count += 1
            if idx in self._task_labels:
                self._task_labels[idx].setText(f"✗  {name}  —  {msg}")
                self._task_labels[idx].setStyleSheet(
                    "color: #f07070; font-size: 12px; background: transparent;"
                )

    def _on_all_done(self):
        self._worker = None
        self._progress.setValue(100)
        if self._fail_count == 0:
            self._progress.setStyleSheet(self._style_bar_ok())
            self._status_lbl.setText(f"Done!  {self._success_count}/{self._task_count} encoded")
            self._status_lbl.setStyleSheet("color: #60e888; font-size: 12px;")
        else:
            self._progress.setStyleSheet(self._style_bar_err())
            self._status_lbl.setText(
                f"Finished.  {self._success_count} ok  /  {self._fail_count} failed"
            )
            self._status_lbl.setStyleSheet("color: #e8c860; font-size: 12px;")

        self._stats_lbl.setVisible(False)
        self._encode_btn.setText("ENCODE AGAIN")
        self._encode_btn.setStyleSheet(self._style_btn_encode())
        self._encode_btn.setEnabled(True)
        self._back_btn.setEnabled(True)

    def cleanup(self):
        if self._worker is not None:
            self._worker.abort()
            self._worker.wait(2000)
            self._worker = None
