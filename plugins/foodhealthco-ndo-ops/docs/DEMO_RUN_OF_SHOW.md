# Scoring Ops Demo — Run of Show

Light run-of-show for demoing the `foodhealthco-ndo-ops` plugin end-to-end. ~30 min total: 5 min intro + ~25 min demo + buffer for Q&A.

Audience: engineers + dietitians + RD reviewers. Goal: prove the plugin replaces SSH/console workflows with a single Claude Code interface, and onboard the team to use it themselves.

> Companion: [SCORING_OPS_GUIDE.md](SCORING_OPS_GUIDE.md) covers the dietitian-facing setup + troubleshooting. This doc is the demo-runner's cheat sheet.

---

## Pre-demo prep (morning-of, ~20 min)

### 1. Pre-stage 5 dummy products on dev

Use the SQL script at [alex-scripts/sql/demo_dummy_products.sql](https://github.com/bitewell/alex-scripts) (or inline SQL — see "Dummy data" section below). Source name: `demo_<your-initials>_<date>` so it's obviously fake.

The 5 products are designed to surface a different teaching moment each:

| # | Product | What it shows |
|---|---|---|
| 1 | Granola bar — complete data | Clean baseline; everything flows through with no blocks |
| 2 | Chicken nuggets — `is_deep_fried` is meaningful, `product_category` NULL | Categorization preflight has work; tagging sets `is_deep_fried=TRUE` |
| 3 | Product missing `protein` | Imputation preflight shows "Will impute: 1" |
| 4 | Product missing macros (calories+fat) | FHS preflight hits the `✗ Missing macros` block bucket — failure is loud |
| 5 | Product with ingredients to be edited mid-demo | Demonstrates the backfill workflow when RD flags a correction |

### 2. Verify plugin works

```bash
# In any Claude Code session:
"Dry-run backfill_tags on dev for source demo_<X>"
```

Confirm preflight prints the bucket read-out. If it errors, fix `.env` per the [dietitian guide](SCORING_OPS_GUIDE.md#5-set-up-your-env) before the demo.

### 3. Tabs/terminals ready

- [SCORING_OPS_GUIDE.md on GitHub](https://github.com/bitewell/foodhealthco-claude-code-plugins/blob/main/plugins/foodhealthco-ndo-ops/docs/SCORING_OPS_GUIDE.md) — link in the chat for participants
- A scratch psql terminal pointed at dev (for the manual-create step + ad-hoc queries)
- The 5 IPM IDs from your dummy data — copy-paste ready
- Claude Code session, started, plugin enabled

### 4. Env-var prereqs (caught during a real rehearsal — don't skip)

These env vars must be in your `.env` (recommend the plugins-repo `.env` since it's first in the discovery chain). Otherwise specific phases silently no-op or crash mid-run:

| Env var | Required for | Symptom if missing |
|---|---|---|
| `NDO_DEV_DATABASE_URL` | All NDO phases | runner errors out at startup |
| `DO_SPACES_ACCESS_KEY` + `DO_SPACES_SECRET_KEY` (must have **`btw-nutrition` write**) | Phases that upload CSV (4, 7 in `--ids` mode, 11, 12, 13, 14, 15) | `InvalidAccessKeyId` on upload — `backfills-test`-scoped keys silently fail |
| `DEFAULT_TAGGING_FILE=t2t_v4.csv` | Phase 4 (Tag) — match production rules | Phase 4 logs "Tagging will run with NO RULES" and applies no tags |
| `CATEGORY_ENDPOINT_URL` (+ TOKEN if BentoML auth is on) | Phase 6 (Categorize) | `Invalid URL 'predict': No scheme supplied` — categorization no-ops |
| `FHS_API_URL` + `FHS_API_TOKEN` | Phase 7 (Score), Phase 12 (Re-score) | scoring fails to call the API |
| `FHSAPI_API_TOKEN` (note: **second** API namespace) | Phase 14 (Send to client) | `ValueError: Token cannot be empty` |

Also: dev DB needs the latest migrations applied. As of the May 2026 release that's `0092_productmatch_foringestion`. Without it, any phase reading via Django ORM fails with `column ingestion_productmatch.foringestion_id does not exist`. Apply via `poetry run python manage.py migrate` from `nutrition-data-ops/` with `DATABASE_URL=$NDO_DEV_DATABASE_URL`.

---

## 5-min intro

Three slides, ~90 seconds each:

| Slide | Content |
|---|---|
| **Problem** | Until recently, scoring ops = SSH droplet → 8+ `manage.py` commands by hand → no preview before writes → no verification after. Easy to silently no-op. Cite CLI-855: "Tagging products: 100%" reported success while applying zero tags because `read_csv` swallowed a missing-config error and `TaggingJob` ran with empty rules |
| **What's new** | `foodhealthco-ndo-ops` marketplace plugin wrapping **19 NDO commands**. Three guarantees: **preflight** shows update/skip/block buckets before any write; **prod requires opt-in** ("type `prod` to continue"); **postflight** verifies writes landed and surfaces drift if anything was silently rejected |
| **What's next** | These skills will be ported to HeroDB once that migration is fully synced and working. ENG-897 tracks the `--db platform` routing gap. Same skill, same UX, just a different target — but for today, NDO only |

---

## Demo flow (~25 min)

Follow a single dummy product (e.g. Product #3) all the way through. Mention the others when relevant.

| # | Phase | What to do | Skill command / tool | Teaching moment |
|---|---|---|---|---|
| 1 | **Receive** | Show the vendor CSV (`demo_vendor.csv` with 5 rows) | n/a — file inspection | "Vendors drop CSV like this. Job is to get those rows into the DB and through scoring." |
| 2 | **Create products** *(SQL today, skill soon)* | Open the SQL template, run via `psql` on dev | `psql` + SQL — **currently a manual step** | Be honest: *"This is the one part still done by hand today. ENG-NNN wraps it in a `bulk_create_products` skill command so dietitians can upload a CSV directly. For now: SQL."* Also mention: *"In real prod, vendors push to the FI (For Ingestion) API. The canonical entry there is `retrieve_data_cache -a 256` — drains FI Pending rows into IPM in batches. We're skipping FI today because the demo data is synthetic, but that's the production-path equivalent of this phase."* |
| 3 | **Match** *(optional, mention briefly)* | "Skipping — our CSV came in with GTINs already." Mention that for products created via `foringestion`, you'd run `match_products --source demo_X` | `match_products` | Just name the command; move on |
| 4 | **Tag** | "Run backfill_tags on source demo_X against dev" | `backfill_tags` | First preflight read-out — explain the buckets. `is_deep_fried` fires for Product #2 |
| 5 | **Impute** | "Run backfill_imputation on source demo_X" | `backfill_imputation` | Preflight: "Will impute: 1, Already complete: 4" — the missing-protein row gets filled |
| 6 | **Categorize** | "Run backfill_categories on source demo_X" | `backfill_categories` | Preflight: "Will categorize: 1, Already categorized: 4" — only Product #2 hits BentoML |
| 7 | **Score** | "Run backfill_fhs **--ids 1,2,3,4,5** against dev" *(NOT --source — preflight only fires with --ids/--csv input)* | `backfill_fhs --ids ...` | Preflight surfaces `✗ Missing macros` bucket on Product #4 — that one's score will be **unreliable** because the FHS API tolerates NULL macros but produces a meaningless number. **Preflight warns operators upfront so they can investigate before approving.** This is the headline feature — visibility before writes. |
| 8 | **Refresh mat view** *(skipped live on dev; mention only)* | "After scoring lands, in prod we refresh the materialized view that feeds OpenSearch" | `refresh_fhs_view_for_index_command` | Note: `backfill_fhs` writes ScoringResult/ASR rows but does **NOT** refresh `fhs_values_with_attr_for_index_mat_view` automatically. Real-prod scoring runs this next. The convenience command `backfill_fhs_and_refresh_view_command` (source-input only) combines Phase 7 + 8. |
| 9 | **Reindex OpenSearch** *(skipped live on dev; mention only)* | "Then push the refreshed view into OpenSearch so scored products become searchable in the consumer app" | `index_scored_view_command` | This is the step that closes the loop between scoring and consumer-facing search. Skip on dev (dev OpenSearch isn't wired the same way); call it out as a required real-prod step. |
| 10 | **Score report** | "Generate the QA xlsx for source demo_X from those 5 IDs" | `generate_qa_report --ids 1,2,3,4,5 --source demo_X` (shells out to `fhs-app/generate_scores.py`, output xlsx lands in `fhs-app/output_scores/`) | "Same skill interface, different tool underneath. Postflight reports how many xlsx files landed — 0 means fhs-app failed silently. The xlsx is what RD reviews." |
| 11 | **Backfill** (after RD review) | "RD flagged Product #5's tag. I edited the source CSV — re-apply with backfill_ni_profiles" | `backfill_ni_profiles --csv corrections.csv --target dev -- -if is_deep_fried` | Shows the round-trip: dietitian edits → CSV → DB. Preflight confirms the update. |
| 12 | **Re-score** | "Re-run backfill_fhs on the edited IDs" | `backfill_fhs --ids ...` | Postflight verifies new scoring result rows landed for the corrected products. **In prod, re-run Phases 8 + 9 (view refresh + OpenSearch reindex) after this** so the corrected scores propagate to consumer search. |
| 13 | **Approve** | "Approve scores against dev — here's the CSV with product_id, fhs" | `approve_scores --csv approvals.csv` | Preflight cross-checks each row's `fhs` against the stored `ScoringResult.fhs`. Show **one matching + one intentionally mismatched** row — narrate the rejection |
| 14 | **Send to client** | "Send to clients for those IDs with client_id=demo_client" | `send_to_clients -- -c demo_client` | Mention this hits external publishers in prod; on dev it's a no-op against a fake client. Postflight verifies `published` flag flipped. *(Note: `send_to_clients` explicitly does NOT refresh the OpenSearch index — that's why Phases 8/9 are separate. An alternative external-push command `publish_scores_to_client` exists in NDO but isn't wrapped in the plugin yet — see follow-ups.)* |
| 15 | **Archive** | "Clean up the demo with `remove_products_and_scores`" *(cascades to SR + ASR)* | `remove_products_and_scores --csv ids.csv -- -r 'demo cleanup' -o 'alex'` | **Cleans up IPM + SR + ASR in one call.** Alternative: 3× `archive_table` calls (`-m approvedscoringresult` → `-m scoringresult` → `-m productmatch`) in that order. Do NOT use `archive_table -m productmatch` alone — it leaves SR + ASR orphans pointing at deleted IPM ids (verified during the rehearsal). |

---

## Q&A buffer (~5 min)

Common questions to prep for:

| Q | A |
|---|---|
| "What if the FHS API is down mid-batch?" | Preflight can't catch upstream-API outages, but postflight WILL surface drift (expected N, observed M). Re-run after the API recovers — idempotent. |
| "What happens if I run prod without `--target prod`?" | Default is dev. To hit prod you'd need to explicitly type `--target prod` AND type the word `prod` at the confirmation prompt. Safe by default. |
| "Can I undo something?" | Archive yes (rows go to `entity_archive`, not hard-deleted; eng can restore). FHS API calls are idempotent — re-running produces the same score. |
| "When does this hit HeroDB?" | ENG-897 tracks the routing. Same skill, same UX, just `--db platform` starts to work once wired. |
| "Can I run this without Claude Code?" | Yes — `ndo_run.py` is a regular Python script. `poetry run python /path/to/ndo_run.py backfill_tags --target dev --ids 1,2,3`. Claude is just a friendly UI on top. |

---

## Known gaps — be honest about these

Don't hide; mention briefly so the team knows what's coming:

1. **Step 2 (Create products) is still SQL.** The plugin doesn't wrap product creation from CSV yet. Frame as a known gap; name the follow-up ticket. (Real-prod alternative for vendor ingest is `retrieve_data_cache` — the FI → IPM canonical entry — which IS wrapped.)
2. **Step 10 (Score report) now wraps `fhs-app/generate_scores.py`** behind the `generate_qa_report` skill command. The skill's existing `generate_scores` is narrower (approved-scores xlsx by vendor) — `generate_qa_report` is the comprehensive RD-facing report (scored + unscorable buckets). Requires fhs-app checked out locally; see `catalog.yaml` for details.
3. **Async commands** (`--sync false`) skip postflight. Demo runs default (sync), so postflight always fires.
4. **`--db platform`** (HeroDB) is blocked pending ENG-897. Mention but don't demo.
5. **Phases 8 + 9 (view refresh + OpenSearch reindex)** are NOT live-demo'd. Dev OpenSearch isn't wired the same as prod, and the rehearsal didn't exercise these — we mention only and rely on prod ops to chain them after `backfill_fhs`. Follow-up: investigate whether the runner should auto-chain `refresh_fhs_view_for_index_command` + `index_scored_view_command` after `backfill_fhs` so this can't be silently skipped.
6. **`publish_scores_to_client` is not wrapped** in the plugin (NDO command exists; lives at `rest_api/management/commands/publish_scores_to_client.py`). It's an alternative external-send path that posts CSV rows direct to FHS API `/score/result`. `send_to_clients` (Phase 14) is the canonical send for the SR/ASR-driven flow; `publish_scores_to_client` is the raw CSV path. Follow-up: wrap it in the plugin if dietitians use it in real ops.

---

## Dummy data

Pre-stage these on dev before the demo. SQL is at [alex-scripts/sql/demo_dummy_products.sql](https://github.com/bitewell/alex-scripts) (or run inline; same content as the file).

The 5 products: structure mirrors a real Tyson-style ingestion but with obviously-fake GTINs.

Same teardown command at the end of the demo (Phase 15) archives them all via `remove_products_and_scores` (cascades to IPM + SR + ASR).

---

## Fallback if a step breaks live

| If… | Then… |
|---|---|
| FHS API returns 5xx | Skip Phase 7's "real run" — just show the dry-run preflight, narrate what would happen on success |
| DO Spaces upload fails | Use `--source` paths instead of `--ids` where possible — those don't upload |
| BentoML category-prediction times out (Phase 6) | Skip; mention it's external infra, not a plugin issue |
| Plugin not loaded in fresh session | `/plugin install foodhealthco-ndo-ops@foodhealthco` live (shows the install UX) |

---

## After the demo

- Share the [SCORING_OPS_GUIDE.md](SCORING_OPS_GUIDE.md) link in Slack
- Schedule 1:1 setup sessions with any dietitian who wants help with first-time `.env` config
- Open follow-up ticket for `bulk_create_products` (the SQL-today gap)
