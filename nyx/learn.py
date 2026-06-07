from __future__ import annotations
import json
import re
from pathlib import Path
from rich.panel import Panel

from nyx.lib import bedrock
from nyx.lib.models import BEDROCK_ADVISOR, OLLAMA_BASE_URL, OLLAMA_MODEL
from nyx.lib.tools import to_openai_tools as _to_openai_tools
from nyx.tools.fs import TOOL_DEFINITIONS_BEDROCK as _FS_BEDROCK
from nyx.lib.format import (
    console, format_learnings, print_learning, print_learnings_table,
)
from nyx.lib.learn_config import (
    get_obsidian_root, set_obsidian_root, mark_obsidian_skipped,
    needs_first_run_prompt,
)
from nyx.lib.learn_export import export_learning, remove_learning_file
from nyx.memory.embed import embed, embed_query
from nyx.memory.supabase import (
    Learning,
    list_learnings, list_learning_topics,
    save_learning as _db_save_learning,
    update_learning as _db_update_learning,
    delete_learning as _db_delete_learning,
    get_learning, search_learnings,
)


_LEARN_PROMPT    = (Path(__file__).parent / "agents" / "learn.md").read_text()
_DISTILL_PROMPT  = (Path(__file__).parent / "agents" / "distill.md").read_text()
_RESEARCH_PROMPT = (Path(__file__).parent / "agents" / "research.md").read_text()


# Tools available during chat. Writes only fire on explicit user request — the
# system prompt tells the advisor not to propose saves on its own.
_CHAT_TOOLS = [
    {
        "name": "search_learnings",
        "description": "Semantic search across the user's saved learnings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "topic": {"type": "string"},
                "kind": {"type": "string", "enum": ["concept", "pattern", "gotcha", "exercise", "definition"]},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file the user has saved locally (~/Documents boundary).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List a directory (~/Documents boundary).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch a URL and return its text content. Useful for looking up references, docs, or source material during a learning session.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
]


# Bedrock toolSpec versions — derived so definitions stay in one place
_CHAT_TOOLS_BEDROCK = bedrock.to_bedrock_tools(_CHAT_TOOLS)

# Distillation tool — the model calls this once with the proposed list
_PROPOSE_LEARNINGS_TOOL = {
    "name": "propose_learnings",
    "description": "Output the polished learnings extracted from the conversation. The user reviews each before anything is persisted. Empty list is fine if nothing converged.",
    "input_schema": {
        "type": "object",
        "properties": {
            "learnings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "topic": {"type": "string"},
                        "kind": {"type": "string", "enum": ["concept", "pattern", "gotcha", "exercise", "definition"]},
                        "language": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "source": {"type": "string"},
                        "rationale": {"type": "string", "description": "One sentence on why this is worth keeping. Shown to the user during review."},
                        "update_id": {"type": "string", "description": "If set, enrich this existing learning instead of creating a new one. Set to the id of the existing entry."},
                    },
                    "required": ["title", "body", "topic", "kind"],
                },
            },
        },
        "required": ["learnings"],
    },
}


_PROPOSE_LEARNINGS_TOOL_BEDROCK = bedrock.to_bedrock_tools([_PROPOSE_LEARNINGS_TOOL])

# save_learning is available to the researcher (saves as it discovers) but not to the learn chat advisor
_SAVE_LEARNING_BEDROCK = bedrock.to_bedrock_tools([{
    "name": "save_learning",
    "description": "Persist a new learning to the user's bank. Call only for findings worth keeping long-term.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "topic": {"type": "string"},
            "kind": {"type": "string", "enum": ["concept", "pattern", "gotcha", "exercise", "definition"]},
            "language": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "string"},
        },
        "required": ["title", "body", "topic", "kind"],
    },
}])

# Researcher tools — learn subset (fetch, read, search, save) + run_bash for codebase exploration
_RESEARCH_TOOL_NAMES = {"fetch_url", "read_file", "list_dir", "search_learnings"}
_RESEARCH_TOOLS = (
    [t for t in _CHAT_TOOLS_BEDROCK if t["toolSpec"]["name"] in _RESEARCH_TOOL_NAMES]
    + _SAVE_LEARNING_BEDROCK
    + [t for t in _FS_BEDROCK if t["toolSpec"]["name"] == "run_bash"]
)


