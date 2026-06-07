"""Probe what qwen3 actually emits on the wire — raw chunk dump."""
from __future__ import annotations
import sys
from openai import OpenAI

OLLAMA_BASE_URL = "http://localhost:11434/v1"
MODEL = "qwen3:14b"

# Two scenarios: implicit (no directive) vs explicit /think
SCENARIOS = {
    "implicit": "You are a helpful advisor.",
    "explicit_think": "/think\n\nYou are a helpful advisor.",
    "explicit_no_think": "/no_think\n\nYou are a helpful advisor.",
}

USER_QUESTION = "where is this project located?"


def probe(label: str, system: str) -> None:
    print(f"\n{'=' * 70}\nScenario: {label}\nSystem: {system!r}\n{'=' * 70}")
    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": USER_QUESTION},
        ],
        stream=True,
    )

    chunk_count = 0
    has_reasoning = False
    has_think_tag = False
    full_content = ""
    full_reasoning = ""

    for chunk in response:
        chunk_count += 1
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue

        # Print raw chunk dict for first few chunks
        if chunk_count <= 5:
            print(f"  [chunk {chunk_count}] {chunk.model_dump_json(exclude_none=True)[:300]}")

        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if reasoning:
            has_reasoning = True
            full_reasoning += reasoning

        if delta.content:
            full_content += delta.content
            if "<think>" in delta.content or "</think>" in delta.content:
                has_think_tag = True

    print(f"\n  total chunks: {chunk_count}")
    print(f"  reasoning_content used: {has_reasoning} (len={len(full_reasoning)})")
    print(f"  <think> tag in content: {has_think_tag}")
    print(f"  content length: {len(full_content)}")
    print(f"  content preview (first 400 chars):\n  ---\n  {full_content[:400]}\n  ---")
    if full_reasoning:
        print(f"  reasoning preview (first 400 chars):\n  ---\n  {full_reasoning[:400]}\n  ---")


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for label, system in SCENARIOS.items():
        if only and label != only:
            continue
        try:
            probe(label, system)
        except Exception as e:
            print(f"  ERROR: {e}")
