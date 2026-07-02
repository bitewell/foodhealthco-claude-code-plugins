---
name: ndo-run
description: Run nutrition-data-ops scoring/tagging/ingestion operations on a set of products. Use when the user wants to score products (FHS), tag them, approve scores, send scores to clients, delete products safely, or any other `manage.py` ops task that would otherwise require logging into the droplet. Triggers include "score these products", "run backfill_fhs on...", "apply tags to source X", "approve these scores", "send scores to client Y".
---

# ndo-run

Wraps the 13 product-list management commands in `nutrition-data-ops/` as a single ops entry point. Handles CSV building, Spaces upload, env translation, and confirmation — you just pick the right command and call the runner.

## When to use

Any time the user asks for a bulk operation against NDO products — scoring, tagging, categorization, imputation, approval, publishing, safe-delete, or waterfall re-matching. Also when they reference a command by name (`backfill_fhs`, `match_products`, etc.) or describe the intent ("re-score everything from source X").

## Routing: intent → command

Start with `catalog.yaml` in this directory — it has the authoritative list of 13 commands with their args, CSV schema requirements, and notes. Common mappings:

| User says… | Command |
|---|---|
| "score these products" / "compute FHS" | `backfill_fhs` |
| "score source X" / "new profiles landed" | `backfill_fhs_and_refresh_view_command` |
| "approve and make searchable" / "approve and push to OpenSearch" | `approve_scores --with-reindex` |
| "approve, reindex, and publish to clients" | `approve_scores --with-reindex --send-to-clients all` |
| "tag products" / "apply tags" | `backfill_tags` |
| "categorize products" | `backfill_categories` |
| "impute missing macros/calories" | `backfill_imputation` |
| "copy these fields onto products" | `backfill_ni_profiles` |
| "set proxy match relationships" | `backfill_proxy_match` |
| "retry matching" / "waterfall match source X" | `match_products` |
| "approve these scores" | `approve_scores` |
| "send scores to client" / "publish" | `send_to_clients` |
| "remove/delete these products safely" | `remove_products_and_scores` |
| "export FHS for vendor X" | `generate_scores` |
| "generate QA xlsx" / "score report for RD" / "fhs-app score those ids" | `generate_qa_report` |
| "backfill detailed FHS norms" | `backfill_detailed_fhs_norms` |

If the user's intent is ambiguous, ask them to clarify before running anything.

## How to invoke

Always use the runner script — do NOT shell out to `manage.py` directly. The runner is a plain Python script with four deps (`pyyaml`, `python-dotenv`, `psycopg2`, `boto3`); any env with those works. By convention we run it from inside `meltano-elt-pipelines`'s Poetry env because that's where the deps already live and where `.env` historically lived (the discovery chain still falls back to it):

```bash
cd /Users/alexpellas/Code/meltano-elt-pipelines
poetry run -- python /path/to/plugins/foodhealthco-ndo-ops/skills/ndo-run/scripts/ndo_run.py \
  <command> [options]
```

`nutrition-data-ops`'s Poetry env also has all four deps (it's a Django + boto3 + psycopg2 app), so `cd /Users/alexpellas/Code/nutrition-data-ops && poetry run -- python ...` works equivalently. The runner has no `meltano_*` imports — meltano-hosting is incidental, not required. See follow-up ticket re: collapsing this dependency.

**Important — note the `--` between `poetry run` and `python`.** Poetry 2.x argument-parses everything between `poetry run` and the script name, so it will grab `--csv` / `--ids` and error out with `The option "--csv" does not exist`. The literal `--` tells Poetry to stop parsing and treat the rest as the command line. Without it, any command that takes a `--csv` or `--ids` arg (which is most of them) will fail before the runner even starts.

The runner handles: reading `.env`, building/uploading the CSV, translating env var names for NDO, streaming output, and safety gates (prod confirmation, HeroDB warning).

### Input forms

- **Pasted IDs:** `--ids 12345,67890,11111` — runner writes a temp CSV with header `product_id` and uploads it.
- **Local CSV:** `--csv /path/to/ids.csv` — runner validates columns against the command's schema (see catalog), then uploads.
- **Existing Spaces key:** `--spaces-key ops-skill/2026-04-24T....csv` — skips upload.
- **Source-only** (`backfill_fhs_and_refresh_view_command`, `match_products`): `--source my_source` — no file input.
- **Vendor-only** (`generate_scores`): pass `-v <code>` via `-- -v my_vendor` (see "passthrough" below).

### Target and DB