def _format_learning_short(learning: Learning) -> str:
    head = f"[{learning.kind}] {learning.title} — {learning.topic}"
    if learning.language:
        head += f" ({learning.language})"
    return f"{head}\n  {learning.body[:240]}{'…' if len(learning.body) > 240 else ''}\n  id: {learning.id}"


def _handle_chat_tool(name: str, inp: dict, profile: str) -> str:
    from nyx.tools.fs import read_file, list_dir

    if name == "search_learnings":
        emb = embed_query(inp["query"])
        results = search_learnings(emb, profile, limit=8,
                                   topic=inp.get("topic"), kind=inp.get("kind"))
        if not results:
            return "No matching learnings."
        return "\n\n".join(_format_learning_short(item) for item in results)

    if name == "save_learning":
        try:
            saved = save_learning_with_export(
                profile,
                title=inp["title"], body=inp["body"],
                topic=inp["topic"], kind=inp["kind"],
                tags=inp.get("tags") or [],
                language=inp.get("language"),
                source=inp.get("source"),
            )
            return f"Saved learning {saved.id}: {saved.title} (topic: {saved.topic})"
        except Exception as e:
            return f"Save failed ({type(e).__name__}): {e}"

    if name == "update_learning":
        try:
            updated = update_learning_with_export(inp["id"], **{
                k: v for k, v in inp.items() if k != "id"
            })
            return f"Updated learning {updated.id}: {updated.title}"
        except Exception as e:
            return f"Update failed ({type(e).__name__}): {e}"

    if name == "delete_learning":
        try:
            removed = delete_learning_with_export(inp["id"])
            return f"Deleted learning {removed.id}: {removed.title}"
        except Exception as e:
            return f"Delete failed ({type(e).__name__}): {e}"

    if name == "read_file":
        return read_file(inp["path"])
    if name == "list_dir":
        return list_dir(inp["path"])
    if name == "fetch_url":
        from nyx.tools.fs import fetch_url
        return fetch_url(inp["url"])
    if name == "run_bash":
        from nyx.tools.fs import run_bash
        return run_bash(inp["command"], inp.get("timeout", 30))
    return f"Unknown tool: {name}"


# ── First-run setup ────────────────────────────────────────────────────────────


def prompt_obsidian_setup_if_needed() -> None:
    """Once-only: ask the user where to export MD files. Idempotent — does nothing if already set or skipped."""
    import typer
    if not needs_first_run_prompt():
        return

    console.print(Panel(
        "[bold]nyx learn — first-run setup[/bold]\n\n"
        "Saved learnings can be exported as clean Markdown files into an "
        "Obsidian vault (or any directory) for human reading. Layout:\n"
        "    [dim]<root>/<topic>/<title>.md[/dim]\n\n"
        "The DB stays the canonical source of truth either way; this just "
        "gives you a readable artefact alongside it.\n\n"
        "[dim]Leave blank to skip — you can enable later with `nyx learn root <path>`.[/dim]",
        border_style="blue",
    ))

    raw = typer.prompt("Obsidian / vault root", default="", show_default=False).strip()
    if not raw:
        mark_obsidian_skipped()
        console.print("[dim]Skipped. MD export disabled until you set a root.[/dim]\n")
        return

    resolved = set_obsidian_root(raw)
    warn = "" if resolved.exists() else "  [yellow](path does not exist yet)[/yellow]"
    console.print(f"[green]Configured:[/green] {resolved}{warn}\n")


# ── Save / update / delete (DB + MD) ───────────────────────────────────────────


def _to_sentence_case(s: str) -> str:
    """Capitalize the first letter, leave the rest as-is so acronyms (API, JSON) survive."""
    s = (s or "").strip()
    if not s:
        return s
    return s[0].upper() + s[1:]


