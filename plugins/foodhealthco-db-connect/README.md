# foodhealthco-db-connect

Local connection helpers for the two production-data Postgres instances:

- **NDO** — DigitalOcean managed Postgres. Direct `psql`.
- **HeroDB** — GCP Cloud SQL Postgres. Requires `cloud-sql-proxy`.

The skill covers connection inventory (hosts, ports, projects, instance names), credential lookup via Dagster Cloud secrets, and the right `psql` invocation per env. Use it whenever Claude needs to run an audit or forensic query against either DB during an investigation.

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
- `gcloud auth list` shows your `@foodhealth.co` account.
- `~/.dagster_cloud_token` (chmod 600) — Dagster Cloud user token. Generate in Dagster Cloud → User settings → Tokens.

## What the skill knows

- The connection name for each Cloud SQL HeroDB instance (dev / staging / prod).
- The DigitalOcean hosts for NDO prod and dev.
- How to pull a password from Dagster Cloud secrets via GraphQL.
- Sanity checks (row counts, `current_database()`) to catch "you connected to the wrong env".
- Common pitfalls (proxy dies between bash calls, public IP not allowlisted, "prod"-named instance is actually dev).

See [`skills/db-connect/SKILL.md`](skills/db-connect/SKILL.md) for the full reference.
