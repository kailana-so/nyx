#!/usr/bin/env bash
# nyx_v1 setup automation. Idempotent; safe to re-run after `git pull`.
#
# Steps:
#   1. uv sync (Python deps)
#   2. symlink bin/nyx into ~/.local/bin/nyx
#   3. scaffold .env from .env.example if .env doesn't exist
#   4. print next steps (keys, ollama pull, migrations)

set -euo pipefail

# Resolve repo root from this script's location, regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
TARGET_BIN="$HOME/.local/bin"

cd "$REPO_DIR"

# ── 1. Prerequisites ──────────────────────────────────────────────────────────

if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv is required but not found." >&2
  echo "  Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "⚠ ollama not found — only required for nyx code/test/learn --local."
  echo "  Install later with: brew install ollama"
fi

# ── 2. Python deps ────────────────────────────────────────────────────────────

echo "→ uv sync"
uv sync

# ── 3. Symlink bin/nyx ────────────────────────────────────────────────────────

mkdir -p "$TARGET_BIN"
src="$REPO_DIR/bin/nyx"
link="$TARGET_BIN/nyx"

if [[ -L "$link" ]]; then
  ln -sf "$src" "$link"
  echo "↻ ~/.local/bin/nyx (symlink refreshed)"
elif [[ -e "$link" ]]; then
  backup="$link.bak.$(date +%s)"
  mv "$link" "$backup"
  ln -s "$src" "$link"
  echo "↺ ~/.local/bin/nyx (existing file backed up to $(basename "$backup"))"
else
  ln -s "$src" "$link"
  echo "+ ~/.local/bin/nyx"
fi

# ── 4. Scaffold .env if missing ──────────────────────────────────────────────

if [[ -f "$REPO_DIR/.env" ]]; then
  echo "✓ .env already exists, leaving alone"
else
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  echo "+ .env (copied from .env.example — fill in the keys)"
fi

# ── 5. Next steps ────────────────────────────────────────────────────────────

cat <<EOF

Done. Next steps:

  1. Fill in $REPO_DIR/.env:
       ANTHROPIC_API_KEY  (console.anthropic.com)
       SUPABASE_URL       (your Supabase project settings)
       SUPABASE_KEY       (anon/public key)
       VOYAGE_API_KEY     (dash.voyageai.com)

  2. If you'll use --local paths (nyx code/test/learn --local):
       brew install ollama   # if not yet installed
       ollama pull qwen3:14b

  3. If this is a fresh Supabase project, paste each migration into the SQL
     editor in numeric order:
       scripts/migrations/001_explicit_spec_fields.sql
       scripts/migrations/002_learnings.sql
       scripts/migrations/003_learnings_rls.sql

  4. Verify: nyx --help (works from any directory)

Make sure ~/.local/bin is on \$PATH. Re-running this script is always safe.
EOF
