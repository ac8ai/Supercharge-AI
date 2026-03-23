---
name: bump-version
description: Bump the project version across all tracked files, commit, and tag. Use when releasing a new version.
disable-model-invocation: true
argument-hint: <new_version>
---

Bump the project version to `$ARGUMENTS`.

Version is tracked across 3 files (pyproject.toml, plugin.json, marketplace.json) plus uv.lock. Never edit version strings manually.

## Steps

1. Run the bump script:
   ```bash
   bash scripts/bump_version.sh $ARGUMENTS
   ```

2. If the script succeeds, commit and tag:
   ```bash
   git add pyproject.toml .claude-plugin/plugin.json .claude-plugin/marketplace.json uv.lock
   git commit -m "Bump version to $ARGUMENTS"
   git tag v$ARGUMENTS
   ```

3. Do NOT push unless the user explicitly asks.

## Version format (PEP 440)

- Stable: `0.3.2`, `0.4.0`
- Pre-release: `0.4.0b1` (beta), `0.4.0a1` (alpha), `0.4.0rc1` (release candidate)
- Never use SemVer pre-release format (`0.4.0-beta.1`) — PyPI rejects it.
