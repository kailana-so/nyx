"""The Obsidian knowledge base — notes written in learn mode, and read back
anywhere. Layout is `<vault>/<topic>/<title>.md`, topics nest with `/`.

Distinct from memory.py: that holds project state (repo) and session history
(episodic). This holds what the user knows, and it is theirs — the cardinal rule
is that nothing here is ever shortened. Notes only grow.
"""
from __future__ import annotations
import re
from datetime import date
from pathlib import Path

from nyx.lib.config import get_vault

# Vault subdirectories nyx manages for itself — never topics.
_RESERVED = {"episodic", "best-practices", "learnings", ".obsidian", ".trash"}


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name.lower()).strip()
    return re.sub(r"[\s_]+", "-", s)[:60] or "untitled"


def _topic_dir(topic: str) -> Path:
    parts = [_slug(p) for p in topic.split("/") if p.strip()]
    return get_vault().joinpath(*parts) if parts else get_vault()


def note_path(topic: str, title: str) -> Path:
    return _topic_dir(topic) / f"{_slug(title)}.md"


def split_frontmatter(text: str) -> tuple[dict, str]:
    """(meta, body). Frontmatter is flat key: value, which is all we write."""
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---")
    fm, sep, body = rest.partition("---")
    if not sep:
        return {}, text
    meta: dict[str, str] = {}
    for line in fm.strip().splitlines():
        key, _, value = line.partition(":")
        if value.strip():
            meta[key.strip()] = value.strip()
    return meta, body.strip()


def read_note(topic: str, title: str) -> str:
    """The note's body — no frontmatter, no leading title heading. write_note re-adds
    the heading, so returning it here would duplicate it on every rewrite."""
    path = note_path(topic, title)
    if not path.exists():
        return ""
    body = split_frontmatter(path.read_text())[1]
    return re.sub(r"\A#\s+.*\n+", "", body).strip()


def write_note(topic: str, title: str, tags: list[str], body: str) -> Path:
    """Write (or rewrite) a note. Preserves the original created date, and unions
    tags with any already on the note — tags only accumulate, never drop."""
    path = note_path(topic, title)
    path.parent.mkdir(parents=True, exist_ok=True)

    created = date.today().isoformat()
    all_tags = {_slug(t) for t in tags if t.strip()}
    if path.exists():
        meta = split_frontmatter(path.read_text())[0]
        created = meta.get("created", created)
        all_tags |= {t.strip() for t in meta.get("tags", "").strip("[]").split(",") if t.strip()}

    tag_list = ", ".join(sorted(all_tags))
    front = (
        "---\n"
        f"topic: {topic}\n"
        f"tags: [{tag_list}]\n"
        f"created: {created}\n"
        f"updated: {date.today().isoformat()}\n"
        "---\n\n"
    )
    path.write_text(f"{front}# {title}\n\n{body.strip()}\n")
    return path


def _note_files() -> list[Path]:
    vault = get_vault()
    if not vault.exists():
        return []
    return [
        p for p in vault.rglob("*.md")
        if not any(part in _RESERVED for part in p.relative_to(vault).parts)
    ]


def list_topics() -> str:
    """The topic tree with note titles and tags — the index the model navigates
    to answer questions about what the user knows."""
    vault = get_vault()
    by_topic: dict[str, list[str]] = {}
    for path in sorted(_note_files()):
        rel = path.relative_to(vault)
        topic = "/".join(rel.parts[:-1]) or "(root)"
        meta, _ = split_frontmatter(path.read_text())
        tags = meta.get("tags", "").strip("[]")
        by_topic.setdefault(topic, []).append(
            f"  - {path.stem}" + (f"  [tags: {tags}]" if tags else "")
        )
    if not by_topic:
        return "No notes yet."
    return "\n".join(f"{topic}\n" + "\n".join(titles) for topic, titles in sorted(by_topic.items()))
