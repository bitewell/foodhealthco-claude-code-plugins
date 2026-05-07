---
name: prototype
description: |
  Use when the user wants to prototype, sketch, try out, or build a feature/change in the
  FoodHealth Co mobile app or Chrome extension — phrases like "let's prototype X", "spin up the
  extension/app to try Y", "set me up to work on Z in the app/extension", or "translate this
  Figma frame to <screen>". Detects the repo (fhs-mobile-app vs foodhealth-score-extension),
  runs prereq checks, starts the right dev server (with explicit user confirmation), and
  translates Figma frames to code via the Figma MCP when requested. Do NOT use for production
  fixes, bug investigations, or work outside these two repos.
---

# FoodHealth Co prototyping

This skill bootstraps a prototyping session in one of two repos:

- **`fhs-mobile-app`** — Expo + React Native (iOS/Android)
- **`foodhealth-score-extension`** — Vite + CRXJS Chrome extension (Manifest V3)

Follow the steps below in order. Stop and ask the user if any step's preconditions aren't met.

## 1. Detect the repo

Read `package.json#name` in the cwd:

| `name` | branch |
|---|---|
| `fhs-mobile-app` | mobile |
| `foodhealth-score-extension` | extension |
| anything else | tell the user this skill is mobile/extension-only and stop. |

If there is no `package.json`, the user is not in a JS project — say so and stop.

## 2. Read the repo's `CLAUDE.md`

Before proposing any code, read the repo-local `CLAUDE.md` for conventions, design tokens, and patterns. The `CLAUDE.md` is **authoritative** over anything in this skill.

If the repo has no `CLAUDE.md`, fall back to `references/<repo>.md` in this skill directory:
- mobile → `references/mobile.md`
- extension → `references/extension.md`

Do not duplicate `CLAUDE.md` content into your responses — reference it.

## 3. Prereq checks (read-only)

Run these checks without modifying anything. Report missing items as a checklist; never auto-create or auto-install.

**Both repos:**
- `node -v` matches `.nvmrc` (currently 24). If `nvm` is the user's shell tool, suggest `nvm use`.
- `node_modules/` exists. If absent, suggest `npm ci` (don't run it without confirmation).
- Working tree state via `git status --short`. If dirty, surface it so the user knows what they're starting from.

**Mobile only:**
- `ios/<app>/GoogleService-Info-Dev.plist` exists.
- `android/app/google-services-dev.json` exists.
- These are gitignored — Alex/the user has to provide them locally.

**Extension only:**
- Note: `dist/` may not exist yet on a fresh checkout. The first `npm run dev` will create it.

## 4. Start the dev loop — only after explicit user confirmation

Ask: "Want me to start the dev server in the background?" Only proceed on a clear "yes".

**Mobile (`fhs-mobile-app`):**
```bash
npx expo start    # Metro bundler; press r to reload, m for menu
```
Run in background. Then offer:
```bash
npx expo run:ios  # First build is 5–10 min (gRPC compile). After that, subsequent JS changes only need Metro reload.
```
Warn the user before running `expo run:ios` if it's a fresh checkout.

**Extension (`foodhealth-score-extension`):**
```bash
npm run dev       # tsc -b && vite build --mode development --watch
```
Run in background. Then walk the user through:
1. Open `chrome://extensions`
2. Enable Developer mode (top-right)
3. Click "Load unpacked"
4. Point at `dist/` in this repo
5. After the first load, you only need to click the reload icon on the extension card after rebuilds. Content-script changes also need a tab refresh.

## 5. Locate where the change goes

Use the repo's `CLAUDE.md` first; the conventions below are a fallback.

**Mobile:**
- New screens → `src/app/` (Expo Router file-based routing)
- Feature UI → `src/features/<name>/components/`
- Shared UI → `src/components/ui/`
- API: Zod schema → `src/services/<name>-api.ts` → TanStack Query hook in the feature dir
- Styling: NativeWind classes using semantic tokens (`text-primary`, `bg-surface`, `text-headline`); see `src/global.css` and `tailwind.config.js`

**Extension:**
- UI components → `src/components/<feature>/`
- Zustand stores → `src/stores/`
- API/business logic → `src/services/`
- Content scripts → `src/content/`
- Extension pages (popup, onboarding, offscreen, auth-callback) → `src/pages/`
- Multi-retailer registry → `src/config/stores.ts` (currently `target.com` only)
- Tailwind classes are prefixed `fhc-` to avoid clashing with retailer pages — keep the prefix.
- Sidebar uses Shadow DOM for CSS isolation — don't reach into it from outside.

## 6. Verify before handing back

Once the user is happy with the prototype, run:

```bash
npm run type-check
npm run lint
```

Surface failures as-is. Do not auto-fix unless the user asks. If the user requested tests, also run `npm test`.

## 7. Never run git mutations automatically

This skill never runs `git add`, `git commit`, `git push`, or any branch-changing command. Wait for an explicit user instruction.

**Exception:** the Figma flow (next section) creates a new branch when translating a *new* frame, because the workflow is explicitly "build this on a branch." It still never pushes.

## 8. Figma-driven workflow (when applicable)

If the user provides a Figma URL or says "translate this Figma frame", "build this Figma design", etc., switch to the procedure in [`references/figma.md`](references/figma.md). The short version:

- **Mobile app only** for now (extension is a follow-up).
- Read `.claude/figma-map.json` (the frame manifest) at the repo root to decide if this is a new translate or a re-translate of an existing frame.
- Pull design context via Figma MCP (`get_design_context`, `get_variable_defs`, `get_screenshot`, `get_code_connect_map`) before writing code.
- Cross-check against `CLAUDE.md` design rules — flag off-token colors, off-scale spacing, score-as-non-circle, etc.
- Use Code Connect mappings; fall back to NativeWind + tokens if a component isn't mapped, and flag the gap.
- For new frames: create on a new branch named `prototype/<frame-kebab>`. For existing frames: targeted edits, preserve code-only changes.
- Update the manifest, run `type-check` + `lint`, hand back with a list of branch, files, and unmapped components.

---

## Failure modes to flag clearly

- **Wrong Node version** → `nvm use` (don't try to switch silently).
- **Missing Firebase configs** (mobile) → ask the user to grab them; don't proceed with `expo run:ios`.
- **Stale `dist/`** (extension) → reload the extension card in `chrome://extensions` and refresh any open retailer tabs.
- **`expo start` says "Metro already running"** → another Metro process exists; tell the user, don't kill it.
- **`npm run dev` fails on TypeScript errors** (extension) → the watch mode runs `tsc -b` first; show the errors, don't suppress.
