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

_MENU_BG, _MENU_FG = "#1c1c1c", "#666666"
_MENU_SEL = "bg:#2a2a2a #c9a87c bold"

_INPUT_STYLE = Style.from_dict({
    "": "bold #c9a87c",
    "completion-menu": f"bg:{_MENU_BG} {_MENU_FG}",
    "completion-menu.completion": f"bg:{_MENU_BG} {_MENU_FG}",
    "completion-menu.completion.current": _MENU_SEL,
    "scrollbar.background": f"bg:{_MENU_BG}",
    "scrollbar.button": "bg:#3a3a3a",
})

_PICKER_STYLE = Style.from_dict({
    "row": f"bg:{_MENU_BG} {_MENU_FG}",
    "sel": _MENU_SEL,
    "dim": f"bg:{_MENU_BG} {_MENU_FG}",
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


def pick_from_list(header: str, rows: list[str], footer: str = "",
                   start: int = 0) -> int | None:
    """Arrow-key picker over pre-formatted rows. Returns the chosen index, or
    None if the user cancelled. Raises if there is no terminal to draw on —
    callers need to tell "chose nothing" apart from "never got asked".

    Rows arrive as plain text already laid out in columns — the picker owns
    selection and scrolling, not formatting, so the caller keeps one place where
    a row is composed.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings as KB
    from prompt_toolkit.layout import Layout, ScrollOffsets, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.containers import HSplit
    from prompt_toolkit.data_structures import Point

    state = {"sel": start, "filter": ""}

    def visible() -> list[int]:
        f = state["filter"].lower()
        return [i for i, r in enumerate(rows) if f in r.lower()] or []

    def body():
        idx = visible()
        if not idx:
            return [("class:dim", f"  no model matches '{state['filter']}'")]
        state["sel"] = min(state["sel"], len(idx) - 1)
        out = []
        for pos, i in enumerate(idx):
            style = "class:sel" if pos == state["sel"] else "class:row"
            marker = "❯ " if pos == state["sel"] else "  "
            out.append((style, f"{marker}{rows[i]}\n"))
        return out

    def cursor():
        return Point(0, state["sel"])

    control = FormattedTextControl(body, get_cursor_position=cursor, focusable=True)
    kb = KB()

    @kb.add("up")
    @kb.add("c-p")
    def _up(event):
        state["sel"] = max(0, state["sel"] - 1)

    @kb.add("down")
    @kb.add("c-n")
    def _down(event):
        state["sel"] = min(len(visible()) - 1, state["sel"] + 1)

    @kb.add("pageup")
    def _pgup(event):
        state["sel"] = max(0, state["sel"] - 10)

    @kb.add("pagedown")
    def _pgdn(event):
        state["sel"] = min(len(visible()) - 1, state["sel"] + 10)

    @kb.add("enter")
    def _accept(event):
        idx = visible()
        event.app.exit(result=idx[state["sel"]] if idx else None)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(result=None)

    @kb.add("backspace")
    def _back(event):
        state["filter"] = state["filter"][:-1]
        state["sel"] = 0

    @kb.add("<any>")
    def _type(event):
        if event.data and event.data.isprintable():
            state["filter"] += event.data
            state["sel"] = 0

    def status():
        f = f"  filter: {state['filter']}" if state["filter"] else "  ↑↓ move · type to filter · enter select · esc cancel"
        return [("class:dim", f + ("\n" + footer if footer else ""))]

    layout = Layout(HSplit([
        Window(FormattedTextControl(lambda: [("class:dim", header)]), height=header.count("\n") + 1),
        Window(control, scroll_offsets=ScrollOffsets(top=1, bottom=1)),
        Window(FormattedTextControl(status), height=2 + footer.count("\n")),
    ]))
    app = Application(layout=layout, key_bindings=kb, style=_PICKER_STYLE,
                      full_screen=False, erase_when_done=True)
    return app.run()
