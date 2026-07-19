from __future__ import annotations
import subprocess
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path

from nyx.lib.config import get_vault, project_root

_REGISTRY: dict[str, dict] = {}

_YES = {"y", "yes", "yep", "yeah", "ok", "okay", "sure", "save", "do it", "go"}
_NO = {"n", "no", "nope", "cancel", "stop"}


def read_verdict(raw: str) -> bool | None:
    """True = approved, False = rejected, None = it's feedback.

    Typing "yes" at a [y/n] prompt used to fall through to the feedback branch,
    so the model would "revise" an approved artefact and re-ask, forever.
    """
    answer = raw.strip().lower().rstrip(".!")
    if answer in _YES:
        return True
    if answer in _NO:
        return False
    return None


def _register(name: str, description: str, parameters: dict):
    def decorator(fn):
        _REGISTRY[name] = {"fn": fn, "description": description, "parameters": parameters}
        return fn
    return decorator


def _safe_path(path: str, write: bool = False) -> Path:
    # Weak models sometimes emit paths wrapped in stray quotes or whitespace.
    p = Path(path.strip().strip("'\"")).expanduser().resolve()
    write_roots = (str(project_root().resolve()), str(get_vault().resolve()))
    read_roots  = write_roots + (str(Path.home() / "Documents"),)
    allowed = write_roots if write else read_roots
    if not any(str(p).startswith(root) for root in allowed):
        raise PermissionError(f"Path outside allowed roots: {p}")
    return p


# ── Filesystem tools ──────────────────────────────────────────────────────────

# Uncapped tool output lands in the history and is re-sent every round after —
# clamp at the source so one careless read can't tax the whole turn.
_MAX_READ_LINES = 1500
_MAX_READ_BYTES = 40_000


def _clamp_read(text: str, total_lines: int) -> str:
    if total_lines <= _MAX_READ_LINES and len(text) <= _MAX_READ_BYTES:
        return text
    lines = text.splitlines()[:_MAX_READ_LINES]
    out = "\n".join(lines)
    if len(out) > _MAX_READ_BYTES:
        out = out[:_MAX_READ_BYTES]
        out = out[:out.rfind("\n")] if "\n" in out else out
    shown = out.count("\n") + 1
    return (out + f"\n[truncated at line {shown} of {total_lines} — "
                  "call read_file with start_line/end_line for the rest]")


