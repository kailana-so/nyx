from __future__ import annotations
import difflib
import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from nyx.lib.config import (
    get_favourites, get_model, get_surfaces, get_tier, project_name,
    project_root, set_model, set_surface,
)
from nyx.lib.format import console
from nyx.lib.llm import MODELS, TIERS, get_caps, get_model as make_model, model_id, make_ai_message, make_text_tool_result, make_tool_message, quiet_turn, stream_turn
from nyx.lib import deps, notes
from nyx.lib.memory import (
    pending_dir, queue_proposals, read_architecture_index, read_decisions,
    read_episodic, read_ideas, read_practices, read_project_context,
    take_proposals, write_episodic, write_practice,
)
from nyx.lib.chat_input import pick_from_list
from nyx.lib.tools import execute_tool, read_verdict, to_langchain_tools

# ── Prompts ───────────────────────────────────────────────────────────────────

_P = Path(__file__).parent / "agents"

_BASE_PROMPT = (_P / "nyx.md").read_text()
_MODE_PROMPTS = {m: (_P / f"{m}.md").read_text()
                 for m in ("plan", "code", "test", "validate-plan", "visualise", "code-validator")}

# ── Tools ─────────────────────────────────────────────────────────────────────

_WRITE_TOOLS = {"write_file", "patch_file", "write_architecture"}
# Interactive tools prompt the user — never run them inside the parallel pool.
_INTERACTIVE_TOOLS = _WRITE_TOOLS | {"submit_spec", "ask_user"}

_BASE_TOOL_NAMES = [
    "read_file", "list_dir", "grep", "glob", "fetch_url", "run_bash",
    "write_file", "patch_file",
    "save_decision", "save_idea", "submit_spec", "search_memory",
    "save_practice", "write_architecture",
    "list_topics", "read_note", "check_package",
]

_BASE_TOOLS = to_langchain_tools(_BASE_TOOL_NAMES)
_PLAN_TOOLS = to_langchain_tools(
    [n for n in _BASE_TOOL_NAMES if n not in _WRITE_TOOLS] + ["ask_user"]
)


def _tools_for(mode: str) -> list[dict]:
    if mode == "learn":
        return []
    if mode in _READONLY_MODES:
        return _PLAN_TOOLS
    return _BASE_TOOLS


# ── Session ───────────────────────────────────────────────────────────────────

_MODES = ("chat", "plan", "code", "test", "validate-plan", "visualise", "code-validator")
_MODE_COLORS = {"chat": "36", "plan": "32", "code": "33", "test": "35", "learn": "35",
                "validate-plan": "32", "visualise": "34", "code-validator": "31"}
_AUTO_MODES = {"code", "test"}  # edits auto-approved (still previewed)
# Review modes never write — they get the read-only toolset and blocked edits.
_READONLY_MODES = {"plan", "validate-plan", "code-validator"}
# Modes that build against or review the project's structure — they get the
# architecture index. chat/visualise/learn don't need the structural contract.
_ARCH_MODES = {"plan", "code", "test", "validate-plan", "code-validator"}

# Which tier each surface runs on. Unlisted surfaces fall back to the session's
# own tier, so this is a set of deliberate exceptions, not a table to maintain.
_SURFACE_TIER = {
    "plan": "thinker", "validate-plan": "thinker", "code-validator": "thinker",
    "compact": "worker", "learn": "worker", "practice": "worker", "visualise": "worker",
}

# Bedrock models aren't in OpenRouter's catalogue, so the live list can't know
# about them. Kept as rows so the menu shows everything reachable, not just
# everything routed.
_LOCAL_MODELS = [("devstral", "mistral.devstral-2-123b"),
                 ("deepseek", "deepseek.v3.2"),
                 ("qwen235b", "qwen.qwen3-235b-a22b-2507-v1:0")]


_AXES = ("coding", "thinking", "agentic")

# What each surface is actually doing, so the list sorts by the score that
# matters for the thing you're assigning rather than by one fixed column.
_SURFACE_AXIS = {
    "code": "coding", "test": "coding",
    "plan": "thinking", "validate-plan": "thinking", "code-validator": "thinking",
    "chat": "agentic", "visualise": "coding", "learn": "thinking",
}


def _model_rows(target: str = "session", filter_text: str = "") -> list[tuple[str, str]]:
    """(provider, model id), best first for whatever `target` does. One row per
    model — a model good at several jobs is one row with several scores, not the
    same name repeated under three headings."""
    from nyx.lib.llm import catalogue
    live = catalogue()
    axis = _SURFACE_AXIS.get(target, "coding")
    favourites = get_favourites()

    ranked = sorted(live, key=lambda m: (m["id"] not in favourites, -m.get(axis, -1.0)))
    rows = [("openrouter", m["id"]) for m in ranked] + _LOCAL_MODELS
    if filter_text:
        rows = [r for r in rows if filter_text.lower() in r[1].lower()]
    return rows


def _routing_line(s: Session) -> str:
    """What will actually run, grouped by model. Naming a provider and a tier
    told you neither which model you get nor that modes can differ."""
    by_model: dict[str, list[str]] = {}
    for surface in ("chat", "code", "test", "plan", "compact"):
        by_model.setdefault(model_id(s.provider_for(surface), s.tier_for(surface)), []).append(surface)
    if len(by_model) == 1:
        return f"all modes → {next(iter(by_model))}"
    return " · ".join(f"{'/'.join(surfaces)} → {model}" for model, surfaces in by_model.items())


