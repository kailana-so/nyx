from __future__ import annotations
import json
import threading
from pathlib import Path
from rich.markdown import Markdown
from rich.panel import Panel

from nyx.lib import bedrock
from nyx.lib.models import BEDROCK_ADVISOR, BEDROCK_COMPACT
from nyx.lib.tools import to_openai_tools as _to_openai_tools
from nyx.lib.format import (
    format_decisions, format_conversations, format_ideas,
    print_spec_saved, print_ideas_table, console,
)
from nyx.memory.embed import embed, embed_query
from nyx.memory.supabase import (
    get_decisions, get_ideas, get_recent_conversations, search_conversations,
    save_conversation, save_decision, create_spec, search_decisions,
    list_all_ideas, list_all_specs, save_idea, update_idea_status,
    search_learnings,
)

_ADVISOR_PROMPT = (Path(__file__).parent / "agents" / "advisor.md").read_text()

_TOOLS = [
    {
        "name": "create_spec",
        "description": "Save an approved spec to the database. Only call after explicit user approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "objective": {"type": "string", "description": "What this spec accomplishes, in one or two sentences."},
                "scope": {"type": "string", "description": "What's in scope: features, behaviour, surfaces."},
                "out_of_scope": {"type": "string", "description": "Features or work explicitly NOT part of this spec."},
                "criteria": {
                    "type": "array",
                    "description": "Concrete acceptance criteria. Each one must be objectively checkable.",
                    "items": {"type": "object", "properties": {"criterion": {"type": "string"}}, "required": ["criterion"]},
                },
                "files_to_touch": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative paths the coding agent is expected to create or modify. Be specific. Empty list means 'unknown — explore first'.",
                },
                "do_not_change": {
                    "type": "string",
                    "description": "Files, modules, or behaviour the coding agent must NOT modify. Use empty string if there are no constraints beyond the usual.",
                },
                "requires_thinking": {
                    "type": "boolean",
                    "description": "true for normal/non-trivial work. false ONLY for mechanical edits where reasoning would be wasted (rename, add a flag, copy-paste a known pattern). Default to true if unsure.",
                },
                "coding_notes": {"type": "string", "description": "Any extra context that helps the coding agent (links, gotchas, prior approach). Keep short."},
            },
            "required": ["title", "objective", "scope", "out_of_scope", "criteria", "files_to_touch", "do_not_change", "requires_thinking"],
        },
    },
    {
        "name": "save_decision",
        "description": "Persist an architectural or technical decision immediately when it's made.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["pattern", "standard", "rejected"]},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["category", "title", "body", "rationale"],
        },
    },
    {
        "name": "save_idea",
        "description": "Park an idea the user mentions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "search_memory",
        "description": "Semantic search across past decisions and conversations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_specs",
        "description": "List specs for the current project. Call before drafting a new spec to avoid duplicates. Filter by status if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done"],
                    "description": "Filter by status. Omit to return all.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "update_idea_status",
        "description": "Move an idea through its lifecycle. Use when the user promotes an idea to exploring or marks it done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "idea_id": {"type": "string", "description": "Full or short-prefix idea ID."},
                "status": {"type": "string", "enum": ["parked", "exploring", "done"]},
            },
            "required": ["idea_id", "status"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch a URL and return its text content. Useful for checking docs or a library's API before speccing work.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file for context (~/Documents boundary).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and subdirectories at a path (~/Documents boundary). Use this to explore unknown directory structure before reading files.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


_TOOLS_OPENAI  = _to_openai_tools(_TOOLS)
_TOOLS_BEDROCK = bedrock.to_bedrock_tools(_TOOLS)


def _handle_tool(name: str, inp: dict, profile: str, project: str) -> str:
    from nyx.tools.fs import read_file, list_dir

    if name == "create_spec":
        spec = create_spec(
            profile, project,
            inp["title"], inp["objective"], inp["scope"],
            inp.get("out_of_scope", ""), inp["criteria"],
            inp.get("coding_notes", ""),
            inp.get("files_to_touch", []),
            inp.get("do_not_change", ""),
            inp.get("requires_thinking", True),
        )
        print_spec_saved(spec)
        return f"Spec saved with ID {spec.id}"

    elif name == "save_decision":
        emb = embed(inp["title"] + " " + inp["body"])
        save_decision(profile, project, inp["category"], inp["title"],
                      inp["body"], inp["rationale"], emb)
        return f"Decision saved: {inp['title']}"

    elif name == "save_idea":
        emb = embed(inp["title"] + " " + inp["body"])
        idea_id = save_idea(profile, project, inp["title"], inp["body"],
                            inp.get("tags", []), emb)
        return f"Idea parked: {inp['title']} (id: {idea_id})"

    elif name == "search_memory":
        emb = embed_query(inp["query"])
        proj = inp.get("project", project)
        convos = search_conversations(emb, profile, 6, proj)
        decisions = search_decisions(emb, profile, 5, proj)
        learnings = search_learnings(emb, profile, 5)
        parts = []
        if decisions:
            parts.append("**Decisions:**\n" + "\n".join(f"- [{d.category}] {d.title}: {d.body[:200]}" for d in decisions))
        if convos:
            parts.append("**Conversations:**\n" + "\n".join(f"- {c.role}: {c.content[:300]}" for c in convos))
        if learnings:
            parts.append("**Learnings:**\n" + "\n".join(f"- [{ln.topic}/{ln.kind}] {ln.title}: {ln.body[:200]}" for ln in learnings))
        return "\n\n".join(parts) if parts else "No relevant memory found."

    elif name == "list_specs":
        specs = list_all_specs(profile, project)
        if status := inp.get("status"):
            specs = [s for s in specs if s.status == status]
        if not specs:
            return "No specs found."
        return "\n".join(f"[{s.status}] {s.id[:8]}… {s.title}" for s in specs)

    elif name == "update_idea_status":
        update_idea_status(inp["idea_id"], inp["status"])
        return f"Idea {inp['idea_id'][:8]}… → {inp['status']}"

    elif name == "fetch_url":
        from nyx.tools.fs import fetch_url
        return fetch_url(inp["url"])

    elif name == "read_file":
        return read_file(inp["path"])

    elif name == "list_dir":
        return list_dir(inp["path"])

    return f"Unknown tool: {name}"


def _last_spec_id(history: list[dict]) -> str | None:
    """Scan history backwards for the most recent create_spec tool result."""
    for m in reversed(history):
        if m.get("role") != "user":
            continue
        for block in m.get("content", []):
            if "toolResult" not in block:
                continue
            text = "".join(c.get("text", "") for c in block["toolResult"].get("content", []))
            if text.startswith("Spec saved with ID "):
                return text.removeprefix("Spec saved with ID ").strip()
    return None


def run_chat(profile: str, project: str) -> None:
    from nyx.lib.chat_input import make_chat_session, chat_prompt
    client = bedrock.make_client()
    history: list[dict] = []

    # Load static context once
    decisions = get_decisions(profile, project)
    parked = get_ideas(profile, "parked", project)
    exploring = get_ideas(profile, "exploring", project)
    ideas = parked + exploring

    static_context = _ADVISOR_PROMPT
    if decisions:
        static_context += "\n\n" + format_decisions(decisions)
    if ideas:
        static_context += "\n\n" + format_ideas(ideas)

    console.print(Panel(
        f"[bold]nyx advisor[/bold]  [dim]{profile}/{project}[/dim]\n"
        f"[dim]{len(decisions)} decisions · {len(ideas)} ideas · {BEDROCK_ADVISOR}[/dim]\n\n"
        f"[dim]/compact  /ideas  /run  ·  shift+enter newline · enter submits · ctrl-c exit[/dim]",
        border_style="cyan",
    ))

    session = make_chat_session()
    while True:
        try:
            user_input = chat_prompt(session, "\033[1;36m>\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue

        # Slash commands
        if user_input == "/compact":
            _compact(client, profile, project, history)
            history = []
            continue

        if user_input == "/ideas":
            ideas_all = list_all_ideas(profile, project)
            print_ideas_table(ideas_all)
            continue

        if user_input == "/run":
            spec_id = _last_spec_id(history)
            if not spec_id:
                console.print("[yellow]No spec created in this session — have the advisor draft one first.[/yellow]")
                continue
            from nyx.agent import run_agent
            console.print(f"\n[dim]Running spec {spec_id[:8]}…[/dim]\n")
            run_agent(spec_id, mode="code")
            continue

        # Build dynamic context
        recent = get_recent_conversations(profile, project, 8)
        try:
            q_emb = embed_query(user_input)
            semantic = search_conversations(q_emb, profile, 6, project)
            seen = {c.id for c in recent}
            all_convos = recent + [c for c in semantic if c.id not in seen]
        except Exception:
            all_convos = recent

        dynamic = format_conversations(all_convos) if all_convos else ""
        system_texts = [static_context] + ([dynamic] if dynamic else [])

        history.append({"role": "user", "content": [{"text": user_input}]})

        # Stream response
        full_response = ""
        console.print()

        try:
            text, tool_uses = bedrock.stream_turn(
                client, BEDROCK_ADVISOR, system_texts, history, _TOOLS_BEDROCK,
                surface="chat", project=project, profile=profile,
            )
            full_response = text

            assistant_content = ([{"text": text}] if text else []) + [
                {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
                for tu in tool_uses
            ]
            history.append({"role": "assistant", "content": assistant_content})

            # Handle tools — loop until the assistant stops calling them
            while tool_uses:
                tool_results = []
                for tu in tool_uses:
                    result = _handle_tool(tu["name"], tu["input"], profile, project)
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tu["id"],
                            "content": [{"text": result}],
                            "status": "success",
                        }
                    })
                history.append({"role": "user", "content": tool_results})

                text, tool_uses = bedrock.stream_turn(
                    client, BEDROCK_ADVISOR, system_texts, history, _TOOLS_BEDROCK,
                    max_tokens=2048, surface="chat", project=project, profile=profile,
                )
                follow_up = text
                assistant_content = ([{"text": text}] if text else []) + [
                    {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
                    for tu in tool_uses
                ]
                history.append({"role": "assistant", "content": assistant_content})
                full_response = follow_up or full_response

            # Save conversation async
            def _save():
                try:
                    u_emb = embed(user_input)
                    a_emb = embed(full_response[:2000])
                    save_conversation(profile, project, "user", user_input, u_emb)
                    save_conversation(profile, project, "assistant", full_response[:2000], a_emb)
                except Exception:
                    pass
            threading.Thread(target=_save, daemon=True).start()

        except KeyboardInterrupt:
            # Drop the pending user turn so history stays consistent
            if history and history[-1]["role"] == "user":
                history.pop()
            console.print("\n[dim]↩ interrupted[/dim]")

        console.print()


def _compact(client, profile: str, project: str, history: list[dict]) -> None:
    if not history:
        console.print("[dim]Nothing to compact.[/dim]")
        return
    console.print("[dim]Compacting...[/dim]")
    summary_prompt = (
        "Summarise this conversation for future recall. Include: decisions made, "
        "specs discussed, files examined, open questions, next steps. Be concise."
    )
    summary, _ = bedrock.invoke_turn(
        client, BEDROCK_COMPACT,
        system_texts=[summary_prompt],
        messages=history,
        max_tokens=800,
        surface="compact", project=project, profile=profile,
    )
    try:
        emb = embed(summary)
        save_conversation(profile, project, "assistant", f"[compact] {summary}", emb)
    except Exception:
        pass
    console.print(Panel(Markdown(summary), title="[dim]Compacted[/dim]", border_style="dim"))


def run_chat_local(profile: str, project: str, think: bool = False) -> None:
    """Advisor session running on local Qwen3 via Ollama, with full tool parity.

    Qwen3 reasoning mode is OFF by default (faster, less verbose). Pass think=True
    to enable — translates to a `/think` directive prepended to each user message.
    """
    from openai import OpenAI
    from nyx.lib.models import OLLAMA_BASE_URL, OLLAMA_MODEL
    from nyx.agent import _stream_response

    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")

    decisions = get_decisions(profile, project)
    parked = get_ideas(profile, "parked", project)
    exploring = get_ideas(profile, "exploring", project)
    ideas = parked + exploring

    # NB: Qwen3 honors /think and /no_think on user messages, not the system prompt.
    directive = "/think" if think else "/no_think"
    static_context = _ADVISOR_PROMPT
    if decisions:
        static_context += "\n\n" + format_decisions(decisions)
    if ideas:
        static_context += "\n\n" + format_ideas(ideas)

    mode = "think" if think else "no-think"
    console.print(Panel(
        f"[bold]nyx advisor[/bold]  [dim]{profile}/{project} · local · {mode}[/dim]\n"
        f"[dim]{len(decisions)} decisions · {len(ideas)} ideas · {OLLAMA_MODEL}[/dim]\n\n"
        f"[dim]/compact  /ideas  /run  ·  shift+enter newline · enter submits · ctrl-c exit[/dim]",
        border_style="magenta",
    ))

    messages: list[dict] = [{"role": "system", "content": static_context}]

    from nyx.lib.chat_input import make_chat_session, chat_prompt
    session = make_chat_session()
    while True:
        try:
            user_input = chat_prompt(session, "\033[1;35m>\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue

        if user_input == "/compact":
            messages = [{"role": "system", "content": static_context}]
            console.print("[dim]History cleared.[/dim]")
            continue

        if user_input == "/ideas":
            ideas_all = list_all_ideas(profile, project)
            print_ideas_table(ideas_all)
            continue

        if user_input == "/run":
            spec_id = None
            for m in reversed(messages):
                if m.get("role") == "tool" and m.get("name") == "create_spec":
                    text = m.get("content", "")
                    if text.startswith("Spec saved with ID "):
                        spec_id = text.removeprefix("Spec saved with ID ").strip()
                        break
            if not spec_id:
                console.print("[yellow]No spec created in this session — have the advisor draft one first.[/yellow]")
                continue
            from nyx.agent import run_agent
            console.print(f"\n[dim]Running spec {spec_id[:8]}…[/dim]\n")
            run_agent(spec_id, mode="code")
            continue

        messages.append({"role": "user", "content": f"{directive}\n\n{user_input}"})
        full_response = ""
        console.print()

        # Tool-use loop — keep going until model returns text only
        while True:
            response = client.chat.completions.create(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=_TOOLS_OPENAI,
                stream=True,
            )
            content, tool_calls = _stream_response(response)
            if content:
                full_response += content

            messages.append({
                "role": "assistant",
                "content": content or "",
                **({"tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                    for c in tool_calls
                ]} if tool_calls else {}),
            })

            if not tool_calls:
                break

            for call in tool_calls:
                console.print(f"\n[dim]→ {call['name']}({_fmt_call_args(call['args'])})[/dim]")
                result = _handle_tool(call["name"], call["args"], profile, project)
                if result and len(result) < 500:
                    console.print(f"[dim]{result}[/dim]")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": result,
                })

        def _save():
            try:
                u_emb = embed(user_input)
                a_emb = embed(full_response[:2000])
                save_conversation(profile, project, "user", user_input, u_emb)
                save_conversation(profile, project, "assistant", full_response[:2000], a_emb)
            except Exception:
                pass
        threading.Thread(target=_save, daemon=True).start()

        console.print()


def _fmt_call_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s!r}")
    return ", ".join(parts)
