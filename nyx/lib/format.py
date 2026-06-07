from __future__ import annotations
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from nyx.memory.supabase import Decision, Idea, Spec, Conversation, Learning

console = Console()

def format_decisions(decisions: list[Decision]) -> str:
    if not decisions:
        return ""
    lines = ["## Past Decisions\n"]
    for d in decisions:
        lines.append(f"### [{d.category}] {d.title}")
        lines.append(d.body)
        if d.rationale:
            lines.append(f"*Rationale: {d.rationale}*")
        lines.append("")
    return "\n".join(lines)

def format_conversations(convos: list[Conversation]) -> str:
    if not convos:
        return ""
    lines = ["## Conversation Context\n"]
    for c in convos:
        role = "You" if c.role == "user" else "Advisor"
        lines.append(f"**{role}**: {c.content[:500]}")
        lines.append("")
    return "\n".join(lines)

def format_ideas(ideas: list[Idea]) -> str:
    if not ideas:
        return ""
    lines = ["## Parked Ideas\n"]
    for i in ideas:
        tags = f" [{', '.join(i.tags)}]" if i.tags else ""
        lines.append(f"- **{i.title}**{tags}: {i.body[:200]}")
    return "\n".join(lines)

def format_spec(spec: Spec) -> str:
    # Criteria may be stored as either {'criterion': '...'} dicts or bare strings
    # (depending on which version of the advisor created the spec). Handle both.
    criteria = "\n".join(
        f"- {c['criterion'] if isinstance(c, dict) else c}"
        for c in spec.criteria
    )
    files = "\n".join(f"- {p}" for p in spec.files_to_touch) if spec.files_to_touch else "(unspecified — explore first)"
    return f"""## Spec: {spec.title}

**Objective:** {spec.objective}

**In scope:** {spec.scope}

**Out of scope:** {spec.out_of_scope}

**Files to touch:**
{files}

**Do not change:** {spec.do_not_change or 'None'}

**Acceptance criteria:**
{criteria}

**Coding notes:** {spec.coding_notes or 'None'}
"""

def print_spec(spec: Spec) -> None:
    """Full view of a spec: title, status/date header, then the formatted body."""
    from datetime import datetime, timezone
    status_colors = {"pending": "yellow", "in_progress": "cyan", "done": "green"}
    color = status_colors.get(spec.status, "white")
    try:
        dt = datetime.fromisoformat(spec.created_at.replace("Z", "+00:00"))
        created = _humanise_age(datetime.now(timezone.utc) - dt)
    except Exception:
        created = spec.created_at or "—"
    header = (
        f"[bold]{spec.title}[/bold]\n"
        f"[{color}]{spec.status}[/{color}]  [dim]·  {created}  ·  {spec.id}[/dim]"
    )
    console.print(Panel(
        header + "\n\n" + format_spec(spec),
        border_style="dim",
    ))


def print_spec_saved(spec: Spec) -> None:
    console.print(Panel(
        f"[bold green]Spec saved[/bold green]\n\n"
        f"[bold]{spec.title}[/bold]\n"
        f"ID: [dim]{spec.id}[/dim]\n\n"
        f"Run [bold cyan]nyx code[/bold cyan] in another split when ready.",
        border_style="green",
    ))

def print_ideas_table(ideas: list[Idea]) -> None:
    if not ideas:
        console.print("[dim]No ideas yet.[/dim]")
        return
    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("Status", width=10)
    table.add_column("Title")
    table.add_column("Tags", style="dim")
    status_colors = {"parked": "yellow", "exploring": "cyan", "adopted": "green", "dropped": "red"}
    for i, idea in enumerate(ideas):
        color = status_colors.get(idea.status, "white")
        table.add_row(
            str(i + 1),
            f"[{color}]{idea.status}[/{color}]",
            idea.title,
            ", ".join(idea.tags) if idea.tags else "",
        )
    console.print(table)

def print_projects_table(
    projects: list[str],
    last_activity: dict[str, str],
    current: str,
    active_window_days: int = 7,
) -> None:
    if not projects:
        console.print("[dim]No projects yet. Use [bold]nyx projects -c <name>[/bold] to start one.[/dim]")
        return
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=active_window_days)

    from nyx.lib.project_config import get_all_roots
    roots = get_all_roots()

    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("", width=2)
    table.add_column("Status", width=8)
    table.add_column("Project")
    table.add_column("Last activity", style="dim")
    table.add_column("Root", style="dim")

    for name in projects:
        marker = "●" if name == current else " "
        ts = last_activity.get(name)
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                is_active = dt >= cutoff
                rel = _humanise_age(now - dt)
            except Exception:
                is_active, rel = False, ts
        else:
            is_active, rel = False, "—"
        status = "[green]active[/green]" if is_active else "[dim]idle[/dim]"
        root = roots.get(name, "[dim italic]default[/dim italic]")
        table.add_row(f"[cyan]{marker}[/cyan]", status, name, rel, root)
    console.print(table)
    console.print(
        "[dim]switch: [bold]nyx projects -sw <name>[/bold]  ·  "
        "create: [bold]-c <name>[/bold]  ·  "
        "delete: [bold]-d <name>[/bold]  ·  "
        "set root: [bold]-r <path>[/bold][/dim]"
    )