def _model_lines(rows: list[tuple[str, str]], target: str) -> tuple[str, list[str]]:
    """(header, one plain-text line per row) for the arrow-key picker. Plain
    because the picker styles the selected row itself."""
    from nyx.lib.llm import catalogue, get_caps
    from nyx.lib.usage import _PRICES
    priced = {m["id"]: m for m in catalogue()}
    favourites = set(get_favourites())
    current = {v: k for k, v in get_surfaces().items()}
    axis = _SURFACE_AXIS.get(target, "coding")
    labels = {"coding": "CODE", "thinking": "THINK", "agentic": "TOOLS"}

    # Only the column being sorted on earns a slot: two thirds of the catalogue
    # is unscored, so three score columns were mostly dashes crowding out the
    # description, which is the only thing those rows can tell you.
    fixed = 2 + 38 + 7 + 16 + 8 + 10
    room = max(24, console.width - fixed - 4)
    header = (f"  {'MODEL':<38}{labels[axis] + '▾':>7}{'$IN/$OUT per 1M':>16}{'CONTEXT':>8}  "
              f"{'CACHE':<10}WHAT IT IS")

    lines = []
    for provider, mid in rows:
        info = priced.get(mid)
        rate = (info["in"], info["out"]) if info else _PRICES.get(mid)
        # OpenRouter prices routers at -1 — a sentinel for "depends which model
        # this routes to", not a rate.
        if not rate:
            price = "—"
        elif min(rate) < 0:
            price = "varies"
        else:
            price = f"${rate[0]:g}/${rate[1]:g}"
        ctx = f"{info['context'] // 1000}k" if info and info["context"] else "—"
        score = f"{info[axis]:.1f}" if info and axis in info else "—"
        blurb = ((info or {}).get("blurb") or MODELS.get(provider, {}).get("blurb", ""))[:room]
        star = "★" if mid in favourites else " "
        used = f" · {current[mid]}" if mid in current else ""
        name = mid if len(mid) <= 38 else mid[:37] + "…"
        lines.append(f"{star} {name:<38}{score:>7}{price:>16}{ctx:>8}  "
                     f"{get_caps(provider, mid)['cache']:<10}{blurb}{used}")
    return header, lines


def _model_menu(s: Session, rows: list[tuple[str, str]], target: str) -> str:
    """Numbered fallback for when there's no terminal to run the picker in.
    Same rows, same columns — one place composes a row, so the two views can't
    drift into disagreeing about what a model costs."""
    header, lines = _model_lines(rows, target)
    head = (f"assign a model to [bold]{target}[/bold]" if target != "session"
            else "[bold]session model[/bold]  [dim]— surfaces you've assigned keep theirs[/dim]")
    out = [f"  {head}", f"  [dim]{header}[/dim]"]
    out += [f"  [dim]{i:>3}[/dim] [dim]{escape(line)}[/dim]" for i, line in enumerate(lines, 1)]
    out.append("\n  [dim]SCORE = artificialanalysis.ai index for the sorted column[/dim]")
    return "\n".join(out)


@dataclass
class Session:
    provider: str
    tier: str
    mode: str = "chat"
    auto_approve: bool = False
    history: list[BaseMessage] = field(default_factory=list)
    learn_history: list[BaseMessage] = field(default_factory=list)
    _models: dict = field(default_factory=dict)

    def tier_for(self, surface: str) -> str:
        """What a surface runs on, most specific first: a model you assigned to
        this surface, then a model pinned for the session, then the tier map."""
        if (assigned := get_surfaces().get(surface)):
            return assigned
        if self.tier not in TIERS:
            return self.tier
        return _SURFACE_TIER.get(surface, self.tier)

    def provider_for(self, surface: str) -> str:
        """A surface can be assigned a model from another provider — a Bedrock
        id while the session runs on OpenRouter. The model decides its provider,
        not the session, or the id would be sent to the wrong endpoint."""
        tier = self.tier_for(surface)
        return next((p for p, m in _LOCAL_MODELS if m == tier),
                    "openrouter" if "/" in tier else self.provider)

    def model_for(self, surface: str):
        """The model for a surface, built once and reused. Switching modes
        mid-session must not pay to reconstruct a client each turn."""
        key = (self.provider_for(surface), self.tier_for(surface))
        if key not in self._models:
            self._models[key] = make_model(*key)
        return self._models[key]

    def side_model(self):
        """Unattended side tasks — compact, notes, summaries."""
        return self.model_for("compact")


# ── System prompt ─────────────────────────────────────────────────────────────

def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True,
                           cwd=str(project_root()), timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _repo_snapshot() -> str:
    """Branch, working-tree status, and file list — so the model doesn't spend
    tool rounds rediscovering the repo every session."""
    status = _git("status", "--short", "--branch")
    files = _git("ls-files", "--others", "--exclude-standard", "--cached")
    if not status and not files:
        return ""
    file_lines = files.splitlines()
    if len(file_lines) > 200:
        file_lines = file_lines[:200] + [f"… {len(file_lines) - 200} more files"]
    parts = ["\n## Repository snapshot\n"]
    if status:
        parts.append(f"```\n$ git status --short --branch\n{status}\n```")
    if file_lines:
        parts.append("Files:\n```\n" + "\n".join(file_lines) + "\n```")
    return "\n".join(parts)


_EXT_LANGS = {".py": "python", ".ts": "typescript", ".tsx": "typescript",
              ".js": "javascript", ".jsx": "javascript", ".go": "go", ".rs": "rust"}


def _detect_languages() -> list[str]:
    files = _git("ls-files", "--others", "--exclude-standard", "--cached").splitlines()
    return sorted({_EXT_LANGS[s] for f in files if (s := Path(f).suffix) in _EXT_LANGS})


