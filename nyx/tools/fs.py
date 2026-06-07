from __future__ import annotations
import subprocess
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path

SANDBOX_ROOT = Path.home() / "Documents"
_REGISTRY: dict[str, dict] = {}


def _register(name: str, description: str, parameters: dict):
    def decorator(fn):
        _REGISTRY[name] = {"fn": fn, "description": description, "parameters": parameters}
        return fn
    return decorator


def _safe_path(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not str(p).startswith(str(SANDBOX_ROOT)):
        raise PermissionError(f"Path outside sandbox: {p}")
    return p


def _read_pdf(p: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(f"--- Page {i} ---\n{text}")
    return "\n\n".join(pages) if pages else "(no extractable text)"


@_register("read_file", "Read the contents of a file. PDFs (.pdf) are auto-extracted to text.", {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Absolute or ~ path to file"}},
    "required": ["path"],
})
def read_file(path: str) -> str:
    try:
        p = _safe_path(path)
        if p.suffix.lower() == ".pdf":
            return _read_pdf(p)
        return p.read_text()
    except PermissionError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return f"ERROR: File not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("write_file", "Write content to a file, creating directories as needed", {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
})
def write_file(path: str, content: str) -> str:
    try:
        p = _safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"OK: wrote {len(content)} chars to {path}"
    except PermissionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("list_dir", "List files and directories at a path", {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
})
def list_dir(path: str) -> str:
    try:
        p = _safe_path(path)
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        lines = []
        for e in entries:
            prefix = "📁 " if e.is_dir() else "📄 "
            lines.append(f"{prefix}{e.name}")
        return "\n".join(lines) if lines else "(empty directory)"
    except PermissionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("patch_file", (
    "Replace an exact string in a file. Safer than write_file for targeted edits — "
    "fails if old_str is not found or matches multiple locations. "
    "IMPORTANT: always re-read the file before patching if you wrote to it earlier this session."
), {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "old_str": {"type": "string", "description": "Exact string to replace. Must match exactly once."},
        "new_str": {"type": "string", "description": "Replacement string."},
    },
    "required": ["path", "old_str", "new_str"],
})
def patch_file(path: str, old_str: str, new_str: str) -> str:
    try:
        p = _safe_path(path)
        content = p.read_text()
        count = content.count(old_str)
        if count == 0:
            return f"ERROR: old_str not found in {path}"
        if count > 1:
            return f"ERROR: old_str matches {count} locations — use a longer, more specific snippet"
        p.write_text(content.replace(old_str, new_str, 1))
        return f"OK: patched {path}"
    except PermissionError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return f"ERROR: File not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "nav", "footer", "head"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "nav", "footer", "head"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self._chunks.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


@_register("fetch_url", "Fetch a URL and return its text content. Useful for checking docs or references.", {
    "type": "object",
    "properties": {"url": {"type": "string"}},
    "required": ["url"],
})
def fetch_url(url: str, timeout: int = 15) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nyx/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parser = _HTMLTextExtractor()
        parser.feed(raw)
        text = parser.get_text()
        return text[:8000] + ("…[truncated]" if len(text) > 8000 else "")
    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} — {url}"
    except urllib.error.URLError as e:
        return f"ERROR: {e.reason} — {url}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("run_bash", "Run a shell command and return stdout/stderr", {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "timeout": {"type": "integer", "default": 30},
    },
    "required": ["command"],
})
def run_bash(command: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(SANDBOX_ROOT)
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"stderr: {err}")
        if result.returncode != 0:
            parts.append(f"exit code: {result.returncode}")
        return "\n".join(parts) if parts else "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


@_register("done", "Signal that the task is complete", {
    "type": "object",
    "properties": {"summary": {"type": "string", "description": "Brief summary of what was done"}},
    "required": ["summary"],
})
def _done_sentinel(summary: str = "") -> str:
    return summary or "Done."


# ── Derived definitions (single source of truth) ───────────────────────────────

TOOL_DEFINITIONS = [
    {"type": "function", "function": {"name": k, "description": v["description"], "parameters": v["parameters"]}}
    for k, v in _REGISTRY.items()
]

TOOL_DEFINITIONS_BEDROCK = [
    {"toolSpec": {"name": k, "description": v["description"], "inputSchema": {"json": v["parameters"]}}}
    for k, v in _REGISTRY.items()
]


def execute_tool(name: str, args: dict) -> str:
    entry = _REGISTRY.get(name)
    if not entry:
        return f"ERROR: unknown tool {name}"
    allowed = entry["parameters"].get("properties", {})
    filtered = {k: v for k, v in args.items() if k in allowed}
    try:
        return entry["fn"](**filtered)
    except Exception as e:
        return f"ERROR: {e}"
