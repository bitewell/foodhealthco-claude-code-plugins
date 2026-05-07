# Reference: foodhealth-score-extension

The repo's `CLAUDE.md` is authoritative — read it first. This file is a fallback / first-time setup helper.

## Stack at a glance
- React 18 + TypeScript 5.7
- Vite 6 + CRXJS (Manifest V3)
- Tailwind CSS 3.4, classes **prefixed `fhc-`** to avoid clashing with retailer pages
- Zustand 5 (state) + TanStack Query v5 (server) + Zod (validation)
- Firebase 11 (web SDK; service worker + offscreen document architecture)
- Vitest + Testing Library
- Lucide React (web, not RN)
- Node 24

## Dev loop
```bash
nvm use
npm ci                 # if node_modules is missing
npm run dev            # tsc -b && vite build --mode development --watch
```

First-time browser setup (one-time):
1. Open `chrome://extensions`
2. Enable Developer mode (top-right toggle)
3. Click "Load unpacked"
4. Select the `dist/` directory at the repo root
5. Pin the extension if you want it visible in the toolbar

After code changes:
- Background/popup/options changes → click the reload ⟳ icon on the extension card
- Content-script changes → reload the card AND refresh any open retailer tab (currently `*.target.com/*`)

Type-check: `npm run type-check`. Lint: `npm run lint`. Test: `npm test` (Vitest).

## Where things go
- UI components → `src/components/<feature>/` (e.g., `auth/`, `sidebar/`, `share-modal/`)
- Zustand stores → `src/stores/` (`auth`, `cart`, `ui`, `swapFlow`)
- API/business logic → `src/services/`
- Content scripts → `src/content/` (entry runs at `document_start` for early interception)
- Extension pages → `src/pages/` (`popup/`, `welcome/`, `onboarding/`, `offscreen/`, `auth-callback/`)
- Service worker → `src/background/index.ts`
- Multi-retailer registry → `src/config/stores.ts`
- Manifest source of truth → `manifest.config.ts` (CRXJS reads this; matched URLs live here)

## Conventions
- Tailwind classes prefixed with `fhc-` — keep the prefix.
- Sidebar uses Shadow DOM for CSS isolation — don't reach into it from the outside.
- Firebase uses an offscreen document (MV3 service workers can't use Firebase Auth directly).
- API services validate responses with Zod; throw `ApiClientError` on failure.
- Don't commit `dist/` — it's a build output.

## Common tripwires
- `npm run dev` failing immediately → usually a TypeScript error in `tsc -b`; the watch-mode log will show it.
- "Service worker registration failed" in the extensions page → check `src/background/index.ts` for syntax errors and reload the card.
- Content script not running on a retailer page → confirm the URL matches `manifest.config.ts` `content_scripts[].matches`.
