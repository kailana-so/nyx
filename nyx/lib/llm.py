from __future__ import annotations
import json
import os
import random
import re
import time
import urllib.request
from pathlib import Path
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

# Tiers name the JOB, not the price. Price is not a single quality ordering —
# the strongest reasoner is not the best at summarising notes, it is just slower
# and dearer at it. What actually varies between our surfaces:
#   worker  — compact, learn, practice, visualise. Unattended, one call, big
#             input, no tools. Wants cheap and a large context window.
#   driver  — chat, code, test. Dozens of turns, many tool calls, watched live.
#             Wants tool-call reliability, a working cache, and fast streaming.
#             Reasoning depth matters less here than not fumbling a patch.
#   thinker — plan, validate-plan, code-validator. Rare, short, and expensive to
#             get wrong: a bad spec costs a whole implementation. Wants judgement.
TIERS = ("worker", "driver", "thinker")

# Provider → tier → model id + factory + caps
MODELS: dict[str, dict[str, Any]] = {
    "qwen235b": {
        "worker":  "qwen.qwen3-235b-a22b-2507-v1:0",
        "driver":  "qwen.qwen3-235b-a22b-2507-v1:0",
        "thinker": "qwen.qwen3-235b-a22b-2507-v1:0",
        "llm": lambda m: ChatBedrock(
            model=m,
            region_name=os.getenv("AWS_REGION", "ap-southeast-2"),
            model_kwargs={"enable_thinking": False},
        ),
        "caps": {"native_tools": False, "reasoning": True, "cache": "none"},
        "blurb": "AWS Bedrock · tool calls parsed from text, no caching",
    },
    "deepseek": {
        "worker":  "deepseek.v3-v1:0",
        "driver":  "deepseek.v3.2",
        "thinker": "deepseek.v3.2",
        "llm": lambda m: ChatBedrockConverse(
            model=m,
            region_name=os.getenv("AWS_REGION", "ap-southeast-2"),
        ),
        "caps": {"native_tools": True, "reasoning": True, "cache": "none"},
        "blurb": "AWS Bedrock · no caching",
    },
    "devstral": {
        "worker":  "mistral.devstral-2-123b",
        "driver":  "mistral.devstral-2-123b",
        "thinker": "mistral.devstral-2-123b",
        "llm": lambda m: ChatBedrockConverse(
            model=m,
            region_name=os.getenv("AWS_REGION", "ap-southeast-2"),
        ),
        "caps": {"native_tools": True, "reasoning": False, "cache": "none"},
        "blurb": "AWS Bedrock · no caching",
    },
    # One OpenAI-compatible endpoint fronting every vendor — models are routed
    # ids, so changing a tier is a one-line edit. https://openrouter.ai/models
    "openrouter": {
        # worker:  cheapest credible model with a 1M window — compact feeds it
        #          a whole session and wants nothing clever back.
        # driver:  MCP-Atlas 76.0, the best published tool-calling score here,
        #          and the failure mode that matters is state loss deep in a
        #          long tool loop, which costs more than the token price gap.
        # thinker: SWE-bench Verified 86.6%, highest of the candidates.
        "worker":  "deepseek/deepseek-v4-flash",
        "driver":  "moonshotai/kimi-k2.7-code",
        "thinker": "x-ai/grok-4.5",
        "llm": lambda m: ChatOpenAI(
            model=m,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
        ),
        # routed: caps come from the model id, not this entry — see _ROUTED_CAPS.
        "routed": True,
        "caps": {"native_tools": True, "reasoning": True, "cache": "none"},
        "blurb": "any model, one key, host failover",
    },
}


def model_id(provider: str, tier: str) -> str:
    """The concrete model for a provider. `tier` is a tier name, or a raw model
    id passed straight through — /model openrouter:anthropic/claude-opus-4-8.
    A router's whole point is reaching models we never enumerated, so a tier
    table that can't be escaped would be the wrong shape."""
    return MODELS.get(provider, {}).get(tier) or tier


def get_model(provider: str, tier: str = "driver"):
    """Return a LangChain chat model for the given provider and tier."""
    return MODELS[provider]["llm"](model_id(provider, tier))


# A router fronts many models, so capabilities belong to the routed model, not
# to the provider. Keyed by model-id prefix; anything unmatched keeps the
# conservative default, which costs a cache miss rather than a broken request.
#
# What decides "cache" is the model's pricing entry at
# https://openrouter.ai/api/v1/models — a cache-WRITE price means caching is
# explicit and we send the blocks; a cache-READ price alone means the host
# caches prefixes itself and we send nothing; neither means no caching, and the
# turn loop prunes history instead of paying to resend it.
_ROUTED_CAPS: dict[str, dict] = {
    "anthropic/":  {"reasoning": True, "cache": "anthropic"},  # read + write priced
    # Read priced, no write → the host caches prefixes itself.
    "deepseek/":   {"reasoning": True, "cache": "auto"},
    "moonshotai/": {"reasoning": True, "cache": "auto"},
    "openai/":     {"reasoning": True, "cache": "auto"},
    "google/":     {"reasoning": True, "cache": "auto"},
    "x-ai/":       {"reasoning": True, "cache": "auto"},
    "z-ai/":       {"reasoning": True, "cache": "auto"},
    "minimax/":    {"reasoning": True, "cache": "auto"},
    "xiaomi/":     {"reasoning": True, "cache": "auto"},
    "stepfun/":    {"reasoning": True, "cache": "auto"},
}


