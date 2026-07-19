from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_USAGE_PATH = Path.home() / ".nyx" / "usage.jsonl"

# Approximate cost per 1M tokens (input, output) in USD
_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001":        (0.80,   4.00),
    "claude-sonnet-4-6":                (3.00,  15.00),
    "claude-opus-4-7":                  (15.00, 75.00),
    "gpt-4o-mini":                      (0.15,   0.60),
    "gpt-4o":                           (2.50,  10.00),
    "gpt-5":                            (10.00, 40.00),
    "qwen.qwen3-235b-a22b-2507-v1:0":  (0.40,   1.20),
    "deepseek/deepseek-v4-flash":       (0.09,   0.18),
    "moonshotai/kimi-k2.7-code":        (0.66,   3.41),
    "anthropic/claude-sonnet-5":        (2.00,  10.00),
}


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    ip, op = _PRICES.get(model, (0.0, 0.0))
    return (in_tok * ip + out_tok * op) / 1_000_000


def record(
    provider: str,
    tier: str,
    model: str,
    surface: str,
    in_tok: int,
    out_tok: int,
    secs: float = 0.0,
    ttft: float = 0.0,
) -> None:
    _USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "project":  _project(),
        "provider": provider,
        "tier":     tier,
        "model":    model,
        "surface":  surface,
        "in_tok":   in_tok,
        "out_tok":  out_tok,
        "cost":     _cost(model, in_tok, out_tok),
        "secs":     round(secs, 3),
        "ttft":     round(ttft, 3),
    }
    with _USAGE_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _project() -> str:
    from nyx.lib.config import project_name
    return project_name()


def load() -> list[dict]:
    if not _USAGE_PATH.exists():
        return []
    rows = []
    for line in _USAGE_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def summarise(records: list[dict]) -> dict:
    def _bucket() -> dict:
        return {"in_tok": 0, "out_tok": 0, "cost": 0.0,
                "calls": 0, "secs": 0.0, "ttft": 0.0}

    total: dict        = _bucket()
    by_project: dict   = defaultdict(_bucket)
    by_provider: dict  = defaultdict(_bucket)
    by_model: dict     = defaultdict(_bucket)

    for r in records:
        i = r.get("in_tok", 0)
        o = r.get("out_tok", 0)
        c = r.get("cost", 0.0)
        secs = r.get("secs")  # absent on rows written before timing existed
        for bucket in (
            total,
            by_project[r.get("project") or "unknown"],
            by_provider[r.get("provider") or "unknown"],
            by_model[r.get("model") or "unknown"],
        ):
            bucket["in_tok"] += i
            bucket["out_tok"] += o
            bucket["cost"]   += c
            if secs is not None:
                bucket["calls"] += 1
                bucket["secs"]  += secs
                bucket["ttft"]  += r.get("ttft", 0.0)

    return {
        "total":       total,
        "by_project":  dict(by_project),
        "by_provider": dict(by_provider),
        "by_model":    dict(by_model),
    }
