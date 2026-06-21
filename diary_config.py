import json
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "diary-mcp"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "diary_config.json"

_DEFAULTS: dict = {
    "date_format": "%Y-%m-%d %H:%M:%S",
    "default_log_limit": 20,
    "default_author": "Claude",
    "show_archived_by_default": False,
    "remote_logger_urls": [],
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**_DEFAULTS, **json.load(f)}
        except (json.JSONDecodeError, OSError):
            pass
    return _DEFAULTS.copy()


def save_config(config_data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
