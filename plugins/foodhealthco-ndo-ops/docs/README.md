# NDO Ops — docs & guides

Reference material for the `ndo-run` skill. Two audiences:

| Doc | For | Format |
|---|---|---|
| [**ndo-run-guide.html**](ndo-run-guide.html) | Dietitians / RDs | Interactive (search + filter) |
| [SCORING_OPS_GUIDE.md](SCORING_OPS_GUIDE.md) | Dietitians / RDs | Markdown (renders on GitHub) |
| [cli-869-scoring-runbook.md](cli-869-scoring-runbook.md) | Operators | Markdown |
| [DEMO_RUN_OF_SHOW.md](DEMO_RUN_OF_SHOW.md) | Demo prep | Markdown |

The two dietitian guides cover the same ground — `SCORING_OPS_GUIDE.md` is the plain-text version that reads inline on GitHub; `ndo-run-guide.html` is the nicer, searchable version to hand to RDs.

## How to view the interactive HTML guide

`ndo-run-guide.html` is a **single self-contained file** — all CSS and JavaScript are inline, no external fonts or assets — so it renders correctly anywhere, no build step and no web server.

GitHub itself shows the HTML *source* (not the rendered page) when you click the file, so use one of these:

**Option A — rendered link (recommended, zero setup).** This repo is public, so htmlpreview renders it in the browser:

<https://htmlpreview.github.io/?https://github.com/bitewell/foodhealthco-claude-code-plugins/blob/main/plugins/foodhealthco-ndo-ops/docs/ndo-run-guide.html>

Clickable and instant — the best link to hand to an RD. (Works once this is merged to `main`.)

**Option B — download and open.** On the GitHub file view click **Download raw file**, then double-click the `.html`. Or from a local clone:
```
open plugins/foodhealthco-ndo-ops/docs/ndo-run-guide.html   # macOS
```

**Option C — claude.ai copy.** A hosted render also lives at <https://claude.ai/code/artifact/be352a2f-2316-475e-b3bf-56758993e79d> (private to the owner by default; ask Alex to share).

> Want a permanent first-party URL instead of htmlpreview? Enable **GitHub Pages** on this repo and the file gets a stable `github.io` address — a small follow-up, not required for the link above to work.

## Keeping it in sync

When `ndo-run` behavior changes (flags, guardrails, the score→approve→publish chain), update **both** `ndo-run-guide.html` and `SCORING_OPS_GUIDE.md` so the two dietitian guides don't drift. The `catalog.yaml` + `SKILL.md` in [`../skills/ndo-run/`](../skills/ndo-run/) are the authority if they ever disagree.
