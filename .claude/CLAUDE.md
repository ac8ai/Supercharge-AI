# SuperchargeAI — Project Instructions

## Versioning

Version is tracked across 3 files (pyproject.toml, plugin.json, marketplace.json) plus uv.lock. **Always use the bump script** — never edit version strings manually:

```bash
bash scripts/bump_version.sh <new_version>   # e.g. 0.3.1
```

The script validates semver, updates all files, regenerates uv.lock, and prints the git commands to commit + tag.