"""Multi-line input helper for chat surfaces.

Bindings:
- Enter            → submit the current buffer
- Ctrl+J           → insert a newline. Pair with a Ghostty config line so
                     Shift+Enter sends the same byte:
                         keybind = shift+enter=text:\\n
                     With that config, Shift+Enter behaves as "newline".
- Tab              → insert a literal tab
- Ctrl+C / Ctrl+D  → standard cancel / EOF, raised to caller as before

prompt_toolkit doesn't natively expose `s-enter` as a key spec — terminals don't
send a distinct byte for Shift+Enter without the kitty keyboard protocol or a
terminal-side keybind, so we route the feature via Ctrl+J instead.
"""
from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

# Input text rendered in bold white so user turns are visually distinct from model output
_INPUT_STYLE = Style.from_dict({"": "bold #c9a87c"})


def _make_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.app.current_buffer.validate_and_handle()

    @kb.add("c-j")
    def _newline_cj(event):
        event.app.current_buffer.insert_text("\n")

    @kb.add("tab")
    def _tab(event):
        event.app.current_buffer.insert_text("\t")

    return kb


def make_chat_session() -> PromptSession:
    """Construct a PromptSession configured for chat input. One per chat surface."""
    return PromptSession(
        multiline=True,
        key_bindings=_make_bindings(),
        history=InMemoryHistory(),
        style=_INPUT_STYLE,
    )


def chat_prompt(session: PromptSession, prompt_ansi: str) -> str:
    """Run the prompt with an ANSI-formatted prompt string. Returns the entered text."""
    return session.prompt(ANSI(prompt_ansi))
