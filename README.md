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

Add API keys to `.env` — only for the providers you actually use:

```env
OPENROUTER_API_KEY=...       # openrouter (any model, one key)
OPENAI_API_KEY=...           # openai
AWS_REGION=ap-southeast-2    # devstral, deepseek, qwen235b via Bedrock
```

Configure model and Obsidian vault path in `~/.nyx/config.json` (created on first run):

```json
{ "model": "devstral", "tier": "balanced", "vault": "/path/to/your/obsidian" }
```

## Modes

One agent, one continuous conversation. Modes change permissions and focus — switching never resets context. Edits are always shown as diffs; read-only modes block them, `/chat` asks per edit (`y` / `a`lways / `n`o — or type feedback, which goes back to the model), `/code` and `/test` auto-approve.

Mode commands take an optional inline message: `/code fix the failing test` switches and runs it.

## Slash commands

| Command | Action |
|---|---|
| `/chat` | Default mode — edits need per-edit approval (`/advise` is an alias) |
| `/plan` | Read-only — produces a verified spec, saved to `specs/` in the repo |
| `/code` | Edits auto-approved (shown as diffs) |
| `/test` | TDD — writes failing tests first, auto-approved |
| `/validate-plan` | Read-only — checks a spec's numbers, docs, scope, footprint |
| `/code-validator` | Read-only — craftsmanship review of written code |
| `/visualise` | Mermaid diagrams from code or descriptions |
| `/auto` | Toggle auto-approve for edits in chat mode |
| `/learn` | Switch to learn mode — silently collects your notes, turn by turn |
| `/write` | (in learn mode) Enhance notes → save to `<vault>/<topic>/<title>.md` |
| `/decide` | Record a decision to `decisions.md` (prompts for title and body) |
| `/ideas` | Print saved ideas for the current project |
| `/compact` | Summarise history → write episodic session → clear history |
| `/practice [pattern] [lang]` | Save current discussion to `best-practices/<pattern>/<lang>.md` |
| `/update-architecture` | Generate `architecture/**/*.md` in the repo |
| `/model <provider>[:<tier>]` | Switch model — e.g. `/model openrouter:top` |

Attach an image to a message with `@path`: `explain this @~/Desktop/screenshot.png`.

## Models

| Provider | Tiers |
|---|---|
| `openrouter` | `cheap` · `balanced` · `top` — any model, one key, host failover |
| `openai` | `cheap` (gpt-4o-mini) · `balanced` (gpt-4o) · `top` (gpt-5) |
| `devstral` | single model via AWS Bedrock — daily coding driver, streams fast |
| `deepseek` | via AWS Bedrock — very slow, background summaries only |
| `qwen235b` | via AWS Bedrock — experimental, tools parsed from text |

Per-provider differences (native tool calling, reasoning output, prompt caching) live in one `caps` dict in `lib/llm.py`. Nothing else branches on provider.

## Memory

Project state lives in the **repo**, so it travels with the code across machines:

```
<project>/
  nyx.md            ← project context, loaded into every prompt
  decisions.md      ← appended by save_decision / /decide
  ideas.md          ← appended by save_idea
  specs/<slug>.md   ← written by plan mode
  architecture/
    <area>/<name>.md  ← written by /update-architecture
```

Session memory and notes live in the **vault**:

```
<vault>/
  episodic/<project>/
    YYYYMMDD-HHMMSS.md  ← one file per session, written on /compact and clean exit
  best-practices/<pattern>/
    <language>.md       ← written by /practice
  <topic>/<title>.md    ← notes written by /write in learn mode
```

Episodic recall is recency — the last few sessions, no embeddings. Reaching further back is what `decisions.md` and `ideas.md` are for. Notes are found by listing topics, then reading the relevant one.

## Project layout

```
nyx/
  cli.py        entry point
  repl.py       single agent loop: modes, approval gate, slash commands
  lib/
    config.py   ~/.nyx/config.json + project = cwd
    llm.py      LangChain model factory, per-provider caps, stream_turn()
    tools.py    tool registry (fs, memory, dependency tools)
    memory.py   repo project state + vault read/write
    notes.py    Obsidian topic notes
    deps.py     package existence checks against npm / PyPI
    image.py    image attachments
    usage.py    cost tracking (~/.nyx/usage.jsonl)
    format.py   Rich output
    chat_input.py  prompt-toolkit input
  agents/
    nyx.md              base system prompt (always active)
    plan.md             plan mode overlay
    code.md             code mode overlay
    test.md             test mode overlay
    validate-plan.md    spec review overlay
    code-validator.md   code review overlay
    visualise.md        diagram overlay
```
