#!/bin/bash
# [SuperchargeAI] SessionStart hook - auto-install and inject orchestrator prompt

# Capture stdin (hook input JSON)
INPUT=$(cat)

# Install uv if missing
if ! command -v uv &> /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null
    export PATH="$HOME/.local/bin:$PATH"
fi

SUPERCHARGE_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# Read plugin version from plugin.json
PLUGIN_VERSION=""
if [ -f "${SUPERCHARGE_ROOT}/.claude-plugin/plugin.json" ]; then
    PLUGIN_VERSION=$(python3 -c "import json; print(json.load(open('${SUPERCHARGE_ROOT}/.claude-plugin/plugin.json')).get('version',''))" 2>/dev/null)
fi

# Install or upgrade supercharge CLI from PyPI
if ! command -v supercharge &> /dev/null; then
    uv tool install supercharge-ai 2>/dev/null
else
    # Upgrade if installed version doesn't match plugin version
    INSTALLED_VERSION=$(supercharge version 2>/dev/null)
    if [ -n "$PLUGIN_VERSION" ] && [ "$INSTALLED_VERSION" != "$PLUGIN_VERSION" ]; then
        uv tool upgrade supercharge-ai 2>/dev/null
    fi
fi

# Delegate to CLI (handles resume skip + prompt injection)
echo "$INPUT" | supercharge hook-session-start
