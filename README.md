# SeqManager

Windows tool for previewing and encoding image sequences directly from the Explorer context menu.

Right-click any folder → **SEQ-MANAGER** → instant animated preview of all sequences found, with one-click encoding to H.264, H.265 or AV1.

---

## Features

- **Context menu integration** — works from any folder or folder background
- **Animated preview** — autoplay with smooth easing, frame scrub by moving the mouse left/right
- **Multi-sequence support** — lists all sequences in the folder, hover to preview each one
- **Encode panel** — select sequences, choose preset, encode to MP4 (H.264 / H.265 / AV1)
- **Progress tracking** — per-sequence progress bar and ETA during encoding
- **EXR support** — optional gamma correction for EXR → SDR workflows
- **Lightweight** — single `.exe`, no installer wizard, no dependencies for the end user

---

## Requirements (development)

- Python 3.12+
- PySide6
- Pillow
- ffmpeg in PATH (for encoding)

```
pip install -r requirements.txt
```

---

## Build

```
build.bat
```

Produces `dist/SeqManager.exe` via PyInstaller.

---

## Install / Uninstall

```
SeqManager.exe            # installs context menu entry
SeqManager.exe --uninstall
```

---

## Author

**Albert Callejo** — [electricavisuals.com](https://www.electricavisuals.com)
