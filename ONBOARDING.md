# Onboarding — running NDO ops with the `ndo-run` skill

This is the one-stop setup for a teammate to run **NDO scoring / tagging / ingestion ops**
(FHS scoring, Text2Tag, imputation, approvals, client publishing, safe-deletes, waterfall
matching) from their own machine via Claude Code — no SSH into the droplet.

It wraps the `nutrition-data-ops` `manage.py` commands behind the `ndo-run` skill, which
handles CSV building, Spaces upload, env translation, prod confirmation, and per-command
preflight read-outs.

> Already set up? Jump to **[Running ops](#5-running-ops)**. For the full command reference
> see [`plugins/foodhealthco-ndo-ops/skills/ndo-run/SKILL.md`](plugins/foodhealthco-ndo-ops/skills/ndo-run/SKILL.md)
> and the dietitian-facing [`SCORING_OPS_GUIDE.md`](plugins/foodhealthco-ndo-ops/docs/SCORING_OPS_GUIDE.md).

---

## 1. Prerequisites

- **Claude Code** installed and logged in.
- **git** + access to the `bitewell` GitHub org.
- **Poetry** (the runner needs `pyyaml`, `python-dotenv`, `psycopg2`, `boto3` — both the
  `nutrition-data-ops` and `meltano-elt-pipelines` Poetry envs already have these).
- Two repos cloned locally (siblings under `~/Code` is easiest — the runner auto-discovers them):
  ```bash
  git clone git@github.com:bitewell/nutrition-data-ops.git ~/Code/nutrition-data-ops
  git clone https://github.com/bitewell/foodhealthco-claude-code-plugins.git ~/Code/foodhealthco-claude-code-plugins
  cd ~/Code/nutrition-data-ops && poetry install     # provides manage.py + deps
  ```

## 2. Install the plugin

```text
/plugin marketplace add https://github.com/bitewell/foodhealthco-claude-code-plugins
/plugin install foodhealthco-ndo-ops@foodhealthco
```
Then **restart Claude Code** (plugins load at startup). Verify with `/plugin` — you should
see `foodhealthco-ndo-ops` enabled and the `ndo-run` skill available.

> Prefer not to use the marketplace? You can run the runner script directly with Poetry —
> see [SKILL.md → How to invoke](plugins/foodhealthco-ndo-ops/skills/ndo-run/SKILL.md).

## 3. Credentials (`.env`)

Copy [`.env.example`](.env.example) and fill it in. The runner finds your `.env` via this
chain (first hit wins):

1. `$NDO_RUN_ENV` (explicit path override)
2. `<foodhealthco-claude-code-plugins>/.env` ← **recommended for plugin installs**
3. `<nutrition-data-ops>/.env`
4. `~/.config/ndo-run/.env`

**Required keys:**

| Key | What | Where to get it |
|-----|------|-----------------|
| `NDO_DEV_DATABASE_URL` | dev NDO Postgres DSN | Dagster Cloud env / DO database console |
| `NDO_PROD_DATABASE_URL` | prod NDO Postgres DSN | Dagster Cloud env / DO database console |
| `DO_SPACES_ACCESS_KEY` / `DO_SPACES_SECRET_KEY` | DigitalOcean Spaces creds (read+write on `btw-nutrition`) | DO console → Spaces keys |
| `DO_SPACES_REGION` | e.g. `nyc3` | DO console |
| `FHS_API_URL` / `FHS_API_TOKEN` | FHS scoring API | ask Platform; see note below |
| `DEFAULT_TAGGING_FILE` | tagging config key in Spaces (prod uses `t2t_v4.csv`) | match prod or tags run with no rules |

**Optional (operations auto-skip if unset):**

| Key | Needed for |
|-----|------------|
| `CATEGORY_ENDPOINT_URL` / `CATEGORY_ENDPOINT_TOKEN` | `backfill_categories` (BentoML) |
| `DO_OPENSEARCH_URL` / `_PORT` / `_USERNAME` / `_PASSWORD` / `_USE_SSL` | `--with-reindex` chain |

> **Heads up on `FHS_API_URL`:** it currently points at the DigitalOcean fhs-app
> (`waterfall-fhs-app`). That service works for **modest batches** but drops the DB
> connection on very large ones — the skill's batch guardrail (below) keeps you safe.
> Migrating scoring to the GCP FHS API is tracked in ENG-965.

## 4. Verify

```bash
cd ~/Code/meltano-elt-pipelines   # any env with the 4 deps; NDO's poetry env works too
poetry run -- python ~/Code/foodhealthco-claude-code-plugins/plugins/foodhealthco-ndo-ops/skills/ndo-run/scripts/ndo_run.py \
  backfill_fhs --ids 1,2,3 --target dev --dry-run
```
Expect a **preflight** read-out (it inspects the dev DB and buckets the ids) and the resolved
`manage.py` invocation, with **no execution**. If you see `error: no .env found` or a missing-key
error, revisit step 3.

## 5. Running ops

Drive it through Claude ("score these product ids", "tag source X", "approve these scores")
or invoke the runner directly. **Safety defaults:**

- **`--dry-run` first**, **`--target dev`** before prod (prod prompts for a typed `prod` confirmation).
- **Batch guardrail:** any `-a`/`-b`/`-bs`/`-l` above `NDO_RUN_MAX_BATCH` (default **1000**) is
  clamped with a warning — a large `IN (...)` list can drop the fhs-app DB connection
  (`SSL SYSCALL error: EOF`). Pass `--allow-large-batch` to override. **Drain backlogs in
  batches of a few hundred**, not tens of thousands.
- Full command catalog + examples: [SKILL.md](plugins/foodhealthco-ndo-ops/skills/ndo-run/SKILL.md).

## 6. herodb access (for when ops move to herodb)

herodb uses **passwordless IAM** — you connect as your own `@foodhealth.co` identity, and
queries are attributable to you (no shared password). Setup is handled by the
**`foodhealthco-db-connect`** plugin
([SKILL.md](plugins/foodhealthco-db-connect/skills/db-connect/SKILL.md)):

```bash
gcloud auth login                          # your @foodhealth.co account
gcloud auth application-default login
cloud-sql-proxy <connection-name> --port <port> --auto-iam-authn
```
Requires `roles/cloudsql.client` + `roles/cloudsql.instanceUser` and membership in
`gke-developers@foodhealth.co`. A break-glass password fallback (via Dagster Cloud / GCP
Secret Manager) exists for when IAM is unavailable. Routing `ndo-run` ops at herodb is gated
on ENG-897.

## 7. Getting access / credentials

Ping **Platform (Data / Infra / Ops)** for:
- NDO DB DSNs (or pull from the Dagster Cloud deployment env)
- DigitalOcean Spaces keys (must have read+write on `btw-nutrition`)
- FHS API token
- GCP IAM roles + `gke-developers` group membership (for herodb)

## 8. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `error: no .env found` | No `.env` in the discovery chain — see step 3, or set `$NDO_RUN_ENV`. |
| `error: NDO_*_DATABASE_URL not set` | Missing DB DSN for the chosen `--target`. |
| `InvalidAccessKeyId` on upload | Spaces key wrong/scoped — needs read+write on `btw-nutrition`. |
| Tags applied but "NO RULES" logged | `DEFAULT_TAGGING_FILE` unset — set it to the prod key (`t2t_v4.csv`). |
| `backfill_categories` crashes on `predict` | `CATEGORY_ENDPOINT_URL` unset. |
| Scoring returns 500 / `SSL SYSCALL error: EOF` | Batch too large for the fhs-app — use a smaller `-a`/`-b` (the guardrail clamps to 1000 by default). |
| `nutrition-data-ops checkout not found` | Clone it (step 1) or set `$NDO_ROOT`. |
