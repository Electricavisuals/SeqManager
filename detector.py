import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SUPPORTED_EXTENSIONS = {'.png', '.exr', '.tga', '.jpg', '.jpeg', '.tiff', '.tif', '.dpx'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.mxf'}


@dataclass
class Sequence:
    name: str
    folder: str
    extension: str
    frames: list
    frame_numbers: list
    gaps: list
    resolution: Optional[tuple]


@dataclass
class Video:
    name: str
    folder: str
    path: str


def _read_resolution(path: str, ext: str) -> Optional[tuple]:
    try:
        if ext == '.exr':
            import OpenEXR
            f = OpenEXR.InputFile(path)
            dw = f.header()['dataWindow']
            w = dw.max.x - dw.min.x + 1
            h = dw.max.y - dw.min.y + 1
            f.close()
            return (w, h)
        else:
            from PIL import Image
            with Image.open(path) as img:
                return img.size
    except Exception:
        return None


def _scan_single(folder: Path) -> list:
    groups = {}
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        m = re.match(r'^(.*?)(\d+)$', entry.stem)
        if not m:
            continue
        prefix, frame_num = m.group(1), int(m.group(2))
        groups.setdefault((prefix, ext), []).append((frame_num, str(entry)))

    sequences = []
    for (prefix, ext), items in groups.items():
        items.sort(key=lambda x: x[0])
        frame_numbers = [n for n, _ in items]
        frames = [p for _, p in items]
        min_f, max_f = frame_numbers[0], frame_numbers[-1]
        span = max_f - min_f + 1
        gaps = [n for n in range(min_f, max_f + 1) if n not in set(frame_numbers)] if span <= 100_000 else []
        sequences.append(Sequence(
            name=prefix or "(unnamed)",
            folder=str(folder),
            extension=ext,
            frames=frames,
            frame_numbers=frame_numbers,
            gaps=gaps,
            resolution=_read_resolution(frames[0], ext),
        ))
    return sequences


def _scan_videos(folder: Path) -> list:
    videos = []
    for entry in sorted(folder.iterdir()):
        if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(Video(name=entry.stem, folder=str(folder), path=str(entry)))
    return videos


def scan_folder(folder_path: str) -> list:
    folder = Path(folder_path)
    if not folder.is_dir():
        return []

    sequences = [s for s in _scan_single(folder) if len(s.frames) >= 2]
    videos = _scan_videos(folder)

    for sub in sorted(folder.iterdir()):
        if sub.is_dir() and not sub.name.startswith('.'):
            sequences.extend(s for s in _scan_single(sub) if len(s.frames) >= 2)
            videos.extend(_scan_videos(sub))

    sequences.sort(key=lambda s: (Path(s.folder).name.lower(), s.name.lower()))
    videos.sort(key=lambda v: (Path(v.folder).name.lower(), v.name.lower()))
    return sequences + videos
