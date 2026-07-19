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

from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

_INPUT_STYLE = Style.from_dict({
    "": "bold #c9a87c",
    "completion-menu": "bg:#1c1c1c #666666",
    "completion-menu.completion": "bg:#1c1c1c #666666",
    "completion-menu.completion.current": "bg:#2a2a2a #c9a87c bold",
    "scrollbar.background": "bg:#1c1c1c",
    "scrollbar.button": "bg:#3a3a3a",
})

_SLASH_COMMANDS = [
    "/chat",
    "/plan",
    "/code",
    "/test",
    "/auto",
    "/learn",
    "/write",
    "/compact",
    "/practice",
    "/update-architecture",
    "/decide",
    "/model",
    "/ideas",
]


class _SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd in _SLASH_COMMANDS:
            if cmd.startswith(text):
                yield Completion(cmd, start_position=-len(text), display=cmd)


def _make_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.app.current_buffer.validate_and_handle()

    @kb.add("c-j")
    def _newline_cj(event):
        event.app.current_buffer.insert_text("\n")

    # Shift+Enter in terminals that send the "modify other keys" CSI sequence
    # (e.g. Ghostty sends ^[[27;2;13~)
    @kb.add("escape", "[", "2", "7", ";", "2", ";", "1", "3", "~")
    def _newline_shift_enter(event):
        event.app.current_buffer.insert_text("\n")

    @kb.add("tab")
    def _tab(event):
        event.app.current_buffer.insert_text("\t")

    return kb


def make_chat_session() -> PromptSession:
    """Construct a PromptSession configured for chat input. One per chat surface."""
    history_path = Path.home() / ".nyx" / "history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        multiline=True,
        key_bindings=_make_bindings(),
        history=FileHistory(str(history_path)),
        style=_INPUT_STYLE,
        completer=_SlashCompleter(),
        complete_while_typing=True,
    )


def chat_prompt(session: PromptSession, prompt_ansi: str) -> str:
    """Run the prompt with an ANSI-formatted prompt string. Returns the entered text."""
    return session.prompt(ANSI(prompt_ansi))
