"""Thin Bedrock Converse API wrapper for nyx. One file, four functions."""
from __future__ import annotations
import json
import sys
import threading
import boto3


# ── Waiting indicator ─────────────────────────────────────────────────────────

class _WaveSpinner:
    """Waveform bar that animates while waiting for the first token, then vanishes."""
    # Scrolling wave: ▁▂▃▄▅▆▇█ window moving right then left
    _WAVE = "▁▂▃▄▅▆▇█▇▆▅▄▃▂"
    _WIDTH = 6

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._cleared = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def clear(self) -> None:
        if not self._cleared:
            self._cleared = True
            self._stop.set()
            self._thread.join(timeout=0.3)
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def _run(self) -> None:
        i = 0
        wave = self._WAVE
        w = self._WIDTH
        n = len(wave)
        while not self._stop.wait(0.07):
            bar = "".join(wave[(i + j) % n] for j in range(w))
            sys.stdout.write(f"\r\033[2m{bar}\033[0m")
            sys.stdout.flush()
            i += 1


# ── Client ────────────────────────────────────────────────────────────────────

def make_client():
    """Create and return a Bedrock runtime client using AWS_PROFILE / AWS_REGION."""
    from nyx.lib.models import AWS_PROFILE, AWS_REGION
    session = boto3.Session(profile_name=AWS_PROFILE)
    return session.client("bedrock-runtime", region_name=AWS_REGION)


def to_bedrock_tools(nyx_tools: list[dict]) -> list[dict]:
    """Translate nyx/Anthropic-style { name, description, input_schema } → Bedrock toolSpec."""
    return [
        {"toolSpec": {
            "name": t["name"],
            "description": t["description"],
            "inputSchema": {"json": t["input_schema"]},
        }}
        for t in nyx_tools
    ]


def to_bedrock_tools_openai(openai_tools: list[dict]) -> list[dict]:
    """Translate OpenAI function-call { type, function: { name, description, parameters } } → Bedrock toolSpec."""
    return [
        {"toolSpec": {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "inputSchema": {"json": t["function"]["parameters"]},
        }}
        for t in openai_tools
    ]


def stream_turn(
    client,
    model_id: str,
    system_texts: list[str],
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 4096,
    surface: str = "",
    project: str = "",
    profile: str = "",
) -> tuple[str, list[dict]]:
    """Stream one assistant turn. Prints text to stdout as it arrives.

    Shows a waveform waiting indicator until the first token arrives.
    Returns (text, tool_uses) where:
      tool_uses = [{"id": str, "name": str, "input": dict}]
    """
    spinner = _WaveSpinner()
    spinner.start()

    response = client.converse_stream(
        modelId=model_id,
        system=[{"text": t} for t in system_texts if t],
        messages=messages,
        toolConfig={"tools": tools},
        inferenceConfig={"maxTokens": max_tokens},
    )

    text_parts: list[str] = []
    tool_uses: list[dict] = []
    current_tool: dict | None = None
    current_input = ""
    usage: dict = {}

    try:
        for event in response["stream"]:
            if "contentBlockStart" in event:
                spinner.clear()
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    current_tool = {
                        "id": start["toolUse"]["toolUseId"],
                        "name": start["toolUse"]["name"],
                    }
                    current_input = ""
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    spinner.clear()
                    print(delta["text"], end="", flush=True)
                    text_parts.append(delta["text"])
                elif "toolUse" in delta:
                    current_input += delta["toolUse"].get("input", "")
            elif "contentBlockStop" in event and current_tool is not None:
                current_tool["input"] = json.loads(current_input) if current_input else {}
                tool_uses.append(current_tool)
                current_tool, current_input = None, ""
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
    except KeyboardInterrupt:
        spinner.clear()
        print()
        raise

    spinner.clear()
    print()
    if usage:
        in_tok  = usage.get("inputTokens",  0)
        out_tok = usage.get("outputTokens", 0)
        label   = f"  ↑ {in_tok:,}  ↓ {out_tok:,}  "
        width   = len(label)
        sys.stdout.write(
            f"\033[2m╭{'─' * width}╮\n"
            f"│{label}│\n"
            f"╰{'─' * width}╯\033[0m\n"
        )
        sys.stdout.flush()
        from nyx.lib.usage import log_usage
        log_usage(profile, surface, project, model_id, in_tok, out_tok)
    return "".join(text_parts), tool_uses


def invoke_turn(
    client,
    model_id: str,
    system_texts: list[str],
    messages: list[dict],
    tools: list[dict] | None = None,
    force_tool_name: str | None = None,
    max_tokens: int = 800,
    surface: str = "",
    project: str = "",
    profile: str = "",
) -> tuple[str, list[dict]]:
    """Non-streaming single turn (used by _compact and _distill).

    Returns (text, tool_uses) where:
      tool_uses = [{"id": str, "name": str, "input": dict}]
    """
    kwargs: dict = dict(
        modelId=model_id,
        system=[{"text": t} for t in system_texts if t],
        messages=messages,
        inferenceConfig={"maxTokens": max_tokens},
    )
    if tools:
        tool_config: dict = {"tools": tools}
        if force_tool_name:
            tool_config["toolChoice"] = {"tool": {"name": force_tool_name}}
        kwargs["toolConfig"] = tool_config

    response = client.converse(**kwargs)
    content = response["output"]["message"]["content"]
    text = next((c["text"] for c in content if "text" in c), "")
    tool_uses = [
        {
            "id": c["toolUse"]["toolUseId"],
            "name": c["toolUse"]["name"],
            "input": c["toolUse"]["input"],
        }
        for c in content if "toolUse" in c
    ]
    u = response.get("usage", {})
    if u:
        from nyx.lib.usage import log_usage
        log_usage(profile, surface, project, model_id, u.get("inputTokens", 0), u.get("outputTokens", 0))
    return text, tool_uses
