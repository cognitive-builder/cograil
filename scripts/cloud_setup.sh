#!/usr/bin/env bash
# Paste this into the Claude Code cloud environment's setup script for cognitive-builder/cograil.
# It runs before Claude Code launches and is cached for later sessions.
set -euo pipefail
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12
uv sync --all-extras --dev
# Local Postgres is not available in cloud sessions; unit tests run without it.
# Integration tests run in CI against the service container.
echo "cograil cloud environment ready"