def _normalize_topic(s: str) -> str:
    """Sentence-case each `/`-separated segment of the topic path."""
    parts = [_to_sentence_case(p.strip()) for p in (s or "").split("/") if p.strip()]
    return "/".join(parts)


_KEBAB_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _to_kebab(tag: str) -> str:
    s = (tag or "").strip().lower()
    s = _KEBAB_NON_ALNUM.sub("-", s)
    return s.strip("-")


def _normalize_tags(tags: list[str] | None) -> list[str]:
    """Kebab-case each tag, drop empties, dedupe while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        kt = _to_kebab(t)
        if kt and kt not in seen:
            seen.add(kt)
            out.append(kt)
    return out


def save_learning_with_export(profile: str, *, title: str, body: str, topic: str,
                              kind: str, tags: list[str] | None = None,
                              language: str | None = None,
                              source: str | None = None) -> Learning:
    title = _to_sentence_case(title)
    topic = _normalize_topic(topic)
    tags = _normalize_tags(tags)
    emb = embed(title + "\n" + body)
    new_id = _db_save_learning(
        profile, title, body, topic, kind,
        tags, language, source, emb,
    )
    saved = get_learning(new_id)
    md_path = export_learning(saved)
    if md_path:
        console.print(f"[dim]→ wrote {md_path}[/dim]")
    return saved


def update_learning_with_export(learning_id: str, **fields) -> Learning:
    previous = get_learning(learning_id)
    fields = {k: v for k, v in fields.items() if v is not None}
    if "title" in fields:
        fields["title"] = _to_sentence_case(fields["title"])
    if "topic" in fields:
        fields["topic"] = _normalize_topic(fields["topic"])
    if "tags" in fields:
        fields["tags"] = _normalize_tags(fields["tags"])
    if "title" in fields or "body" in fields:
        new_title = fields.get("title", previous.title)
        new_body = fields.get("body", previous.body)
        fields["embedding"] = embed(new_title + "\n" + new_body)
    _db_update_learning(learning_id, **fields)
    updated = get_learning(learning_id)
    md_path = export_learning(updated, previous=previous)
    if md_path:
        console.print(f"[dim]→ updated {md_path}[/dim]")
    return updated


def delete_learning_with_export(learning_id: str) -> Learning:
    target = get_learning(learning_id)
    _db_delete_learning(learning_id)
    removed = remove_learning_file(target)
    if removed:
        console.print(f"[dim]→ removed {removed}[/dim]")
    return target


# ── Distillation ───────────────────────────────────────────────────────────────


def _flatten_bedrock_history(messages: list[dict]) -> list[dict]:
    """Strip tool turns so the distiller sees a clean user↔assistant transcript."""
    cleaned: list[dict] = []
    for m in messages:
        role, content = m.get("role"), m.get("content", [])
        text_parts = [c["text"] for c in content if "text" in c]
        if text_parts:
            cleaned.append({"role": role, "content": [{"text": "\n".join(text_parts)}]})
    return cleaned


def _distill(client, history: list[dict], static_context: str, profile: str = "") -> list[dict]:
    """One-shot call asking the model to propose polished learnings from the conversation."""
    transcript = _flatten_bedrock_history(history)
    if not transcript:
        return []
    if transcript[-1]["role"] != "user":
        transcript.append({
            "role": "user",
            "content": [{"text": "Distill the conversation above using the propose_learnings tool."}],
        })
    _, tool_uses = bedrock.invoke_turn(
        client, BEDROCK_ADVISOR,
        system_texts=[static_context, _DISTILL_PROMPT],
        messages=transcript,
        tools=_PROPOSE_LEARNINGS_TOOL_BEDROCK,
        force_tool_name="propose_learnings",
        max_tokens=4096,
        surface="distill", profile=profile,
    )
    for tu in tool_uses:
        if tu["name"] == "propose_learnings":
            return tu["input"].get("learnings", []) or []
    return []


def _review_proposals(profile: str, proposals: list[dict]) -> int:
    """Walk through each proposal: accept / edit / skip / quit. Returns count saved."""
    import typer

    if not proposals:
        console.print("[dim]Nothing converged — nothing to save.[/dim]")
        return 0

    console.print(f"\n[bold]Distillation:[/bold] {len(proposals)} proposed learning(s).\n")
    saved = 0

    for i, p in enumerate(proposals, 1):
        update_id = p.get("update_id") or None
        action_label = "enrich" if update_id else "new"
        head = (
            f"[bold]({i}/{len(proposals)}) {p.get('title', '(no title)')}[/bold]"
            f"  [dim]{action_label}[/dim]\n"
            f"[dim]kind:[/dim] {p.get('kind', '?')}"
            f"  [dim]· topic:[/dim] {p.get('topic', '?')}"
            + (f"  [dim]· lang:[/dim] {p['language']}" if p.get("language") else "")
            + (f"  [dim]· tags:[/dim] {', '.join(p['tags'])}" if p.get("tags") else "")
        )

        if update_id:
            try:
                existing = get_learning(update_id)
                console.print(Panel(
                    existing.body,
                    title=f"[dim]existing {existing.id[:8]}…[/dim]",
                    border_style="dim",
                ))
            except Exception:
                update_id = None  # fall back to create if lookup fails

        body = p.get("body", "")
        rationale = p.get("rationale", "")
        rationale_block = f"\n\n[italic dim]why save: {rationale}[/italic dim]" if rationale else ""
        console.print(Panel(f"{head}\n\n{body}{rationale_block}", border_style="dim"))

        choice = typer.prompt("[a]ccept / [e]dit / [s]kip / [q]uit",
                              default="a", show_default=True).strip().lower()
        if choice in ("q", "quit"):
            console.print("[dim]Stopping review. Remaining proposals discarded.[/dim]")
            break
        if choice in ("s", "skip", "n", "no"):
            continue
        if choice in ("e", "edit"):
            new_body = typer.edit(p.get("body", ""))
            if new_body is None:
                console.print("[yellow]No changes — skipped.[/yellow]")
                continue
            p["body"] = new_body.rstrip()

        try:
            if update_id:
                saved_learning = update_learning_with_export(update_id, body=p["body"], tags=p.get("tags") or [])
                console.print(f"[green]Updated:[/green] {saved_learning.title}  [dim]({saved_learning.id[:8]}…)[/dim]")
            else:
                saved_learning = save_learning_with_export(
                    profile,
                    title=p["title"], body=p["body"],
                    topic=p["topic"], kind=p["kind"],
                    tags=p.get("tags") or [],
                    language=p.get("language"),
                    source=p.get("source"),
                )
                console.print(f"[green]Saved:[/green] {saved_learning.title}  [dim]({saved_learning.id[:8]}…)[/dim]")
            saved += 1
        except Exception as e:
            console.print(f"[red]Save failed:[/red] {e}")

    return saved


# ── Chat loop ──────────────────────────────────────────────────────────────────


def run_learn(profile: str, topic: str | None = None) -> None:
    """Interactive study-buddy chat. Saves happen post-hoc via /distill or on exit."""
    prompt_obsidian_setup_if_needed()
    client = bedrock.make_client()
    history: list[dict] = []

    recent = list_learnings(profile, topic=topic, limit=12)
    topics = list_learning_topics(profile)

    static_context = _LEARN_PROMPT
    if topics:
        topic_index = "## Topics in the bank\n\n" + "\n".join(
            f"- {t} ({n})" for t, n in topics
        )
        static_context += "\n\n" + topic_index
    if recent:
        static_context += "\n\n" + format_learnings(recent)

    obsidian = get_obsidian_root()
    obsidian_status = (
        f"[green]obsidian:[/green] {obsidian}" if obsidian
        else "[yellow]obsidian:[/yellow] not configured (set with `nyx learn root <path>`)"
    )

    header = f"[bold]nyx learn[/bold]  [dim]{profile}[/dim]"
    if topic:
        header += f"  [dim]· topic: {topic}[/dim]"
    header += f"\n[dim]{len(recent)} recent learnings · {len(topics)} topics · {BEDROCK_ADVISOR}[/dim]"
    header += f"\n{obsidian_status}"
    header += "\n\n[dim]/distill  /list  /topics  ·  shift+enter newline · enter submits · ctrl-c exit[/dim]"
    console.print(Panel(header, border_style="blue"))

    from nyx.lib.chat_input import make_chat_session, chat_prompt
    session = make_chat_session()
    system_texts = [static_context]
    interrupted = False
    while True:
        try:
            user_input = chat_prompt(session, "\033[1;34m>\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            interrupted = True
            break

        if not user_input:
            continue

        if user_input in ("/distill", "/save"):
            _distill_and_review(client, history, static_context, profile)
            continue
        if user_input == "/list":
            print_learnings_table(list_learnings(profile, topic=topic, limit=50))
            continue
        if user_input == "/topics":
            if not topics:
                console.print("[dim]No topics yet — save your first learning to start one.[/dim]")
            else:
                for t, n in topics:
                    console.print(f"  [bold]{t}[/bold]  [dim]({n})[/dim]")
            continue
        if user_input in ("/quit", "/exit"):
            break

        history.append({"role": "user", "content": [{"text": user_input}]})

        console.print()
        try:
            text, tool_uses = bedrock.stream_turn(
                client, BEDROCK_ADVISOR, system_texts, history, _CHAT_TOOLS_BEDROCK,
                surface="learn", profile=profile,
            )

            assistant_content = ([{"text": text}] if text else []) + [
                {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
                for tu in tool_uses
            ]
            history.append({"role": "assistant", "content": assistant_content})

            while tool_uses:
                tool_results = []
                for tu in tool_uses:
                    console.print(f"\n[dim]→ {tu['name']}({_fmt_call_args(tu['input'])})[/dim]")
                    result = _handle_chat_tool(tu["name"], tu["input"], profile)
                    if result and len(result) < 500:
                        console.print(f"[dim]{result}[/dim]")
                    is_err = isinstance(result, str) and result.startswith("ERROR")
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tu["id"],
                            "content": [{"text": result}],
                            "status": "error" if is_err else "success",
                        }
                    })
                history.append({"role": "user", "content": tool_results})

                text, tool_uses = bedrock.stream_turn(
                    client, BEDROCK_ADVISOR, system_texts, history, _CHAT_TOOLS_BEDROCK,
                    max_tokens=2048, surface="learn", profile=profile,
                )
                assistant_content = ([{"text": text}] if text else []) + [
                    {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
                    for tu in tool_uses
                ]
                history.append({"role": "assistant", "content": assistant_content})

        except KeyboardInterrupt:
            if history and history[-1]["role"] == "user":
                history.pop()
            console.print("\n[dim]↩ interrupted[/dim]")

        console.print()

    # Exit path — offer distillation if there's anything to distill
    if history:
        if interrupted:
            console.print()  # ensure newline after ^C
        console.print("\n[bold]Distill this session into learnings?[/bold] [dim][Y/n][/dim]")
        try:
            answer = chat_prompt(session, "> ").strip().lower() or "y"
        except (KeyboardInterrupt, EOFError):
            answer = "n"
        if answer in ("y", "yes"):
            _distill_and_review(client, history, static_context, profile)
        else:
            console.print("[dim]Skipped distillation. Conversation discarded.[/dim]")
    console.print("[dim]Goodbye.[/dim]")


def _distill_and_review(client, history: list[dict], static_context: str, profile: str) -> None:
    if not history:
        console.print("[dim]No conversation to distill yet.[/dim]")
        return
    console.print("[dim]Distilling…[/dim]")
    proposals = _distill(client, history, static_context, profile)
    _review_proposals(profile, proposals)


# ── Local (Ollama / Qwen3) backend ─────────────────────────────────────────────


_CHAT_TOOLS_OPENAI = _to_openai_tools(_CHAT_TOOLS)
_PROPOSE_LEARNINGS_TOOL_OPENAI = _to_openai_tools([_PROPOSE_LEARNINGS_TOOL])[0]


def _fmt_call_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s!r}")
    return ", ".join(parts)


def _valid_proposal(p: dict) -> bool:
    required = ("title", "body", "topic", "kind")
    return all(isinstance(p.get(k), str) and p.get(k) for k in required)


def _flatten_history_for_distill(history: list[dict]) -> list[dict]:
    """Strip tool calls / tool results so the distiller sees a clean user↔assistant transcript.
    The distillation tool list doesn't include the chat tools, and some servers reject messages
    referencing tool calls that aren't in the current tool set."""
    cleaned: list[dict] = []
    for m in history:
        role = m.get("role")
        if role == "user" and isinstance(m.get("content"), str):
            cleaned.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            text = m.get("content") or ""
            if text.strip():
                cleaned.append({"role": "assistant", "content": text})
    return cleaned


