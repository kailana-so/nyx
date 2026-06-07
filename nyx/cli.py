from __future__ import annotations
from pathlib import Path
import typer
from typing import Optional
from nyx.lib.session import load_env, get_profile, get_project, set_active_project

app = typer.Typer(
    name="nyx",
    add_completion=False,
    pretty_exceptions_show_locals=False,
    epilog=(
        "Active project is used by default; switch with `nyx projects -sw <name>`. "
        "Manage specs (mark done, delete) with `nyx specs`. "
        "Personal study log: `nyx learn`. "
        "Run `nyx COMMAND --help` for per-command options."
    ),
)

learn_app = typer.Typer(
    name="learn",
    add_completion=False,
    help="Personal study log — separate from project work. Pivots on topic / language / kind.",
)
app.add_typer(learn_app, name="learn", invoke_without_command=True)

specs_app = typer.Typer(
    name="specs",
    add_completion=False,
    help="Spec management — interactive menu, plus subcommands for direct ops by spec ID.",
)
app.add_typer(specs_app, name="specs", invoke_without_command=True)

def _setup() -> tuple[str, str]:
    load_env()
    return get_profile(), get_project()


def _resolve_project(profile: str, explicit: Optional[str]) -> str:
    """Pick a project: positional arg > active project > cwd match > interactive picker."""
    from nyx.memory.supabase import get_projects, get_project_last_activity
    from nyx.lib.format import print_projects_table, console

    projs = get_projects(profile)
    if explicit:
        if explicit not in projs:
            console.print(f"[yellow]'{explicit}' has no data yet — using it anyway.[/yellow]")
        return explicit

    active = get_project()
    if active in projs:
        return active

    # Active project set via `nyx projects -sw <name>` but no DB rows yet — trust it.
    # The project comes into existence on first save (spec / decision / idea / learning).
    # Skip "misc" since that's the default fallback, not an explicit choice.
    if active and active != "misc":
        console.print(f"[dim]Using project [bold]{active}[/bold] (no data yet)[/dim]")
        return active

    cwd_name = Path.cwd().name
    if cwd_name in projs:
        console.print(f"[dim]Using project [bold]{cwd_name}[/bold] (matched cwd)[/dim]")
        return cwd_name

    if not projs:
        console.print("[red]No projects yet. Use [bold]nyx projects -c <name>[/bold] first.[/red]")
        raise typer.Exit(1)

    activity = get_project_last_activity(profile)
    print_projects_table(projs, activity, active)
    choice = typer.prompt(f"Pick (1-{len(projs)} or name)", default="1")
    if choice in projs:
        return choice
    try:
        return projs[int(choice) - 1]
    except (ValueError, IndexError):
        console.print("[red]Invalid selection.[/red]")
        raise typer.Exit(1)


