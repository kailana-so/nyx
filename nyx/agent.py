from __future__ import annotations
import json
from pathlib import Path
from rich.panel import Panel
from rich.syntax import Syntax

from nyx.lib import bedrock
from nyx.lib.models import BEDROCK_ADVISOR
from nyx.lib.format import format_spec, format_decisions, console
from nyx.lib.project_config import get_project_root
from nyx.lib.session import get_profile
from nyx.memory.supabase import get_spec, update_spec_status, get_decisions
from nyx.tools.fs import TOOL_DEFINITIONS_BEDROCK, execute_tool

_CODING_PROMPT = (Path(__file__).parent / "agents" / "coding.md").read_text()
_TEST_PROMPT   = (Path(__file__).parent / "agents" / "test.md").read_text()
_PLAN_PROMPT   = (Path(__file__).parent / "agents" / "plan.md").read_text()

# Read-only tools available during the planning phase
_PLAN_TOOL_NAMES = {"read_file", "list_dir", "fetch_url", "run_bash"}
_PLAN_TOOLS = [t for t in TOOL_DEFINITIONS_BEDROCK if t["toolSpec"]["name"] in _PLAN_TOOL_NAMES] + [
    {"toolSpec": {
        "name": "save_decision",
        "description": "Persist an architectural or technical decision made during planning. Use when a concrete technical choice is confirmed — approach, pattern, library, constraint.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["pattern", "standard", "rejected"]},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["category", "title", "body", "rationale"],
        }},
    }},
]


def _implementation_loop(
    client,
    system_texts: list[str],
    messages: list[dict],
    spec_id: str | None = None,
    surface: str = "agent",
    project: str = "",
    profile: str = "",
) -> None:
    """Core coding loop — shared by run_agent (spec-backed) and run_plan (ad-hoc)."""
    nudges = 0
    MAX_NUDGES = 1

    try:
        while True:
            text, tool_uses = bedrock.stream_turn(
                client, BEDROCK_ADVISOR, system_texts, messages, TOOL_DEFINITIONS_BEDROCK,
                surface=surface, project=project, profile=profile,
            )

            if not tool_uses:
                if text:
                    messages.append({"role": "assistant", "content": [{"text": text}]})
                if nudges < MAX_NUDGES:
                    nudges += 1
                    console.print("\n[yellow]No tool call — nudging the model to continue or call `done`.[/yellow]")
                    messages.append({"role": "user", "content": [{"text": (
                        "You produced text but didn't call a tool. "
                        "If the task is complete, call `done` with a brief summary. "
                        "Otherwise continue with the next tool call."
                    )}]})
                    continue
                tail = "leaving spec as in_progress. Use [bold]nyx specs[/bold] to mark done or rerun." if spec_id else ""
                console.print(f"\n[dim]Agent stopped without calling `done` after a nudge. {tail}[/dim]")
                break

            nudges = 0

            assistant_content = ([{"text": text}] if text else []) + [
                {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
                for tu in tool_uses
            ]
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            finished = False
            for tu in tool_uses:
                name, args = tu["name"], tu["input"]

                if name == "done":
                    summary = args.get("summary", "")
                    console.print(Panel(f"[bold green]Done[/bold green]\n\n{summary}", border_style="green"))
                    if spec_id:
                        update_spec_status(spec_id, "done")
                    finished = True
                    break

                console.print(f"\n[dim]→ {name}({_fmt_args(args)})[/dim]")
                result = execute_tool(name, args)

                if name == "write_file" and "ERROR" not in result:
                    _print_written_file(args.get("path", ""), args.get("content", ""))
                elif name == "patch_file" and "ERROR" not in result:
                    console.print(f"[dim]{result}[/dim]")
                elif result and result != "(no output)":
                    console.print(f"[dim]{result[:500]}[/dim]")

                is_err = isinstance(result, str) and result.startswith("ERROR")
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tu["id"],
                        "content": [{"text": result}],
                        "status": "error" if is_err else "success",
                    }
                })

            if finished:
                break
            messages.append({"role": "user", "content": tool_results})

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        if spec_id:
            console.print("[dim]Spec left as in_progress — use [bold]nyx specs[/bold] to rerun or mark done.[/dim]")


