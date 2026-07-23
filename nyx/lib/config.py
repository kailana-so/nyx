from __future__ import annotations
import json
from pathlib import Path

_CONFIG_PATH = Path.home() / ".nyx" / "config.json"

_DEFAULTS: dict = {
    "model": "openrouter",
    "tier": "driver",
    "vault": str(Path.home() / "Documents" / "obsidian"),
    # surface → model id. Beats the tier mapping, so /code and /chat can run
    # different models without either being "the" session model.
    "surfaces": {},
    # Model ids you have assigned, most recent first — they head the /model list.
    "favourites": [],
}


# Tiers used to be named for price. A config written before the rename holds a
# name no provider defines, and since an unknown tier is passed through as a
# literal model id, it would reach the API as a model called "balanced".
_LEGACY_TIERS = {"cheap": "worker", "balanced": "driver", "top": "thinker"}


def _load() -> dict:
    if not _CONFIG_PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = {**_DEFAULTS, **json.loads(_CONFIG_PATH.read_text())}
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)
    data["tier"] = _LEGACY_TIERS.get(data["tier"], data["tier"])
    return data


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


def set_model(provider: str, tier: str = "driver") -> None:
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


def get_surfaces() -> dict[str, str]:
    """surface → model id, for surfaces you have assigned explicitly."""
    got = _load().get("surfaces")
    return dict(got) if isinstance(got, dict) else {}


def set_surface(surface: str, model: str) -> None:
    """Assign one surface. `surface` of "" clears back to tier routing."""
    data = _load()
    surfaces = dict(data.get("surfaces") or {})
    if model:
        surfaces[surface] = model
    else:
        surfaces.pop(surface, None)
    data["surfaces"] = surfaces
    _save(data)
    if model:
        remember_favourite(model)


def get_favourites() -> list[str]:
    got = _load().get("favourites")
    return [m for m in got if isinstance(m, str)] if isinstance(got, list) else []


def remember_favourite(model: str, keep: int = 8) -> None:
    """Most recently assigned first. Bounded — a favourites list you never prune
    stops being a shortlist."""
    data = _load()
    favs = [m for m in (data.get("favourites") or []) if isinstance(m, str) and m != model]
    data["favourites"] = [model, *favs][:keep]
    _save(data)
