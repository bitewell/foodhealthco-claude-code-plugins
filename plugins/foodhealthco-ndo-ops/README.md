# foodhealthco-ndo-ops

Run nutrition-data-ops (NDO) management commands — scoring, tagging, ingestion, archival, publishing — from a Claude Code session without SSHing into the droplet or navigating the DO App Platform console.

## What this plugin gives you

A single `ndo-run` skill that wraps **19 NDO `manage.py` commands** behind one entry point. It handles:

- **CSV building + Spaces upload**: pasted `--ids 1,2,3` or a local `--csv path/to/file.csv` gets staged to DO Spaces (`btw-nutrition/ops-skill/...`) and the resulting key is passed to NDO automatically.
- **Env translation**: meltano-style `DO_SPACES_*` → NDO-style `DO_*`, plus `NDO_DEV_DATABASE_URL` / `NDO_PROD_DATABASE_URL` selection by `--target`.
- **Schema validation**: rejects malformed CSVs before upload (e.g. `approve_scores` requires `fhs` + `product_id`).
- **Preflight read-outs**: bucket the input set against the target DB (`✓ update / ↻ skip / ✗ block`) BEFORE any write, then prompt for opt-in on prod.
- **Prod safety**: confirms with `Type the word 'prod' to continue` for any `--target prod` invocation (skip via `--force` only in non-interactive contexts).

## Commands covered

| Category | Commands |
|---|---|
| Scoring | `backfill_fhs`, `backfill_fhs_and_refresh_view_command`, `refresh_fhs_view_for_index_command`, `index_scored_view_command`, `backfill_detailed_fhs_norms` |
| Tagging | `backfill_tags`, `backfill_categories`, `text2tag_qa` |
| Backfills | `backfill_imputation`, `backfill_ni_profiles`, `backfill_proxy_match` |
| Ingestion | `create_products`, `match_products`, `archive_table`, `remove_products_and_scores`, `generate_scores` |
| Scoring review | `retrieve_data_cache`, `approve_scores`, `send_to_clients` |

Authoritative list with args, CSV schemas, and notes: [skills/ndo-run/catalog.yaml](skills/ndo-run/catalog.yaml).

## Install

```
/plugin marketplace add bitewell/foodhealthco-claude-code-plugins
/plugin install foodhealthco-ndo-ops@foodhealthco
```

## Prerequisites

1. **Local checkouts** of both `meltano-elt-pipelines` and `nutrition-data-ops`. The skill auto-discovers them at:
   - `$NDO_ROOT` env var (explicit), OR
   - Sibling of `meltano-elt-pipelines` (default layout: `~/Code/meltano-elt-pipelines/`, `~/Code/nutrition-data-ops/`), OR
   - Walk-up from CWD looking for those repo names.

2. **A `.env` file** with the required keys. Discovery chain (first hit wins):
   1. `$NDO_RUN_ENV` (explicit path)
   2. `<foodhealthco-claude-code-plugins>/.env` (the plugin repo itself — recommended for new installs)
   3. `<meltano-elt-pipelines>/.env` (legacy / backward-compat)
   4. `~/.config/ndo-run/.env` (XDG-ish per-user)

   Copy `.env.example` from this repo root → fill in your keys → drop at one of the above paths.

## Required env vars

```
NDO_DEV_DATABASE_URL      # postgres connection string for the dev NDO DB
NDO_PROD_DATABASE_URL     # postgres connection string for the prod NDO DB
DO_SPACES_ACCESS_KEY      # DigitalOcean Spaces key with read+write on btw-nutrition
DO_SPACES_SECRET_KEY      # matching secret
DO_SPACES_REGION          # nyc3 by default
FHS_API_URL               # FHS scoring API endpoint
FHS_API_TOKEN             # FHS scoring API token
DEFAULT_TAGGING_FILE      # the DO Spaces key for the production t2t config (e.g. t2t_v4.csv)
# Optional:
CATEGORY_ENDPOINT_URL     # BentoML category-prediction (for backfill_categories)
CATEGORY_ENDPOINT_TOKEN
FHS_HUB_DATABASE_URL      # only if you intend to use --db platform (ENG-897 — not yet functional)
```

## Quick start

After install, in any Claude Code session:

```
"Score products 1,2,3 against prod"
"Tag everything from source nielsen on dev with a dry-run"
"Generate an FHS export xlsx for vendor kroger"
```

Claude will route through the skill and handle the upload + invocation + opt-in flow.

## Direct invocation (CLI)

If you'd rather skip Claude and invoke the runner directly:

```bash
cd /path/to/meltano-elt-pipelines  # any directory with a usable poetry env works
poetry run python /path/to/foodhealthco-claude-code-plugins/plugins/foodhealthco-ndo-ops/skills/ndo-run/scripts/ndo_run.py \
  backfill_tags --target prod --ids 1,2,3 --summary-out /tmp/run.json
```

The runner is self-contained and discoverable from any CWD.

## Migration from `meltano-elt-pipelines/.claude/skills/ndo-run/`

This plugin is a drop-in replacement. The legacy in-meltano version still works via the same discovery chain; to migrate:

1. Install this plugin (above).
2. Copy the ndo-run keys from `meltano-elt-pipelines/.env` → `foodhealthco-claude-code-plugins/.env` (or `~/.config/ndo-run/.env`).
3. (Optional, once you confirm everything works) Remove `meltano-elt-pipelines/.claude/skills/ndo-run/` and clean up `.env` of ndo-run-only keys.
