# Scoring Ops — Dietitian's Guide

A step-by-step reference for running tagging and scoring on products in NDO, using the `foodhealthco-ndo-ops` Claude Code plugin. No SSH, no droplet console, no `manage.py` memorization — just chat with Claude.

> **TL;DR**: after a one-time setup, you tell Claude in plain English what you want to do (e.g. *"re-tag and re-score these 5 products"*), Claude runs the skill, shows you what it's about to change, you approve, it happens, and Claude verifies it landed.

---

## Who this is for

- **You**, a dietitian re-tagging / re-scoring products after editing ingredient text, nutrient values, or category. Or generating an updated score report for review.
- Approving scores (`approve_scores`) and publishing to clients (`send_to_clients`) are part of the RD workflow — you run the full chain through publish, with QA review before approving (see "When to escalate" below).
- Not for: brand-new product ingestion from scratch (eng helps with that), or archive/delete operations.

---

## One-time setup

Do this once on your laptop. Roughly 15–30 minutes the first time. Ask an engineer (Alex / Sam / whoever's on platform that week) if anything below blocks you.

### 1. Install Claude Code

Download from <https://claude.com/claude-code>. Sign in with your foodhealth.co account.

### 2. Clone the nutrition-data-ops repo

In a terminal:

```bash
mkdir -p ~/Code
cd ~/Code
git clone git@github.com:bitewell/nutrition-data-ops.git
```

> If `git clone` fails with a permissions error: ask eng to add you to the bitewell GitHub org and set up an SSH key (one-time, ~5 min).

### 3. Install Python deps

The plugin shells out to NDO's `manage.py`, which needs its own poetry env:

```bash
# Install poetry once if you don't have it:
curl -sSL https://install.python-poetry.org | python3 -

cd ~/Code/nutrition-data-ops
poetry install
```

### 4. Install the plugin

In any Claude Code session:

```
/plugin marketplace add bitewell/foodhealthco-claude-code-plugins
/plugin install foodhealthco-ndo-ops@foodhealthco
```

You should see `foodhealthco-ndo-ops` in `/plugin list`.

### 5. Set up your `.env`

This is where the credentials live. Each line is a key the plugin needs:

```bash
# Create the file in the plugin repo's clone (gitignored — won't be committed)
touch ~/Code/foodhealthco-claude-code-plugins/.env
```

Open `~/Code/foodhealthco-claude-code-plugins/.env` in your editor and paste this template, then fill in real values (ask eng / 1Password for each):

```env
# --- NDO Postgres connection strings ---
# DEV: a smaller throwaway DB — always test against this first
NDO_DEV_DATABASE_URL=postgresql://user:pass@host:port/dbname

# PROD: the live NDO database — only used when you pass --target prod
NDO_PROD_DATABASE_URL=postgresql://user:pass@host:port/dbname

# --- DigitalOcean Spaces (object storage for CSV uploads) ---
# Get these from 1Password "DO Spaces — btw-nutrition prod write" entry,
# OR from the DigitalOcean console (cloud.digitalocean.com → API → Spaces Keys).
# THE KEYS MUST HAVE WRITE ACCESS TO THE `btw-nutrition` BUCKET.
# Backfills-test keys won't work — you'll get InvalidAccessKeyId errors.
DO_SPACES_ACCESS_KEY=
DO_SPACES_SECRET_KEY=
DO_SPACES_REGION=nyc3

# --- FHS scoring API (DigitalOcean waterfall host — NDO main scores against this) ---
# NDO sends an `items` schema this host speaks. NOT the GCP fhs-api.foodhealth.co
# Laravel API (that expects a `products` schema NDO doesn't send).
FHS_API_URL=https://waterfall-fhs-app-p5un2.ondigitalocean.app
FHS_API_TOKEN=

# --- Tagging config ---
# Tells NDO which Text2Tag config to use. MUST match what production uses
# (currently t2t_v4.csv). If unset, NDO defaults to an older config and your
# tags won't match production results.
DEFAULT_TAGGING_FILE=t2t_v4.csv

# --- Optional: BentoML category prediction (for backfill_categories) ---
CATEGORY_ENDPOINT_URL=
CATEGORY_ENDPOINT_TOKEN=
```

### 6. Verify setup

In a Claude Code session in any directory, ask:

> *"Do a dry-run of backfill_tags against dev for product IDs 1,2,3"*

Claude should:
1. Use the `foodhealthco-ndo-ops` skill
2. Print a preflight read-out showing what would change
3. NOT actually write anything (because it's a dry-run)

If you see `error: NDO_DEV_DATABASE_URL not set` or `InvalidAccessKeyId`, your `.env` is missing something. Fix and retry.

---

## The core workflow

The standard pattern, in plain English:

1. **You have N products** you want to re-tag or re-score (e.g. dietitian edited ingredient text in admin, or new vendor data landed)
2. **Get their IPM IDs** (Integer Product Match IDs — the primary keys in `ingestion_productmatch`)
3. **Run tagging** — applies Text2Tag rules to set is_deep_fried, is_artificial_colors, etc.
4. **Run scoring** — calls the FHS API to compute the food health score
5. **Generate a score report** — xlsx of the new scores for review

Each step has a **preflight** (Claude shows you what will change before any write) and a **postflight** (Claude verifies the write actually landed). For production, Claude will also pause and ask you to confirm.

---

## Recipe 1: Re-tag and re-score a small batch of products

**Scenario**: you edited ingredient text or nutrition values on 5 products in admin, and want to refresh their tags + score.

### Step 1: get the IPM IDs

You probably already have these from admin. If not, ask Claude:

> *"Find the IPM IDs for products with these GTINs: 00077900003660, 00023700062970"*

Claude will query the DB and print the matching `id` values.

### Step 2: dry-run the tagging on dev

Always rehearse on dev first.

> *"Dry-run backfill_tags against dev for IPM IDs 3067665,3067666,3067667,3067668,3067669"*

Expected output:

```
Pre-flight: backfill_tags
  Target: 🟢 NDO dev
  Input: 5 product_ids

Will WRITE:
  ✓  5  Will tag  (TaggingJob runs with overwrite=True)

Will SKIP (precondition fails):
  ✗  0  No ingredients_text  (TaggingJob yields no tags without ingredients)
  ✗  0  Not in IPM

$ (cd ~/Code/nutrition-data-ops && poetry run python manage.py backfill_tags -f ops-skill/DRYRUN-backfill_tags.csv -sy true)

[dry-run] not executing
```

If preflight shows `✗ Not in IPM` rows, those IDs don't exist on dev (dev has different data from prod). That's normal — proceed.

### Step 3: real tagging run on prod

> *"Run backfill_tags against prod for IPM IDs 3067665,3067666,3067667,3067668,3067669"*

Claude will:
1. Print the preflight
2. Ask you to type `prod` to confirm (because it's a destructive prod write)
3. Upload a CSV to DO Spaces
4. Run the actual tagging
5. Print a postflight showing how many writes landed

Expected postflight on success:

```
Post-flight: backfill_tags
  ✅ all 5 expected writes landed
  source: ingestion_productmatch.updated_at >= 2026-05-22T14:15:32+00:00
```

If postflight shows `⚠ drift` (e.g. "expected 5, observed 3"), 2 products didn't get tagged. Take a screenshot of the output and send to eng — that's a real bug worth investigating.

### Step 4: dry-run scoring on dev, then real on prod

Same pattern:

> *"Dry-run backfill_fhs against dev for IPM IDs 3067665,3067666,3067667,3067668,3067669"*

Then:

> *"Run backfill_fhs against prod for those same IDs"*

The preflight for `backfill_fhs` checks that each product has the minimum macros (calories, fat, protein, carbs). If any are missing, you'll see them in the `✗ Missing macros` bucket — fix the missing nutrients in admin first, then re-run.

### Step 5: generate a score report

> *"Run fhs-app's generate_scores for IPM IDs 3067665,3067666,3067667,3067668,3067669"*

Claude will run [fhs-app/generate_scores.py](https://github.com/bitewell/fhs-app/blob/main/generate_scores.py) which writes an xlsx of scored vs. unscorable products to `~/Code/fhs-app/output_scores/`. Open the file, share with the reviewer.

---

## Recipe 2: Re-score a whole vendor batch

**Scenario**: vendor sent updated nutrient data for all their SKUs, you want to re-score everything under that source.

> *"Run backfill_fhs against prod for source tyson_20260521"*

Same flow as Recipe 1 but Claude uses the `--source` filter instead of `--ids`. Preflight + postflight work identically.

For very large batches (1,000+ products), the FHS API can take 30+ minutes. Just wait for the postflight to print.

---

## Recipe 3: Generate an updated score report after RD review

**Scenario**: RD reviewed scores, flagged 3 with bad `is_deep_fried` tags. You manually corrected those in admin, now you need to re-score and regenerate the report.

1. *"Re-score IPM IDs 1587997,2669182 against prod"* (just `backfill_fhs`, since tagging was manual)
2. *"Generate a new score report for those IDs"*

---

## Glossary

| Term | What it means |
|---|---|
| **IPM** | "Ingestion Product Match" — a row in `ingestion_productmatch`. The primary key (`id`) is the IPM ID you'll see in most commands. |
| **FHS** | Food Health Score (0–100). Computed by the FHS API from nutrients + ingredients + tags. |
| **ScoringResult** | A row in `scoring_review_scoringresult`. Created when you run `backfill_fhs`. Holds the FHS value awaiting approval. |
| **ApprovedScoringResult** | A row in `scoring_review_approvedscoringresult`. Created when eng approves a ScoringResult. This is what gets published to clients. |
| **Tagging / Text2Tag (t2t)** | The system that reads ingredient text and sets boolean flags like `is_artificial_colors`, `is_deep_fried`, etc. Rules live in a CSV in DO Spaces (`t2t_v4.csv`). |
| **Preflight** | The Claude preview that runs *before* a command, showing how many rows will be touched and bucketed by outcome (✓ update / ↻ skip / ✗ block). |
| **Postflight** | The Claude verification that runs *after* a successful command, querying the DB to confirm the writes landed. Surfaces drift if anything was silently rejected. |
| **Dry-run** | `--dry-run` flag — runs preflight + prints the exact command that would run, but doesn't actually upload or write. Always safe. |
| **Source** | The `source` column on `ingestion_productmatch`. Used to group products by vendor + ingestion batch (e.g. `tyson_20260521`, `nielsen`, `kroger`). |
| **--target dev / prod** | Which database the command talks to. Dev = safe rehearsal. Prod = the real one. |

---

## Troubleshooting

### "InvalidAccessKeyId" when uploading

Your `DO_SPACES_ACCESS_KEY` / `DO_SPACES_SECRET_KEY` in `.env` don't have write access to the `btw-nutrition` bucket. The keys that are scoped to `backfills-test` will not work. Ask eng for the `btw-nutrition` write keys (1Password entry "DO Spaces — btw-nutrition prod write").

### "Tagging products: 100%" but no tags actually applied

Almost certainly `DEFAULT_TAGGING_FILE` isn't set correctly in `.env`. Should be `DEFAULT_TAGGING_FILE=t2t_v4.csv` (or whatever prod is using — check DO App Platform → NDO app → Env Variables for current value). The plugin now logs WARN-level "Tagging will run with NO RULES" if it can't load the config — look for that line.

### Postflight shows drift

Postflight ran the same query against the DB after the command. If it shows "expected N, observed M" with M < N, some writes didn't land. Common causes:

- FHS API rejected items mid-batch for missing data (check the run output for `4xx` errors)
- Database constraint violation
- Async writes still in flight (postflight skips when `--sync false`; this shouldn't happen with default settings)

Screenshot the postflight output and send to eng.

### Preflight shows `✗ Not in IPM`

The IDs you provided don't exist in `ingestion_productmatch` on the target DB. Common reasons:

- Typo in the ID list
- IDs are from prod but you're targeting dev (dev has different/older data)
- Product was archived/deleted

### "preflight skipped: no postflight implementation for X"

The command you ran doesn't have a preflight/postflight implementation yet. Currently 11 of 19 commands have them; the rest still work but won't show the preview/verification. Ask in #platform if you need preflight for a specific command.

### Claude says "command not found" or doesn't seem to use the plugin

In Claude Code, run `/plugin list`. You should see `foodhealthco-ndo-ops` listed and **enabled**. If not, repeat the install step. Restart Claude Code after install.

---

## When to escalate to engineering

| You should ask eng about… | Why |
|---|---|
| Adding a new product source from scratch (creating IPM rows from raw vendor data) | Use [alex-scripts/sql/NDO Updates.sql](https://github.com/bitewell/alex-scripts/blob/main/sql/NDO%20Updates.sql) as a template; eng reviews |
| Archive / delete operations | Destructive |
| Anything that fails with "InvalidAccessKeyId" or "permission denied" after retrying | Credential issue |
| Anything where postflight reports drift > 0 | Real bug worth investigating |

What you CAN do solo — the full scoring → publish chain:
- `backfill_tags`, `backfill_imputation`, `backfill_fhs` on a set of IPM IDs / one source (dev or prod)
- `generate_qa_report` — and **review the QA xlsx before approving**
- `approve_scores` — gates what gets published; run only after QA review looks right
- `send_to_clients` — publishes to clients; the irreversible end of the chain
- Dry-runs of anything

---

## Reference

- Full catalog of all 19 commands: [catalog.yaml](../skills/ndo-run/catalog.yaml)
- Detailed README for the skill: [skills/ndo-run/README.md](../skills/ndo-run/README.md)
- Plugin source: [bitewell/foodhealthco-claude-code-plugins](https://github.com/bitewell/foodhealthco-claude-code-plugins)
- Engineering Slack: #platform
