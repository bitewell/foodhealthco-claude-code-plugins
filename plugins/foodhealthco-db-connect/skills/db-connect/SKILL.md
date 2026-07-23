---
name: db-connect
description: Connect to FoodHealth's NDO Postgres (DigitalOcean managed) and HeroDB (GCP Cloud SQL via cloud-sql-proxy). HeroDB connects under the operator's own IAM identity (passwordless, --auto-iam-authn); NDO uses a password. Handles proxy lifecycle, IAM/credential auth, and the right `psql` invocation for each env. Use when the user asks to query NDO or HeroDB, run audit/forensic SQL, verify data after a Dagster or Databricks run, or set up a local DB connection for the first time. Trigger phrases include "query NDO", "query HeroDB", "connect to herodb", "run this SQL on prod", "check the for_ingestion table", "check gtin_matrix".
---

# db-connect

Reach FoodHealth's production data stores from a local machine in a consistent, repeatable way.

Two databases, two patterns:

- **NDO (Django ingestion DB)** — DigitalOcean managed Postgres. Public-IP, direct `psql` works.
- **HeroDB (operational DB)** — GCP Cloud SQL Postgres. Requires `cloud-sql-proxy` because public IP isn't allowlisted from a laptop.

**Auth model — read this first.** For HeroDB the default is your **own IAM identity, passwordless**: `cloud-sql-proxy --auto-iam-authn` injects your Google OAuth token and you connect as your `@foodhealth.co` email, so every query is attributable to you in the DB audit log. The shared `dagster` password is **break-glass fallback only**. When an operation needs `dagster`-owned **write** privileges, don't connect as `dagster` from a laptop — route it through Dagster (see [Writes & privileged operations](#writes--privileged-operations)) so the trace ties to the actor. **NDO moved off DigitalOcean (2026-07-17):** it now lives on the same hero-db Cloud SQL instances as a **peer database `ndo`**, reached via the same proxy — but its tables are owned by the password-based **`ndo`** role, so NDO queries use that role's password (not IAM).

## Connection inventory

### NDO Postgres (now on GCP Cloud SQL — migrated off DigitalOcean 2026-07-17)

The DigitalOcean NDO Postgres is **DELETED**. NDO now lives on the **same hero-db Cloud SQL instances** as a **peer database `ndo`** (schema `public`; every table/view name identical to the old DO `defaultdb`). Connect exactly like HeroDB below, but `dbname=ndo` as the **`ndo` role** — which owns all NDO tables (`dagster`/IAM authenticate but have **no grants** on them).

| Env | Connection name | DB | User | Password source |
|---|---|---|---|---|
| **prod** | `foodhealth-platform-prod:us-central1:hero-db-prod` | `ndo` | `ndo` | Dagster Cloud secret `NDO_PROD_DB_PASSWORD` (cache to `~/.herodb_ndo_password`) |
| **dev**  | `foodhealth-platform-dev:us-central1:hero-db-dev`   | `ndo` | `ndo` | Dagster Cloud secret `NDO_DEV_DB_PASSWORD` |

Because the `ndo` role is password-based, it needs a **non-IAM** proxy (the default `--auto-iam-authn` proxy serves IAM only) — run one on its own port (see the NDO recipe below).

### HeroDB (GCP Cloud SQL)

| Env | Connection name | Local port | Break-glass `dagster` password (fallback) |
|---|---|---|---|
| **dev** | `foodhealth-platform-dev:us-central1:hero-db-dev` | `5434` | GCP Secret Manager `dagster-pwd` in `foodhealth-platform-dev`, cached at `~/.herodb_dev_db_password` |
| **staging** | `foodhealth-platform-staging:us-central1:foodhealth-postgres-staging` | `5435` | rarely used; fetch its own `dagster` password before connecting |
| **prod** | `foodhealth-platform-prod:us-central1:hero-db-prod` | `5433` | Dagster Cloud secret `HERODB_DB_PASSWORD`, cached at `~/.herodb_db_password` (public IP `34.70.167.23`; confirmed 2026-05-07) |

DB: `herodb`. **Default user = your own `@foodhealth.co` IAM identity (passwordless)** — derive it with `gcloud config get-value account`; read access comes via the `gke-developers@foodhealth.co` group, which is granted `SELECT` on schema `public` in both dev and prod (prod grant added 2026-06-02). The `dagster` role (the table owner) is the break-glass fallback; **each env has a DIFFERENT `dagster` password and its own fixed local port — dev and prod creds are not interchangeable.** Port convention: prod `5433`, dev `5434` (offset from a local Postgres on `5432`). The connection name encodes `project:region:instance` and is the source of truth.

## Break-glass: pulling the `dagster` password

