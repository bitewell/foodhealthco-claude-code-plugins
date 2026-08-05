---
name: ndo-query
description: Look up NDO scoring data through the catalogued, read-only query set (dietitian schema). Use when a dietitian or operator asks about score status, the approval queue, recent approvals, unsent/unpublished scores, per-source counts, GTIN/UPC product lookups, scorability preflights, or ingestion status. Triggers include "what's the status of these products", "what's in my review queue", "was this approved/sent", "look up this UPC", "can these be scored". NOT for arbitrary SQL (engineers use db-connect) and NOT for writes (scoring/approving/publishing runs via ndo-run or the Dagster jobs).
---

# ndo-query

Catalogued, parameterized, **read-only** lookups against the NDO database —
the dietitian-safe complement to `ndo-run` (which executes ops) and
`db-connect` (engineer-only, arbitrary SQL).

## Security model (why this exists)

- You connect as **your own IAM identity** — passwordless (`cloud-sql-proxy
  --auto-iam-authn`), attributable in the DB audit log, revocable centrally.
  No `.env`, no shared password.
- Your grants cover **only the `dietitian` schema** of curated views. The
  database enforces the allowlist — the catalog is UX on top, not the fence.
- The session is pinned read-only with a statement timeout.
- Full results land in a **local CSV** (`~/.ndo-query/results/`); stdout gets
  a row count + small sample. When driven from Claude Code, bulk data stays
  out of the model context by design — don't cat the CSV back into the
  conversation; open it in Numbers/Excel or hand the path to the user.

## Routing: intent → query

| User says… | Query |
|---|---|
| "status of these products" / "were these scored/approved/sent" | `score_status --param ids=…` |
| "what's in the review queue" / "what needs approval" | `approval_queue [--param source=…]` |
| "what got approved this week" | `recent_approvals [--param days=7]` |
| "approved but not sent" / "send backlog" | `unsent_approved [--param source=…]` |
| "how many scored/approved/published for source X" / funnel overview | `source_counts [--param source=…]` |
| "look up this UPC/GTIN" | `product_lookup --param code=…` |
| "can these be scored" / pre-scoring sanity check | `scorability_check --param ids=…` |
| "did vendor X's file land" / ingestion pipeline state | `ingestion_status [--param vendor_code=…] [--param status=Pending]` |

`--list` prints the full catalog with parameter docs.

## How to invoke

Plain Python 3 with `pyyaml` + `psycopg2` (the nutrition-data-ops and
meltano-elt-pipelines poetry envs both have them):

```bash
cd /Users/alexpellas/Code/nutrition-data-ops
poetry run -- python /path/to/plugins/foodhealthco-ndo-ops/skills/ndo-query/scripts/ndo_query.py \
  score_status --param ids=12345,67890 --target prod
```

The runner auto-starts the right IAM proxy (prod `:5453`, dev `:5454` — note
these are different ports from ndo-run's password proxies on `:5443/:5444`;
one proxy process cannot serve both auth modes), reuses one already
listening, and stops only what it started (`--keep-proxy` to leave it up).

### Examples

```bash
# Review queue for one source, biggest first page
ndo_query.py approval_queue --param source=nielsen --param limit=500

# Everything about two specific products
ndo_query.py score_status --param ids=8675309,8675310

# UPC lookup (leading zeros handled)
ndo_query.py product_lookup --param code=00016000275270

# Funnel counts for a single source (fast) or all sources (slow, 300s cap)
ndo_query.py source_counts --param source=hyvee

# Ingestion backlog for a client feed
ndo_query.py ingestion_status --param vendor_code=kroger --param status=Pending
```

## Prerequisites (once per machine)

1. `brew install --cask google-cloud-sdk && brew install cloud-sql-proxy`
2. `gcloud auth login && gcloud auth application-default login`
3. Platform eng has provisioned you: `roles/cloudsql.client` +
   `roles/cloudsql.instanceUser` and a `CLOUD_IAM_USER` login (Terraform,
   `ndo-db.tf`), plus `dietitian_ro` membership (grant runbook in
   nutrition-data-ops `db/sqls/dietitian_read_surface.sql`).

No `.env` file is needed — that's the point.

## Interpreting results

- `approved` is tri-state: empty/`∅` = awaiting review, `t` = approved,
  `f` = rejected.
- `date_approved` / `date_reviewed` are Django `auto_now` columns — they move
  on any row update. Treat "approved in the last N days" as approximate.
- `pm_fhs` (on the product) and `review_fhs` / `approved_fhs` can differ; the
  approved row is what clients receive.
- GTIN/UPC zero-padding differs by source — `product_lookup` already tries
  both forms.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `role "you@foodhealth.co" does not exist` | Your IAM DB login isn't provisioned — needs the Terraform `google_sql_user` entry. |
| `permission denied for schema/table` | Auth worked; grants missing. Apply `dietitian_read_surface.sql` (Section B), or add you to `dietitian_ro` (Section A.3). |
| Proxy exits immediately | `gcloud auth application-default login`, and confirm `roles/cloudsql.client` + `roles/cloudsql.instanceUser`. |
| A column you need isn't in any view | PR to widen the view in nutrition-data-ops `db/sqls/dietitian_read_surface.sql`, re-run `create_view`, then add/extend a catalog query here. The schema stays the allowlist. |

## What this skill is NOT

- **Not arbitrary SQL.** If a real question doesn't fit any catalog entry,
  the answer is a PR adding a query (or widening a view) — not a raw psql
  session. Engineers with broader grants use `db-connect`.
- **Not a write path.** Scoring, tagging, approving, publishing = `ndo-run`
  (CLI) or the `ndo_score_product_set` / `ndo_approve_score_set` /
  `ndo_publish_score_set` Dagster jobs.