@app.command()
def chat(
    project: Optional[str] = typer.Argument(None, help="Project name. Omit to use the active project (set via `nyx projects -sw`)."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
    local: bool = typer.Option(False, "--local", "-l",
                               help="Run advisor against local Qwen3 (Ollama) instead of Claude."),
    think: bool = typer.Option(False, "--think", help="Enable Qwen3's reasoning mode (only meaningful with --local; default is /no_think for faster, less verbose responses)."),
) -> None:
    """Start an advisor session."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    if local:
        from nyx.chat import run_chat_local
        run_chat_local(p, proj, think=think)
    else:
        from nyx.chat import run_chat
        run_chat(p, proj)


@app.command()
def code(
    project: Optional[str] = typer.Argument(None, help="Project name. Omit to use the active project (set via `nyx projects -sw`)."),
    spec_id: Optional[str] = typer.Option(None, "--spec-id", "-s", help="Pre-select a spec by full UUID or short ID prefix; skip the picker."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Run the coding agent on a spec."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    _run_agent(spec_id, "code", p, proj)


@app.command()
def research(
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Deep research session — fetches sources, reads code, synthesises, saves to learn bank."""
    load_env()
    p = profile or get_profile()
    from nyx.learn import run_research
    run_research(p)


@app.command()
def plan(
    project: Optional[str] = typer.Argument(None, help="Project name. Omit to use the active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Describe a task, model reads the code and plans, /build implements. No spec required."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    from nyx.agent import run_plan
    run_plan(p, proj)


@app.command()
def test(
    project: Optional[str] = typer.Argument(None, help="Project name. Omit to use the active project (set via `nyx projects -sw`)."),
    spec_id: Optional[str] = typer.Option(None, "--spec-id", "-s", help="Pre-select a spec by full UUID or short ID prefix; skip the picker."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Run the test-writing agent on a spec."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    _run_agent(spec_id, "test", p, proj)


def _parse_spec_selection(choice: str, n: int) -> list[int]:
    """Parse '1', '1-3', '1,3,5', '1,3-5' into 0-based indices. Raises ValueError on bad input."""
    indices: list[int] = []
    for part in choice.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            for i in range(lo, hi + 1):
                indices.append(i - 1)
        else:
            indices.append(int(part) - 1)
    if not indices or any(i < 0 or i >= n for i in indices):
        raise ValueError("out of range")
    seen: set[int] = set()
    return [i for i in indices if not (i in seen or seen.add(i))]


def _run_agent(spec_id: Optional[str], mode: str, profile: str, project: str) -> None:
    from nyx.memory.supabase import get_pending_specs
    from nyx.lib.format import print_specs_table, console
    from nyx.agent import run_agent

    if spec_id:
        run_agent(spec_id, mode)
        return

    specs = get_pending_specs(profile, project)
    if not specs:
        console.print("[dim]No pending specs. Run [bold]nyx chat[/bold] to create one.[/dim]")
        raise typer.Exit()
    print_specs_table(specs)
    if len(specs) == 1:
        choice = typer.prompt("Pick spec", default="1")
    else:
        choice = typer.prompt("Pick spec(s) (e.g. 1, 1-3, 1,3-5)")
    try:
        idxs = _parse_spec_selection(choice, len(specs))
    except ValueError:
        console.print("[red]Invalid selection.[/red]")
        raise typer.Exit(1)

    selected = [specs[i] for i in idxs]
    if len(selected) > 1:
        console.print(f"[dim]Running {len(selected)} specs sequentially: "
                      + ", ".join(s.title for s in selected) + "[/dim]")
    for s in selected:
        run_agent(s.id, mode)


@app.command()
def ideas(
    project: Optional[str] = typer.Argument(None, help="Project name. Omit to use the active project (set via `nyx projects -sw`)."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Browse and manage the ideas parking lot."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)

    from nyx.memory.supabase import list_all_ideas, update_idea_status
    from nyx.lib.format import print_ideas_table, console

    while True:
        all_ideas = list_all_ideas(p, proj)
        print_ideas_table(all_ideas)

        if not all_ideas:
            break

        console.print("\n[dim]u <n> <status>  to update  ·  q to quit[/dim]")
        try:
            cmd = console.input("[dim]>[/dim] ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if cmd in ("q", "quit", ""):
            break

        parts = cmd.split()
        if len(parts) == 3 and parts[0] == "u":
            try:
                idx = int(parts[1]) - 1
                status = parts[2]
                valid = ("parked", "exploring", "adopted", "dropped")
                if status not in valid:
                    console.print(f"[red]Status must be one of: {', '.join(valid)}[/red]")
                    continue
                update_idea_status(all_ideas[idx].id, status)
                console.print("[green]Updated.[/green]")
            except (ValueError, IndexError):
                console.print("[red]Invalid.[/red]")


@specs_app.callback(invoke_without_command=True)
def specs(
    ctx: typer.Context,
    project: Optional[str] = typer.Option(None, "--project", help="Target a non-active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """
    Spec management. Bare `nyx specs` opens the interactive menu:

      s <n>        show full spec
      d <n>        mark done
      x <n>        delete
      r <n>        run coding agent
      t <n>        run test agent
      root         show project root
      root <path>  set project root
      q            quit

    n can be a single index (1), a range (1-3), or a list (1,3-5). All actions
    accept multi-select. Order of action and indices can be flipped: `1,2 x`
    works the same as `x 1,2`.

    Or use the subcommands below for direct ops by spec ID (or short prefix).
    """
    if ctx.invoked_subcommand is not None:
        return
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)

    from nyx.memory.supabase import list_all_specs, update_spec_status, delete_spec
    from nyx.lib.format import print_specs_table, print_spec, console
    from nyx.lib.project_config import get_project_root, set_project_root
    from nyx.agent import run_agent

    while True:
        specs_list = list_all_specs(p, proj)
        print_specs_table(specs_list)
        console.print(f"[dim]Project:[/dim] [bold]{proj}[/bold]  "
                      f"[dim]Root:[/dim] {get_project_root(proj)}")

        console.print(
            "\n[bold]Actions[/bold]  [dim italic](n = 1, 1-3, or 1,3-5)[/dim italic]\n"
            "  [bold]s[/bold] <n>        show full spec\n"
            "  [bold]d[/bold] <n>        mark done\n"
            "  [bold]x[/bold] <n>        delete\n"
            "  [bold]r[/bold] <n>        run coding agent\n"
            "  [bold]t[/bold] <n>        run test agent\n"
            "  [bold]root[/bold]         show project root\n"
            "  [bold]root[/bold] <path>  set project root\n"
            "  [bold]q[/bold]            quit"
        )
        try:
            cmd = console.input("[dim]>[/dim] ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if cmd.lower() in ("q", "quit", ""):
            break

        # `root` action: standalone (show) or `root <path>` (set)
        tokens = cmd.split(maxsplit=1)
        if tokens and tokens[0].lower() == "root":
            if len(tokens) == 1:
                console.print(f"[dim]Root for[/dim] [bold]{proj}[/bold]: {get_project_root(proj)}")
            else:
                resolved = set_project_root(proj, tokens[1].strip())
                warn = "" if resolved.exists() else "  [yellow](path does not exist yet)[/yellow]"
                console.print(f"[green]Root set:[/green] {resolved}{warn}")
            continue

        if not specs_list:
            console.print("[dim]No specs yet. Run [bold]nyx chat[/bold] to create one.[/dim]")
            continue

        parsed = _parse_spec_command(cmd, len(specs_list))
        if parsed is None:
            console.print("[red]Use: s|d|x|r|t <n>  ·  root [<path>]  ·  q[/red]")
            continue

        action, idxs = parsed
        targets = [specs_list[i] for i in idxs]

        if action == "show":
            for s in targets:
                print_spec(s)
        elif action == "done":
            for s in targets:
                update_spec_status(s.id, "done")
            console.print(
                f"[green]Marked done ({len(targets)}):[/green] "
                + ", ".join(s.title for s in targets)
            )
        elif action == "delete":
            console.print("[yellow]About to delete:[/yellow]")
            for s in targets:
                console.print(f"  • {s.title}")
            confirm = typer.prompt("Confirm? [y/N]", default="n", show_default=False).strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[dim]Aborted.[/dim]")
                continue
            for s in targets:
                delete_spec(s.id)
            console.print(f"[green]Deleted {len(targets)}.[/green]")
        elif action in ("run", "test"):
            mode = "code" if action == "run" else "test"
            if len(targets) > 1:
                console.print(f"[dim]Running {len(targets)} specs sequentially ({mode}): "
                              + ", ".join(s.title for s in targets) + "[/dim]")
            for s in targets:
                run_agent(s.id, mode)


_SPEC_ACTIONS = {
    "s": "show", "show": "show", "view": "show",
    "d": "done", "done": "done",
    "x": "delete", "delete": "delete", "rm": "delete",
    "r": "run", "run": "run", "code": "run",
    "t": "test", "test": "test",
}


def _parse_spec_command(cmd: str, n: int) -> Optional[tuple[str, list[int]]]:
    """Parse 'd 1', 'x 1-3', '1,2 delete', etc. Returns (action, 0-based indices) or None."""
    tokens = cmd.replace(",", " ").split()
    if not tokens:
        return None
    action: Optional[str] = None
    idx_tokens: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in _SPEC_ACTIONS:
            if action is None:
                action = _SPEC_ACTIONS[low]
            # else: redundant action keyword (e.g. 'x 4 delete') — ignore
        else:
            idx_tokens.append(tok)
    if action is None or not idx_tokens:
        return None
    try:
        idxs = _parse_spec_selection(",".join(idx_tokens), n)
    except ValueError:
        return None
    return action, idxs


def _resolve_spec_id(prefix: str, profile: str, project: str):
    """Match a Spec by full UUID or short ID prefix (within the given project)."""
    from nyx.memory.supabase import get_spec, list_all_specs
    if len(prefix) >= 32:
        try:
            return get_spec(prefix)
        except Exception:
            return None
    matches = [s for s in list_all_specs(profile, project) if s.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        from nyx.lib.format import console
        console.print(f"[yellow]Ambiguous prefix '{prefix}' — {len(matches)} matches.[/yellow]")
    return None


def _resolve_specs_or_exit(ids: list[str], profile: str, project: str) -> list:
    from nyx.lib.format import console
    resolved = []
    for raw in ids:
        spec = _resolve_spec_id(raw, profile, project)
        if spec is None:
            console.print(f"[red]No spec matches '{raw}'.[/red]")
            raise typer.Exit(1)
        resolved.append(spec)
    return resolved


@specs_app.command("show")
def specs_show(
    spec_id: str = typer.Argument(..., help="Full UUID or short ID prefix."),
    project: Optional[str] = typer.Option(None, "--project", help="Target a non-active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Show the full body of one spec."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    from nyx.lib.format import print_spec, console
    spec = _resolve_spec_id(spec_id, p, proj)
    if spec is None:
        console.print(f"[red]No spec matches '{spec_id}'.[/red]")
        raise typer.Exit(1)
    print_spec(spec)


@specs_app.command("done")
def specs_done(
    ids: list[str] = typer.Argument(..., help="One or more spec IDs (full or prefix)."),
    project: Optional[str] = typer.Option(None, "--project", help="Target a non-active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Mark one or more specs as done."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    from nyx.memory.supabase import update_spec_status
    from nyx.lib.format import console
    targets = _resolve_specs_or_exit(ids, p, proj)
    for s in targets:
        update_spec_status(s.id, "done")
    console.print(f"[green]Marked done ({len(targets)}):[/green] "
                  + ", ".join(s.title for s in targets))


@specs_app.command("delete")
def specs_delete(
    ids: list[str] = typer.Argument(..., help="One or more spec IDs (full or prefix)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", help="Target a non-active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Delete one or more specs."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    from nyx.memory.supabase import delete_spec
    from nyx.lib.format import console
    targets = _resolve_specs_or_exit(ids, p, proj)
    if not yes:
        console.print("[yellow]About to delete:[/yellow]")
        for s in targets:
            console.print(f"  • {s.title}")
        confirm = typer.prompt("Confirm? [y/N]", default="n", show_default=False).strip().lower()
        if confirm not in ("y", "yes"):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit()
    for s in targets:
        delete_spec(s.id)
    console.print(f"[green]Deleted {len(targets)}.[/green]")


@specs_app.command("run")
def specs_run(
    ids: list[str] = typer.Argument(..., help="One or more spec IDs (full or prefix)."),
    project: Optional[str] = typer.Option(None, "--project", help="Target a non-active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Run the coding agent on one or more specs (sequentially)."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    from nyx.agent import run_agent
    from nyx.lib.format import console
    targets = _resolve_specs_or_exit(ids, p, proj)
    if len(targets) > 1:
        console.print(f"[dim]Running {len(targets)} specs sequentially (code): "
                      + ", ".join(s.title for s in targets) + "[/dim]")
    for s in targets:
        run_agent(s.id, "code")


@specs_app.command("test")
def specs_test(
    ids: list[str] = typer.Argument(..., help="One or more spec IDs (full or prefix)."),
    project: Optional[str] = typer.Option(None, "--project", help="Target a non-active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Run the test-writing agent on one or more specs (sequentially)."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    from nyx.agent import run_agent
    from nyx.lib.format import console
    targets = _resolve_specs_or_exit(ids, p, proj)
    if len(targets) > 1:
        console.print(f"[dim]Running {len(targets)} specs sequentially (test): "
                      + ", ".join(s.title for s in targets) + "[/dim]")
    for s in targets:
        run_agent(s.id, "test")


@specs_app.command("root")
def specs_root(
    path: Optional[str] = typer.Argument(None, help="New root path. Omit to show the current root."),
    project: Optional[str] = typer.Option(None, "--project", help="Target a non-active project."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Show or set the filesystem root for the active project."""
    load_env()
    p = profile or get_profile()
    proj = _resolve_project(p, project)
    from nyx.lib.project_config import get_project_root, set_project_root
    from nyx.lib.format import console
    if path is None:
        console.print(f"[dim]Root for[/dim] [bold]{proj}[/bold]: {get_project_root(proj)}")
        return
    resolved = set_project_root(proj, path)
    warn = "" if resolved.exists() else "  [yellow](path does not exist yet)[/yellow]"
    console.print(f"[green]Root set:[/green] {resolved}{warn}")


@app.command()
def cost(
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Show token usage and cost breakdown by project, agent, and model."""
    load_env()
    from nyx.lib.usage import load_usage, summarise
    from nyx.lib.format import print_cost_summary, console
    p = profile or get_profile()
    records = load_usage(p)
    if not records:
        console.print("[dim]No usage data yet — run nyx chat, plan, or code first.[/dim]")
        return
    summary = summarise(records)
    print_cost_summary(summary)


@app.command()
def projects(
    create: Optional[str] = typer.Option(None, "--create", "-c", metavar="NAME",
                                         help="Create and switch to a new project."),
    switch: Optional[str] = typer.Option(None, "--switch", "-sw", metavar="NAME",
                                         help="Switch the active project."),
    delete: Optional[str] = typer.Option(None, "--delete", "-d", metavar="NAME",
                                         help="Delete all data for a project."),
    set_root: Optional[str] = typer.Option(None, "--set-root", "-r", metavar="PATH",
                                           help="Set filesystem root for the active project (or use --name)."),
    unset_root: bool = typer.Option(False, "--unset-root",
                                    help="Clear the configured root for the active project (or use --name)."),
    name: Optional[str] = typer.Option(None, "--name", "-n", metavar="NAME",
                                       help="Target a specific project for --set-root / --unset-root."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt for --delete."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """List, create, switch, or delete projects, or configure project roots."""
    load_env()
    p = profile or get_profile()

    from nyx.memory.supabase import (
        get_projects, get_project_last_activity, delete_project,
    )
    from nyx.lib.format import print_projects_table, console
    from nyx.lib.project_config import (
        set_project_root, unset_project_root, get_project_root,
    )

    flags = [bool(create), bool(switch), bool(delete), bool(set_root), bool(unset_root)]
    if sum(flags) > 1:
        console.print("[red]Pass only one of -c, -sw, -d, --set-root, --unset-root.[/red]")
        raise typer.Exit(1)

    if set_root or unset_root:
        target = name or get_project()
        if set_root:
            resolved = set_project_root(target, set_root)
            warn = "" if resolved.exists() else "  [yellow](path does not exist yet)[/yellow]"
            console.print(f"[green]Root for[/green] [bold]{target}[/bold]: {resolved}{warn}")
        else:
            if unset_project_root(target):
                console.print(f"[green]Cleared root for[/green] [bold]{target}[/bold]; "
                              f"falling back to default ({get_project_root(target)})")
            else:
                console.print(f"[dim]No custom root was set for {target}.[/dim]")
        return

    if create or switch:
        target = create or switch
        existing = get_projects(p)
        if create and target in existing:
            console.print(f"[yellow]'{target}' already exists — switching to it.[/yellow]")
        elif switch and target not in existing:
            console.print(f"[yellow]'{target}' has no data yet — switching anyway.[/yellow]")
        set_active_project(target)
        console.print(f"[green]Active project:[/green] [bold]{target}[/bold]  [dim](profile: {p})[/dim]")
        return

    if delete:
        existing = get_projects(p)
        if delete not in existing:
            console.print(f"[red]'{delete}' not found in profile '{p}'.[/red]")
            raise typer.Exit(1)
        if delete == get_project():
            console.print(f"[red]'{delete}' is the active project — switch first with `nyx projects -sw <other>`.[/red]")
            raise typer.Exit(1)
        if not yes:
            confirm = typer.prompt(
                f"Delete ALL specs/ideas/decisions/conversations for '{delete}'? Type the name to confirm",
                default="",
                show_default=False,
            )
            if confirm != delete:
                console.print("[yellow]Aborted.[/yellow]")
                raise typer.Exit()
        counts = delete_project(p, delete)
        total = sum(counts.values())
        console.print(f"[green]Deleted {total} rows[/green] from '{delete}': "
                      + ", ".join(f"{k}={v}" for k, v in counts.items()))
        return

    projs = get_projects(p)
    activity = get_project_last_activity(p)
    print_projects_table(projs, activity, get_project())


# ── nyx learn ──────────────────────────────────────────────────────────────────


@learn_app.callback(invoke_without_command=True)
def learn_default(
    ctx: typer.Context,
    topic: Optional[str] = typer.Option(None, "--topic", "-t", help="Pre-filter recent learnings shown to the advisor by topic."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
    local: bool = typer.Option(False, "--local", "-L", help="Run the chat (and distillation) on local Qwen3 via Ollama instead of Claude."),
    think: bool = typer.Option(False, "--think", help="Enable Qwen3's reasoning mode (only meaningful with --local; default is /no_think for faster, less verbose responses)."),
) -> None:
    """Open the interactive study buddy. Use a subcommand (add/list/show/edit/rm) for direct ops."""
    if ctx.invoked_subcommand is not None:
        return
    load_env()
    p = profile or get_profile()
    if local:
        from nyx.learn import run_learn_local
        run_learn_local(p, topic=topic, think=think)
    else:
        from nyx.learn import run_learn
        run_learn(p, topic=topic)


@learn_app.command("add")
def learn_add(
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Manually capture a learning (interactive prompts)."""
    load_env()
    p = profile or get_profile()
    from nyx.learn import add_learning_interactive
    add_learning_interactive(p)


@learn_app.command("list")
def learn_list(
    topic: Optional[str] = typer.Option(None, "--topic", "-t", help="Filter by topic (exact match; supports nesting like 'category theory/algebras')."),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Filter by language tag."),
    kind: Optional[str] = typer.Option(None, "--kind", "-k", help="Filter by kind: concept | pattern | gotcha | exercise | definition."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows to return."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """List learnings with optional filters."""
    load_env()
    p = profile or get_profile()
    from nyx.memory.supabase import list_learnings
    from nyx.lib.format import print_learnings_table
    items = list_learnings(p, topic=topic, language=language, kind=kind, limit=limit)
    print_learnings_table(items)


@learn_app.command("show")
def learn_show(
    learning_id: str = typer.Argument(..., help="Full or prefix ID."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Show the full body of a learning."""
    load_env()
    p = profile or get_profile()
    from nyx.lib.format import print_learning, console
    target = _resolve_learning_id(learning_id, p)
    if target is None:
        console.print(f"[red]No learning matches '{learning_id}'.[/red]")
        raise typer.Exit(1)
    print_learning(target)


@learn_app.command("rm")
def learn_rm(
    learning_id: str = typer.Argument(..., help="Full or prefix ID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Delete a learning (DB row + Obsidian MD file if configured)."""
    load_env()
    p = profile or get_profile()
    from nyx.learn import delete_learning_with_export
    from nyx.lib.format import console
    target = _resolve_learning_id(learning_id, p)
    if target is None:
        console.print(f"[red]No learning matches '{learning_id}'.[/red]")
        raise typer.Exit(1)
    if not yes:
        console.print(f"[yellow]About to delete:[/yellow] {target.title}")
        confirm = typer.prompt("Confirm? [y/N]", default="n", show_default=False).strip().lower()
        if confirm not in ("y", "yes"):
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit()
    delete_learning_with_export(target.id)
    console.print(f"[green]Deleted:[/green] {target.title}")


@learn_app.command("edit")
def learn_edit(
    learning_id: str = typer.Argument(..., help="Full or prefix ID."),
    title: Optional[str] = typer.Option(None, "--title", help="New title."),
    topic: Optional[str] = typer.Option(None, "--topic", help="New topic (use '/' for nesting)."),
    kind: Optional[str] = typer.Option(None, "--kind", help="New kind: concept | pattern | gotcha | exercise | definition."),
    language: Optional[str] = typer.Option(None, "--language", help="New language tag (pass empty string to clear)."),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated, replaces existing tags."),
    source: Optional[str] = typer.Option(None, "--source", help="New source URL or citation (pass empty string to clear)."),
    body: Optional[str] = typer.Option(None, "--body", help="Replace body inline; omit to open $EDITOR."),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Override the active profile."),
) -> None:
    """Edit fields on an existing learning. Pass flags to set; omit --body to open $EDITOR for the body."""
    load_env()
    p = profile or get_profile()
    from nyx.learn import update_learning_with_export
    from nyx.lib.format import console, print_learning

    target = _resolve_learning_id(learning_id, p)
    if target is None:
        console.print(f"[red]No learning matches '{learning_id}'.[/red]")
        raise typer.Exit(1)

    fields: dict = {}
    if title is not None:
        fields["title"] = title
    if topic is not None:
        fields["topic"] = topic
    if kind is not None:
        fields["kind"] = kind
    if language is not None:
        fields["language"] = language or None
    if source is not None:
        fields["source"] = source or None
    if tags is not None:
        fields["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

    if body is not None:
        fields["body"] = body
    elif not any(v is not None for v in [title, topic, kind, language, tags, source]):
        # No flags at all — open editor for body
        new_body = typer.edit(target.body)
        if new_body is None or new_body.strip() == target.body.strip():
            console.print("[dim]No changes.[/dim]")
            raise typer.Exit()
        fields["body"] = new_body.rstrip()

    updated = update_learning_with_export(target.id, **fields)
    print_learning(updated)


@learn_app.command("root")
def learn_root(
    path: Optional[str] = typer.Argument(None, help="New Obsidian vault root. Omit to show the current value."),
    unset: bool = typer.Option(False, "--unset", help="Clear the configured Obsidian root."),
) -> None:
    """Show or set the Obsidian vault root where learning MD files get exported."""
    load_env()
    from nyx.lib.learn_config import get_obsidian_root, set_obsidian_root, unset_obsidian_root
    from nyx.lib.format import console
    if unset:
        if unset_obsidian_root():
            console.print("[green]Cleared Obsidian root.[/green]  [dim]MD export now disabled.[/dim]")
        else:
            console.print("[dim]No Obsidian root was set.[/dim]")
        return
    if path is None:
        current = get_obsidian_root()
        if current is None:
            console.print("[yellow]No Obsidian root configured.[/yellow]  "
                          "[dim]Set with: nyx learn root <path>[/dim]")
        else:
            console.print(f"[dim]Obsidian root:[/dim] {current}")
        return
    resolved = set_obsidian_root(path)
    warn = "" if resolved.exists() else "  [yellow](path does not exist yet)[/yellow]"
    console.print(f"[green]Obsidian root set:[/green] {resolved}{warn}")


def _resolve_learning_id(prefix: str, profile: str):
    """Accept full UUID or short prefix; return matching Learning or None."""
    from nyx.memory.supabase import get_learning, list_learnings
    if len(prefix) >= 32:
        try:
            return get_learning(prefix)
        except Exception:
            return None
    matches = [item for item in list_learnings(profile, limit=200) if item.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        from nyx.lib.format import console
        console.print(f"[yellow]Ambiguous prefix — {len(matches)} matches.[/yellow]")
    return None


if __name__ == "__main__":
    app()