def _build_system(mode: str) -> tuple[str, str]:
    """(stable, volatile) halves of the system prompt. Stable comes first so
    provider prefix caches survive across rounds and turns; anything that can
    change between turns (git state, episodic memory) goes in volatile."""
    stable = [_BASE_PROMPT]
    if mode in _MODE_PROMPTS:
        stable.append(f"\n## Active mode: /{mode}\n\n{_MODE_PROMPTS[mode]}")
    if mode in ("code", "test"):
        practices = read_practices(_detect_languages() + ["general"])
        if practices:
            stable.append(f"\n## Your saved practices — follow these\n\n{practices}")
    ctx = read_project_context()
    if ctx:
        stable.append(f"\n## Project context (nyx.md)\n\n{ctx}")
    decisions = read_decisions()
    if decisions:
        stable.append(
            "\n## Decisions already made on this project\n\n"
            "These are settled. Follow them, and don't re-propose anything they rule out — "
            "if you think one is wrong, say so explicitly rather than working around it.\n\n"
            f"{decisions}"
        )
    ideas = read_ideas()
    if ideas:
        stable.append(f"\n## Parked ideas\n\nRaise these only when the conversation touches them.\n\n{ideas}")
    if mode in _ARCH_MODES and (arch := read_architecture_index()):
        stable.append(
            "\n## Architecture docs\n\n"
            "This project's structure is documented here. Read the relevant doc with "
            "`read_file` before implementing, reviewing, or speccing in that area — "
            "conform to it, don't invent a parallel structure.\n\n"
            f"{arch}"
        )
    volatile = [f"\nCurrent mode: **{mode}**  Project: **{project_name()}**  Root: `{project_root()}`"]
    snapshot = _repo_snapshot()
    if snapshot:
        volatile.append(snapshot)
    mem = read_episodic(limit=2)
    if mem:
        volatile.append(f"\n## Recent session history\n\n{mem}")
    return "\n".join(stable), "\n" + "\n".join(volatile)


# ── Edit previews and approval ────────────────────────────────────────────────

def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else project_root() / p


def _print_diff(path: str, old: str, new: str) -> None:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=path, tofile=path, lineterm="",
    ))[2:]  # skip ---/+++ header
    if len(lines) > 120:
        lines = lines[:120] + [f"… {len(lines) - 120} more lines"]
    body = Text()
    for line in lines:
        style = ("green" if line.startswith("+") else
                 "red" if line.startswith("-") else
                 "cyan" if line.startswith("@@") else "dim")
        body.append(line + "\n", style=style)
    console.print(Panel(body, title=f"[dim]{escape(path)}[/dim]", border_style="dim"))


def _write_target(name: str, args: dict) -> str:
    if name == "write_architecture":
        return f"architecture/{args.get('area', '?')}/{args.get('name', '?')}.md"
    return args.get("path", "?")


def _preview_write(name: str, args: dict) -> None:
    path = _write_target(name, args)
    if name == "patch_file":
        _print_diff(path, args.get("old_str", ""), args.get("new_str", ""))
        return
    content = args.get("content", "")
    try:
        old = _resolve(path).read_text()
    except OSError:
        old = ""
    if old:
        _print_diff(path, old, content)
    else:
        ext = Path(path).suffix.lstrip(".")
        lang = {"py": "python", "ts": "typescript", "js": "javascript",
                "sh": "bash", "md": "markdown", "json": "json"}.get(ext, "text")
        console.print(Panel(
            Syntax(content[:2000], lang, theme="monokai", line_numbers=True),
            title=f"[dim]new file: {escape(path)}[/dim]", border_style="dim",
        ))


def _check_deps(name: str, args: dict) -> str | None:
    """Reject a manifest that declares packages which don't exist. The model can't
    tell an invented package name from a real one — the registry can."""
    if name not in ("write_file", "patch_file"):
        return None
    target = Path(_write_target(name, args))
    if target.name not in deps.MANIFEST_FILENAMES:
        return None
    try:
        if name == "patch_file":
            old = _resolve(str(target)).read_text()
            text = old.replace(args.get("old_str", ""), args.get("new_str", ""), 1)
        else:
            text = args.get("content", "")
        problems = deps.check_manifest(target, text)
    except Exception:
        return None  # never let the checker itself break a write
    if not problems:
        return None
    console.print(Panel(
        "\n".join(f"[red]✗[/red] {p}" for p in problems),
        title="[red]hallucinated dependencies — write blocked[/red]", border_style="red",
    ))
    return (
        "BLOCKED — this manifest declares packages that do not exist:\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\n\nYou invented these. Do not guess package names or versions, and do not "
          "work around this by removing the feature or writing a mock. Find the real "
          "package name (check_package, or fetch_url the registry), then rewrite the manifest."
    )


def _gate_write(name: str, args: dict, s: Session) -> str | None:
    """Ask for approval BEFORE executing a write. Returns a rejection message
    for the model, or None if approved."""
    path = _write_target(name, args)
    if s.mode in _READONLY_MODES:
        return (f"Blocked: file edits are not allowed in {s.mode} mode. "
                "Present your findings instead — the user can switch to /code to act on them.")
    if (bad := _check_deps(name, args)) is not None:
        return bad
    _preview_write(name, args)
    if s.auto_approve or s.mode in _AUTO_MODES:
        return None
    while True:
        try:
            answer = console.input("  approve? [dim]\\[y]es · \\[a]lways · \\[n]o — or type feedback[/dim] › ").strip()
        except (KeyboardInterrupt, EOFError):
            return f"User rejected the edit to {path}."
        if not answer:
            continue  # a bare enter is a slip, not a rejection — ask again
        if answer.lower() in ("a", "always"):
            s.auto_approve = True
            console.print("  [dim]auto-approving edits for this session — /auto to turn off[/dim]")
            return None
        verdict = read_verdict(answer)
        if verdict is True:
            return None
        if verdict is False:
            return f"User rejected the edit to {path}. Ask what they want changed if it isn't clear."
        return f"User rejected the edit to {path} with feedback: {answer}"


