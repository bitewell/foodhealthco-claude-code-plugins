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
| 2 | **Create products** *(SQL today, skill soon)* | Open the SQL template, run via `psql` on dev | `psql` + SQL — **currently a manual step** | Be honest: *"This is the one part still done by hand today. ENG-NNN wraps it in a `bulk_create_products` skill command so dietitians can upload a CSV directly. For now: SQL."* |
| 3 | **Match** *(optional, mention briefly)* | "Skipping — our CSV came in with GTINs already." Mention that for products created via `foringestion`, you'd run `match_products --source demo_X` | `match_products` | Just name the command; move on |
| 4 | **Tag** | "Run backfill_tags on source demo_X against dev" | `backfill_tags` | First preflight read-out — explain the buckets. `is_deep_fried` fires for Product #2 |
| 5 | **Impute** | "Run backfill_imputation on source demo_X" | `backfill_imputation` | Preflight: "Will impute: 1, Already complete: 4" — the missing-protein row gets filled |
| 6 | **Categorize** | "Run backfill_categories on source demo_X" | `backfill_categories` | Preflight: "Will categorize: 1, Already categorized: 4" — only Product #2 hits BentoML |
| 7 | **Score** | "Run backfill_fhs on source demo_X" | `backfill_fhs` | Preflight surfaces `✗ Missing macros` block bucket on Product #4 — that one **won't** score. **Failure is loud and prevented upfront, not silent.** This is the headline feature |
| 8 | **Score report** | "Generate the QA xlsx for source demo_X from those 5 IDs" | `generate_qa_report --ids 1,2,3,4,5 --source demo_X` (shells out to `fhs-app/generate_scores.py`, output xlsx lands in `fhs-app/output_scores/`) | "Same skill interface, different tool underneath. Postflight reports how many xlsx files landed — 0 means fhs-app failed silently. The xlsx is what RD reviews." |
| 9 | **Backfill** (after RD review) | "RD flagged Product #5's tag. I edited the source CSV — re-apply with backfill_ni_profiles" | `backfill_ni_profiles --csv corrections.csv --target dev -- -if is_deep_fried` | Shows the round-trip: dietitian edits → CSV → DB. Preflight confirms the update. |
| 10 | **Re-score** | "Re-run backfill_fhs on the edited IDs" | `backfill_fhs --ids ...` | Postflight verifies new scoring result rows landed for the corrected products |
| 11 | **Approve** | "Approve scores against dev — here's the CSV with product_id, fhs" | `approve_scores --csv approvals.csv` | Preflight cross-checks each row's `fhs` against the stored `ScoringResult.fhs`. Show **one matching + one intentionally mismatched** row — narrate the rejection |
| 12 | **Send to client** | "Send to clients for those IDs with client_id=demo_client" | `send_to_clients -- -c demo_client` | Mention this hits external publishers in prod; on dev it's a no-op against a fake client. Postflight verifies `published` flag flipped |
| 13 | **Archive** | "Archive the demo products via archive_table -m productmatch" | `archive_table -- -m productmatch -r 'demo cleanup' -om 'alex' -ot delete` | Cleans up after the demo. Show preflight bucketing on `entity_archive`. `-m approvedscoringresult` archives only scores; `-m productmatch` archives the IPM row |

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

1. **Step 2 (Create products) is still SQL.** The plugin doesn't wrap product creation from CSV yet. Frame as a known gap; name the follow-up ticket.
2. **Step 8 (Score report) now wraps `fhs-app/generate_scores.py`** behind the `generate_qa_report` skill command. The skill's existing `generate_scores` is narrower (approved-scores xlsx by vendor) — `generate_qa_report` is the comprehensive RD-facing report (scored + unscorable buckets). Requires fhs-app checked out locally; see `catalog.yaml` for details.
3. **Async commands** (`--sync false`) skip postflight. Demo runs default (sync), so postflight always fires.
4. **`--db platform`** (HeroDB) is blocked pending ENG-897. Mention but don't demo.

---

## Dummy data

Pre-stage these on dev before the demo. SQL is at [alex-scripts/sql/demo_dummy_products.sql](https://github.com/bitewell/alex-scripts) (or run inline; same content as the file).

The 5 products: structure mirrors a real Tyson-style ingestion but with obviously-fake GTINs.

Same teardown command at the end of the demo (Phase 13) archives them all by `-m productmatch`.

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