@_register("read_file", (
    "Read the contents of a file. PDFs are auto-extracted to text. Large files are "
    "truncated — pass start_line/end_line (1-indexed, inclusive) to read a specific range."
), {
    "type": "object",
    "properties": {
        "path":       {"type": "string"},
        "start_line": {"type": "integer", "description": "1-indexed first line to read"},
        "end_line":   {"type": "integer", "description": "1-indexed last line to read (inclusive)"},
    },
    "required": ["path"],
})
def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    try:
        p = _safe_path(path)
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            pages = [f"--- Page {i} ---\n{pg.extract_text() or ''}"
                     for i, pg in enumerate(PdfReader(str(p)).pages, 1)]
            text = "\n\n".join(pages) or "(no extractable text)"
            return _clamp_read(text, text.count("\n") + 1)
        text = p.read_text()
        if start_line or end_line:
            lines = text.splitlines()
            total = len(lines)
            lo = max(1, int(start_line or 1))
            hi = min(total, int(end_line or total))
            if lo > hi:
                return f"ERROR: empty range {lo}-{hi} (file has {total} lines)"
            body = "\n".join(lines[lo - 1:hi])
            return f"[lines {lo}-{hi} of {total}]\n" + _clamp_read(body, hi - lo + 1)
        return _clamp_read(text, text.count("\n") + 1)
    except PermissionError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("list_dir", "List files and directories at a path.", {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
})
def list_dir(path: str) -> str:
    try:
        p = _safe_path(path)
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        return "\n".join(
            ("  " if e.is_dir() else "  ") + e.name for e in entries
        ) or "(empty)"
    except PermissionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("write_file", "Write content to a file, creating directories as needed.", {
    "type": "object",
    "properties": {
        "path":    {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
})
def write_file(path: str, content: str) -> str:
    try:
        p = _safe_path(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"OK: wrote {len(content)} chars to {path}"
    except PermissionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("patch_file", (
    "Replace an exact string in a file. Fails if old_str is not found or matches multiple times. "
    "Re-read the file before patching if you wrote to it this session."
), {
    "type": "object",
    "properties": {
        "path":    {"type": "string"},
        "old_str": {"type": "string", "description": "Exact string to replace. Must match exactly once."},
        "new_str": {"type": "string"},
    },
    "required": ["path", "old_str", "new_str"],
})
def patch_file(path: str, old_str: str, new_str: str) -> str:
    try:
        p = _safe_path(path, write=True)
        content = p.read_text()
        count = content.count(old_str)
        if count == 0:
            return f"ERROR: old_str not found in {path}"
        if count > 1:
            return f"ERROR: old_str matches {count} times — use a longer, more specific snippet"
        p.write_text(content.replace(old_str, new_str, 1))
        return f"OK: patched {path}"
    except PermissionError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"


@_register("grep", (
    "Search file contents with a regex. Returns matching lines as path:line:text. "
    "ALWAYS use this instead of run_bash with grep/find/rg."
), {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex to search for"},
        "path":    {"type": "string", "description": "Directory or file to search (default: project root)"},
        "glob":    {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
    },
    "required": ["pattern"],
})
def grep(pattern: str, path: str | None = None, glob: str | None = None) -> str:
    try:
        p = _safe_path(path) if path else project_root()
        cmd = ["rg", "-n", "-S", "--max-count", "5", "--max-columns", "200"]
        if glob:
            cmd += ["--glob", glob]
        cmd += ["--", pattern, str(p)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 1:
            return "No matches found."
        if r.returncode != 0:
            return f"ERROR: {r.stderr.strip()[:200]}"
        lines = r.stdout.splitlines()
        root = str(project_root()) + "/"
        lines = [line.removeprefix(root) for line in lines]
        if len(lines) > 100:
            lines = lines[:100] + [f"… {len(lines) - 100} more matching lines — narrow the pattern"]
        return "\n".join(lines)
    except PermissionError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return "ERROR: ripgrep (rg) is not installed"
    except Exception as e:
        return f"ERROR: {e}"


@_register("glob", (
    "Find files by name pattern relative to the project root, e.g. '**/*.py' or 'src/**/test_*.ts'. "
    "Results are sorted by modification time, newest first. "
    "ALWAYS use this instead of run_bash with find/ls."
), {
    "type": "object",
    "properties": {"pattern": {"type": "string"}},
    "required": ["pattern"],
})
def glob_files(pattern: str) -> str:
    _SKIP = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", "dist", "build"}
    try:
        root = project_root()
        matches = []
        for p in root.glob(pattern.strip().strip("'\"")):
            if any(part in _SKIP for part in p.parts):
                continue
            if p.is_file():
                matches.append(p)
            if len(matches) >= 500:
                break
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = [str(p.relative_to(root)) for p in matches[:100]]
        if len(matches) > 100:
            out.append(f"… {len(matches) - 100} more files — narrow the pattern")
        return "\n".join(out) or "No files match."
    except Exception as e:
        return f"ERROR: {e}"


@_register("run_bash", (
    "Run a shell command and return stdout/stderr. Default timeout 120s — raise it for "
    "installs and builds (npm/pip installs routinely take minutes). "
    "For anything that does not exit on its own — dev servers, watchers, simulators "
    "(npm start, react-native start, run-ios) — set background=true, or it will time out."
), {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "timeout": {"type": "integer", "default": 120, "description": "Seconds. Max 600."},
        "background": {"type": "boolean", "description": "Run detached, log to a file, return immediately. Use for long-lived processes."},
    },
    "required": ["command"],
})
def run_bash(command: str, timeout: int = 120, background: bool = False) -> str:
    timeout = max(1, min(int(timeout), 600))

    if background:
        logs = project_root() / ".nyx" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / f"{abs(hash(command)) % 10**8}.log"
        with open(log, "w") as fh:
            proc = subprocess.Popen(
                command, shell=True, stdout=fh, stderr=subprocess.STDOUT,
                cwd=str(project_root()), start_new_session=True,
            )
        return (f"Started in background (pid {proc.pid}). Output → {log}\n"
                f"Check it with: run_bash(\"sleep 5; tail -40 {log}\")")

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(project_root()),
        )
        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr.strip():
            parts.append(f"stderr: {result.stderr.strip()}")
        if result.returncode != 0:
            parts.append(f"exit code: {result.returncode}")
        out = "\n".join(parts) if parts else "(no output)"
        if len(out) > 10_000:
            # keep head + tail — failures usually show at the end of test output
            out = f"{out[:6000]}\n[… truncated {len(out) - 9000} chars …]\n{out[-3000:]}"
        return out
    except subprocess.TimeoutExpired:
        return (
            f"TIMED OUT after {timeout}s. This is NOT a failure — the command did not "
            f"finish in time, and may have partly completed.\n"
            f"Do NOT conclude the tool, package, or dependency is broken, and do NOT "
            f"redesign around it (no mocks, stubs, or hand-rolled replacements).\n"
            f"Either re-run with a larger timeout, or — if this command never exits on "
            f"its own (a server, watcher, or simulator) — re-run it with background=true."
        )
    except Exception as e:
        return f"ERROR: {e}"


@_register("fetch_url", "Fetch a URL and return its text content.", {
    "type": "object",
    "properties": {"url": {"type": "string"}},
    "required": ["url"],
})
def fetch_url(url: str, timeout: int = 15) -> str:
    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self._chunks: list[str] = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "footer", "head"):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "footer", "head"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip and data.strip():
                self._chunks.append(data.strip())

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 nyx/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parser = _Extractor()
        parser.feed(raw)
        text = "\n".join(parser._chunks)
        return text[:8000] + ("…[truncated]" if len(text) > 8000 else "")
    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} — {url}"
    except urllib.error.URLError as e:
        return f"ERROR: {e.reason}"
    except Exception as e:
        return f"ERROR: {e}"


