#!/usr/bin/env bash
# SessionStart hook: install project deps in Claude Code cloud sessions only.
# Locally this is a no-op — manage your own venv with `uv sync`.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/..}"

# Python 3.12+ required; use the VM's interpreter if new enough, else let uv fetch one.
uv sync --frozen 2>/dev/null || uv sync
echo "openvigie: deps synced ($(uv run python --version))"