# ── Tool execution ────────────────────────────────────────────────────────────

def _short_arg(args: dict) -> str:
    for key in ("path", "command", "url", "query", "title", "slug", "pattern", "name"):
        if key in args:
            v = str(args[key]).replace("\n", " ")
            if key == "path":
                try:
                    v = str(Path(v).resolve().relative_to(project_root()))
                except ValueError:
                    pass
            return escape(v if len(v) <= 80 else v[:77] + "…")
    return ""


def _result_summary(name: str, result: str) -> str | None:
    """One informative line (or a short block) per result — no raw dumps.
    Returns None when there is nothing worth showing the user."""
    if result.startswith("ERROR"):
        return f"[red]{escape(result[:200])}[/red]"
    if name == "run_bash":
        lines = result.splitlines()
        if not lines or result == "(no output)":
            return None
        tail = lines[-3:]
        prefix = f"[dim]… {len(lines) - 3} more lines[/dim]\n  " if len(lines) > 3 else ""
        return prefix + "\n  ".join(f"[dim]{escape(line[:120])}[/dim]" for line in tail)
    if name == "search_memory" and result != "No matches found.":
        lines = result.splitlines()
        return "\n  ".join(f"[dim]{escape(line[:120])}[/dim]" for line in lines[:2])
    if name.startswith("save_") or name in ("write_architecture", "create_spec"):
        return f"[green]{escape(result[:120])}[/green]"
    return None  # read_file / list_dir / fetch_url succeed silently


# Weak models loop on identical calls; serve exact repeats from cache with a
# nudge. run_bash is exempt (rerunning tests with the same command is normal).
# Only tools that can change project files invalidate the cache.
def _repeatable(name: str) -> bool:
    return name != "run_bash"

_CACHE_INVALIDATORS = {"run_bash", "write_file", "patch_file", "write_architecture"}


def _call_key(tc: dict) -> str:
    return f"{tc['name']}:{json.dumps(tc['args'], sort_keys=True, default=str)}"


