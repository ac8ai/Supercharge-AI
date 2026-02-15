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

# Install or upgrade supercharge CLI from PyPI (pinned to plugin version)
if [ -z "$PLUGIN_VERSION" ]; then
    # Cannot determine target version — skip install, delegate to CLI if present
    if ! command -v supercharge &> /dev/null; then
        echo '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"[SuperchargeAI] Could not determine plugin version and CLI is not installed. Run: uv tool install supercharge-ai"}}'
        exit 0
    fi
elif ! command -v supercharge &> /dev/null; then
    uv tool install "supercharge-ai==${PLUGIN_VERSION}" 2>/dev/null
else
    INSTALLED_VERSION=$(supercharge version 2>/dev/null)
    if [ "$INSTALLED_VERSION" != "$PLUGIN_VERSION" ]; then
        uv tool install "supercharge-ai==${PLUGIN_VERSION}" 2>/dev/null

        # Clean old plugin cache versions (Claude Code doesn't do this automatically)
        PLUGIN_CACHE="$HOME/.claude/plugins/cache"
        if [ -d "$PLUGIN_CACHE" ]; then
            for marketplace_dir in "$PLUGIN_CACHE"/*/supercharge-ai/; do
                [ -d "$marketplace_dir" ] || continue
                for version_dir in "$marketplace_dir"*/; do
                    [ -d "$version_dir" ] || continue
                    # Keep the directory matching PLUGIN_VERSION, remove the rest
                    dir_version=$(basename "$version_dir")
                    if [ "$dir_version" != "$PLUGIN_VERSION" ]; then
                        rm -rf "$version_dir"
                    fi
                done
            done
        fi
    fi
fi

# Delegate to CLI (handles resume skip + prompt injection)
echo "$INPUT" | supercharge hook-session-start
