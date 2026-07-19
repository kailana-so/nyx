from __future__ import annotations
import json
from pathlib import Path

_CONFIG_PATH = Path.home() / ".nyx" / "config.json"

_DEFAULTS: dict = {
    "model": "devstral",
    "tier": "balanced",
    "vault": str(Path.home() / "Documents" / "obsidian"),
}


def _load() -> dict:
    if not _CONFIG_PATH.exists():
        return dict(_DEFAULTS)
    try:
        return {**_DEFAULTS, **json.loads(_CONFIG_PATH.read_text())}
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)


def _save(data: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")


def project_name() -> str:
    return Path.cwd().name


def project_root() -> Path:
    return Path.cwd()


def get_vault() -> Path:
    return Path(_load()["vault"]).expanduser()


def get_model() -> str:
    return _load()["model"]


def get_tier() -> str:
    return _load()["tier"]


def set_model(provider: str, tier: str = "balanced") -> None:
    data = _load()
    data["model"] = provider
    data["tier"] = tier
    _save(data)


def set_vault(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    data = _load()
    data["vault"] = str(resolved)
    _save(data)
    return resolved