- `--target dev` (default) → uses `NDO_DEV_DATABASE_URL`
- `--target prod` → uses `NDO_PROD_DATABASE_URL`; runner prompts for confirmation ("type `prod` to continue")
- `--db ndo` (default) → routes to NDO
- `--db platform` → intended for HeroDB but BLOCKED with a warning pending [ENG-897](https://linear.app/foodhealthco/issue/ENG-897). Requires `--force` to run, and still only works as a single-DB swap.

### Sync

Defaults to `--sync true` so we don't rely on a prod Celery worker picking up queued tasks. Only override (`--sync false`) if the user explicitly wants async enqueue (rare for local runs).

### Passthrough args

Anything after `--` is appended verbatim to the `manage.py` command:

```bash
ndo_run.py match_products --source my_source --target dev -- -l 100 -st nielsen_exact_match
```

Use this for: `-r`/`-o` on `remove_products_and_scores`, `-v` on `generate_scores`, `-if`/`-ef`/`-t` on `backfill_ni_profiles`, `-bs`/`-o` on `backfill_categories`, etc.

## Safety defaults

1. **Always `--dry-run` first** when you're unsure — prints the exact `poetry run` invocation and the would-be Spaces key without making any changes.
2. **Always start with `--target dev`** for new operations or unusual inputs. Move to prod only after dev looks right.
3. **Never bypass the prod confirmation** with `--force` unless the user explicitly asked for it.
4. **Validate CSVs locally first** if the user hands you an unfamiliar file — check the header against the command's `csv_schema` in `catalog.yaml`.
5. **Batch-size guardrail.** Any `-a`/`-b`/`-bs`/`-l` value above `NDO_RUN_MAX_BATCH` (default 1000) is clamped to the ceiling with a loud warning — a large `IN (...)` list can drop the fhs-app DB connection (`SSL SYSCALL error: EOF`, ENG-965). Pass `--allow-large-batch` to send the full value anyway; the clamp/override is recorded in `--summary-out`.

## Pre-flight preview (ENG-938)

For commands with a registered preflight implementation, the runner inspects the input set against the target DB and prints a structured report before any write. Each input ID is bucketed:

- **`✓ update`** — will be written by the command
- **`↻ skip`** — silent no-op (e.g. `overwrite=false` skips already-categorized rows)
- **`✗ block`** — precondition fails (e.g. `ingredients_text IS NULL` → BentoML rejects)

For `--target prod` the runner then prompts `[y]es / [N]o` before proceeding. `--target dev` prints the report and auto-proceeds. `--dry-run` prints the report and stops without uploading or invoking. The report is also embedded in the `--summary-out` JSON so Dagster ops parse it.

To opt out, pass `--no-preflight` (audited in the summary JSON).

Most write commands now have a preflight impl: `backfill_categories`, `backfill_tags`, `backfill_fhs`, `backfill_imputation`, `backfill_ni_profiles`, `backfill_proxy_match`, `backfill_detailed_fhs_norms`, `approve_scores`, `send_to_clients`, `bulk_create_products`, `remove_products_and_scores`, and `archive_table` (see `scripts/preflight.py` → `PREFLIGHT_REGISTRY`). Commands without one report "no preflight implementation for `<cmd>` yet" and proceed unchanged.

## Propagation at approval time (`--with-reindex`, `--send-to-clients`)

The consumer search index is rebuilt **from the approved-scores view** (`index_scored_view_command` reads that view), so propagation belongs to **approval, not scoring**. `backfill_fhs` only writes UNAPPROVED ScoringResult/ASR rows — those legitimately shouldn't reach consumer search until an operator approves them. Both propagation steps therefore hang off `approve_scores`:

```bash
# Approve a CSV of scores, rebuild the index views, and push to OpenSearch
ndo_run.py approve_scores --csv /tmp/approvals.csv --target prod --with-reindex

# ...and also publish the approved scores to all clients on those rows
ndo_run.py approve_scores --csv /tmp/approvals.csv --target prod \
  --with-reindex --send-to-clients all

# ...or publish to a single named client
ndo_run.py approve_scores --csv /tmp/approvals.csv --target prod \
  --with-reindex --send-to-clients select --client-id 42
```

`--with-reindex` behavior:

- **Only applies to `approve_scores`.** After a successful approve, it chains `refresh_fhs_view_for_index_command` then `index_scored_view_command`. Passing it to any other command is a hard error (with a pointer to this flow) — `backfill_fhs` no longer reindexes.
- **Auto-skips on `--target dev`** (dev OpenSearch isn't wired the same as prod) and **if `DO_OPENSEARCH_URL` is unset** (loud `[chain]` log line; audited in `--summary-out`).
- **Runs synchronously** (`-sy true`) so the runner blocks until OpenSearch is fully reindexed. Large reindexes can take several minutes.
- **If the chain fails partway**, the approve run itself is unaffected — the failure is logged and the runner exits non-zero.

`--send-to-clients` behavior:

- **Only applies to `approve_scores`**, and runs **after** the reindex chain. Reuses the same Spaces key the approve consumed (it carries `product_id`).
- **`all`** → publishes to every client on the approved rows. **`select`** → one client (requires `--client-id`). **`requested`** (only clients who requested the product) is **not supported yet** — NDO has no requested-client concept; it's rejected at parse time and tracked as a future feature (ENG-895 area).
- **Prod-only** — auto-skips on `--target dev` (client publish is a real external side effect). Audited in `--summary-out`.

When these flags are **not** passed, the runner prints loud one-line reminders so the gap between "written to Postgres" and "visible to consumers/clients" is never silent: after `backfill_fhs` it points at `approve_scores --with-reindex`; after a bare `approve_scores` it points at `--with-reindex` / `--send-to-clients`.

## Examples

All examples below assume `cd /Users/alexpellas/Code/meltano-elt-pipelines` first. Note the `poetry run --` (with the literal `--`) — this is required so Poetry doesn't grab `--csv`/`--ids` before the runner sees them.

```bash
# FHS backfill on 3 pasted IDs, dev target, dry-run first
poetry run -- python /path/to/ndo_run.py backfill_fhs \
  --ids 12345,67890,11111 --target dev --dry-run

# For real
poetry run -- python /path/to/ndo_run.py backfill_fhs \
  --ids 12345,67890,11111 --target dev

# Approve scores AND propagate to OpenSearch (prod only — dev OpenSearch not wired)
python .claude/skills/ndo-run/scripts/ndo_run.py approve_scores \
  --csv /tmp/approvals.csv --target prod --with-reindex

# Tag all products from a source (no CSV needed — source drives selection)
poetry run -- python /path/to/ndo_run.py backfill_tags \
  --source nielsen --target dev

# Approve scores from a local CSV (validates fhs + product_id columns)
poetry run -- python /path/to/ndo_run.py approve_scores \
  --csv /tmp/approvals.csv --target prod

# Safe-delete with required reason + operator metadata (via passthrough)
poetry run -- python /path/to/ndo_run.py remove_products_and_scores \
  --csv /tmp/obsolete.csv --target prod \
  -- -r "duplicate of canonical source" -o "alex@bitewell 2026-04-24"

# Waterfall re-match a source with a specific stage
poetry run -- python /path/to/ndo_run.py match_products \
  --source hyvee --target dev \
  -- -l 500 -st nielsen_exact_match
```

## Checking for prerequisites

Before the first run in a session:

- Confirm `nutrition-data-ops/` is checked out (runner discovers it via `$NDO_ROOT`, walk-up from CWD, or `~/Code/nutrition-data-ops`; exits with a clone hint if missing).
- Confirm the `.env` (plugins-repo, NDO checkout, or `~/.config/ndo-run/.env` — see `discover_env_file` for the chain) has `NDO_DEV_DATABASE_URL`, `NDO_PROD_DATABASE_URL`, `DO_SPACES_ACCESS_KEY`, `DO_SPACES_SECRET_KEY`, `FHS_API_URL`, `FHS_API_TOKEN`. The runner will fail loudly if any are missing.

## Programmatic invocation (Dagster)

The `dagster_ndo/jobs/scoring_chain.py` orchestrator (`ndo_score_product_set` job) shells out through this runner once per chain step. Two relevant flags:

- `--summary-out PATH` — writes a JSON file at `PATH` on exit (success or failure) with `command`, `target`, `db`, `source`, `spaces_key`, `input_count`, `exit_code`, `started_at`, `completed_at`, `elapsed_s`. The Dagster ops parse this for structured metadata.
- `--force` — required when called from a non-interactive context (no stdin), since prod confirmation reads from stdin. Dagster ops always pass it.

Other callers wanting structured run metadata can pass `--summary-out /tmp/foo.json` and parse the file after the runner exits.

## Related tickets

- [ENG-926](https://linear.app/foodhealthco/issue/ENG-926) — Epic: post-pipeline scoring automation (NDO priority)
- [ENG-928](https://linear.app/foodhealthco/issue/ENG-928) — Shared chain orchestrator (this skill is the workhorse it shells into)
- [ENG-895](https://linear.app/foodhealthco/issue/ENG-895) — Automate client match request ingestion (upstream of `match_products`)
- [ENG-896](https://linear.app/foodhealthco/issue/ENG-896) — Automate nutrition profile ingestion (upstream of `backfill_fhs_and_refresh_view_command`)
- [ENG-897](https://linear.app/foodhealthco/issue/ENG-897) — Wire `--db` through NDO commands (unblocks `--db platform`)
