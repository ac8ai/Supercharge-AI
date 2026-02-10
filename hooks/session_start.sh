#!/bin/bash
# [SuperchargeAI] SessionStart hook - auto-install and inject orchestrator prompt

# Capture stdin (hook input JSON)
INPUT=$(cat)

# Install uv if missing
if ! command -v uv &> /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null
    export PATH="$HOME/.local/bin:$PATH"
fi

# Install supercharge CLI if missing
if ! command -v supercharge &> /dev/null; then
    SUPERCHARGE_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
    uv tool install "${SUPERCHARGE_ROOT}" 2>/dev/null
fi

# Delegate to CLI (handles resume skip + prompt injection)
echo "$INPUT" | supercharge hook-session-start