def _run_tool_calls(tool_calls: list[dict], s: Session, seen: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Execute one round of tool calls. Returns (results, productive) —
    productive is False when every call was a repeat or malformed (a stalled model)."""
    from nyx.lib.tools import missing_required

    results: dict[str, str] = {}
    background, interactive, repeats = [], [], []
    for tc in tool_calls:
        missing = missing_required(tc["name"], tc["args"])
        if missing:
            # Bounce malformed calls straight back — no UI, no approval prompt.
            console.print(f"  [dim]✗ {tc['name']}  (malformed — missing {', '.join(missing)})[/dim]")
            results[tc["id"]] = execute_tool(tc["name"], tc["args"])
        elif _repeatable(tc["name"]) and _call_key(tc) in seen:
            repeats.append(tc)
        elif tc["name"] in _INTERACTIVE_TOOLS:
            interactive.append(tc)
        else:
            background.append(tc)

    for tc in repeats:
        console.print(f"  [dim]↻ {tc['name']}  {_short_arg(tc['args'])}  (repeat — served from cache)[/dim]")
        results[tc["id"]] = (
            "You already called this tool with these exact arguments this turn — "
            "do not repeat it. Use the result you have. It was:\n"
            + seen[_call_key(tc)][:1000]
        )

    for tc in background:
        console.print(f"  [dim]→ {tc['name']}  {_short_arg(tc['args'])}[/dim]")

    with ThreadPoolExecutor() as ex:
        futures = {ex.submit(execute_tool, tc["name"], tc["args"]): tc for tc in background}

        # Interactive tools run sequentially in this thread so prompts never overlap.
        for tc in interactive:
            name, args = tc["name"], tc["args"]
            if name in _WRITE_TOOLS:
                console.print(f"\n  [yellow]→ {name}[/yellow]  [dim]{_short_arg(args)}[/dim]")
                rejection = _gate_write(name, args, s)
                result = rejection if rejection is not None else execute_tool(name, args)
            else:
                result = execute_tool(name, args)
            results[tc["id"]] = result

        for fut in as_completed(futures):
            results[futures[fut]["id"]] = fut.result()

    for tc in background:
        summary = _result_summary(tc["name"], results.get(tc["id"], ""))
        if summary:
            console.print(f"  {summary}")

    # Update the repeat cache. A state-changing call means files may differ now —
    # drop everything and don't cache this batch (parallel order is indeterminate).
    if any(tc["name"] in _CACHE_INVALIDATORS for tc in background + interactive):
        seen.clear()
    else:
        for tc in background:
            if _repeatable(tc["name"]):
                seen[_call_key(tc)] = results.get(tc["id"], "")
    return results, bool(background or interactive)


# ── History pruning ───────────────────────────────────────────────────────────

# Old tool results are re-sent to the model every round after they land. Stub
# the big ones once they're stale — the model can re-run the tool if it must.
_PRUNE_THRESHOLD = 1000
_PRUNE_KEEP_ROUNDS = 2


def _is_tool_result(m: BaseMessage) -> bool:
    return isinstance(m, ToolMessage) or (
        isinstance(m, HumanMessage) and isinstance(m.content, str)
        and m.content.startswith("<tool_response>")
    )


def _prune_history(history: list[BaseMessage], seen: dict[str, str] | None = None,
                   keep_rounds: int = _PRUNE_KEEP_ROUNDS) -> None:
    """Stub out large tool results older than the last keep_rounds tool rounds
    (keep_rounds=0 prunes them all). Pruned entries are also dropped from the
    repeat-cache so a legitimate re-read isn't bounced."""
    rounds: list[list[int]] = []
    prev_was_tool = False
    for i, m in enumerate(history):
        if _is_tool_result(m):
            if not prev_was_tool:
                rounds.append([])
            rounds[-1].append(i)
            prev_was_tool = True
        else:
            prev_was_tool = False
    stale = rounds if keep_rounds == 0 else rounds[:-keep_rounds]
    for group in stale:
        for i in group:
            m = history[i]
            c = m.content
            if not isinstance(c, str) or len(c) <= _PRUNE_THRESHOLD:
                continue
            stub = (c[:200] + f"\n[… pruned {len(c) - 200} more chars — "
                              "re-run the tool if you need this again]")
            history[i] = m.model_copy(update={"content": stub})
            if seen:
                for key in [k for k, v in seen.items() if v == c]:
                    del seen[key]


# ── Turn loop ─────────────────────────────────────────────────────────────────

# Weaker models can loop on tools forever (e.g. re-reading the same file).
_MAX_TOOL_ROUNDS = 25
# Consecutive rounds of pure repeats/malformed calls before forcing an answer.
_MAX_STALLED_ROUNDS = 3


def _run_turn(s: Session, user_content=None) -> None:
    system = _build_system(s.mode)
    tools = _tools_for(s.mode)
    # No prompt cache to preserve → prune mid-turn; otherwise only at turn end,
    # so the cached prefix stays byte-identical across rounds.
    prune_in_turn = get_caps(s.provider_for(s.mode), s.tier_for(s.mode))["cache"] == "none"
    if user_content is not None:
        s.history.append(HumanMessage(content=user_content))

    text, tool_calls = stream_turn(s.model_for(s.mode), system, s.history, tools,
                                   provider=s.provider_for(s.mode), tier=s.tier_for(s.mode), surface=s.mode)
    s.history.append(make_ai_message(text, tool_calls))

    rounds = 0
    stalled = 0
    wrote_files = False       # a write_file/patch_file succeeded this turn
    verified = True           # a run_bash ran in a round AFTER the last write
    nudged = False
    seen: dict[str, str] = {}
    try:
        while tool_calls:
            rounds += 1
            results, productive = _run_tool_calls(tool_calls, s, seen)
            stalled = 0 if productive else stalled + 1
            round_wrote = any(
                tc["name"] in _WRITE_TOOLS and results.get(tc["id"], "").startswith("OK")
                for tc in tool_calls
            )
            # A backgrounded process (a server, a watcher) checks nothing — it must
            # not satisfy the post-write verification requirement.
            round_ran = any(tc["name"] == "run_bash" and not tc["args"].get("background")
                            for tc in tool_calls)
            if round_wrote:
                # bash in the SAME round runs concurrently with the write and may
                # predate it — only a later round counts as verification.
                wrote_files, verified = True, False
            elif round_ran:
                verified = True
            for tc in tool_calls:
                r = results.get(tc["id"], "")
                if tc.get("text_call"):
                    s.history.append(make_text_tool_result(tc["name"], r))
                else:
                    s.history.append(make_tool_message(r, tc["id"]))
            if prune_in_turn:
                _prune_history(s.history, seen)
            if rounds >= _MAX_TOOL_ROUNDS or stalled >= _MAX_STALLED_ROUNDS:
                why = "is repeating itself" if stalled >= _MAX_STALLED_ROUNDS else f"used {rounds} tool rounds"
                console.print(f"[yellow]Model {why} — asking it to wrap up.[/yellow]")
                s.history.append(HumanMessage(content=(
                    "Stop calling tools. You already have everything you need — "
                    "answer now with what you have."
                )))
                text, _ = stream_turn(s.model_for(s.mode), system, s.history, [],
                                      provider=s.provider_for(s.mode), tier=s.tier_for(s.mode), surface=s.mode)
                s.history.append(make_ai_message(text, []))
                return
            text, tool_calls = stream_turn(s.model_for(s.mode), system, s.history, tools,
                                           provider=s.provider_for(s.mode), tier=s.tier_for(s.mode), surface=s.mode)
            s.history.append(make_ai_message(text, tool_calls))
            if (not tool_calls and wrote_files and not verified and not nudged
                    and (s.mode in _AUTO_MODES or s.auto_approve)):
                nudged = True
                console.print("  [yellow]⚠ edits not verified — asking the model to run checks[/yellow]")
                s.history.append(HumanMessage(content=(
                    "You edited files but ran nothing afterwards. Run the project's "
                    "tests/checks on what you changed now; fix any failures; then give "
                    "your final answer."
                )))
                text, tool_calls = stream_turn(s.model_for(s.mode), system, s.history, tools,
                                               provider=s.provider_for(s.mode), tier=s.tier_for(s.mode), surface=s.mode)
                s.history.append(make_ai_message(text, tool_calls))
    finally:
        # Next turn starts lean on every provider (cross-turn caches are busted
        # by the volatile system suffix anyway).
        _prune_history(s.history, keep_rounds=0)


def _repair_history(s: Session) -> None:
    """After an interrupt, close any dangling tool calls so the API accepts the history."""
    if not s.history:
        return
    last = s.history[-1]
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        for tc in last.tool_calls:
            s.history.append(make_tool_message("Interrupted by user.", tc["id"]))


# ── Learn: notes → Obsidian ───────────────────────────────────────────────────
#
# The notes are the user's. Enhancement means typos fixed, gaps filled, structure
# added — never condensation. A model told to "format these notes" will summarise
# by default, so the no-shrink rule is enforced in code below, not just asked for.

_NOTES_SYSTEM = """You are polishing the user's study notes for their Obsidian vault.

Return JSON only — no prose, no fences:
{"title": "...", "topic": "...", "tags": ["..."], "body": "..."}

body — the notes, enhanced. Absolute rules:
- NEVER shorten, summarise, condense, or drop anything. Every point the user made
  must survive, in their order and their voice.
- Fix typos, spelling, grammar, and malformed notation.
- Expand where they were terse: finish half-thoughts, add the precise definition,
  supply a worked example, name the concept they were circling.
- Add structure: markdown headers, lists, tables, code blocks, LaTeX where apt.
- Mark anything you add that they did not say, and that they may want to verify,
  with a trailing " ^added". Do not mark ordinary typo fixes or formatting.
- The result must be LONGER than the input. If it is shorter, you have failed.

title — the note's subject, sentence case.
topic — the Obsidian folder. Nest with "/" (e.g. "category theory/functors").
tags — lowercase, kebab-case."""


def _notes_text(history: list[BaseMessage]) -> str:
    """Only the user's own turns. In learn mode that is all of them."""
    out = []
    for m in history:
        if not isinstance(m, HumanMessage):
            continue
        if isinstance(m.content, str):
            out.append(m.content)
        else:  # image blocks ride along with a text block
            out.extend(b["text"] for b in m.content if b.get("type") == "text")
    return "\n\n".join(out).strip()


def _write_notes(s: Session) -> None:
    raw = _notes_text(s.learn_history)
    if not raw:
        console.print("[yellow]No notes yet.[/yellow]")
        return

    console.print("[dim]Enhancing notes...[/dim]")
    system = _NOTES_SYSTEM
    reply = quiet_turn(s.model_for("learn"), system, raw, provider=s.provider_for("learn"), tier=s.tier_for("learn"), surface="learn")

    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", (reply or "").strip(), flags=re.MULTILINE).strip())
        title, topic = data["title"], data["topic"]
        body, tags = data["body"], data.get("tags", [])
    except (json.JSONDecodeError, KeyError, AttributeError):
        console.print("[yellow]Couldn't parse the enhanced notes — saving yours verbatim.[/yellow]")
        title, topic, tags, body = "Notes", "inbox", [], raw

    existing = notes.read_note(topic, title)
    if existing:
        console.print(f"[dim]Merging into existing note: {topic}/{title}[/dim]")
        merged = quiet_turn(
            s.model_for("learn"),
            "Weave the new notes into the existing note below. Keep EVERY line of the "
            "existing note — you may reorder and re-header, but never delete or shorten. "
            "Integrate the new material where it belongs. Return the merged markdown body "
            "only: no frontmatter, no title header, no fences.\n\n"
            f"## Existing note\n\n{existing}",
            body,
            provider=s.provider_for("learn"), tier=s.tier_for("learn"), surface="learn",
        )
        # The merge must not lose the note that was already there.
        body = merged if merged and len(merged) >= len(existing) else f"{existing}\n\n{body}"

    # The load-bearing guard: notes only ever grow.
    floor = len(raw) + len(existing)
    if len(body) < floor:
        console.print("[yellow]Enhancement came back shorter than your notes — keeping yours in full.[/yellow]")
        body = f"{existing}\n\n{raw}".strip() if existing else raw

    path = notes.write_note(topic, title, tags, body)
    saved_tags = notes.split_frontmatter(path.read_text())[0].get("tags", "").strip("[]")
    console.print(f"[green]Saved →[/green] {path}")
    if saved_tags:
        console.print(f"[dim]  tags: {saved_tags}[/dim]")
    s.learn_history = []


# ── Session capture ───────────────────────────────────────────────────────────
#
# A session produces two things. The summary is episodic — it exists so the
# project can be picked back up, and it's only ever read by recency. Decisions
# and ideas are durable: they go to the repo and ride in every future prompt.
# Reaching back past the last few sessions relies entirely on them, so capture
# can't depend on the model remembering to call save_decision mid-conversation.

_CAPTURE_SYSTEM = """Close out this coding session. Return JSON only — no prose, no fences.

{"summary": "...", "decisions": [{"title": "...", "body": "..."}], "ideas": [{"title": "...", "body": "..."}]}

summary — markdown, for picking this project back up later. Four short sections:
What we did / Where we left off / Open questions / Next. Be specific: names, paths,
error messages. Someone reading it cold in three weeks should know where to start.

decisions — only what the user actually committed to: a technology choice, a pattern,
a constraint, or an approach explicitly ruled out. The body must say what was chosen
AND what it rules out. An empty list is the common case and always acceptable.

ideas — raised but not acted on, worth revisiting. Empty list is fine.

Never invent. Never propose something already recorded below. Anything the user only
considered, or that you suggested and they didn't take up, is not a decision."""


def _capture(s: Session, transcript: str) -> tuple[str, list[dict]]:
    """(summary, proposals). Proposals are unsaved until the user reviews them."""
    known = ""
    if (d := read_decisions()):
        known += f"\n\n## Already recorded — decisions\n{d}"
    if (i := read_ideas()):
        known += f"\n\n## Already recorded — ideas\n{i}"

    raw = quiet_turn(s.side_model(), _CAPTURE_SYSTEM + known, transcript,
                     provider=s.provider_for("compact"), tier=s.tier_for("compact"), surface="compact")
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip())
    except (json.JSONDecodeError, AttributeError):
        # A summary we can't parse is still worth keeping — losing the session is
        # the one outcome we can't recover from.
        return (raw or ""), []

    proposals = [{"kind": k, **p} for k in ("decision", "idea")
                 for p in data.get(k + "s", []) if p.get("title") and p.get("body")]
    return data.get("summary", ""), proposals


