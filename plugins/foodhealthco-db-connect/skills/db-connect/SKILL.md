---
name: db-connect
description: Connect to FoodHealth's NDO Postgres (DigitalOcean managed) and HeroDB (GCP Cloud SQL via cloud-sql-proxy). Handles proxy lifecycle, credential lookup from Dagster Cloud secrets, and the right `psql` invocation for each env. Use when the user asks to query NDO or HeroDB, run audit/forensic SQL, verify data after a Dagster or Databricks run, or set up a local DB connection for the first time. Trigger phrases include "query NDO", "query HeroDB", "connect to herodb", "run this SQL on prod", "check the for_ingestion table", "check gtin_matrix".
---

# db-connect

Reach FoodHealth's production data stores from a local machine in a consistent, repeatable way.

Two databases, two patterns:

- **NDO (Django ingestion DB)** — DigitalOcean managed Postgres. Public-IP, direct `psql` works.
- **HeroDB (operational DB)** — GCP Cloud SQL Postgres. Requires `cloud-sql-proxy` because public IP isn't allowlisted from a laptop.

## Connection inventory

### NDO Postgres (DigitalOcean)

| Env | Host | Port | DB | User | Password source |
|---|---|---|---|---|---|
| **prod** | `ndo-production-database-do-user-12255452-0.e.db.ondigitalocean.com` | `25060` | `defaultdb` | `doadmin` | Dagster Cloud secret `NDO_PROD_DB_PASSWORD` (or `bitewell-databricks` cluster env `DB_CONNECTION_STRING`) |
| **dev** | `ndo-db-development-do-user-12255452-0.d.db.ondigitalocean.com` | `25060` | `defaultdb` | `doadmin` | Dagster Cloud secret `NDO_DEV_DB_PASSWORD` |

`sslmode=require` is mandatory.

### HeroDB (GCP Cloud SQL)

| Env | Project | Region | Instance | Connection name |
|---|---|---|---|---|
| **dev** | `foodhealth-platform-dev` | `us-central1` | `hero-db-dev` | `foodhealth-platform-dev:us-central1:hero-db-dev` |
| **staging** | `foodhealth-platform-staging` | `us-central1` | `foodhealth-postgres-staging` | `foodhealth-platform-staging:us-central1:foodhealth-postgres-staging` |
| **prod** | `foodhealth-platform-prod` | `us-central1` | `hero-db-prod` | `foodhealth-platform-prod:us-central1:hero-db-prod` (public IP `34.70.167.23`; confirmed against Dagster Cloud `prod` deployment secrets, 2026-05-07) |

DB: `herodb`. User: `dagster`. Local proxy port: pick a free port (default `15432`). Password: Dagster Cloud secret `HERODB_DB_PASSWORD`.

## Pulling a credential from Dagster Cloud

If the local password file doesn't exist, fetch it via the Dagster Cloud GraphQL API:

```bash
SECRET_NAME='HERODB_DB_PASSWORD'      # or NDO_PROD_DB_PASSWORD, NDO_DEV_DB_PASSWORD, etc.
OUTFILE="$HOME/.$(echo "$SECRET_NAME" | tr '[:upper:]' '[:lower:]')"

curl -sS -X POST 'https://food-health-company.dagster.cloud/prod/graphql' \
  -H "Dagster-Cloud-Api-Token: $(cat ~/.dagster_cloud_token)" \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ secretsOrError { __typename ... on Secrets { secrets { secretName secretValue } } } }"}' \
  | SECRET_NAME="$SECRET_NAME" python3 -c "
import json, os, sys
d = json.load(sys.stdin)
target = os.environ['SECRET_NAME']
for s in d.get('data', {}).get('secretsOrError', {}).get('secrets', []) or []:
    if s.get('secretName') == target:
        sys.stdout.write(s.get('secretValue') or '')
        sys.exit(0)
sys.exit(f'secret {target} not found')
" > "$OUTFILE"
chmod 600 "$OUTFILE"
echo "wrote $OUTFILE"
```

> The Dagster Cloud `prod` deployment endpoint (`/prod/graphql`) is the deployment name, not necessarily the production-data DB. Read the secret values and verify host before drawing conclusions about which env you're on.

## Connecting

### NDO (no proxy)

```bash
PGPASSWORD=$(cat ~/.ndo_prod_db_password) psql \
  "host=ndo-production-database-do-user-12255452-0.e.db.ondigitalocean.com port=25060 user=doadmin dbname=defaultdb sslmode=require" \
  -c "<sql>"
```

### HeroDB (proxy required)

```bash
# 1. Start the proxy. In Claude Code, ALWAYS start with run_in_background: true so
#    the proxy survives across tool calls — a `&`-backgrounded proxy dies when the
#    spawning bash invocation finishes.
cloud-sql-proxy "<connection-name>" --port 15432 &
sleep 3   # let it bind

# 2. Run queries
PGPASSWORD=$(cat ~/.herodb_db_password) psql \
  "host=127.0.0.1 port=15432 user=dagster dbname=herodb" \
  -c "<sql>"

# 3. Stop the proxy when done
lsof -ti:15432 | xargs kill
```

## Sanity checks after connecting

```sql
SELECT current_database(), current_user, inet_server_addr();
```

For HeroDB, double-check via row counts on a known table:

```sql
SELECT COUNT(*) FROM gtin_matrix;        -- if much smaller than expected, you're probably on dev
SELECT COUNT(*) FROM nutrient_profiles;
```

## Security

- Every credential file MUST be `chmod 600`. Verify with `ls -la ~/.<file>`.
- Never echo a credential into a shell command line that gets logged. Use `PGPASSWORD=$(cat ~/.<file>)` so the secret stays out of `ps`.
- After investigation work is complete, suggest the user rotate: Dagster Cloud token (User settings → Tokens), HeroDB password (DB admin), NDO password (DigitalOcean console).

## Common pitfalls

- **Direct psql to HeroDB times out.** Cloud SQL public IP isn't allowlisted from arbitrary networks. Always use `cloud-sql-proxy`.
- **`gcloud sql instances list` returns 0 items.** Cloud SQL Admin API isn't enabled in that GCP project, or your account lacks `roles/cloudsql.viewer`. Try `--project=foodhealth-platform-dev` if the current default doesn't have HeroDB.
- **Background proxy dies between bash calls.** When invoking via Claude Code, use `run_in_background: true`. A simple `&` only lives as long as the parent shell.
- **Verify the env you're on.** Dagster Cloud has a deployment named `prod`, and its `HERODB_DB_HOST` secret points to `34.70.167.23` (the `hero-db-prod` Cloud SQL instance). If you're querying for production data, double-check via `inet_server_addr()` or row counts before drawing conclusions.

## Suggested workflow when the user says "run this query on HeroDB prod"

1. Confirm which connection name to use (the table above is the source of truth).
2. Check `~/.herodb_db_password` exists and is `chmod 600`. If not, fetch from Dagster Cloud (above).
3. Start `cloud-sql-proxy <connection-name> --port 15432` with `run_in_background: true`.
4. Run the query.
5. Sanity-check the result (row counts, table existence) against expected env.
6. Kill the proxy when the user is done: `lsof -ti:15432 | xargs kill`.

## Updating this skill

When you discover a missing connection name, a new env, or a confirmed-prod instance, edit this `SKILL.md` directly and PR. The connection-inventory table is the most-referenced section — keep it accurate.
