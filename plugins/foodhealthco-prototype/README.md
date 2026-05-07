# foodhealthco-prototype

Bootstrap a prototyping session for the FoodHealth Score mobile app or Chrome extension.

## What it does

When you say something like "let's prototype a tweak to the home screen" inside `fhs-mobile-app` or `foodhealth-score-extension`, this skill:

1. Detects which repo you're in (via `package.json#name`).
2. Reads the repo's `CLAUDE.md` for conventions.
3. Runs prereq checks (Node version, dependencies installed, Firebase config files for mobile).
4. Asks before starting the dev server (`npx expo start` or `npm run dev`).
5. Points you at the right place for the change (Expo Router, feature dirs, etc.).
6. Runs `npm run type-check && npm run lint` before handing back.

## What it doesn't do

- Doesn't run `git commit` / `git push`.
- Doesn't auto-install dependencies or create missing config files.
- Doesn't replace the repo's `CLAUDE.md` — it defers to it.
- Doesn't trigger on production fixes, bug investigations, or unrelated repos.

## Files

```
skills/prototype/
├── SKILL.md                # The skill itself
└── references/
    ├── mobile.md           # Fallback for fhs-mobile-app if its CLAUDE.md is missing
    └── extension.md        # Fallback for foodhealth-score-extension
```

## Updating

After editing `SKILL.md`, run `/reload-plugins` in any Claude Code session and bump the version in `.claude-plugin/plugin.json`.
