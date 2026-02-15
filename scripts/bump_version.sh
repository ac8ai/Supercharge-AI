#!/bin/bash
# Bump the SuperchargeAI version across all files that contain it.
#
# Usage:
#   bash scripts/bump_version.sh <new_version>
#
# Files updated:
#   - pyproject.toml                    (PyPI package version)
#   - .claude-plugin/plugin.json        (Claude Code plugin version)
#   - .claude-plugin/marketplace.json   (marketplace registry entry)
#
# All three MUST match. session_start.sh reads plugin.json at runtime
# and pins the PyPI install to that version.

set -euo pipefail

NEW_VERSION="${1:?Usage: bump_version.sh <new_version>}"

# Validate semver format (loose check)
if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$'; then
    echo "Error: '$NEW_VERSION' doesn't look like a valid semver (e.g., 0.2.0)" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Read current version from pyproject.toml
CURRENT=$(grep -oP '^version = "\K[^"]+' "$REPO_ROOT/pyproject.toml")
if [ -z "$CURRENT" ]; then
    echo "Error: Could not read current version from pyproject.toml" >&2
    exit 1
fi

if [ "$CURRENT" = "$NEW_VERSION" ]; then
    echo "Already at version $NEW_VERSION — nothing to do."
    exit 0
fi

echo "Bumping $CURRENT → $NEW_VERSION"

# 1. pyproject.toml
sed -i "s/^version = \"$CURRENT\"/version = \"$NEW_VERSION\"/" "$REPO_ROOT/pyproject.toml"
echo "  Updated pyproject.toml"

# 2. .claude-plugin/plugin.json
sed -i "s/\"version\": \"$CURRENT\"/\"version\": \"$NEW_VERSION\"/" "$REPO_ROOT/.claude-plugin/plugin.json"
echo "  Updated .claude-plugin/plugin.json"

# 3. .claude-plugin/marketplace.json
sed -i "s/\"version\": \"$CURRENT\"/\"version\": \"$NEW_VERSION\"/" "$REPO_ROOT/.claude-plugin/marketplace.json"
echo "  Updated .claude-plugin/marketplace.json"

# Verify all three files now have the new version
PY_VER=$(grep -oP '^version = "\K[^"]+' "$REPO_ROOT/pyproject.toml")
PLUGIN_VER=$(python3 -c "import json; print(json.load(open('$REPO_ROOT/.claude-plugin/plugin.json')).get('version',''))")
MARKET_VER=$(python3 -c "import json; print(json.load(open('$REPO_ROOT/.claude-plugin/marketplace.json'))['plugins'][0].get('version',''))")

ERRORS=0
for label_ver in "pyproject.toml:$PY_VER" "plugin.json:$PLUGIN_VER" "marketplace.json:$MARKET_VER"; do
    label="${label_ver%%:*}"
    ver="${label_ver#*:}"
    if [ "$ver" != "$NEW_VERSION" ]; then
        echo "Error: $label has version '$ver', expected '$NEW_VERSION'" >&2
        ERRORS=$((ERRORS + 1))
    fi
done

if [ "$ERRORS" -gt 0 ]; then
    echo "Version mismatch detected — check files manually." >&2
    exit 1
fi

# 4. Regenerate uv.lock to pick up the new version
(cd "$REPO_ROOT" && uv lock 2>/dev/null) && echo "  Updated uv.lock" || echo "  Warning: uv lock failed — run manually"

echo "Done. All files now at $NEW_VERSION"
echo ""
echo "Next steps:"
echo "  1. git add pyproject.toml .claude-plugin/plugin.json .claude-plugin/marketplace.json uv.lock"
echo "  2. git commit -m \"Bump version to $NEW_VERSION\""
echo "  3. git tag v$NEW_VERSION"
echo "  4. Publish to PyPI: uv build && uv publish"
