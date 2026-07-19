from __future__ import annotations
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from nyx.lib.config import get_vault, project_name, project_root


def _vault() -> Path:
    return get_vault()


def _episodic_dir() -> Path:
    p = _vault() / "episodic" / project_name()
    p.mkdir(parents=True, exist_ok=True)
    return p


def pending_dir() -> Path:
    """Raw exit transcripts waiting to be summarised on next launch."""
    p = _episodic_dir() / "pending"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _proposals_path() -> Path:
    return _episodic_dir() / "_proposals.json"


def queue_proposals(proposals: list[dict]) -> None:
    """Decisions/ideas extracted from a session that ended. The summariser runs on
    a background thread and can't prompt, so proposals wait for the next session."""
    if not proposals:
        return
    existing = take_proposals()
    _proposals_path().write_text(json.dumps(existing + proposals))


def take_proposals() -> list[dict]:
    """Read and clear the queue."""
    path = _proposals_path()
    if not path.exists():
        return []
    try:
        out = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        out = []
    path.unlink(missing_ok=True)
    return out


def write_episodic(content: str) -> Path:
    """One file per session. Sessions are the unit you resume from, so they stay
    separate — collapsing a day's sessions into one file blurs the boundary."""
    path = _episodic_dir() / f"{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(content.strip() + "\n")
    return path


def read_episodic(limit: int = 3) -> str:
    """The last few sessions on this project, newest first. Resume is a recency
    question — reaching further back is what decisions.md and ideas.md are for."""
    files = sorted(_episodic_dir().glob("*.md"), reverse=True)[:limit]
    if not files:
        return ""
    return "\n\n".join(f"### session {_stamp(f)}\n{f.read_text().strip()}" for f in files)


def _stamp(path: Path) -> str:
    try:
        return datetime.strptime(path.stem, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return path.stem


def read_practices(languages: list[str], cap: int = 4000) -> str:
    """Saved best-practice docs for the given languages, newest first, capped
    so they can ride along in every code/test-mode system prompt."""
    root = _vault() / "best-practices"
    if not root.exists():
        return ""
    files = [p for lang in languages for p in sorted(root.glob(f"*/{lang}.md"))]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    parts: list[str] = []
    used = 0
    for p in files:
        entry = f"### {p.parent.name}/{p.stem}\n{p.read_text().strip()}"
        if used + len(entry) > cap:
            break
        parts.append(entry)
        used += len(entry)
    return "\n\n".join(parts)


def write_practice(pattern: str, language: str, content: str) -> Path:
    p = _vault() / "best-practices" / pattern
    p.mkdir(parents=True, exist_ok=True)
    path = p / f"{language}.md"
    path.write_text(content)
    return path


def write_spec(slug: str, content: str) -> Path:
    p = project_root() / "specs"
    p.mkdir(parents=True, exist_ok=True)
    path = p / f"{slug}.md"
    path.write_text(content)
    return path


def append_decision(title: str, body: str) -> Path:
    path = project_root() / "decisions.md"
    entry = f"\n## {title}\n\n{body}\n"
    if path.exists():
        path.write_text(path.read_text() + entry)
    else:
        path.write_text(f"# Decisions\n{entry}")
    return path


def append_idea(title: str, body: str) -> Path:
    path = project_root() / "ideas.md"
    entry = f"\n- **{title}**: {body}\n"
    if path.exists():
        path.write_text(path.read_text() + entry)
    else:
        path.write_text(f"# Ideas\n{entry}")
    return path


def _read_capped(path: Path, cap: int) -> str:
    """These ride in every prompt. Providers without a prompt cache re-send them
    on every turn, so an unbounded file here quietly throttles the whole session."""
    if not path.exists():
        return ""
    text = path.read_text().strip()
    if len(text) <= cap:
        return text
    return (
        text[:cap]
        + f"\n\n[… {path.name} truncated at {cap} chars — it is too long to carry in "
          f"every prompt. Prune it, or move the detail into specs/.]"
    )


def read_decisions(cap: int = 6000) -> str:
    return _read_capped(project_root() / "decisions.md", cap)


def read_ideas(cap: int = 3000) -> str:
    return _read_capped(project_root() / "ideas.md", cap)


def write_architecture(area: str, name: str, content: str) -> Path:
    p = project_root() / "architecture" / area
    p.mkdir(parents=True, exist_ok=True)
    path = p / f"{name}.md"
    path.write_text(content)
    return path


def read_project_context() -> str:
    nyx_md = project_root() / "nyx.md"
    return nyx_md.read_text() if nyx_md.exists() else ""


def read_architecture_index() -> str:
    """The architecture docs as an index — each doc's path and its first heading —
    not the full contents. Loaded into building/reviewing modes so the model knows
    the structure exists and reads the relevant doc before touching that area,
    rather than inventing a parallel structure or paying for every doc every turn."""
    root = project_root() / "architecture"
    if not root.is_dir():
        return ""
    lines = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(project_root())
        heading = ""
        for line in path.read_text().splitlines():
            if line.startswith("#"):
                heading = line.lstrip("# ").strip()
                break
        lines.append(f"- `{rel}`" + (f" — {heading}" if heading else ""))
    return "\n".join(lines)


_STOPWORDS = {
    "the", "and", "for", "was", "were", "did", "does", "how", "why", "what", "when",
    "with", "from", "that", "this", "there", "their", "have", "has", "had", "you",
    "your", "our", "are", "not", "but", "about", "into", "any", "can", "use", "used",
}


def _terms(query: str) -> list[str]:
    """A natural-language query is not a regex. Pull the content words out and
    match on those — passing the raw sentence to rg matches nothing."""
    words = re.findall(r"[\w'-]+", query.lower())
    terms = [w for w in words if len(w) > 2 and w not in _STOPWORDS]
    return terms or words


def search_vault(query: str) -> str:
    """Keyword search over the vault — past sessions, practices, learnings.
    Files are ranked by how many distinct query terms they hit."""
    vault = _vault()
    terms = _terms(query)
    if not terms:
        return "No matches found."
    pattern = "|".join(re.escape(t) for t in terms)

    hits: dict[Path, set[str]] = {}
    result = subprocess.run(
        ["rg", "-i", "--only-matching", "--with-filename", "--no-line-number",
         "-e", pattern, str(vault)],
        capture_output=True, text=True, timeout=10,
    )
    for line in result.stdout.splitlines():
        filepath, _, matched = line.rpartition(":")
        if filepath:
            hits.setdefault(Path(filepath), set()).add(matched.lower())

    if not hits:
        return "No matches found."

    # Most distinct terms first, then most recent — a file hitting three of your
    # words beats one hitting the same word thirty times.
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), -kv[0].stat().st_mtime))

    parts: list[str] = []
    for filepath, matched in ranked[:5]:
        r = subprocess.run(
            ["rg", "-i", "--max-count=3", "-C1", "-e", pattern, str(filepath)],
            capture_output=True, text=True, timeout=5,
        )
        rel = filepath.relative_to(vault)
        parts.append(f"**{rel}** (matched: {', '.join(sorted(matched))})\n{r.stdout[:500]}")

    return "\n\n".join(parts)