# ── Memory tools ──────────────────────────────────────────────────────────────

@_register("save_decision", (
    "Record an architectural or technical decision in decisions.md in the project repo. "
    "Call this when the user commits to a direction — a technology choice, a pattern, a "
    "constraint, or an approach explicitly ruled out. Decisions are loaded into every "
    "future session, so write the rationale into the body: what was chosen, and what it rules out."
), {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body":  {"type": "string"},
    },
    "required": ["title", "body"],
})
def save_decision(title: str, body: str) -> str:
    # decisions.md rides in every prompt. A spec pasted in here bloats the context
    # of every future turn — on providers with no prompt cache, that alone can
    # grind a session to a halt.
    if len(body) > 1200:
        return (f"REJECTED: that body is {len(body)} chars — far too long for a decision. "
                "A decision is a few sentences: what was chosen, and what it rules out. "
                "If this is a spec, call submit_spec instead. Re-call save_decision with a "
                "short body, or drop it.")
    from nyx.lib.memory import append_decision
    path = append_decision(title, body)
    return f"Saved decision: {path}"


@_register("save_idea", "Park an idea in ideas.md in the project repo, for later consideration.", {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body":  {"type": "string"},
    },
    "required": ["title", "body"],
})
def save_idea(title: str, body: str) -> str:
    from nyx.lib.memory import append_idea
    path = append_idea(title, body)
    return f"Saved idea: {path}"


