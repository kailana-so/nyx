from __future__ import annotations
import json
import os
import re
import time
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_aws import ChatBedrock, ChatBedrockConverse
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)
from rich.text import Text

from nyx.lib.format import console

# Per-provider capabilities — the one place vendor differences live.
#   native_tools: honours API tool_use blocks (False → tool calling injected
#                 into the system prompt and parsed back out of the text).
#   reasoning:    emits thinking (inline <think> or reasoning content blocks).
#   cache:        "anthropic" = explicit cache_control blocks
#                 "bedrock"   = Converse cachePoint (recognised; no current provider)
#                 "auto"      = provider caches prefixes itself, nothing to send
#                 "none"      = no caching — the turn loop prunes history instead
_DEFAULT_CAPS = {"native_tools": True, "reasoning": False, "cache": "none"}

# Provider → tier → model id + factory + caps
MODELS: dict[str, dict[str, Any]] = {
    "openai": {
        "cheap":    "gpt-4o-mini",
        "balanced": "gpt-4o",
        "top":      "gpt-5",
        "llm": lambda m: ChatOpenAI(model=m, api_key=os.getenv("OPENAI_API_KEY")),
        "caps": {"native_tools": True, "reasoning": False, "cache": "auto"},
        "blurb": "fast all-rounder, cheap side tasks",
    },
    "qwen235b": {
        "cheap":    "qwen.qwen3-235b-a22b-2507-v1:0",
        "balanced": "qwen.qwen3-235b-a22b-2507-v1:0",
        "top":      "qwen.qwen3-235b-a22b-2507-v1:0",
        "llm": lambda m: ChatBedrock(
            model=m,
            region_name=os.getenv("AWS_REGION", "ap-southeast-2"),
            model_kwargs={"enable_thinking": False},
        ),
        "caps": {"native_tools": False, "reasoning": True, "cache": "none"},
        "blurb": "experimental — slow, text-parsed tools",
    },
    "deepseek": {
        "cheap":    "deepseek.v3-v1:0",
        "balanced": "deepseek.v3.2",
        "top":      "deepseek.v3.2",
        "llm": lambda m: ChatBedrockConverse(
            model=m,
            region_name=os.getenv("AWS_REGION", "ap-southeast-2"),
        ),
        "caps": {"native_tools": True, "reasoning": True, "cache": "none"},
        "blurb": "very slow — background summaries only",
    },
    "devstral": {
        "cheap":    "mistral.devstral-2-123b",
        "balanced": "mistral.devstral-2-123b",
        "top":      "mistral.devstral-2-123b",
        "llm": lambda m: ChatBedrockConverse(
            model=m,
            region_name=os.getenv("AWS_REGION", "ap-southeast-2"),
        ),
        "caps": {"native_tools": True, "reasoning": False, "cache": "none"},
        "blurb": "daily coding driver, streams fast",
    },
    # One OpenAI-compatible endpoint fronting every vendor — models are routed
    # ids, so changing a tier is a one-line edit. https://openrouter.ai/models
    "openrouter": {
        "cheap":    "deepseek/deepseek-v4-flash",
        "balanced": "moonshotai/kimi-k2.7-code",
        "top":      "anthropic/claude-sonnet-5",
        "llm": lambda m: ChatOpenAI(
            model=m,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
        ),
        "caps": {"native_tools": True, "reasoning": True, "cache": "auto"},
        "blurb": "any model, one key, host failover",
    },
}


def get_model(provider: str, tier: str = "balanced"):
    """Return a LangChain chat model for the given provider and tier."""
    config = MODELS[provider]
    return config["llm"](config[tier])


def get_caps(provider: str) -> dict:
    return {**_DEFAULT_CAPS, **MODELS.get(provider, {}).get("caps", {})}


def extract_text(content) -> str:
    if isinstance(content, str):
        return _strip_thinking(content)
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _record_usage(provider: str, tier: str, surface: str, usage_metadata,
                  secs: float = 0.0, ttft: float = 0.0) -> None:
    if not provider or not usage_metadata:
        return
    from nyx.lib import usage as _usage
    _usage.record(
        provider=provider,
        tier=tier,
        model=MODELS.get(provider, {}).get(tier, ""),
        surface=surface,
        in_tok=usage_metadata.get("input_tokens", 0),
        out_tok=usage_metadata.get("output_tokens", 0),
        secs=secs,
        ttft=ttft,
    )


# ── Qwen text-based tool calling ─────────────────────────────────────────────

_QWEN_TOOL_HEADER = """

# Tools

You have access to tools. To call a tool, output ONLY this block — nothing else on those lines:

<tool_call>
{"name": "<tool_name>", "arguments": {<args as JSON>}}
</tool_call>

Do NOT write markdown code blocks. Do NOT narrate what you will do. Call the tool directly and wait for the result. Available tools:

"""