def run_agent(spec_id: str, mode: str = "code") -> None:
    client = bedrock.make_client()
    system_prompt = _CODING_PROMPT if mode == "code" else _TEST_PROMPT

    spec = get_spec(spec_id)
    update_spec_status(spec_id, "in_progress")

    project_root = str(get_project_root(spec.project)) if spec.project else ""
    brief = (
        f"Here is your spec:\n\n{format_spec(spec)}\n\n"
        f"**Project:** {spec.project or '(unknown)'}\n"
        f"**Project root:** {project_root or '(unknown)'}\n\n"
        "All paths in the spec are relative to the project root above. "
        "Resolve them against that path; do not work outside it.\n\nBegin."
    )

    console.print(Panel(
        f"[bold]nyx {'coding' if mode == 'code' else 'test'} agent[/bold]\n"
        f"[dim]Spec: {spec.title}[/dim]\n"
        f"[dim]Project: {spec.project or '(unknown)'}  ·  Root: {project_root or '(unknown)'}[/dim]\n"
        f"[dim]Model: {BEDROCK_ADVISOR}[/dim]",
        border_style="yellow" if mode == "code" else "magenta",
    ))

    _implementation_loop(
        client,
        system_texts=[system_prompt],
        messages=[{"role": "user", "content": [{"text": brief}]}],
        spec_id=spec_id,
        surface=mode,
        project=spec.project or "",
        profile=get_profile(),
    )