def _distill_local(client, history: list[dict], static_context: str) -> list[dict]:
    """Ollama equivalent of _distill — forces a single propose_learnings call."""
    transcript = _flatten_history_for_distill(history)
    if not transcript:
        return []
    messages = [
        {"role": "system", "content": static_context + "\n\n" + _DISTILL_PROMPT},
        *transcript,
    ]
    response = client.chat.completions.create(
        model=OLLAMA_MODEL,
        messages=messages,
        tools=[_PROPOSE_LEARNINGS_TOOL_OPENAI],
        tool_choice={"type": "function", "function": {"name": "propose_learnings"}},
    )
    msg = response.choices[0].message
    for call in (msg.tool_calls or []):
        if call.function.name != "propose_learnings":
            continue
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            return []
        learnings = args.get("learnings") or []
        return [p for p in learnings if isinstance(p, dict) and _valid_proposal(p)]
    return []


def _distill_and_review_local(client, history: list[dict],
                              static_context: str, profile: str) -> None:
    if not history:
        console.print("[dim]No conversation to distill yet.[/dim]")
        return
    console.print("[dim]Distilling…[/dim]")
    try:
        proposals = _distill_local(client, history, static_context)
    except Exception as e:
        console.print(f"[red]Distillation failed:[/red] {e}")
        return
    _review_proposals(profile, proposals)


