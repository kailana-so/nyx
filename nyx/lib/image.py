"""Image attachment support for chat surfaces.

Usage in a message: @~/Desktop/screenshot.png or @/absolute/path/to/image.png

The @ref is stripped from the text and attached as a Bedrock image content block.
Supported formats: JPEG, PNG, GIF, WEBP.
"""
from __future__ import annotations
import re
from pathlib import Path

from nyx.lib.format import console

_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_FMT  = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".gif": "gif", ".webp": "webp"}
# Match @path — handles backslash-escaped spaces (e.g. drag-and-drop from terminal)
_REF  = re.compile(r"@((?:\\.|\S)+)")


def extract_images(text: str) -> tuple[str, list[dict]]:
    """Parse @<path> image refs from text.

    Returns (cleaned_text, bedrock_image_blocks).
    Unresolvable or non-image @refs are left in the text unchanged.
    """
    image_blocks: list[dict] = []
    errors: list[str] = []

    def _replace(m: re.Match) -> str:
        raw = m.group(1).rstrip(".,;:!?)")  # strip trailing punctuation
        unescaped = raw.replace("\\ ", " ")  # undo terminal backslash-escaping
        path = Path(unescaped).expanduser().resolve()
        if path.suffix.lower() not in _EXTS:
            return m.group(0)
        if not path.exists():
            errors.append(f"image not found: {unescaped}")
            return m.group(0)
        fmt = _FMT[path.suffix.lower()]
        try:
            data = path.read_bytes()
        except OSError as e:
            errors.append(f"cannot read {raw}: {e}")
            return m.group(0)
        image_blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
        kb = len(data) / 1024
        console.print(f"  [dim]📎 {path.name} ({kb:.0f} KB)[/dim]")
        return f"[image: {path.name}]"

    cleaned = _REF.sub(_replace, text).strip()
    for err in errors:
        console.print(f"  [yellow]{err}[/yellow]")

    return cleaned, image_blocks