def _review_proposals(proposals: list[dict]) -> None:
    """Nothing is written without a yes. Silence on exit means nothing is saved."""
    if not proposals:
        return
    console.print(f"\n[bold]{len(proposals)} thing(s) worth keeping from that session:[/bold]")
    for p in proposals:
        kind = p["kind"]
        console.print(Panel(
            Markdown(f"**{p['title']}**\n\n{p['body']}"),
            title=f"[dim]{kind}[/dim]", border_style="yellow",
        ))
        try:
            answer = console.input("  keep? [dim]\\[y]es · \\[n]o[/dim] › ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("[dim]skipped the rest[/dim]")
            return
        if answer.startswith("y"):
            tool = "save_decision" if kind == "decision" else "save_idea"
            console.print(f"[green]{execute_tool(tool, {'title': p['title'], 'body': p['body']})}[/green]")


def _transcript(history: list[BaseMessage]) -> str:
    rows = []
    for m in history:
        role = ("user" if isinstance(m, HumanMessage)
                else "assistant" if isinstance(m, AIMessage) else "tool")
        text = m.content if isinstance(m.content, str) else json.dumps(m.content, default=str)
        rows.append(f"{role}: {text[:1500]}")
    return "\n\n".join(rows)


def _compact(s: Session) -> None:
    if not s.history:
        console.print("[dim]Nothing to compact.[/dim]")
        return
    console.print("[dim]Compacting...[/dim]")
    summary, proposals = _capture(s, _transcript(s.history))
    if summary:
        path = write_episodic(summary)
        console.print(Panel(Markdown(summary), title=f"[dim]session saved → {path.name}[/dim]", border_style="dim"))
    _review_proposals(proposals)
    s.history = []


def _stash_transcript(s: Session) -> None:
    """Dump the transcript on exit — no model call, so quitting stays instant.
    A background thread summarises it on next launch."""
    if not s.history:
        return
    path = pending_dir() / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"transcript": _transcript(s.history)}))