def run_learn_local(profile: str, topic: str | None = None, think: bool = False) -> None:
    """Interactive study-buddy chat backed by local Qwen3 via Ollama. Full tool parity.

    Qwen3 reasoning mode is OFF by default (faster, less verbose). Pass think=True
    to enable — translates to a `/think` directive in the system prompt.
    """
    from openai import OpenAI
    from nyx.agent import _stream_response

    prompt_obsidian_setup_if_needed()
    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")

    recent = list_learnings(profile, topic=topic, limit=12)
    topics = list_learning_topics(profile)

    directive = "/think" if think else "/no_think"
    # NB: Qwen3 honors /think and /no_think on user messages, not on the system prompt.
    # The directive gets prepended to each user input below.
    static_context = _LEARN_PROMPT
    if topics:
        topic_index = "## Topics in the bank\n\n" + "\n".join(
            f"- {t} ({n})" for t, n in topics
        )
        static_context += "\n\n" + topic_index
    if recent:
        static_context += "\n\n" + format_learnings(recent)

    obsidian = get_obsidian_root()
    obsidian_status = (
        f"[green]obsidian:[/green] {obsidian}" if obsidian
        else "[yellow]obsidian:[/yellow] not configured (set with `nyx learn root <path>`)"
    )

    mode = "think" if think else "no-think"
    header = f"[bold]nyx learn[/bold]  [dim]{profile} · local · {mode}[/dim]"
    if topic:
        header += f"  [dim]· topic: {topic}[/dim]"
    header += f"\n[dim]{len(recent)} recent learnings · {len(topics)} topics · {OLLAMA_MODEL}[/dim]"
    header += f"\n{obsidian_status}"
    header += "\n\n[dim]/distill  /list  /topics  ·  shift+enter newline · enter submits · ctrl-c exit[/dim]"
    console.print(Panel(header, border_style="blue"))

    messages: list[dict] = [{"role": "system", "content": static_context}]
    history_started = False
    interrupted = False

    from nyx.lib.chat_input import make_chat_session, chat_prompt
    session = make_chat_session()
    while True:
        try:
            user_input = chat_prompt(session, "\033[1;34m>\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            interrupted = True
            break

        if not user_input:
            continue

        if user_input in ("/distill", "/save"):
            _distill_and_review_local(client, messages[1:], static_context, profile)
            continue
        if user_input == "/list":
            print_learnings_table(list_learnings(profile, topic=topic, limit=50))
            continue
        if user_input == "/topics":
            if not topics:
                console.print("[dim]No topics yet — save your first learning to start one.[/dim]")
            else:
                for t, n in topics:
                    console.print(f"  [bold]{t}[/bold]  [dim]({n})[/dim]")
            continue
        if user_input in ("/quit", "/exit"):
            break

        messages.append({"role": "user", "content": f"{directive}\n\n{user_input}"})
        history_started = True
        console.print()

        # Tool-use loop — keep going until model returns text only
        while True:
            response = client.chat.completions.create(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=_CHAT_TOOLS_OPENAI,
                stream=True,
            )
            content, tool_calls = _stream_response(response)

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
                result = _handle_chat_tool(call["name"], call["args"], profile)
                if result and len(result) < 500:
                    console.print(f"[dim]{result}[/dim]")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": result,
                })

        console.print()

    # Exit path — offer distillation if there's anything to distill
    if history_started:
        if interrupted:
            console.print()
        console.print("\n[bold]Distill this session into learnings?[/bold] [dim][Y/n][/dim]")
        try:
            answer = chat_prompt(session, "> ").strip().lower() or "y"
        except (KeyboardInterrupt, EOFError):
            answer = "n"
        if answer in ("y", "yes"):
            _distill_and_review_local(client, messages[1:], static_context, profile)
        else:
            console.print("[dim]Skipped distillation. Conversation discarded.[/dim]")
    console.print("[dim]Goodbye.[/dim]")


