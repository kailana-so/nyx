from __future__ import annotations
import sys

import typer
from dotenv import load_dotenv

app = typer.Typer(
    name="nyx",
    add_completion=False,
    pretty_exceptions_show_locals=False,
    invoke_without_command=True,
    context_settings={"allow_extra_args": True},
)


def _inline_prompt() -> str | None:
    if not sys.stdin.isatty():
        return sys.stdin.read().strip() or None
    tokens = [t for t in sys.argv[1:] if not t.startswith("-") and t != "cost"]
    return " ".join(tokens) if tokens else None


@app.callback()
def main(ctx: typer.Context) -> None:
    """nyx — AI coding assistant. Chat, plan, code, and learn from your project directory."""
    if ctx.invoked_subcommand is not None:
        return
    load_dotenv()
    prompt = _inline_prompt()
    from nyx.repl import run_repl
    run_repl(prompt=prompt)


@app.command()
def cost() -> None:
    """Show token usage and cost breakdown."""
    load_dotenv()
    from nyx.lib.usage import load, summarise
    from nyx.lib.format import print_cost_summary, console
    records = load()
    if not records:
        console.print("[dim]No usage data yet — run nyx first.[/dim]")
        return
    print_cost_summary(summarise(records))


if __name__ == "__main__":
    app()
