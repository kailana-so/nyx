from __future__ import annotations
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


def print_cost_summary(summary: dict) -> None:
    total = summary["total"]

    groups = [
        (title, sorted(
            [(n, d) for n, d in summary[key].items()],
            key=lambda x: -x[1]["cost"],
        ))
        for key, title in (
            ("by_project",  "project"),
            ("by_provider", "provider"),
            ("by_model",    "model"),
        )
        if any(n != "—" for n in summary.get(key, {}))
    ]

    def _lat(d: dict) -> tuple[str, str]:
        calls = d.get("calls", 0)
        if not calls:
            return "—", "—"
        return f"{d['secs'] / calls:.1f}s", f"{d['ttft'] / calls:.1f}s"

    t = Table(box=box.SIMPLE, show_header=True, header_style="dim", pad_edge=False, expand=False)
    t.add_column("", style="dim", no_wrap=True)
    t.add_column("↑ in", justify="right", style="dim", no_wrap=True)
    t.add_column("↓ out", justify="right", style="dim", no_wrap=True)
    t.add_column("avg", justify="right", style="dim", no_wrap=True)
    t.add_column("ttft", justify="right", style="dim", no_wrap=True)
    t.add_column("cost", justify="right", style="green", no_wrap=True)

    t.add_row("[bold]total[/bold]",
              f"{total['in_tok']:,}",
              f"{total['out_tok']:,}",
              *_lat(total),
              f"[bold]${total['cost']:.4f}[/bold]")

    for title, rows in groups:
        if not rows:
            continue
        t.add_row(f"[dim]{title}[/dim]", "", "", "", "", "")
        for name, d in rows:
            t.add_row(
                f"  {name}",
                f"{d['in_tok']:,}",
                f"{d['out_tok']:,}",
                *_lat(d),
                f"${d['cost']:.4f}",
            )

    console.print(t)