def _summarise_pending(s: Session) -> None:
    """Daemon thread at launch. It can't prompt, so proposals are queued for the
    main loop to review. Failures leave the file for next time."""
    for path in sorted(pending_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text())
            transcript = (
                "\n\n".join(f"{r['role']}: {r['text']}" for r in data)  # pre-existing stash format
                if isinstance(data, list) else data["transcript"]
            )
            summary, proposals = _capture(s, transcript)
            if summary:
                write_episodic(summary)
            queue_proposals(proposals)
            path.unlink()
        except Exception:
            pass


# ── Slash command handler ─────────────────────────────────────────────────────

_KNOWN_CMDS = {
    "chat", "advise", "plan", "code", "test", "auto", "learn", "write",
    "compact", "practice", "update-architecture", "model", "ideas", "decide",
    "validate-plan", "visualise", "code-validator",
}

_MODE_BANNERS = {
    "chat": "edits need approval",
    "plan": "read-only — produces specs",
    "code": "edits auto-approved",
    "test": "TDD — writes failing tests, auto-approved",
    "validate-plan": "read-only — numbers, docs, scope, footprint",
    "visualise": "mermaid diagrams from code or descriptions",
    "code-validator": "read-only — craftsmanship review",
}


def _handle_slash(cmd: str, s: Session) -> None:
    parts = cmd.split()
    verb = parts[0].lstrip("/")
    rest = cmd[len(parts[0]):].strip()
    if verb == "advise":
        verb = "chat"

    if verb in _MODES:
        s.mode = verb
        console.print(f"[bold cyan]→ {verb} mode[/bold cyan]  [dim]{_MODE_BANNERS[verb]}[/dim]")
        if rest:
            _run_turn(s, rest)
        return

    if verb == "auto":
        s.auto_approve = not s.auto_approve
        state = "on — all edits auto-approved" if s.auto_approve else "off — edits need approval"
        console.print(f"[cyan]auto-approve {state}[/cyan]")
        return

    if verb == "learn":
        s.mode = "learn"
        console.print("[bold magenta]→ learn mode[/bold magenta]  [dim]type notes · /write to save · /chat to exit[/dim]")
        return

    if verb == "write" and s.mode == "learn":
        if not s.learn_history:
            console.print("[yellow]No notes yet.[/yellow]")
            return
        _write_notes(s)
        return

    if verb == "decide":
        title = rest
        if not title:
            try:
                title = console.input("  title › ").strip()
            except (KeyboardInterrupt, EOFError):
                return
        if not title:
            return
        try:
            body = console.input("  body  › ").strip()
        except (KeyboardInterrupt, EOFError):
            return
        result = execute_tool("save_decision", {"title": title, "body": body})
        console.print(f"[green]{result}[/green]")
        return

    if verb == "compact":
        _compact(s)
        return

    if verb == "practice":
        pattern = parts[1] if len(parts) > 1 else "general"
        language = parts[2] if len(parts) > 2 else "general"
        if not s.history:
            console.print("[yellow]Nothing to save yet.[/yellow]")
            return
        console.print("[dim]Saving practice...[/dim]")
        system = f"Summarise the best practice or architectural pattern discussed in this conversation as a concise markdown document. Pattern: {pattern}, Language/stack: {language}."
        content, _ = stream_turn(s.side_model(), system, s.history, [],
                                 provider=s.provider_for("practice"), tier=s.tier_for("practice"), surface="practice")
        if content:
            path = write_practice(pattern, language, content)
            console.print(f"[green]Saved →[/green] {path}")
        return

    if verb == "update-architecture":
        brief = (
            f"Read the codebase in {project_root()} and generate architecture documentation. "
            "For each significant client-side feature, call write_architecture(area='client', name=<feature>, content=<markdown with mermaid diagram>). "
            "For each significant server-side service, call write_architecture(area='server', name=<service>, content=<markdown with mermaid diagram>). "
            "Be thorough but concise."
        )
        _run_turn(s, brief)
        return

    if verb == "model":
        # /model              → pick the session model
        # /model code         → pick the model for code mode only
        # /model code reset   → back to tier routing for that surface
        # /model <text>       → same, filtered to matching ids
        target, _, filter_text = rest.partition(" ")
        if target and target not in _MODES and target != "learn":
            target, filter_text = "session", rest
        target = target or "session"

        if filter_text.strip() == "reset":
            set_surface(target, "")
            s._models.clear()
            console.print(f"[cyan]{target}[/cyan] [dim]back to tier routing[/dim]")
            return

        rows = _model_rows(target, filter_text.strip())
        if not rows:
            console.print(f"[yellow]No models match '{filter_text.strip()}'.[/yellow]")
            return
        header, lines = _model_lines(rows, target)
        title = (f"  assign a model to {target}" if target != "session"
                 else "  session model — surfaces you've assigned keep theirs")
        try:
            picked = pick_from_list(f"{title}\n{header}", lines)
            if picked is None:
                return  # cancelled — leave without redrawing anything
            provider, model = rows[picked]
        except Exception:
            # No terminal to draw on (piped input, a script) — fall back to the
            # numbered list so /model still works headless.
            console.print(_model_menu(s, rows, target), highlight=False)
            try:
                choice = console.input("  pick [# or model id]: ").strip()
            except (KeyboardInterrupt, EOFError):
                return
            if not choice:
                return
            if choice.isdigit() and 1 <= int(choice) <= len(rows):
                provider, model = rows[int(choice) - 1]
            else:
                model = choice
                provider = next((p for p, m in rows if m == model), "openrouter")

        if target == "session":
            s.provider, s.tier = provider, model
            set_model(provider, model)
        else:
            set_surface(target, model)
        s._models.clear()
        console.print(f"[cyan]{target}[/cyan] [dim]→ {model} · cache={get_caps(provider, model)['cache']}[/dim]")
        return

    if verb == "ideas":
        ideas = read_ideas()
        console.print(Markdown(ideas) if ideas else "[dim]No ideas saved yet.[/dim]")
        return

    console.print(f"[dim]Unknown command: /{verb}[/dim]")