> **HeroDB default is IAM (passwordless) — you usually don't need this.** Use the password path only when IAM is unavailable (no ADC, or your account isn't yet in the IAM DB-users group) or when connecting as the `dagster` owner for a one-time admin task. **NDO**, by contrast, always uses these passwords (DigitalOcean has no IAM auth).

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

### HeroDB dev password (GCP Secret Manager, not Dagster Cloud)

Dev's `dagster` password is NOT the same as prod's, and it does not come from Dagster Cloud — it lives in GCP Secret Manager in the `foodhealth-platform-dev` project as `dagster-pwd`:

```bash
umask 177
gcloud secrets versions access latest --secret=dagster-pwd \
  --project=foodhealth-platform-dev > ~/.herodb_dev_db_password
chmod 600 ~/.herodb_dev_db_password
```

> `foodhealth-platform-dev` also holds `HERODB_RW_*` / `HERODB_RO_*` role creds, but this skill standardizes on `user=dagster`, whose password is `dagster-pwd`.

## Connecting

### NDO (now via a non-IAM Cloud SQL proxy on hero-db)

The DO host is gone; NDO is the `ndo` peer DB on hero-db (above). The `ndo` role is password-based, so use a proxy WITHOUT `--auto-iam-authn` on its own port:

```bash
cloud-sql-proxy foodhealth-platform-prod:us-central1:hero-db-prod --port 5443 &   # non-IAM
PGPASSWORD=$(cat ~/.herodb_ndo_password) psql \
  "host=127.0.0.1 port=5443 user=ndo dbname=ndo sslmode=disable" \
  -c "<sql>"
lsof -nP -iTCP:5443 -sTCP:LISTEN -t | xargs kill   # when done
```

### HeroDB (proxy required — IAM, passwordless)

**Always ensure the proxy for the target env is listening before running a query.** The helper runs the proxy with `--auto-iam-authn`, so the proxy injects your IAM token and you connect as your own `@foodhealth.co` identity with **no password**. It no-ops when the proxy is already up, so it is safe to call before every query:

```bash
# Ensure the HeroDB IAM proxy for an env is listening (idempotent). Run before EVERY query.
herodb_proxy_up() {
  local env="$1" port conn
  case "$env" in
    prod) port=5433; conn="foodhealth-platform-prod:us-central1:hero-db-prod" ;;
    dev)  port=5434; conn="foodhealth-platform-dev:us-central1:hero-db-dev" ;;
    *) echo "unknown env: $env (use dev|prod)" >&2; return 1 ;;
  esac
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "herodb $env proxy already up on $port"; return 0
  fi
  nohup cloud-sql-proxy "$conn" --port "$port" --auto-iam-authn >"/tmp/herodb-proxy-$env.log" 2>&1 &
  for _ in $(seq 1 20); do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1 && { echo "started herodb $env proxy on $port"; return 0; }
    sleep 0.5
  done
  echo "herodb $env proxy failed to bind on $port — see /tmp/herodb-proxy-$env.log" >&2; return 1
}

# Your IAM DB username is your active gcloud account. Note `psql -w` (never prompt for a password).
IAM_USER=$(gcloud config get-value account)   # e.g. alex@foodhealth.co

# DEV (port 5434)
herodb_proxy_up dev
psql -w "host=127.0.0.1 port=5434 user=${IAM_USER} dbname=herodb" -c "<sql>"

# PROD (port 5433)
herodb_proxy_up prod
psql -w "host=127.0.0.1 port=5433 user=${IAM_USER} dbname=herodb" -c "<sql>"

# Stop a proxy when done
lsof -nP -iTCP:5434 -sTCP:LISTEN -t | xargs kill   # dev
lsof -nP -iTCP:5433 -sTCP:LISTEN -t | xargs kill   # prod
```

#### Break-glass (password, no IAM)