def get_caps(provider: str, tier: str = "") -> dict:
    """Capabilities for a provider, narrowed to the routed model when the
    provider is a router and the tier says which model that is."""
    config = MODELS.get(provider, {})
    caps = {**_DEFAULT_CAPS, **config.get("caps", {})}
    if not config.get("routed") or not tier:
        return caps
    model = model_id(provider, tier)
    routed = next((v for k, v in _ROUTED_CAPS.items() if model.startswith(k)), {})
    return {**caps, **routed}


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
        model=model_id(provider, tier),
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

    _WORDS = [
        "thinking", "considering", "wondering", "reasoning", "reflecting", "day dreamering",
        "analysing", "evaluating", "big pondering", "contemplating", "deliberating", "fishing"
        "speculating", "inferring", "deducing", "musing", "debating",
        "little questioning", "reviewing", "inspecting", "tinkering", "mulling",
        "infering", "estimating", "calculating", "weighing", "comparing",
        "reviewing", "revisiting", "reconsidering", "recalling", "remembering",
        "imagining", "visualising", "hypothesising", "theorising", "gone doing",
        "let me thinking", "extrapolating", "one seconding", "checking", "okey dokeying",
        "observing", "perceiving", "noodle scratching", "acknowledging", "streaming", "noggining"
    ]

    def __init__(self):
        self._t0 = time.time()
        self._status = console.status(self, spinner="dots")
        self._live = False
        self._word = random.choice(self._WORDS)

    def __rich__(self) -> Text:
        return Text(f"{self._word}… {time.time() - self._t0:.0f}s", style="dim")

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
    caps = get_caps(provider, tier)
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


# ── OpenRouter catalogue ──────────────────────────────────────────────────────

_CATALOGUE_PATH = Path.home() / ".nyx" / "openrouter-models.json"
_CATALOGUE_TTL = 60 * 60 * 24  # a day — the catalogue moves, but not hourly


# Third-party indices, not our ranking. Matched by substring because the exact
# key names are OpenRouter's to change, and a missing score must degrade to
# "unrated" rather than to a number we made up.
_SCORE_AXES = {"coding": "coding", "thinking": "intelligence", "agentic": "agentic"}


def _scores(benchmarks: dict) -> dict[str, float]:
    aa = benchmarks.get("artificial_analysis") or {}
    flat = {k.lower(): v for k, v in aa.items() if isinstance(v, (int, float))}
    out = {}
    for axis, needle in _SCORE_AXES.items():
        hit = next((v for k, v in flat.items() if needle in k), None)
        if hit is not None:
            out[axis] = float(hit)
    return out


def _blurb(model: str, description: str, limit: int = 110) -> str:
    """The vendor's own one-liner. Their words, not ours — a hand-written blurb
    per model is opinion that goes stale. Stored generously and cut to fit at
    render time, so a wide terminal isn't limited by what we cached."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", description).strip()
    # "GLM 5.2 is a large-scale reasoning model from Z.ai." → "large-scale
    # reasoning model from Z.ai" — the name is already in the row beside it.
    text = re.sub(r"^.{0,40}?\s+is\s+(?:a|an|the)\s+", "", text, count=1)
    text = text.split(". ")[0].rstrip(".")
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def catalogue(category: str = "", force: bool = False) -> list[dict]:
    """[{id, in, out, cache_read, context}] from OpenRouter, newest fetch cached
    on disk. Returns [] on any failure — a model picker that dies without a
    network is worse than one that falls back to what you already know."""
    if not force and _CATALOGUE_PATH.exists():
        age = time.time() - _CATALOGUE_PATH.stat().st_mtime
        if age < _CATALOGUE_TTL:
            try:
                return json.loads(_CATALOGUE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    try:
        # No category filter by default: a category view silently omits models
        # we ourselves default to, and the picker filters interactively anyway.
        url = "https://openrouter.ai/api/v1/models" + (f"?category={category}" if category else "")
        req = urllib.request.Request(url, headers={"User-Agent": "nyx"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = json.loads(r.read())["data"]
    except Exception:
        try:
            return json.loads(_CATALOGUE_PATH.read_text())  # stale beats nothing
        except (json.JSONDecodeError, OSError, FileNotFoundError):
            return []
    models = []
    for m in raw:
        p = m.get("pricing") or {}
        models.append({
            "id": m.get("id", ""),
            "in": float(p.get("prompt") or 0) * 1e6,
            "out": float(p.get("completion") or 0) * 1e6,
            "cache_read": float(p.get("input_cache_read") or 0) * 1e6,
            "context": m.get("context_length") or 0,
            "blurb": _blurb(m.get("id", ""), m.get("description") or ""),
            **_scores(m.get("benchmarks") or {}),
        })
    models.sort(key=lambda m: m["in"])
    try:
        _CATALOGUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CATALOGUE_PATH.write_text(json.dumps(models))
    except OSError:
        pass
    return models
