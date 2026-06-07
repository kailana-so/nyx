# nyx

Local-first AI development assistant. Claude for thinking, Qwen3 for building.

```
nyx chat    → advisor session (Claude) — memory, specs, decisions
nyx code    → coding agent (Qwen3, local) — implements a spec
nyx test    → test agent (Qwen3, local) — writes failing tests first
nyx ideas   → browse and update the ideas parking lot
```

## How it works

You talk to the advisor in one Ghostty pane. When a spec is approved it gets saved to Supabase. You open a second pane when you're ready and run `nyx code` — no auto-spawning, no magic. The coding agent runs Qwen3 locally with full tool use (read, write, bash) and streams its thinking live.

Memory persists across sessions via Supabase + Voyage AI embeddings. The same Supabase project can be shared with a work machine so decisions and context follow you.

## Setup

```bash
git clone <repo-url> ~/Documents/nyx_v1   # any path is fine; install.sh self-locates
cd ~/Documents/nyx_v1
./scripts/install.sh
```

`install.sh` runs `uv sync`, symlinks `bin/nyx` into `~/.local/bin/`, scaffolds `.env` from `.env.example`, and prints next steps (fill in keys, pull `qwen3:14b` if you'll use `--local`, paste the migrations into a fresh Supabase project). Re-running is always safe.

If you're already using lilith_v2, reuse the same `SUPABASE_URL`, `SUPABASE_KEY`, and `VOYAGE_API_KEY` — nyx reads from the same tables.

## Personal vs work scoping

nyx scopes data by `NYX_PROFILE`. Set the default per machine in `.env`:

| Machine | `NYX_PROFILE` |
|---|---|
| Home | `personal` |
| Work | `work` |

The same Supabase project backs both — projects, specs, decisions, ideas, learnings are tagged with the profile that created them. Default queries filter to the active profile.

To **cross-fetch** from the other scope (e.g. read a work-project decision while working personally), pass `--profile <other>` to the relevant subcommand:

```bash
nyx specs --profile work          # see work specs from a personal session
nyx ideas --profile work          # cross-scope idea browsing
nyx learn list --profile work     # search work learnings
```

Data follows you across machines (one Supabase project, profile-tagged rows); day-to-day commands stay scoped to whichever context you're actually working in.

## Workflow

```
# Pane 1 — advisor
nyx chat

# Talk through what you want to build.
# Advisor drafts a spec, you approve it.
# Spec is saved, advisor prints the ID.

# Pane 2 — open a Ghostty split, cd to your project
nyx code          # shows pending specs, pick one
# or
nyx code --spec-id <uuid>

# Qwen3 reads the spec, explores the codebase, writes code, runs tests.
# Streams thinking + tool calls live. Calls done() when finished.
```

### Advisor slash commands

| Command | Action |
|---------|--------|
| `/compact` | Summarise and compress conversation history |
| `/ideas` | Show ideas inline |

### Ideas statuses

`parked` → `exploring` → `adopted` / `dropped`

Update from the ideas command: `u 2 exploring`

## Project structure

```
nyx/
├── chat.py       advisor loop
├── agent.py      Qwen3 tool-use loop (coding + test)
├── cli.py        CLI entry point
├── memory/
│   ├── supabase.py   all DB ops
│   └── embed.py      Voyage AI embeddings
├── tools/
│   └── fs.py         read_file, write_file, run_bash, list_dir
├── agents/
│   ├── advisor.md    Claude system prompt
│   ├── coding.md     Qwen3 coding agent prompt
│   └── test.md       Qwen3 test agent prompt
└── lib/
    ├── models.py     model constants
    ├── format.py     Rich output helpers
    └── session.py    profile/project from env
```

## Switching projects

```bash
NYX_PROJECT=my-app nyx chat
# or set in .env
```
