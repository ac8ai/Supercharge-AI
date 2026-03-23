# SuperchargeAI — Project Instructions

## Versioning

Use `/bump-version <new_version>` to bump. Never edit version strings manually.

## Branching & Release Strategy

- `main` — stable releases only. Marketplace installs pull from here (default branch).
- `beta` — pre-release testing. Developer installs from local clone tracking this branch.
- Feature branches merge into `beta` for testing, then `beta` merges into `main` for stable release.

### Local beta install

Clone the repo, checkout `beta`, and install editable:

```bash
git clone https://github.com/ac8ai/Supercharge-AI.git
cd Supercharge-AI && git checkout beta
uv tool install -e . --force
```

The session_start hook auto-pulls the latest beta on each session.
Supercharge-AI: @/workspaces/Supercharge-AI/prompts/claude-md.md
