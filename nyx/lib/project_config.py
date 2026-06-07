from __future__ import annotations
import json
import os
from pathlib import Path


def _config_path() -> Path:
    return Path.home() / ".nyx" / "project_config.json"


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


def _default_base() -> Path:
    return Path(os.getenv("NYX_PROJECT_DIR", str(Path.home() / "Documents"))).expanduser()


def get_project_root(name: str) -> Path:
    """Configured root for `name`, else NYX_PROJECT_DIR/<name>."""
    entry = _load().get(name)
    if isinstance(entry, dict) and entry.get("root"):
        return Path(entry["root"]).expanduser()
    return _default_base() / name


def set_project_root(name: str, root: str) -> Path:
    resolved = Path(root).expanduser().resolve()
    data = _load()
    entry = data.get(name)
    if not isinstance(entry, dict):
        entry = {}
    entry["root"] = str(resolved)
    data[name] = entry
    _save(data)
    return resolved


def unset_project_root(name: str) -> bool:
    data = _load()
    entry = data.get(name)
    if not isinstance(entry, dict) or "root" not in entry:
        return False
    del entry["root"]
    if not entry:
        del data[name]
    else:
        data[name] = entry
    _save(data)
    return True


def get_all_roots() -> dict[str, str]:
    return {k: v["root"] for k, v in _load().items()
            if isinstance(v, dict) and v.get("root")}
