# CLI-869 Prep Packet — Dietitian Scoring Ops

**For:** running a demo + handing off to two dietitians (RDs) starting from scratch.
**Goal:** RDs run CLI-869 ("Push protein powders through the scoring pipeline") end-to-end on prod, including `approve_scores` and `send_to_clients`. (`SCORING_OPS_GUIDE.md` was updated so those steps are documented as RD-owned.)

---

## 0. Credentials to hand over

The RDs need a scoped `.env` — only the keys `ndo-run` consumes, mirroring the authoritative `foodhealthco-claude-code-plugins/.env`. **Live prod secrets — distribute via 1Password / direct, never commit or Slack.** Critically: scoring must target the **DigitalOcean waterfall host** (NDO sends an `items` schema only that host speaks — NOT the GCP `fhs-api.foodhealth.co` API, which wants `products` and would 422).

---

## 1. From-scratch setup (per RD, ~20–30 min)

Mostly follow the existing [ONBOARDING.md](../../../../Users/alexpellas/Code/foodhealthco-claude-code-plugins/ONBOARDING.md) — corrections inline below.

1. **Claude Code** — install from <https://claude.com/claude-code>, sign in with `@foodhealth.co`.
2. **Repo access** — they're in the `bitewell` GitHub org already, but confirm they're added to the two **private** repos this needs: **`nutrition-data-ops`** and **`fhs-app`** (the latter only for `generate_qa_report`). `foodhealthco-claude-code-plugins` installs via the marketplace and doesn't require a clone. Also confirm each has a working SSH key for `git clone git@github.com:…`.
3. **Clone repos** (siblings under `~/Code` so the runner auto-discovers them):
   ```bash
   mkdir -p ~/Code && cd ~/Code
   git clone git@github.com:bitewell/nutrition-data-ops.git
   git clone git@github.com:bitewell/fhs-app.git              # needed for generate_qa_report (QA xlsx)
   git clone https://github.com/bitewell/foodhealthco-claude-code-plugins.git
   ```
4. **Poetry + deps:**
   ```bash
   curl -sSL https://install.python-poetry.org | python3 -
   cd ~/Code/nutrition-data-ops && poetry install
   cd ~/Code/fhs-app && poetry install        # generate_qa_report reads fhs-app's OWN .env
   ```
5. **Install the plugin** (in any Claude Code session), then restart Claude Code:
   ```
   /plugin marketplace add bitewell/foodhealthco-claude-code-plugins
   /plugin install foodhealthco-ndo-ops@foodhealthco
   ```
6. **Drop in the scoped `.env`.** Since they're not cloning the plugins repo, put it at **`~/Code/nutrition-data-ops/.env`** (next in the discovery chain) or `~/.config/ndo-run/.env`. Both are auto-discovered by the runner.
7. **fhs-app `.env`** — `generate_qa_report` shells into fhs-app, which reads its *own* `.env` (`API_TOKEN`, `DATABASE_URL`, `SENTRY_DSN`). They'll need that too, or QA report generation fails. (Separate from the scoped ndo `.env`.)

**Verify (no writes):**
```bash
cd ~/Code/nutrition-data-ops
poetry run -- python ~/Code/foodhealthco-claude-code-plugins/plugins/foodhealthco-ndo-ops/skills/ndo-run/scripts/ndo_run.py \
  backfill_fhs --ids 1,2,3 --target dev --dry-run
```
Expect a preflight read-out + the resolved `manage.py` line, no execution. (`✗ Not in IPM` on dev is normal.)

---

## 2. Live demo script (~15 min, DEV only — safe)

Run this *with* them so they see the loop: plain English → preflight → confirm → run → postflight. The real-write step uses **tagging** on dev (NDO-internal, safe). Protein powders live in **prod**, so dev IDs are only for showing mechanics.

> ⚠️ **Dev scoring returns 0 — by design, and verified.** `backfill_fhs` sends only `{product_id}`; the FHS host resolves nutrients from *its own* (prod) dataset, so it doesn't recognize dev IDs. On dev, scoring preflight says "Will score N" but the run writes **0** ScoringResult rows. That's expected. Real FHS scores only come back for **prod** products (confirmed: prod ID 3067619 → `fhs 2.0`, and a 3-ID prod `backfill_fhs` wrote 3 rows, postflight ✓). So demo *scoring* as a dry-run only; demo the real-write loop with *tagging*.

1. **Show the plugin is live:** `/plugin list` → `foodhealthco-ndo-ops` enabled.
2. **Dry-run scoring (no write):** *"Dry-run backfill_fhs against dev for IPM IDs 1,2,3."* → point out the preflight buckets (✓ update / ↻ skip / ✗ block) and that nothing executed. (Don't run it for real on dev — it'd return 0; see caveat above.)
3. **A real dev write (tagging):** *"Run backfill_tags against dev for IPM IDs &lt;a few that exist on dev&gt;."* → show the postflight verifying writes landed. (Grab live dev IDs with *"find a few IPM ids on dev that have ingredients_text."*)
4. **Show a real score:** *"Score prod ID 3067619 against the FHS host and show me the result"* (or generate a QA report for a couple already-scored prod IDs). This is where they see an actual FHS value — dev can't produce one.
5. **Talk through the safety rails:** dry-run first, dev before prod, the typed-`prod` confirmation, the 1000-row batch guardrail.

---

## 3. CLI-869 runbook — protein powders, full chain (PROD)

CLI-869 maps onto this chain. RDs drive each step in plain English; commands shown for reference.

### Step 0 — Identify the set
The scoring chain operates on `ingestion_productmatch` rows, keyed by `ingestion_productmatch.id`. "Not approved" = no `scoring_review_approvedscoringresult` row for that `product_match_id`. `product_category` is **free text** (no enum), so confirm the exact stored string first.

