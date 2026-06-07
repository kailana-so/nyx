"""One-way export: learning rows in DB → markdown files in the user's Obsidian vault.

Layout: <obsidian_root>/<topic>/<title-slug>.md  (topic may contain `/` for nesting).
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from nyx.memory.supabase import Learning
from nyx.lib.learn_config import get_obsidian_root


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_TRIM_DOTS = re.compile(r'^\.+|\.+$')


def _slug_segment(name: str) -> str:
    """Sanitise a single path segment for the filesystem (preserves spaces and case)."""
    s = _INVALID_FILENAME_CHARS.sub("-", name).strip()
    s = _TRIM_DOTS.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s[:120] or "untitled"


def _topic_to_subpath(topic: str) -> Path:
    """Convert a topic string (which may contain `/` for nesting) into a relative path."""
    parts = [_slug_segment(p) for p in topic.split("/") if p.strip()]
    return Path(*parts) if parts else Path("misc")


def md_path_for(learning: Learning, root: Optional[Path] = None) -> Optional[Path]:
    root = root or get_obsidian_root()
    if root is None:
        return None
    return root / _topic_to_subpath(learning.topic) / f"{_slug_segment(learning.title)}.md"


def _frontmatter(learning: Learning) -> str:
    lines = ["---"]
    lines.append(f"id: {learning.id}")
    lines.append(f"kind: {learning.kind}")
    lines.append(f"topic: {learning.topic}")
    if learning.language:
        lines.append(f"language: {learning.language}")
    if learning.tags:
        tags_inline = ", ".join(learning.tags)
        lines.append(f"tags: [{tags_inline}]")
    if learning.source:
        lines.append(f"source: {learning.source}")
    if learning.created_at:
        lines.append(f"created: {learning.created_at}")
    if learning.updated_at:
        lines.append(f"updated: {learning.updated_at}")
    lines.append("---")
    return "\n".join(lines)


def export_learning(learning: Learning, *, previous: Optional[Learning] = None) -> Optional[Path]:
    """Write the MD file for `learning`. If `previous` is given and its path differs, remove the old file."""
    root = get_obsidian_root()
    if root is None:
        return None

    target = md_path_for(learning, root)
    if target is None:
        return None

    if previous is not None:
        old = md_path_for(previous, root)
        if old is not None and old != target and old.exists():
            try:
                old.unlink()
            except OSError:
                pass
            # Best-effort cleanup of newly empty parents (stop at root)
            parent = old.parent
            while parent != root and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_frontmatter(learning) + "\n\n# " + learning.title + "\n\n" + learning.body.rstrip() + "\n")
    return target


def remove_learning_file(learning: Learning) -> Optional[Path]:
    """Delete the MD file for a learning (best-effort). Returns the removed path or None."""
    root = get_obsidian_root()
    if root is None:
        return None
    target = md_path_for(learning, root)
    if target is None or not target.exists():
        return None
    try:
        target.unlink()
    except OSError:
        return None
    parent = target.parent
    while parent != root and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return target