# ── Researcher ────────────────────────────────────────────────────────────────


def run_research(profile: str) -> None:
    """Deep research session — fetches sources, reads code, synthesises, saves learnings."""
    from nyx.lib.chat_input import make_chat_session, chat_prompt

    prompt_obsidian_setup_if_needed()
    client = bedrock.make_client()

    topics = list_learning_topics(profile)
    static_context = _RESEARCH_PROMPT
    if topics:
        static_context += "\n\n## Topics already in your learning bank\n\n" + "\n".join(
            f"- {t} ({n})" for t, n in topics
        )

    obsidian = get_obsidian_root()
    obsidian_status = (
        f"[green]obsidian:[/green] {obsidian}" if obsidian
        else "[yellow]obsidian:[/yellow] not configured"
    )
    console.print(Panel(
        f"[bold]nyx research[/bold]  [dim]{profile}[/dim]\n"
        f"[dim]{len(topics)} topics in bank · {BEDROCK_ADVISOR}[/dim]\n"
        f"{obsidian_status}\n\n"
        f"[dim]Describe what to research. Model fetches sources and synthesises.\n"
        f"/save to save a summary  ·  ctrl-c to exit[/dim]",
        border_style="cyan",
    ))

    system_texts = [static_context]
    history: list[dict] = []
    session = make_chat_session()

    while True:
        try:
            user_input = chat_prompt(session, "\033[1;36m>\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            return

        if not user_input:
            continue

        if user_input == "/save":
            _save_research_summary(client, history, static_context, profile)
            continue

        history.append({"role": "user", "content": [{"text": user_input}]})
        console.print()

        text, tool_uses = bedrock.stream_turn(
            client, BEDROCK_ADVISOR, system_texts, history, _RESEARCH_TOOLS,
            surface="research", profile=profile,
        )

        assistant_content = ([{"text": text}] if text else []) + [
            {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
            for tu in tool_uses
        ]
        history.append({"role": "assistant", "content": assistant_content})

        while tool_uses:
            tool_results = []
            for tu in tool_uses:
                console.print(f"\n[dim]→ {tu['name']}({_fmt_call_args(tu['input'])})[/dim]")
                result = _handle_chat_tool(tu["name"], tu["input"], profile)
                if result and len(result) < 500:
                    console.print(f"[dim]{result}[/dim]")
                is_err = isinstance(result, str) and result.startswith("ERROR")
                tool_results.append({
                    "toolResult": {
                        "toolUseId": tu["id"],
                        "content": [{"text": result}],
                        "status": "error" if is_err else "success",
                    }
                })
            history.append({"role": "user", "content": tool_results})

            text, tool_uses = bedrock.stream_turn(
                client, BEDROCK_ADVISOR, system_texts, history, _RESEARCH_TOOLS,
                max_tokens=2048, surface="research", profile=profile,
            )
            assistant_content = ([{"text": text}] if text else []) + [
                {"toolUse": {"toolUseId": tu["id"], "name": tu["name"], "input": tu["input"]}}
                for tu in tool_uses
            ]
            history.append({"role": "assistant", "content": assistant_content})

        console.print()


def _save_research_summary(client, history: list[dict], static_context: str, profile: str) -> None:
    """Force a single save_learning call summarising the full research session."""
    if not history:
        console.print("[dim]Nothing to save yet.[/dim]")
        return
    console.print("[dim]Summarising research…[/dim]")
    transcript = _flatten_bedrock_history(history)
    save_tool = _SAVE_LEARNING_BEDROCK
    _, tool_uses = bedrock.invoke_turn(
        client, BEDROCK_ADVISOR,
        system_texts=[static_context],
        messages=transcript + [{"role": "user", "content": [{"text": (
            "Summarise this research session into a single learning entry using save_learning. "
            "Capture the key findings concisely."
        )}]}],
        tools=save_tool,
        force_tool_name="save_learning",
        max_tokens=1024,
        surface="research", profile=profile,
    )
    for tu in tool_uses:
        if tu["name"] == "save_learning":
            try:
                saved = save_learning_with_export(profile, **{
                    k: v for k, v in tu["input"].items()
                    if k in ("title", "body", "topic", "kind", "tags", "language", "source")
                })
                console.print(f"[green]Saved:[/green] {saved.title}  [dim]({saved.id[:8]}…)[/dim]")
            except Exception as e:
                console.print(f"[red]Save failed:[/red] {e}")


# ── Manual capture ─────────────────────────────────────────────────────────────


def add_learning_interactive(profile: str) -> None:
    """Manual capture: prompt for fields, embed, save (DB + MD)."""
    import typer

    prompt_obsidian_setup_if_needed()

    title = typer.prompt("Title").strip()
    if not title:
        console.print("[red]Title required.[/red]")
        raise typer.Exit(1)

    topic = typer.prompt("Topic (use '/' for nesting, e.g. 'category theory/algebras')").strip()
    if not topic:
        console.print("[red]Topic required.[/red]")
        raise typer.Exit(1)

    valid_kinds = ("concept", "pattern", "gotcha", "exercise", "definition")
    while True:
        kind = typer.prompt(f"Kind ({'/'.join(valid_kinds)})", default="concept").strip().lower()
        if kind in valid_kinds:
            break
        console.print(f"[red]Must be one of: {', '.join(valid_kinds)}[/red]")

    language = typer.prompt("Language (blank if N/A)", default="", show_default=False).strip() or None
    tags_raw = typer.prompt("Tags (comma-separated, optional)", default="", show_default=False).strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
    source = typer.prompt("Source URL or citation (optional)", default="", show_default=False).strip() or None

    console.print("\n[dim]Body — paste, then enter ':done' on its own line to finish.[/dim]")
    body_lines: list[str] = []
    while True:
        try:
            line = console.input("")
        except (KeyboardInterrupt, EOFError):
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(1)
        if line.strip() == ":done":
            break
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body:
        console.print("[red]Body required.[/red]")
        raise typer.Exit(1)

    saved = save_learning_with_export(
        profile, title=title, body=body, topic=topic, kind=kind,
        tags=tags, language=language, source=source,
    )
    console.print(f"\n[green]Saved:[/green] {saved.id[:8]}…")
    print_learning(saved)