Ask Claude: *"Query prod NDO for protein powders that haven't been approved and export their ids to a CSV."* Reference SQL:

```sql
-- 0a. Confirm the exact stored category string (free text — could be
--     "Protein Powder", "protein_powder", "Protein powder"…)
SELECT product_category, COUNT(*)
FROM ingestion_productmatch
WHERE product_category ILIKE '%protein%powder%'
GROUP BY product_category
ORDER BY COUNT(*) DESC;

-- 0b. Protein powders with NO approved score yet → CSV.
--     Column is named product_id (what the chain + approve_scores expect) but
--     holds ingestion_productmatch.id, the integer the whole chain threads through.
SELECT pm.id AS product_id
FROM ingestion_productmatch pm
LEFT JOIN scoring_review_approvedscoringresult asr
       ON asr.product_match_id = pm.id
WHERE pm.product_category ILIKE '%protein%powder%'   -- tighten once 0a confirms the string
  AND asr.id IS NULL
ORDER BY pm.id;
```

> **FI-only rows (the ticket's "NIQ/NIX stuff not in that table" + "uncategorized" buckets):** `ingestion_foringestion` has its own free-text `category` and **no relational FK to IPM** — it's matched in via UPC/GTIN ETL. Protein powders still sitting in foringestion (not yet matched into `ingestion_productmatch`) won't appear in 0b and **can't be scored until they're bridged into IPM** (`retrieve_data_cache`, or eng). Treat 0b (already-IPM protein powders) as the main batch; flag the FI-only remainder separately rather than blocking the run on it.

### Step 1 — Categorize uncategorized (only if needed)
> ⚠️ **Blocked as configured.** `backfill_categories` needs `CATEGORY_ENDPOINT_URL` / `CATEGORY_ENDPOINT_TOKEN`, which are **not** in the scoped env (or your source env). If the identify query already filters on `product_category = 'Protein Powder'`, skip this. If you must categorize uncategorized rows, provision the BentoML endpoint creds first.

### Step 2 — t2t (tagging)
*"Run backfill_tags against prod for &lt;the protein-powder IPM IDs / source&gt;."*
Overwrites existing tags; batch size fixed at 300. Needs `DEFAULT_TAGGING_FILE=t2t_v4.csv` (it's in the scoped env) or tags run with NO RULES.

### Step 3 — Impute added sugars
*"Run backfill_imputation against prod for those IDs."*
Imputes missing calories/fat/protein/carbs/**added sugars**.

### Step 4 — Score
*"Run backfill_fhs against prod for those IDs."*
Writes ScoringResult rows. Preflight blocks any product missing the minimum macros (fix in admin, re-run).
- **Search propagation:** scored products don't reach consumer search until the reindex chain runs. `--with-reindex` auto-skips here because `DO_OPENSEARCH_URL` isn't in the scoped env. If these need to be searchable, that's a follow-up (provision OpenSearch creds, or eng runs the reindex).
- **Batching:** keep batches ≤ a few hundred; the 1000 guardrail clamps larger `IN (...)` lists.

> ⚠️ **If scoring returns 404 for every item, stop.** The FHS scoring host (`waterfall-fhs-app`) is a **deprecated** service being migrated to `fhs-food-intel` (GKE/HeroDB) — it's been observed down. A 404 means the host is gone, not a transient; don't retry blindly. Fallback: `generate_qa_report` scores **locally** (no API) so you can still produce a QA report — but it does **not** write ScoringResult, so `approve_scores` / `send_to_clients` can't proceed on those rows until the host is back or the migration lands. Escalate to eng (ENG-874).

### Step 5 — Score QA
*"Generate a score QA report for those IDs."* → `generate_qa_report` writes scored + unscorable xlsx to `~/Code/fhs-app/output_scores/`. **Review before approving.**

### Step 6 — Approve
`approve_scores` needs a CSV with **both `fhs` and `product_id` columns** — IDs alone are rejected by the command. The FHS values were written to `scoring_review_scoringresult` in Step 4, so build the CSV from there.

Ask Claude: *"Build an approve CSV (product_id + fhs) from the ScoringResult rows for these product_ids, then run approve_scores against prod."* The query it runs:

```sql
SELECT sr.product_match_id AS product_id, sr.fhs AS fhs
FROM scoring_review_scoringresult sr
WHERE sr.product_match_id IN ( <the protein-powder ids from Step 0> )
  AND sr.fhs IS NOT NULL;
```

`fhs` is a 1–100 float on `scoring_review_scoringresult`; `product_match_id` is the same `ingestion_productmatch.id` threaded through the whole chain. Then `approve_scores --csv <that file> --target prod` (batch size fixed at 300).

### Step 7 — Publish to clients
*"Run send_to_clients against prod for those product_ids."* → publishes ApprovedScoringResults. External side effect — this is the irreversible end of the chain.

---

## Prerequisites & known gaps
- **RD repo access:** both RDs must be added to private repos **`nutrition-data-ops`** + **`fhs-app`** with working SSH keys before the session.
- **Scoring host:** scoring runs against the **DO waterfall host** (`items` schema), not GCP `fhs-api.foodhealth.co` (`products` schema). Verified: prod scoring returns real FHS values; **dev scoring returns 0** (host doesn't hold dev IDs — see the demo caveat).
- **`backfill_categories`** (categorize uncategorized PPs) needs `CATEGORY_ENDPOINT_*`; **`--with-reindex`** (search propagation) needs `DO_OPENSEARCH_*`. Provision before those steps — both auto-skip/block cleanly otherwise.
