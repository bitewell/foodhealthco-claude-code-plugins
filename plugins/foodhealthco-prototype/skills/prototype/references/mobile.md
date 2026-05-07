# Reference: fhs-mobile-app

The repo's `CLAUDE.md` is authoritative — read it first. This file exists only as a fallback if `CLAUDE.md` is missing.

## Stack at a glance
- Expo SDK 55, React Native 0.83, React 19 (with React Compiler — no manual `useMemo`/`useCallback` needed)
- Expo Router v7 (file-based routes in `src/app/`)
- TypeScript 5 strict
- Zustand (global state) + TanStack Query v5 (server state)
- Zod for runtime validation
- NativeWind v4 + tailwind-merge
- @react-native-firebase (modular API, not namespaced)
- Sentry, PostHog
- EAS Build + EAS Update
- Node 24 (use `nvm use`)

## Dev loop
```bash
nvm use
npm ci                 # if node_modules is missing
npx expo start         # Metro bundler — press r to reload
npx expo run:ios       # iOS sim; first build is 5–10 min, subsequent JS changes only need Metro reload
```
For iOS, requires `ios/<app>/GoogleService-Info-Dev.plist`. For Android, `android/app/google-services-dev.json`. Both gitignored.

Type-check: `npm run type-check`. Lint: `npm run lint`. Test: `npm test` (Jest + jest-expo).

## Where things go
- New screen → `src/app/` (file-based routing)
- Feature module → `src/features/<name>/{components,hooks,services}/`
- Shared UI primitives → `src/components/ui/`
- API: Zod schema in `src/schemas/` → service in `src/services/` → TanStack Query hook in the feature dir
- Constants/tokens → `src/config/constants.ts` (re-exports hex values for runtime use; tailwind config is authoritative)

## Conventions
- Use semantic Tailwind tokens (`text-primary`, `bg-surface`, `text-headline`, `p-md`). Don't hand-roll `text-[15px]`.
- Dark mode is automatic via CSS variables — no `dark:` variants needed for semantic tokens.
- Lucide icons (tree-shakable). Don't pull `Ionicons`.
- Score is always a filled color circle. Never a pill/badge/rectangle.
- Firebase: modular API only (`import { getAuth } from '@react-native-firebase/auth'`).