@_register("create_spec", "Create a spec file in specs/ in the project repo.", {
    "type": "object",
    "properties": {
        "slug":    {"type": "string", "description": "Filename slug, e.g. 'add-auth-flow'"},
        "content": {"type": "string"},
    },
    "required": ["slug", "content"],
})
def create_spec(slug: str, content: str) -> str:
    from nyx.lib.memory import write_spec
    path = write_spec(slug, content)
    return f"Spec saved: {path}"


@_register("submit_spec", (
    "Show the spec to the user and ask for approval before saving. "
    "Use this instead of create_spec. If the user rejects, return their feedback so you can revise."
), {
    "type": "object",
    "properties": {
        "slug":    {"type": "string", "description": "Filename slug, e.g. 'add-auth-flow'"},
        "content": {"type": "string"},
    },
    "required": ["slug", "content"],
})
def submit_spec(slug: str, content: str) -> str:
    from nyx.lib.format import console
    from nyx.lib.deps import check_spec
    from rich.markdown import Markdown
    from rich.panel import Panel

    # A spec that names a package which doesn't exist sends the implementer
    # confidently in a wrong direction. Check before the user is asked to approve.
    try:
        problems = check_spec(content)
    except Exception:
        problems = []
    if problems:
        console.print(Panel(
            "\n".join(f"[red]✗[/red] {p}" for p in problems),
            title="[red]this spec names packages that do not exist[/red]", border_style="red",
        ))

    console.print(Panel(Markdown(content), title=f"[dim]spec: {slug}[/dim]", border_style="green"))
    try:
        raw = console.input("  Save this spec? [dim]\\[y]es · \\[n]o — or type feedback[/dim] › ").strip()
    except (KeyboardInterrupt, EOFError):
        return "User cancelled."

    flagged = ("\n\nUNRESOLVED — this spec still names packages that do not exist:\n"
               + "\n".join(f"  - {p}" for p in problems)
               + "\nYou invented these. Find the real packages with check_package and fix the spec.") if problems else ""

    verdict = read_verdict(raw)
    if verdict is True:
        from nyx.lib.memory import write_spec
        path = write_spec(slug, content)
        return f"Spec saved: {path}{flagged}"
    if verdict is False:
        return f"User rejected the spec. Do not save it.{flagged}"
    return f"User rejected with feedback: {raw}. Revise the spec accordingly, then call submit_spec again.{flagged}"


@_register("save_practice", "Save a best practice or architectural pattern to the vault.", {
    "type": "object",
    "properties": {
        "pattern":  {"type": "string", "description": "Category, e.g. 'auth', 'api-design'"},
        "language": {"type": "string", "description": "Language/stack, e.g. 'python', 'typescript'"},
        "content":  {"type": "string"},
    },
    "required": ["pattern", "language", "content"],
})
def save_practice(pattern: str, language: str, content: str) -> str:
    from nyx.lib.memory import write_practice
    path = write_practice(pattern, language, content)
    return f"Practice saved: {path}"


@_register("search_memory", (
    "Search past sessions and personal notes in the vault. Project decisions, ideas and "
    "specs are already in your context — don't search for those."
), {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
})
def search_memory(query: str) -> str:
    from nyx.lib.memory import search_vault
    return search_vault(query)


@_register("check_package", (
    "Verify a package exists on npm or PyPI before you depend on it. "
    "Use this for ANY package you are not certain of — inventing a plausible-looking "
    "package name is a common and costly failure. Checking is cheap; a 404 during "
    "install after you have written code against it is not."
), {
    "type": "object",
    "properties": {
        "ecosystem": {"type": "string", "enum": ["npm", "pypi"]},
        "name":      {"type": "string", "description": "Exact package name, e.g. 'react-native-macos'"},
        "version":   {"type": "string", "description": "Optional version to confirm, e.g. '0.72.0'"},
    },
    "required": ["ecosystem", "name"],
})
def check_package(ecosystem: str, name: str, version: str = "") -> str:
    from nyx.lib.deps import check_one
    problem = check_one(ecosystem, name, version)
    return problem or f"OK — {name}{'@' + version if version else ''} exists on {ecosystem}."