def _build_qwen_tool_prompt(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        fn = t["function"]
        lines.append(f"**{fn['name']}**: {fn['description']}\n  Parameters: {json.dumps(fn['parameters'])}")
    return _QWEN_TOOL_HEADER + "\n\n".join(lines)


def _parse_qwen_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Extract <tool_call> blocks from Qwen text response."""
    calls = []
    pattern = r"<tool_call>\s*(\{.*?\})\s*</tool_call>"
    for i, m in enumerate(re.finditer(pattern, text, re.DOTALL)):
        try:
            data = json.loads(m.group(1))
            calls.append({
                "id":        f"qtc_{i}",
                "name":      data["name"],
                "args":      data.get("arguments", data.get("args", {})),
                "type":      "tool_call",
                "text_call": True,
            })
        except (ValueError, KeyError):
            pass
    clean = re.sub(pattern, "", text, flags=re.DOTALL).strip()
    return clean, calls


def make_text_tool_result(name: str, result: str) -> HumanMessage:
    """Tool result message for text-based tool calling (Qwen)."""
    return HumanMessage(content=f"<tool_response>\n{json.dumps({'name': name, 'result': result})}\n</tool_response>")


# ── Streaming helpers ─────────────────────────────────────────────────────────

def _chunk_parts(content) -> tuple[str, str]:
    """(text, reasoning) of one streamed chunk. Inline <think> tags are left in
    the text channel (that's _ThinkFilter's job); reasoning delivered as separate
    content blocks (deepseek Converse, anthropic thinking) lands in the second."""
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return "", ""
    text: list[str] = []
    think: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            text.append(b.get("text", ""))
        elif kind == "thinking":
            think.append(b.get("thinking", ""))
        elif kind == "reasoning_content":
            rc = b.get("reasoning_content")
            think.append(rc.get("text", "") if isinstance(rc, dict) else str(rc or ""))
    return "".join(text), "".join(think)


class _ThinkFilter:
    """Splits <think>...</think> spans out of streamed text, even when a tag is
    split across chunks. Reasoning models (qwen, deepseek) emit these inline.
    feed() returns (visible, think) so callers can render thinking dimmed."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self._pend = ""
        self._in_think = False

    def feed(self, s: str) -> tuple[str, str]:
        self._pend += s
        out: list[str] = []
        think: list[str] = []
        while True:
            if self._in_think:
                i = self._pend.find(self.CLOSE)
                if i == -1:
                    # keep just enough tail to catch a split closing tag
                    keep = len(self.CLOSE) - 1
                    think.append(self._pend[:-keep])
                    self._pend = self._pend[-keep:]
                    break
                think.append(self._pend[:i])
                self._pend = self._pend[i + len(self.CLOSE):]
                self._in_think = False
            else:
                i = self._pend.find(self.OPEN)
                if i == -1:
                    # emit everything except a possible partial opening tag
                    keep = 0
                    for k in range(min(len(self.OPEN) - 1, len(self._pend)), 0, -1):
                        if self.OPEN.startswith(self._pend[-k:]):
                            keep = k
                            break
                    out.append(self._pend[:-keep] if keep else self._pend)
                    self._pend = self._pend[-keep:] if keep else ""
                    break
                out.append(self._pend[:i])
                self._pend = self._pend[i + len(self.OPEN):]
                self._in_think = True
        return "".join(out), "".join(think)

    def flush(self) -> tuple[str, str]:
        text, think = ("", self._pend) if self._in_think else (self._pend, "")
        self._pend = ""
        return text, think


class _Spinner:
    """'thinking… Ns' status with elapsed seconds, shown until first output."""

    def __init__(self):
        self._t0 = time.time()
        self._status = console.status(self, spinner="dots")
        self._live = False

    def __rich__(self) -> Text:
        return Text(f"thinking… {time.time() - self._t0:.0f}s", style="dim")

    def start(self) -> None:
        self._status.start()
        self._live = True

    def stop(self) -> None:
        if self._live:
            self._status.stop()
            self._live = False


# ── Main turn function ────────────────────────────────────────────────────────

def _cache_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Copy of messages with an anthropic cache breakpoint on the newest message
    that can safely carry one (non-empty Human/AI content). The breakpoint
    advances every round, so all prior history becomes a cache hit."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, (HumanMessage, AIMessage)):
            continue
        c = m.content
        if isinstance(c, str) and c.strip():
            blocks = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(c, list) and c and isinstance(c[-1], dict) and c[-1].get("type") == "text" and c[-1].get("text"):
            blocks = list(c[:-1]) + [{**c[-1], "cache_control": {"type": "ephemeral"}}]
        else:
            continue
        clone = m.model_copy(update={"content": blocks})
        return messages[:i] + [clone] + messages[i + 1:]
    return messages


def stream_turn(
    model,
    system: str | tuple[str, str],
    messages: list[BaseMessage],
    tools: list[dict],
    *,
    provider: str = "",
    tier: str = "",
    surface: str = "chat",
) -> tuple[str, list[dict]]:
    """Run one model turn, streaming text to stdout as it arrives.
    Returns (text, tool_calls).

    system may be a (stable, volatile) tuple — cache-capable providers mark the
    stable part cacheable; everyone else gets the two joined.

    tool_calls format: [{"id": str, "name": str, "args": dict, "type": "tool_call"}]
    Text-based tool calls (Qwen) include "text_call": True — callers must use
    make_text_tool_result() instead of make_tool_message() for these.
    """
    caps = get_caps(provider)
    stable, volatile = system if isinstance(system, tuple) else (system, "")

    # ── Text tool calling: inject tools into prompt, parse <tool_call> blocks ──
    if not caps["native_tools"] and tools:
        augmented = stable + volatile + _build_qwen_tool_prompt(tools)
        all_messages: list[BaseMessage] = [SystemMessage(content=augmented)] + messages
        t0 = time.time()
        spinner = _Spinner()
        spinner.start()
        try:
            result = model.invoke(all_messages)
        except Exception:
            # Bedrock/langchain_aws raises ValidationError when qwen returns a
            # tool-only response with no text (content=None). Treat as empty turn.
            return "", []
        finally:
            spinner.stop()
        secs = time.time() - t0
        text = extract_text(result.content)
        clean_text, tool_calls = _parse_qwen_tool_calls(text)
        if clean_text:
            print(clean_text)
        if hasattr(result, "usage_metadata"):
            _record_usage(provider, tier, surface, result.usage_metadata,
                          secs=secs, ttft=secs)
        return clean_text, tool_calls

    # ── Standard path: stream text live, accumulate tool calls ────────────────
    if caps["cache"] == "anthropic":
        sys_content: list = [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]
        if volatile:
            sys_content.append({"type": "text", "text": volatile})
        all_messages = [SystemMessage(content=sys_content)] + _cache_messages(messages)
    else:
        all_messages = [SystemMessage(content=stable + volatile)] + messages
    bound = model.bind_tools(tools) if tools else model

    filt = _ThinkFilter()
    parts: list[str] = []
    dimmed = False  # think text was printed — needs the trailing newline too
    spinner = _Spinner()

    _DIM, _RESET = "\033[2m", "\033[0m"

    def _emit(text: str, think: str = "") -> None:
        nonlocal dimmed
        if think:
            spinner.stop()
            print(f"{_DIM}{think}{_RESET}", end="", flush=True)
            dimmed = True
        if text:
            spinner.stop()
            print(text, end="", flush=True)
            parts.append(text)

    acc = None
    t0 = time.time()
    ttft: float | None = None
    spinner.start()
    try:
        for chunk in bound.stream(all_messages):
            if ttft is None:
                ttft = time.time() - t0
            acc = chunk if acc is None else acc + chunk
            text, block_think = _chunk_parts(chunk.content)
            vis, tag_think = filt.feed(text)
            _emit(vis, block_think + tag_think)
        _emit(*filt.flush())
    except KeyboardInterrupt:
        if parts or dimmed:
            print()
        raise
    except Exception:
        if acc is not None or parts:
            raise
        # Provider rejected streaming for this request — fall back to blocking.
        acc = bound.invoke(all_messages)
        text, block_think = _chunk_parts(acc.content)
        vis, tag_think = filt.feed(text)
        _emit(vis, block_think + tag_think)
        _emit(*filt.flush())
    finally:
        spinner.stop()

    if parts or dimmed:
        print()

    tool_calls = [
        {"id": tc.get("id") or f"tc_{i}", "name": tc["name"], "args": tc["args"], "type": "tool_call"}
        for i, tc in enumerate(getattr(acc, "tool_calls", None) or [])
    ]
    if acc is not None and getattr(acc, "usage_metadata", None):
        secs = time.time() - t0
        _record_usage(provider, tier, surface, acc.usage_metadata,
                      secs=secs, ttft=ttft if ttft is not None else secs)
    return "".join(parts), tool_calls


def quiet_turn(model, system: str, text: str, *,
               provider: str = "", tier: str = "", surface: str = "") -> str:
    """One blocking model call with no terminal output — for background work."""
    t0 = time.time()
    result = model.invoke([SystemMessage(content=system), HumanMessage(content=text)])
    secs = time.time() - t0
    if getattr(result, "usage_metadata", None):
        _record_usage(provider, tier, surface, result.usage_metadata, secs=secs, ttft=secs)
    return extract_text(result.content)


def make_ai_message(text: str, tool_calls: list[dict]) -> AIMessage:
    """Build an AIMessage. Text-based tool calls (text_call=True) are excluded
    from tool_calls so they don't create orphaned toolUse blocks in Bedrock."""
    api_calls = [tc for tc in tool_calls if not tc.get("text_call")]
    return AIMessage(content=text or "", tool_calls=api_calls)


def make_tool_message(result: str, tool_call_id: str) -> ToolMessage:
    return ToolMessage(content=result, tool_call_id=tool_call_id)


def make_human_message(text: str) -> HumanMessage:
    return HumanMessage(content=text)
