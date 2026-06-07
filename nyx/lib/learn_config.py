from __future__ import annotations
import json
from pathlib import Path
from typing import Optional


def _config_path() -> Path:
    return Path.home() / ".nyx" / "learn_config.json"


def _load() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def get_obsidian_root() -> Optional[Path]:
    """Return the configured Obsidian root, expanded. None if not set."""
    raw = _load().get("obsidian_root")
    if not raw:
        return None
    return Path(raw).expanduser()


def set_obsidian_root(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    data = _load()
    data["obsidian_root"] = str(resolved)
    data.pop("obsidian_skipped", None)
    _save(data)
    return resolved


def unset_obsidian_root() -> bool:
    data = _load()
    if "obsidian_root" not in data:
        return False
    del data["obsidian_root"]
    _save(data)
    return True


def is_obsidian_skipped() -> bool:
    return bool(_load().get("obsidian_skipped"))


def mark_obsidian_skipped() -> None:
    data = _load()
    data["obsidian_skipped"] = True
    _save(data)


def needs_first_run_prompt() -> bool:
    """True if neither a root nor a skip decision has been recorded."""
    data = _load()
    return not data.get("obsidian_root") and not data.get("obsidian_skipped")