@_register("list_topics", (
    "List every topic and note title in the user's Obsidian vault, with tags. "
    "Use this first when they ask what they know or have written about something — "
    "it's the index. Then read_note the ones that look relevant."
), {"type": "object", "properties": {}})
def list_topics() -> str:
    from nyx.lib.notes import list_topics as _list
    return _list()


@_register("read_note", "Read one of the user's notes in full, by topic and title (as shown by list_topics).", {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "e.g. 'category theory/functors'"},
        "title": {"type": "string", "description": "The note title, as listed by list_topics."},
    },
    "required": ["topic", "title"],
})
def read_note(topic: str, title: str) -> str:
    from nyx.lib.notes import read_note as _read
    return _read(topic, title) or f"No note at {topic}/{title}."


@_register("write_architecture", (
    "Write an architecture doc for a client feature or server service into architecture/ in the project repo. "
    "Content should include a mermaid diagram."
), {
    "type": "object",
    "properties": {
        "area":    {"type": "string", "enum": ["client", "server"]},
        "name":    {"type": "string", "description": "Feature or service slug, e.g. 'auth', 'payment-service'"},
        "content": {"type": "string"},
    },
    "required": ["area", "name", "content"],
})
def write_architecture(area: str, name: str, content: str) -> str:
    from nyx.lib.memory import write_architecture as _write
    path = _write(area, name, content)
    return f"Architecture doc saved: {path}"


@_register("ask_user", (
    "Ask the user up to 3 clarifying questions before writing the spec. "
    "Each question has a short label and up to 4 options. "
    "The user picks an option by number or types a free-text answer."
), {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options":  {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                },
                "required": ["question", "options"],
            },
        },
    },
    "required": ["questions"],
})
def ask_user(questions: list) -> str:
    from nyx.lib.format import console
    answers = []
    for q in questions:
        console.print(f"\n  [bold]{q['question']}[/bold]")
        opts = q.get("options", [])
        for i, opt in enumerate(opts, 1):
            console.print(f"    [dim]{i}[/dim]  {opt}")
        try:
            raw = console.input("  › ").strip()
        except (KeyboardInterrupt, EOFError):
            raw = ""
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            answer = opts[int(raw) - 1]
        else:
            answer = raw or "(no answer)"
        answers.append(f"Q: {q['question']}\nA: {answer}")
    return "\n\n".join(answers)


# ── Export ────────────────────────────────────────────────────────────────────

def to_langchain_tools(names: list[str] | None = None) -> list[dict]:
    """Return OpenAI-format tool defs for LangChain .bind_tools()."""
    items = list(_REGISTRY.items()) if names is None else [
        (n, _REGISTRY[n]) for n in names if n in _REGISTRY
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": k,
                "description": v["description"],
                "parameters": v["parameters"],
            },
        }
        for k, v in items
    ]


# Required params where an empty string is legitimate (e.g. deleting text).
_EMPTY_OK = {"new_str"}


def missing_required(name: str, args: dict) -> list[str]:
    """Required parameters absent or empty in a tool call — lets callers bounce
    malformed calls before doing any UI work."""
    entry = _REGISTRY.get(name)
    if not entry:
        return []
    missing = []
    for k in entry["parameters"].get("required", []):
        v = args.get(k)
        if v is None or (isinstance(v, str) and not v.strip() and k not in _EMPTY_OK):
            missing.append(k)
    return missing


def execute_tool(name: str, args: dict) -> str:
    entry = _REGISTRY.get(name)
    if not entry:
        return f"ERROR: unknown tool '{name}'"
    params = entry["parameters"]
    allowed = params.get("properties", {})
    filtered = {k: v for k, v in args.items() if k in allowed}
    missing = missing_required(name, filtered)
    if missing:
        return (f"ERROR: malformed call to {name} — missing required parameter(s): "
                f"{', '.join(missing)}. Re-emit the call with clean, complete arguments.")
    try:
        return entry["fn"](**filtered)
    except Exception as e:
        return f"ERROR: {e}"
