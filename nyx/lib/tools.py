"""Shared tool-format conversion helpers."""
from __future__ import annotations


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Translate nyx/Anthropic-style { name, description, input_schema } → OpenAI function-call format."""
    return [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }}
        for t in tools
    ]
