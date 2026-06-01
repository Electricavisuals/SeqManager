import re
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

PRESETS = [
    {
        "name": "AV1     HQ   CRF 18",
        "ext": ".mp4",
        "vcodec": "libsvtav1",
        "crf": 18,
        "preset_speed": None,
        "pix_fmt": "yuv420p",
        "extra": ["-preset", "4"],
    },
    {
        "name": "AV1     Med  CRF 28",
        "ext": ".mp4",
        "vcodec": "libsvtav1",
        "crf": 28,
        "preset_speed": None,
        "pix_fmt": "yuv420p",
        "extra": ["-preset", "6"],
    },
    {
        "name": "H.264   HQ   CRF 18",
        "ext": ".mp4",
        "vcodec": "libx264",
        "crf": 18,
        "preset_speed": "slow",
        "pix_fmt": "yuv420p",
        "extra": [],
    },
    {
        "name": "H.264   Med  CRF 23",
        "ext": ".mp4",
        "vcodec": "libx264",
        "crf": 23,
        "preset_speed": "medium",
        "pix_fmt": "yuv420p",
        "extra": [],
    },
    {
        "name": "H.264   Web  CRF 28",
        "ext": ".mp4",
        "vcodec": "libx264",
        "crf": 28,
        "preset_speed": "medium",
        "pix_fmt": "yuv420p",
        "extra": ["-movflags", "+faststart"],
    },
    {
        "name": "H.265   HQ   CRF 18",
        "ext": ".mp4",
        "vcodec": "libx265",
        "crf": 18,
        "preset_speed": "slow",
        "pix_fmt": "yuv420p",
        "extra": [],
    },
    {
        "name": "H.265   Med  CRF 23",
        "ext": ".mp4",
        "vcodec": "libx265",
        "crf": 23,
        "preset_speed": "medium",
        "pix_fmt": "yuv420p",
        "extra": [],
    },
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_FFMPEG_FALLBACK_DIRS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\Program Files (x86)\ffmpeg\bin",
]


def find_ffmpeg() -> str:
    """Return the ffmpeg executable path, checking PATH then common install dirs."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for d in _FFMPEG_FALLBACK_DIRS:
        candidate = Path(d) / "ffmpeg.exe"
        if candidate.exists():
            return str(candidate)
    return "ffmpeg"  # fallback — will raise FileNotFoundError at runtime


def ffmpeg_available() -> bool:
    found = shutil.which("ffmpeg")
    if found:
        return True
    return any((Path(d) / "ffmpeg.exe").exists() for d in _FFMPEG_FALLBACK_DIRS)


def get_available_presets() -> list:
    if not ffmpeg_available():
        return PRESETS
    try:
        result = subprocess.run(
            [find_ffmpeg(), "-encoders"],
            capture_output=True, text=True, timeout=8,
            creationflags=_NO_WINDOW,
        )
        output = result.stdout + result.stderr
        return [p for p in PRESETS if p["vcodec"] in output]
    except Exception:
        return PRESETS


def build_input_pattern(seq) -> tuple:
    """Returns (ffmpeg_pattern_path, start_number)."""
    first = Path(seq.frames[0])
    m = re.match(r'^(.*?)(\d+)$', first.stem)
    digits = m.group(2)
    fmt = f"%0{len(digits)}d" if (len(digits) > 1 and digits[0] == '0') else "%d"
    pattern = str(first.parent / f"{seq.name}{fmt}{seq.extension}")
    return pattern, seq.frame_numbers[0]


def output_stem(seq) -> str:
    s = seq.name.rstrip('_.- ')
    if not s or s == '(unnamed)':
        return Path(seq.folder).name
    return s


@dataclass
class EncodeTask:
    seq: object
    out_path: Path
    fps: int
    preset: dict
    gamma_fix: bool


class EncodeWorker(QThread):
    task_started  = Signal(int)            # task index
    task_progress = Signal(int, int, str)  # current_frame, total_frames, speed_str
    task_done     = Signal(int, bool, str) # task_index, success, message
    all_done      = Signal()

    def __init__(self, tasks: list, parent=None):
        super().__init__(parent)
        self._tasks = tasks
        self._abort = False

    def abort(self):
        self._abort = True

    def run(self):
        for i, task in enumerate(self._tasks):
            if self._abort:
                self.task_done.emit(i, False, "Aborted")
                break
            self.task_started.emit(i)
            success, msg = self._encode(task)
            self.task_done.emit(i, success, msg)
        self.all_done.emit()

    def _encode(self, task: EncodeTask):
        pattern, start = build_input_pattern(task.seq)
        total = len(task.seq.frames)
        p = task.preset

        cmd = [
            find_ffmpeg(), "-y",
            "-framerate", str(task.fps),
            "-start_number", str(start),
            "-i", pattern,
        ]

        vf_parts = []
        if task.gamma_fix and task.seq.extension == '.exr':
            vf_parts.append(
                "zscale=transfer=linear:matrix=bt709:primaries=bt709,"
                "tonemap=hable,"
                "zscale=transfer=bt709:matrix=bt709:primaries=bt709"
            )

        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]

        cmd += ["-vcodec", p["vcodec"], "-crf", str(p["crf"]), "-pix_fmt", p["pix_fmt"]]
        if p.get("preset_speed"):
            cmd += ["-preset", p["preset_speed"]]
        cmd += p.get("extra", [])
        cmd += [str(task.out_path)]

        try:
            proc = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
            )

            stderr_lines = []
            remaining = ""
            while True:
                chunk = proc.stderr.read(256)
                if not chunk:
                    break
                if self._abort:
                    proc.kill()
                    return False, "Aborted"
                text = remaining + chunk
                lines = re.split(r'[\r\n]', text)
                remaining = lines[-1]
                for line in lines[:-1]:
                    stderr_lines.append(line)
                    fm = re.search(r'frame=\s*(\d+)', line)
                    if fm:
                        current = int(fm.group(1))
                        sm = re.search(r'speed=\s*(\S+)', line)
                        self.task_progress.emit(current, total, sm.group(1) if sm else "")

            proc.wait()
            if proc.returncode != 0:
                keywords = ('unknown encoder', 'codec not found', 'not found', 'error', 'invalid', 'no such', 'too few')
                useful = [l.strip() for l in stderr_lines
                          if any(k in l.lower() for k in keywords)
                          and l.strip()
                          and not l.strip().startswith('[')]
                if not useful:
                    useful = [l.strip() for l in stderr_lines if l.strip()]
                msg = useful[0][:120] if useful else f"ffmpeg exit {proc.returncode}"
                return False, msg

            size_mb = task.out_path.stat().st_size / (1024 * 1024) if task.out_path.exists() else 0
            return True, f"{task.out_path.name}  ({size_mb:.1f} MB)"

        except FileNotFoundError:
            return False, "ffmpeg not found in PATH"
        except Exception as e:
            return False, str(e)