def run_plan(profile: str, project: str) -> None:
    """Describe a task, model reads the code and plans, /build implements — no spec required."""
    from nyx.lib.chat_input import make_chat_session, chat_prompt

    client = bedrock.make_client()
    project_root = str(get_project_root(project)) if project else ""

    # Load decisions so the model respects existing architectural choices
    decisions = get_decisions(profile, project)
    system_texts = [_PLAN_PROMPT]
    if project_root:
        system_texts.append(f"Project root: {project_root}")
    if decisions:
        system_texts.append(format_decisions(decisions))

    console.print(Panel(
        f"[bold]nyx plan[/bold]  [dim]{profile}/{project}[/dim]\n"
        f"[dim]{len(decisions)} decisions · {BEDROCK_ADVISOR}[/dim]\n\n"
        f"[dim]Describe what to build. The model will read the code before proposing.\n"
        f"/build to implement  ·  ctrl-c to exit[/dim]",
        border_style="green",
    ))

    messages: list[dict] = []
    session = make_chat_session()

    while True:
        try:
            user_input = chat_prompt(session, "\033[1;32m>\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            return

        if not user_input:
            continue

        if user_input == "/build":
            if not messages:
                console.print("[yellow]Describe what you want to build first.[/yellow]")
                continue
            _start_implementation(client, system_texts, messages, project=project, profile=profile)
            return

        messages.append({"role": "user", "content": [{"text": user_input}]})
        console.print()

        text, tool_uses = bedrock.stream_turn(
            client, BEDROCK_ADVISOR, system_texts, messages, _PLAN_TOOLS,
            surface="plan", project=project, profile=profile,
        )

        assistant_content = ([{"text": text}] if text else []) + [
            {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
            for tu in tool_uses
        ]
        messages.append({"role": "assistant", "content": assistant_content})

        while tool_uses:
            tool_results = []
            for tu in tool_uses:
                console.print(f"\n[dim]→ {tu['name']}({_fmt_args(tu['input'])})[/dim]")
                result = _handle_plan_tool(tu["name"], tu["input"], profile, project)
                if result and result != "(no output)":
                    console.print(f"[dim]{result}[/dim]")
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tu["id"],
                        "content": [{"text": result}],
                        "status": "success",
                    }
                })
            messages.append({"role": "user", "content": tool_results})

            text, tool_uses = bedrock.stream_turn(
                client, BEDROCK_ADVISOR, system_texts, messages, _PLAN_TOOLS,
                surface="plan", project=project, profile=profile,
            )
            assistant_content = ([{"text": text}] if text else []) + [
                {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
                for tu in tool_uses
            ]
            messages.append({"role": "assistant", "content": assistant_content})

        console.print()


def _handle_plan_tool(name: str, inp: dict, profile: str, project: str) -> str:
    """Tool handler for the planning phase — fs tools + save_decision."""
    if name == "save_decision":
        from nyx.memory.embed import embed
        from nyx.memory.supabase import save_decision as _save_decision
        emb = embed(inp["title"] + " " + inp["body"])
        _save_decision(profile, project, inp["category"], inp["title"], inp["body"], inp["rationale"], emb)
        return f"Decision saved: {inp['title']}"
    return execute_tool(name, inp)


def _start_implementation(client, plan_system_texts: list[str], plan_messages: list[dict], project: str = "", profile: str = "") -> None:
    """Swap the system prompt to the coding agent and execute against the planning context."""
    import typer

    plan_text = ""
    for m in reversed(plan_messages):
        if m.get("role") == "assistant":
            texts = [c["text"] for c in m.get("content", []) if "text" in c]
            if texts:
                plan_text = "\n".join(texts)
                break
    if plan_text:
        console.print(Panel(plan_text[:2000], title="[bold]Plan[/bold]", border_style="green"))

    if not typer.confirm("\nImplement this?", default=True):
        console.print("[dim]Cancelled — continue planning or ctrl-c to exit.[/dim]")
        return

    console.print("\n[bold green]Implementing…[/bold green]\n")

    system_texts = [_CODING_PROMPT] + plan_system_texts[1:]  # drop _PLAN_PROMPT, keep rest

    messages = list(plan_messages) + [{
        "role": "user",
        "content": [{"text": (
            "The plan above is approved. Implement it now. "
            "Do not ask further questions — begin with the first file operation."
        )}],
    }]
    _implementation_loop(client, system_texts=system_texts, messages=messages,
                         surface="agent", project=project, profile=profile)


# ── Local (Ollama) backend helpers — kept for run_chat_local / run_learn_local ──

def _stream_response(response) -> tuple[str, list[dict]]:
    content = ""
    tool_calls_raw: dict[int, dict] = {}
    in_think = False

    DIM, RESET = "\033[2m", "\033[0m"

    for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue

        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if reasoning:
            print(f"{DIM}{reasoning}{RESET}", end="", flush=True)

        if delta.content:
            text = delta.content
            if "<think>" in text:
                in_think = True
                text = text.replace("<think>", "")
            if "</think>" in text:
                in_think = False
                text = text.replace("</think>", "")
                print(flush=True)
            if in_think:
                print(f"{DIM}{text}{RESET}", end="", flush=True)
            else:
                print(text, end="", flush=True)
                content += text

        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_raw:
                    tool_calls_raw[idx] = {"id": tc.id or "", "name": "", "args_str": ""}
                if tc.function:
                    if tc.function.name:
                        tool_calls_raw[idx]["name"] += tc.function.name
                    if tc.function.arguments:
                        tool_calls_raw[idx]["args_str"] += tc.function.arguments

    print()

    tool_calls = []
    for raw in sorted(tool_calls_raw.values(), key=lambda x: list(tool_calls_raw.values()).index(x)):
        try:
            args = json.loads(raw["args_str"]) if raw["args_str"] else {}
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({"id": raw["id"], "name": raw["name"], "args": args})

    return content, tool_calls


def _fmt_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        if k == "content":
            parts.append(f"content=<{len(str(v))} chars>")
        else:
            parts.append(f"{k}={repr(str(v)[:60])}")
    return ", ".join(parts)


def _print_written_file(path: str, content: str) -> None:
    ext = Path(path).suffix.lstrip(".")
    lang_map = {"py": "python", "ts": "typescript", "js": "javascript",
                "sh": "bash", "md": "markdown", "json": "json", "toml": "toml"}
    lang = lang_map.get(ext, "text")
    console.print(Panel(
        Syntax(content[:2000], lang, theme="monokai", line_numbers=True),
        title=f"[dim]wrote {path}[/dim]",
        border_style="dim",
    ))
