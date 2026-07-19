# nyx

AI coding assistant. One session, all modes as slash commands.

```
nyx        → open REPL (project = current directory)
nyx cost   → show token usage and cost breakdown
```

## Setup

```bash
git clone <repo> ~/Documents/nyx_v1
cd ~/Documents/nyx_v1
uv sync
```

Add API keys to `.env`:

```env
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
AWS_REGION=ap-southeast-2   # for qwen235b via Bedrock
```

Configure model and Obsidian vault path in `~/.nyx/config.json` (created on first run):

```json
{ "model": "anthropic", "tier": "balanced", "vault": "/path/to/your/obsidian" }
```

## Modes

One agent, one continuous conversation. Modes change permissions and focus — switching never resets context. Edits are always shown as diffs; `/plan` blocks them, `/chat` asks per edit (`y` / `a`lways / `n`o — or type feedback, which goes back to the model), `/code` and `/test` auto-approve.

Mode commands take an optional inline message: `/code fix the failing test` switches and runs it.

## Slash commands

| Command | Action |
|---|---|
| `/chat` | Default mode — edits need per-edit approval |
| `/plan` | Plan mode — read-only, produces specs saved to vault |
| `/code` | Code mode — edits auto-approved (shown as diffs) |
| `/test` | Test mode — TDD, writes failing tests first |
| `/auto` | Toggle auto-approve for edits in chat mode |
| `/learn` | Switch to learn mode — collect notes |
| `/write` | (in learn mode) Format notes → save to vault as `learnings/<slug>.md` |
| `/compact` | Summarise history → save episodic to vault → clear history |
| `/practice [pattern] [lang]` | Save current discussion to `best-practices/<pattern>/<lang>.md` |
| `/update-architecture` | Generate `architecture/client/*.md` and `architecture/server/*.md` in repo |
| `/model <provider>[:<tier>]` | Switch model — e.g. `/model openai:top` |
| `/ideas` | Print saved ideas for the current project |

## Models

| Provider | Tiers |
|---|---|
| `anthropic` | `cheap` (haiku) · `balanced` (sonnet) · `top` (opus) |
| `openai` | `cheap` (gpt-4o-mini) · `balanced` (gpt-4o) · `top` (gpt-5) |
| `gemini` | `cheap` (flash-lite) · `balanced` (flash) · `top` (pro) |
| `qwen235b` | (single model via AWS Bedrock) |

## Memory

Everything lives in your Obsidian vault:

```
<vault>/
  projects/<name>/
    specs/           ← saved by plan agent
    decisions.md     ← appended by save_decision tool
    ideas.md         ← appended by save_idea tool
  episodic/<name>/
    YYYY-MM-DD.md    ← written on /compact and on clean exit
  best-practices/<pattern>/
    <language>.md    ← written by /practice
  learnings/
    <slug>.md        ← written by /write in learn mode
```

Architecture docs live in the repo:

```
<project>/
  architecture/
    client/<feature>.md
    server/<service>.md
```

## Project layout

```
nyx/
  cli.py        entry point
  repl.py       single agent loop: modes, approval gate, slash commands
  lib/
    config.py   ~/.nyx/config.json + project = cwd
    llm.py      LangChain model factory + stream_turn()
    tools.py    tool registry (fs + memory tools)
    memory.py   Obsidian read/write
    usage.py    cost tracking (~/.nyx/usage.jsonl)
    format.py   Rich output
    chat_input.py  prompt-toolkit input
  agents/
    nyx.md      base system prompt (always active)
    plan.md     plan mode overlay
    code.md     code mode overlay
    test.md     test mode overlay
```
