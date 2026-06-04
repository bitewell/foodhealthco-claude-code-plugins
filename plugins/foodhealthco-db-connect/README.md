# foodhealthco-db-connect

Local connection helpers for the two production-data Postgres instances:

- **NDO** — DigitalOcean managed Postgres. Direct `psql`.
- **HeroDB** — GCP Cloud SQL Postgres. Requires `cloud-sql-proxy`.

The skill covers connection inventory (hosts, ports, projects, instance names), **passwordless IAM auth for HeroDB** (you connect under your own `@foodhealth.co` identity, so queries are attributable to you) with break-glass credential lookup as fallback, and the right `psql` invocation per env. Use it whenever Claude needs to run an audit or forensic query against either DB during an investigation.

## Install

In any Claude Code session:

```
/plugin marketplace add bitewell/foodhealthco-claude-code-plugins
/plugin install foodhealthco-db-connect@foodhealthco
```

Then ask Claude to e.g. "query gtin_matrix on HeroDB prod" or "check `for_ingestion` for GTIN X" and the skill will trigger.

## Prerequisites (one-time, on each machine)

- `psql` (Postgres 17 client). `brew install postgresql@17`.
- `cloud-sql-proxy`. `brew install cloud-sql-proxy`.
- **IAM auth (the HeroDB default):**
  - `gcloud auth login` — your `@foodhealth.co` account (`gcloud auth list` shows it active).
  - `gcloud auth application-default login` — ADC, so `cloud-sql-proxy --auto-iam-authn` can mint IAM tokens.
  - Your account holds `roles/cloudsql.client` + `roles/cloudsql.instanceUser` and is in the `gke-developers@foodhealth.co` group (which has the HeroDB read grant on dev + prod). Engineers in that group are covered automatically.
- `~/.dagster_cloud_token` (chmod 600) — Dagster Cloud user token, **needed for NDO password pulls and HeroDB break-glass only**, not the HeroDB default. Generate in Dagster Cloud → User settings → Tokens.

## What the skill knows

- That HeroDB connects under your **own IAM identity, passwordless** (`--auto-iam-authn`) by default — and that `dagster`-owned **writes** route through Dagster, not a shared password, so the audit trail ties to the actor.
- The connection name for each Cloud SQL HeroDB instance (dev / staging / prod).
- The DigitalOcean hosts for NDO prod and dev.
- The break-glass password path (Dagster Cloud secrets / GCP Secret Manager) for when IAM is unavailable.
- Sanity checks (row counts, `current_database()`) to catch "you connected to the wrong env".
- Common pitfalls (IAM needs ADC + Cloud SQL roles, proxy dies between bash calls, public IP not allowlisted, "prod"-named instance is actually dev).

See [`skills/db-connect/SKILL.md`](skills/db-connect/SKILL.md) for the full reference.
