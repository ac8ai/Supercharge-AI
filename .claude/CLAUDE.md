# SuperchargeAI — Project Instructions

## Versioning

Version is tracked across 3 files (pyproject.toml, plugin.json, marketplace.json) plus uv.lock. **Always use the bump script** — never edit version strings manually:

```bash
bash scripts/bump_version.sh <new_version>   # e.g. 0.3.1, 0.4.0b1
```

The script validates PEP 440 format, updates all files, regenerates uv.lock, and prints the git commands to commit + tag.

## Branching & Release Strategy

- `main` — stable releases only. Marketplace installs pull from here (default branch).
- `beta` — pre-release testing. Developer installs from local clone tracking this branch.
- Feature branches merge into `beta` for testing, then `beta` merges into `main` for stable release.

### Version format (PEP 440)

- Stable: `0.3.2`, `0.4.0`
- Pre-release: `0.4.0b1` (beta), `0.4.0a1` (alpha), `0.4.0rc1` (release candidate)
- Never use SemVer pre-release format (`0.4.0-beta.1`) — PyPI rejects it.

### Local beta install

Clone the repo, checkout `beta`, and install editable:

```bash
git clone https://github.com/ac8ai/Supercharge-AI.git
cd Supercharge-AI && git checkout beta
uv tool install -e . --force
```

The session_start hook auto-pulls the latest beta on each session.