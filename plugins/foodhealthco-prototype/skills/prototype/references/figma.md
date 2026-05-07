# Figma → code workflow (mobile app)

When the user gives a Figma URL or says "translate this Figma frame", "build this design", etc., follow this procedure. **Mobile app only** (`fhs-mobile-app`) for now. The Chrome extension is a follow-up.

## Prerequisites (check, don't auto-fix)

1. **Figma MCP installed** — verify by listing available tools; look for `mcp__Figma__*` (or equivalent). If absent, tell the user to set up the Figma MCP server (Figma → Personal access token → Claude Code MCP config) and stop.
2. **The user is in `fhs-mobile-app`** — if not, stop.
3. **Frame URL or node ID present** — Figma URLs look like `https://www.figma.com/design/<fileKey>/<title>?node-id=<nodeId>`. Extract `fileKey` and `nodeId`. If the user gave only a file URL with no node ID, ask which frame.

## Workflow

### 1. Read the frame manifest

Look for `.claude/figma-map.json` at the repo root. If it doesn't exist, that's fine — you'll create it on first translate.

Manifest format:
```json
{
  "version": 1,
  "fileKey": "abc123XYZ",
  "frames": {
    "1:234": {
      "name": "Home – Default",
      "path": "src/app/(tabs)/index.tsx",
      "lastTranslatedAt": "2026-04-29T12:00:00Z"
    }
  }
}
```

- **`fileKey`** is the Figma file. Manifest is per-file (the mobile app likely has one file). If the URL's `fileKey` doesn't match the manifest's, warn the user and stop — multi-file support is a follow-up.
- **`frames`** maps Figma node ID → the screen/file it renders.

### 2. Decide: new frame or existing frame?

- **Existing** (node ID is in `frames`): this is a re-translate. The goal is *targeted edits* to the existing file, not a regeneration. Read the file first, then apply the smallest set of changes needed to match the new Figma frame.
- **New** (node ID is absent): this is a first translate. Ask the user where the screen should live (e.g., `src/app/(tabs)/community.tsx`). Don't guess.

### 3. Read design context from Figma

For both new and existing flows, fetch:

- `mcp__Figma__get_design_context` — node tree, layout, layers, text content
- `mcp__Figma__get_variable_defs` — design tokens used in the frame
- `mcp__Figma__get_screenshot` — visual reference (helpful for layout sanity checks)
- `mcp__Figma__get_code_connect_map` — current Figma component → React component mappings

Read everything before writing code. Don't translate piecemeal.

### 4. Read the repo's `CLAUDE.md`

The mobile app's design system rules (tokens, typography scale, spacing scale, score-rendering constraint) are authoritative. Cross-check the Figma frame against them — if the frame uses a non-token color or off-scale spacing, surface it ("the frame uses `#3F00FF` directly, but the matching token is `bg-primary` — using the token") rather than translating literal values.

### 5. Generate or edit code

Apply Code Connect mappings: every Figma component that has a Code Connect entry should produce its mapped React Native component, not a generic re-implementation. If a component is *not* mapped, fall back to NativeWind primitives + the design tokens — don't invent component names.

For a **new frame**: create the file at the path the user confirmed in step 2, on a new branch named like `prototype/<frame-name-kebab>`.

For an **existing frame**: read the file, compute the diff against the Figma frame, apply only the changes needed. Preserve any code-level edits that aren't visual (handlers, side effects, hooks). If a code-level change conflicts with a Figma change, ask the user before resolving.

### 6. Update the manifest

```json
{
  "1:234": {
    "name": "Home – Default",
    "path": "src/app/(tabs)/index.tsx",
    "lastTranslatedAt": "2026-04-29T13:45:00Z"
  }
}
```

Bump `lastTranslatedAt`. If new, add the entry. Commit the manifest with the code in the same branch.

### 7. Verify

Run `npm run type-check && npm run lint`. If either fails, surface it. Do not auto-fix unless the user asks.

### 8. Hand back

Report:
- Branch name
- Files changed
- Manifest changes
- Any tokens/components in the Figma frame that lacked a clean code mapping (so the user knows what to add to Code Connect next)

## Loop ergonomics

When the user says "tweaked the frame, re-translate" without giving a fresh URL, default to the most recently translated frame in the manifest unless the cwd context suggests otherwise.

For "create a new screen from this Figma frame", always create on a new branch — never work on `main` directly.

## What this workflow does NOT do

- **Doesn't push to Figma.** This is a one-way pull (Figma → code).
- **Doesn't auto-resolve Figma↔code conflicts.** When code-level edits conflict with new Figma layout, ask.
- **Doesn't generate components from scratch.** Only screens (= frames). Net-new components belong in Figma + Code Connect first, then in code.
- **Doesn't bypass the design system.** Off-token colors, off-scale spacing, score-as-pill — all flagged, never silently translated.

## Common tripwires

- **Figma file uses a different fileKey than the manifest** → multi-file support not yet built. Stop and ask.
- **No Code Connect mapping for a Figma component used in the frame** → generated code will be plausible but not on-brand. Surface the gap so it can be added.
- **Frame uses Figma effects (drop shadows, blurs) that have no NativeWind equivalent** → use `react-native` `StyleSheet` shadow props or `expo-blur` as appropriate; flag the choice in the handoff.
- **Auto-layout in Figma doesn't 1:1 map to flexbox** in some cases (e.g., negative gaps, spacers) — translate to the closest flex equivalent and flag.
