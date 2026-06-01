import json
from pathlib import Path

_CONFIG_DIR = Path.home() / "AppData" / "Local" / "SeqManager"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

DEFAULTS = {
    "fps": 24,
    "max_height": 200,
    "encode_fps": 30,
    "preset_index": 0,
    "gamma_fix": False,
}


def load() -> dict:
    try:
        with _CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {**DEFAULTS, **data}
    except Exception:
        return dict(DEFAULTS)


def save(config: dict) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump({k: config[k] for k in DEFAULTS if k in config}, f, indent=2)