def print_cost_summary(summary: dict) -> None:
    """Render token usage and cost breakdown by project, agent, and model in a panel."""
    total = summary["total"]

    # Collect all rows across groups to compute consistent column widths
    groups = [
        (key, title, sorted(
            [(n, d) for n, d in summary[key].items() if n != "—"],
            key=lambda x: -x[1]["cost"],
        ))
        for key, title in (("by_project", "By project"), ("by_surface", "By agent"), ("by_model", "By model"))
        if any(n != "—" for n in summary[key])
    ]

    all_rows = [(n, d) for _, _, rows in groups for n, d in rows]
    name_w = max((len(n) for n, _ in all_rows), default=0)
    in_w   = max((len(f"{d['in_tok']:,}") for _, d in all_rows), default=0)
    out_w  = max((len(f"{d['out_tok']:,}") for _, d in all_rows), default=0)

    lines: list[str] = [
        f"[bold]Total[/bold]  "
        f"[dim]↑ {total['in_tok']:,}  ↓ {total['out_tok']:,}[/dim]  "
        f"[bold green]${total['cost']:.4f}[/bold green]",
    ]

    for _, title, rows in groups:
        if not rows:
            continue
        lines.append(f"\n[dim]{title}[/dim]")
        for name, d in rows:
            lines.append(
                f"  {name:<{name_w}}  "
                f"[dim]↑ {d['in_tok']:>{in_w},}  ↓ {d['out_tok']:>{out_w},}[/dim]  "
                f"[green]${d['cost']:.4f}[/green]"
            )

    console.print(Panel("\n".join(lines), title="[dim]nyx cost[/dim]", border_style="dim"))


def _humanise_age(delta) -> str:
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def print_specs_table(specs: list[Spec]) -> None:
    if not specs:
        console.print("[dim]No pending specs.[/dim]")
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("Status", width=12)
    table.add_column("Title")
    table.add_column("Created", style="dim")
    table.add_column("ID", style="dim")
    status_colors = {"pending": "yellow", "in_progress": "cyan", "done": "green"}
    for i, spec in enumerate(specs):
        color = status_colors.get(spec.status, "white")
        try:
            dt = datetime.fromisoformat(spec.created_at.replace("Z", "+00:00"))
            created = _humanise_age(now - dt)
        except Exception:
            created = spec.created_at or "—"
        table.add_row(
            str(i + 1),
            f"[{color}]{spec.status}[/{color}]",
            spec.title,
            created,
            spec.id[:8] + "...",
        )
    console.print(table)


def print_learnings_table(learnings: list[Learning]) -> None:
    if not learnings:
        console.print("[dim]No learnings yet. Start one with [bold]nyx learn add[/bold] or just [bold]nyx learn[/bold].[/dim]")
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    kind_colors = {
        "concept": "cyan", "pattern": "magenta", "gotcha": "yellow",
        "exercise": "blue", "definition": "green",
    }
    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("Kind", width=10)
    table.add_column("Topic")
    table.add_column("Lang", style="dim", width=8)
    table.add_column("Title")
    table.add_column("Created", style="dim")
    table.add_column("ID", style="dim")
    for i, learning in enumerate(learnings):
        color = kind_colors.get(learning.kind, "white")
        try:
            dt = datetime.fromisoformat(learning.created_at.replace("Z", "+00:00"))
            created = _humanise_age(now - dt)
        except Exception:
            created = learning.created_at or "—"
        table.add_row(
            str(i + 1),
            f"[{color}]{learning.kind}[/{color}]",
            learning.topic,
            learning.language or "—",
            learning.title,
            created,
            learning.id[:8] + "...",
        )
    console.print(table)


def print_learning(learning: Learning) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(learning.created_at.replace("Z", "+00:00"))
        created = _humanise_age(now - dt)
    except Exception:
        created = learning.created_at or "—"
    meta = (
        f"[dim]topic:[/dim] {learning.topic}"
        f"  [dim]·  kind:[/dim] {learning.kind}"
        + (f"  [dim]·  lang:[/dim] {learning.language}" if learning.language else "")
        + (f"  [dim]·  tags:[/dim] {', '.join(learning.tags)}" if learning.tags else "")
        + f"  [dim]·  {created}[/dim]"
    )
    body = learning.body
    if learning.source:
        body += f"\n\n[dim]source:[/dim] {learning.source}"
    console.print(Panel(
        f"[bold]{learning.title}[/bold]\n{meta}\n\n{body}",
        border_style="dim",
    ))


def format_learnings(learnings: list[Learning]) -> str:
    """Compact text representation for system-prompt context."""
    if not learnings:
        return ""
    lines = ["## Recent Learnings\n"]
    for learning in learnings:
        head = f"### [{learning.kind}] {learning.title} — {learning.topic}"
        if learning.language:
            head += f" ({learning.language})"
        lines.append(head)
        lines.append(learning.body)
        if learning.tags:
            lines.append(f"*tags: {', '.join(learning.tags)}*")
        lines.append("")
    return "\n".join(lines)