Only when IAM is unavailable. The `dagster` password path needs a proxy **without** `--auto-iam-authn` (one proxy can't serve both modes), so bring one up on a separate temp port and tear it down after:

```bash
cloud-sql-proxy "foodhealth-platform-prod:us-central1:hero-db-prod" --port 5443 &   # no --auto-iam-authn
PGPASSWORD=$(cat ~/.herodb_db_password) psql \
  "host=127.0.0.1 port=5443 user=dagster dbname=herodb" -c "<sql>"
lsof -nP -iTCP:5443 -sTCP:LISTEN -t | xargs kill
```

> **Claude Code caveat.** The Bash tool tears down the proxy's process tree when the tool call returns, so a `&`/`nohup` proxy started *inside* a query call won't survive to the next call. When driving this from Claude Code, start each proxy in its **own** Bash call with `run_in_background: true`, wait for it to bind, then run queries in later calls. The listening-check (the `lsof -sTCP:LISTEN` line in `herodb_proxy_up`) is still the right precondition to run before every query — only the start path differs.

## Writes & privileged operations

The IAM identity above is the **read / investigation** path. Application tables in HeroDB are owned by the `dagster` role, and writes to them should be **attributable to the job that made them**, so:

- **Don't** hand a human the shared `dagster` password to run writes from a laptop — that breaks the audit trail (every change looks like `dagster`).
- **Do** route privileged writes through Dagster, which executes them under its own tracked run/identity. The `foodhealthco-ndo-ops` plugin is the model: its `ndo_run.py` runner wraps the `manage.py` ops, and the Dagster orchestrator (`dagster_ndo/jobs/scoring_chain.py`) shells into it, so the operation traces back to the run that triggered it.
- A genuine **one-time admin** task that must run as the `dagster` owner (e.g. granting a read role) is the rare exception — use the break-glass password path above, and prefer a `GRANT` to a Google group over per-user grants (this is how `gke-developers@foodhealth.co` got prod read access on 2026-06-02).

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

- **Prefer IAM (passwordless) for HeroDB** — no secret on disk, and queries are attributable to you. Reach for a password file only on the break-glass path.
- Any credential file you do create MUST be `chmod 600`. Verify with `ls -la ~/.<file>`.
- Never echo a credential into a shell command line that gets logged. Use `PGPASSWORD=$(cat ~/.<file>)` so the secret stays out of `ps`.
- After break-glass work is complete, suggest the user rotate: Dagster Cloud token (User settings → Tokens), HeroDB password (DB admin), NDO password (DigitalOcean console).

## Common pitfalls

- **IAM connect fails with an auth error.** The proxy needs Application Default Credentials (`gcloud auth application-default login`) to mint IAM tokens, and your account needs `roles/cloudsql.client` + `roles/cloudsql.instanceUser`. Pass `user=$(gcloud config get-value account)` — the IAM DB username is your full email, not `dagster`.
- **IAM connects but `permission denied for table`.** Auth worked; you're only missing a table grant. Read access comes via the `gke-developers@foodhealth.co` group `SELECT` grant — if a newly created table isn't readable, the grant / default privileges may need extending (a `dagster`-owner break-glass task).
- **Mixing IAM and password on one port.** A proxy started with `--auto-iam-authn` serves IAM only; a `dagster` password connection needs a separate proxy without that flag on its own port. Don't expect one proxy to do both.
- **Direct psql to HeroDB times out.** Cloud SQL public IP isn't allowlisted from arbitrary networks. Always use `cloud-sql-proxy`.
- **`gcloud sql instances list` returns 0 items.** Cloud SQL Admin API isn't enabled in that GCP project, or your account lacks `roles/cloudsql.viewer`. Try `--project=foodhealth-platform-dev` if the current default doesn't have HeroDB.
- **Background proxy dies between bash calls.** When invoking via Claude Code, use `run_in_background: true`. A simple `&` only lives as long as the parent shell.
- **Verify the env you're on.** Dagster Cloud has a deployment named `prod`, and its `HERODB_DB_HOST` secret points to `34.70.167.23` (the `hero-db-prod` Cloud SQL instance). If you're querying for production data, double-check via `inet_server_addr()` or row counts before drawing conclusions.

## Suggested workflow when the user says "run this query on HeroDB <env>"

1. Map env → port + connection name (prod `5433`, dev `5434`; the table above is the source of truth).
2. Confirm your IAM identity: `IAM_USER=$(gcloud config get-value account)` shows your `@foodhealth.co` account (and ADC is set). No password file needed for the default path.
3. Ensure the IAM proxy is listening on that port (`herodb_proxy_up <env>`, or `lsof -nP -iTCP:<port> -sTCP:LISTEN`). If not, start it with `--auto-iam-authn` via `run_in_background: true` and wait for it to bind.
4. Run the query passwordless: `psql -w "host=127.0.0.1 port=<port> user=${IAM_USER} dbname=herodb" -c "<sql>"`.
5. Sanity-check the result against the expected env — e.g. prod `nutrient_profiles` is ~7M, dev ~1.3M.
6. If you hit `permission denied for table`, auth is fine — it's a missing grant (see pitfalls); fall back to break-glass only if truly blocked.
7. Leave the proxy running for follow-ups; kill when the user is done: `lsof -nP -iTCP:<port> -sTCP:LISTEN -t | xargs kill`.

## Updating this skill

When you discover a missing connection name, a new env, or a confirmed-prod instance, edit this `SKILL.md` directly and PR. The connection-inventory table is the most-referenced section — keep it accurate.
