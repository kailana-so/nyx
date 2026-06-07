"""Token usage logging (Supabase) and cost aggregation."""
from __future__ import annotations
from collections import defaultdict


def log_usage(profile: str, surface: str, project: str, model: str,
              in_tok: int, out_tok: int) -> None:
    """Synchronous insert — happens after the response is already printed so latency is fine."""
    try:
        from nyx.lib.models import BEDROCK_INPUT_PRICE_PER_M, BEDROCK_OUTPUT_PRICE_PER_M
        from nyx.memory.supabase import insert_usage
        cost = (in_tok * BEDROCK_INPUT_PRICE_PER_M + out_tok * BEDROCK_OUTPUT_PRICE_PER_M) / 1_000_000
        insert_usage(
            profile or "personal",
            project or "misc",
            surface or "unknown",
            model,
            in_tok, out_tok, cost,
        )
    except Exception as e:
        from nyx.lib.format import console
        console.print(f"[bold red][usage] insert failed:[/bold red] {e}")


def load_usage(profile: str) -> list[dict]:
    """Load all usage records for this profile from Supabase."""
    from nyx.memory.supabase import load_usage_records
    records = load_usage_records(profile)
    return [
        {
            "ts":      r.created_at,
            "surface": r.agent,
            "project": r.project,
            "model":   r.model,
            "in_tok":  r.input_tokens,
            "out_tok": r.output_tokens,
            "cost":    float(r.cost_usd),
        }
        for r in records
    ]


def summarise(records: list[dict]) -> dict:
    """Aggregate by project, surface (agent), and model."""
    def _bucket() -> dict:
        return {"in_tok": 0, "out_tok": 0, "cost": 0.0}

    total          = _bucket()
    by_project: dict = defaultdict(_bucket)
    by_surface: dict = defaultdict(_bucket)
    by_model:   dict = defaultdict(_bucket)

    for r in records:
        i = r.get("in_tok", 0)
        o = r.get("out_tok", 0)
        c = r.get("cost", 0.0)
        proj  = r.get("project") or "—"
        surf  = r.get("surface") or "—"
        model = r.get("model")   or "—"

        for bucket in (total, by_project[proj], by_surface[surf], by_model[model]):
            bucket["in_tok"] += i
            bucket["out_tok"] += o
            bucket["cost"]   += c

    return {
        "total":      total,
        "by_project": dict(by_project),
        "by_surface": dict(by_surface),
        "by_model":   dict(by_model),
    }
