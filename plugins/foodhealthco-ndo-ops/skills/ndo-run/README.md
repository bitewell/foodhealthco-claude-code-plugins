# ndo-run

Claude Code skill that wraps `nutrition-data-ops` management commands so you don't have to SSH into the droplet.

> **Looking for the end-to-end operator workflow?** See the [NDO scoring runbook](../../../docs/ndo-scoring-runbook.md) for a from-ticket-to-published walkthrough that covers when to use this skill vs the Dagster `ndo_score_product_set` job.

## What it replaces

```bash
# before
ssh droplet
cd /app/nutrition-data-ops
# upload CSV to btw-nutrition somehow
python manage.py backfill_fhs -f ops/my-ids.csv --sync true
```

```bash
# after (via Claude)
"score products 12345, 67890, 11111"
```

## How it works

1. Reads credentials from `meltano-elt-pipelines/.env`
2. Builds a CSV (from pasted IDs) or accepts a local CSV path / existing Spaces key
3. Uploads to `btw-nutrition` under `ops-skill/<timestamp>-<cmd>.csv`
4. Runs `poetry run python manage.py <cmd>` in the sibling `nutrition-data-ops/` checkout with translated env vars
5. Streams output live

No SSH. No Celery dependency (forces `--sync true` by default). Explicit confirmation before touching prod.

## Layout

```
.claude/skills/ndo-run/
├── SKILL.md            # Claude's instructions (read first)
├── README.md           # this file
├── catalog.yaml        # 13-command spec: args, input mode, CSV schema, notes
├── docs/
│   └── tickets.md      # Linear ticket URLs (automation + HeroDB gap)
└── scripts/
    ├── ndo_run.py      # main runner
    └── upload.py       # boto3 upload to btw-nutrition
```

## Prerequisites

### Repos and Python env

- `nutrition-data-ops/` checked out as a **sibling** of `meltano-elt-pipelines/`
- Python 3.12 (3.14 breaks `spacy` + `httpcore`; 3.11 works too)
- `poetry env use python3.12 && poetry install --no-root` in `nutrition-data-ops/`

### Credentials

Everything the skill needs is set in `meltano-elt-pipelines/.env`. See [`.env.example`](../../../.env.example) — the "NDO scoring chain" section lists each variable with a `# Get from <where>` comment.

**Required for any scoring op:**

| Variable | Source |
|---|---|
| `NDO_DEV_DATABASE_URL` / `NDO_PROD_DATABASE_URL` | DigitalOcean → Databases → ndo-{dev,production}-database → URI string |
| `FHS_API_URL`, `FHS_API_TOKEN` | DO App Platform → `waterfall-fhs-app` → Env Vars (or 1Password "FHS API tokens") |
| `DO_SPACES_ACCESS_KEY`, `DO_SPACES_SECRET_KEY` | 1Password "DO Spaces — btw-nutrition prod write" (the project-default keys are scoped to `backfills-test` and can't write to btw-nutrition) |

**Required for `backfill_categories`:**

| Variable | Source |
|---|---|
| `CATEGORY_ENDPOINT_URL`, `CATEGORY_ENDPOINT_TOKEN` | BentoML Cloud (`foodhealthco.cloud.bentoml.com`) → Deployments → `category-predictor-*` → Endpoint + API Tokens tabs |

**Optional:**

| Variable | When you need it |
|---|---|
| `FHS_HUB_DATABASE_URL` | Only for `--db platform` (HeroDB). See [ENG-897](https://linear.app/foodhealthco/issue/ENG-897). |
| `NDO_SCORING_CHAIN_SLACK_WEBHOOK_URL` | Dagster Slack notifications; unset = no posts |

**Quick setup for a new operator** (one-time per machine):

```bash
# 1. Sibling checkouts under ~/Code/ or similar
git clone git@github.com:bitewell/meltano-elt-pipelines.git
git clone git@github.com:bitewell/nutrition-data-ops.git

# 2. Python env for NDO
cd nutrition-data-ops
poetry env use $(which python3.12)
poetry install --no-root
cd ..

# 3. Copy and populate .env
cd meltano-elt-pipelines
cp .env.example .env
$EDITOR .env   # fill in the NDO scoring chain block per the table above

# 4. (Optional) gcloud Drive scope if you'll fetch CSVs from Google Sheets
gcloud auth login --enable-gdrive-access --update-adc

# 5. Smoke test (dry-run, no prod writes)
.venv/bin/python .claude/skills/ndo-run/scripts/ndo_run.py backfill_fhs \
  --ids 12345 --target dev --dry-run
```

**Rotating a key:** update the value in `.env`, no other changes needed — the runner re-reads `.env` on every invocation. (Tracked under ENG-937; future iteration may pull from GCP Secret Manager or 1Password CLI for hands-free rotation.)

## Manual invocation

See `SKILL.md` for detailed usage. Quickstart:

```bash
# Dry-run against dev
python .claude/skills/ndo-run/scripts/ndo_run.py backfill_fhs \
  --ids 12345,67890 --target dev --dry-run

# Real run against dev
python .claude/skills/ndo-run/scripts/ndo_run.py backfill_fhs \
  --ids 12345,67890 --target dev

# Prod (prompts for confirmation)
python .claude/skills/ndo-run/scripts/ndo_run.py backfill_fhs \
  --ids 12345,67890 --target prod
```

## Related work

See `docs/tickets.md` for the three Linear tickets filed alongside this skill:
- automate client match request ingestion (upstream of `match_products`)
- automate nutrition profile ingestion (upstream of `backfill_fhs_and_refresh_view_command`)
- wire `--db` routing through NDO commands (closes the HeroDB gap)
