import os
from pathlib import Path
from dotenv import load_dotenv

def _root() -> Path:
    return Path(__file__).parent.parent.parent

def load_env() -> None:
    load_dotenv(_root() / ".env")

def get_profile() -> str:
    return os.getenv("NYX_PROFILE", "personal")

def get_project() -> str:
    return os.getenv("NYX_PROJECT", "misc")

def set_active_project(name: str) -> None:
    """Update NYX_PROJECT in .env, preserving all other lines."""
    env_path = _root() / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith("NYX_PROJECT="):
            lines[i] = f"NYX_PROJECT={name}"
            break
    else:
        lines.append(f"NYX_PROJECT={name}")
    env_path.write_text("\n".join(lines) + "\n")
    os.environ["NYX_PROJECT"] = name