# ── Main REPL ─────────────────────────────────────────────────────────────────

def run_repl(prompt: str | None = None) -> None:
    from nyx.lib.chat_input import chat_prompt, make_chat_session
    from nyx.lib.image import extract_images

    provider = get_model()
    tier = get_tier()
    s = Session(provider=provider, tier=tier)
    threading.Thread(target=_summarise_pending, args=(s,), daemon=True).start()

    console.print(Panel(
        f"[bold]nyx[/bold]  [dim]{project_name()}[/dim]\n[dim]{_routing_line(s)}[/dim]\n\n"
        "[dim]/plan  /code  /test  /chat — modes · /auto — toggle edit approval[/dim]\n"
        "[dim]/validate-plan  /visualise  /code-validator — review modes[/dim]\n"
        "[dim]/learn  /compact  /practice  /update-architecture  /model  /ideas[/dim]",
        border_style="cyan",
    ))

    if prompt:
        try:
            _run_turn(s, prompt)
        except Exception as e:
            console.print(f"\n[red]model error:[/red] {escape(str(e)[:300])}")
            raise SystemExit(1)
        return

    session = make_chat_session()
    while True:
        # Proposals from the last session land here once the background summariser
        # finishes — surfaced between turns, never mid-turn.
        _review_proposals(take_proposals())

        color = _MODE_COLORS.get(s.mode, "36")
        label = s.mode + ("·auto" if s.auto_approve and s.mode == "chat" else "")
        try:
            user_input = chat_prompt(
                session, f"\033[1;{color}m[{label}]\033[0m \033[1;36m›\033[0m "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input:
            continue

        first_token = user_input.split()[0].lstrip("/")
        if user_input.startswith("/") and first_token in _KNOWN_CMDS:
            try:
                _handle_slash(user_input, s)
            except KeyboardInterrupt:
                _repair_history(s)
                console.print("\n[dim]interrupted[/dim]")
            except Exception as e:
                _repair_history(s)
                console.print(f"\n[red]model error:[/red] {escape(str(e)[:300])}")
                console.print("[dim]history kept — try again or /model to switch provider[/dim]")
            continue

        cleaned, image_blocks = extract_images(user_input)
        content = image_blocks + [{"type": "text", "text": cleaned}] if image_blocks else cleaned

        try:
            if s.mode == "learn":
                # Silent capture. The model never speaks here, so nothing it says
                # can end up in the notes — what gets saved is only ever yours.
                s.learn_history.append(HumanMessage(content=content))
                console.print(f"[dim]  ▪ noted ({len(s.learn_history)}) — /write to save[/dim]")
            else:
                console.print()
                _run_turn(s, content)
        except KeyboardInterrupt:
            _repair_history(s)
            console.print("\n[dim]interrupted — history kept, keep typing[/dim]")
        except Exception as e:
            # Provider timeouts/5xx must not kill the session and its history.
            _repair_history(s)
            console.print(f"\n[red]model error:[/red] {escape(str(e)[:300])}")
            console.print("[dim]history kept — try again or /model to switch provider[/dim]")
        console.print()

    _stash_transcript(s)
    console.print("\n[dim]Goodbye.[/dim]")
