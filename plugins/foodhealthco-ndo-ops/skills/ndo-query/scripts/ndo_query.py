#!/usr/bin/env python3
"""ndo_query — run a catalogued, parameterized read query against the NDO DB.

Dietitian-safe by construction:
  * Connects as the operator's OWN IAM identity (cloud-sql-proxy
    --auto-iam-authn, passwordless) — every query is attributable, and the
    role's grants (SELECT on the `dietitian` view schema only) are the real
    security boundary. No .env, no shared password, no secrets on disk.
  * Only queries named in queries.yaml can run; parameters are typed, coerced,
    and bound server-side (psycopg2 params — never string-interpolated).
  * The session is pinned read-only (default_transaction_read_only=on) with a
    statement timeout, belt-and-braces on top of the grants.
  * Full results are written to a LOCAL CSV; stdout gets only a row count and
    a small sample, so bulk data doesn't transit the model context when this
    is driven from Claude Code.

Usage:
  ndo_query.py --list
  ndo_query.py <query> [--param k=v ...] [--target prod|dev] [--show N]
  ndo_query.py score_status --param ids=12345,67890 --target prod
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import yaml

CATALOG_PATH = Path(__file__).resolve().parent.parent / "queries.yaml"
RESULTS_DIR = Path(os.environ.get("NDO_QUERY_RESULTS_DIR", "~/.ndo-query/results")).expanduser()

# IAM proxies get their own ports — a proxy started with --auto-iam-authn can't
# serve password logins, so these must not collide with ndo-run's non-IAM
# proxies (prod 5443 / dev 5444).
CLOUDSQL_INSTANCES = {
    "prod": "foodhealth-platform-prod:us-central1:hero-db-prod",
    "dev": "foodhealth-platform-dev:us-central1:hero-db-dev",
}
IAM_PROXY_PORTS = {"prod": 5453, "dev": 5454}
PROXY_READY_TIMEOUT_S = 20

DEFAULT_TIMEOUT_S = 120
DEFAULT_SHOW_ROWS = 10
MAX_SHOW_ROWS = 50
MAX_CELL_WIDTH = 36


# ----------------------------------------------------------------------------
# Catalog
# ----------------------------------------------------------------------------

def load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict]:
    with open(path) as fd:
        doc = yaml.safe_load(fd)
    queries = doc.get("queries", {})
    if not queries:
        sys.exit(f"error: no queries found in {path}")
    return queries


def coerce_param(name: str, spec: dict, raw: Optional[str]) -> Any:
    """Coerce a raw CLI value against the catalog's typed param spec."""
    ptype = spec.get("type", "str")
    if raw is None:
        if "default" in spec:
            raw_val = spec["default"]
            # defaults in the YAML are already typed; clamp ints below anyway
            return _clamp(name, spec, raw_val)
        if spec.get("required", False):
            sys.exit(f"error: missing required param --param {name}=<{ptype}>")
        # optional with no default: empty sentinel by type
        return [] if ptype.endswith("_list") else ""
    if ptype == "int":
        try:
            return _clamp(name, spec, int(raw))
        except ValueError:
            sys.exit(f"error: param {name} must be an integer (got {raw!r})")
    if ptype == "int_list":
        try:
            vals = [int(v) for v in str(raw).replace(" ", "").split(",") if v]
        except ValueError:
            sys.exit(f"error: param {name} must be a comma-separated list of integers")
        if not vals:
            sys.exit(f"error: param {name} is empty")
        max_items = spec.get("max_items", 10000)
        if len(vals) > max_items:
            sys.exit(f"error: param {name} has {len(vals)} items (max {max_items})")
        return vals
    if ptype == "str_list":
        vals = [v.strip() for v in str(raw).split(",") if v.strip()]
        if not vals:
            sys.exit(f"error: param {name} is empty")
        return vals
    if ptype == "date":
        try:
            return dt.date.fromisoformat(str(raw))
        except ValueError:
            sys.exit(f"error: param {name} must be an ISO date (YYYY-MM-DD)")
    # plain str
    return str(raw)


def _clamp(name: str, spec: dict, val: Any) -> Any:
    if isinstance(val, int) and "max" in spec and val > spec["max"]:
        print(f"[clamp] {name}={val} exceeds max {spec['max']} — clamped", file=sys.stderr)
        return spec["max"]
    return val


# ----------------------------------------------------------------------------
# IAM identity + proxy
# ----------------------------------------------------------------------------

def iam_account() -> str:
    gcloud = shutil.which("gcloud")
    if not gcloud:
        sys.exit(
            "error: `gcloud` not found. Install the Google Cloud SDK "
            "(brew install --cask google-cloud-sdk), then:\n"
            "  gcloud auth login && gcloud auth application-default login"
        )
    out = subprocess.run(
        [gcloud, "config", "get-value", "account"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    if not out or out == "(unset)":
        sys.exit(
            "error: no active gcloud account. Run:\n"
            "  gcloud auth login && gcloud auth application-default login"
        )
    return out


def _port_listening(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class ProxyHandle:
    def __init__(self, proc: subprocess.Popen, logf, port: int) -> None:
        self.proc, self.logf, self.port = proc, logf, port


def start_iam_proxy_if_needed(target: str) -> Optional[ProxyHandle]:
    """Ensure an --auto-iam-authn proxy serves `target`; start one if needed.

    Returns a handle only for a proxy THIS call started (caller stops it);
    None when one was already listening.
    """
    port = IAM_PROXY_PORTS[target]
    if _port_listening(port):
        return None
    binary = shutil.which("cloud-sql-proxy")
    if not binary:
        sys.exit(
            "error: `cloud-sql-proxy` is not on your PATH.\n"
            "  Install it: brew install cloud-sql-proxy"
        )
    instance = CLOUDSQL_INSTANCES[target]
    print(f"[proxy] starting IAM cloud-sql-proxy for {instance} on :{port} …", flush=True)
    logf = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [binary, instance, "--port", str(port), "--auto-iam-authn"],
        stdout=logf, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + PROXY_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            logf.seek(0)
            out = logf.read().decode("utf-8", "replace")
            logf.close()
            sys.exit(
                "error: cloud-sql-proxy exited before it was ready:\n"
                f"{out}\n"
                "Check `gcloud auth application-default login` and that your "
                "account has roles/cloudsql.client + roles/cloudsql.instanceUser."
            )
        if _port_listening(port):
            print(f"[proxy] ready on 127.0.0.1:{port}", flush=True)
            return ProxyHandle(proc, logf, port)
        time.sleep(0.4)
    proc.terminate()
    logf.close()
    sys.exit(f"error: proxy not ready on :{port} within {PROXY_READY_TIMEOUT_S}s")


def stop_proxy(handle: Optional[ProxyHandle]) -> None:
    if handle is None:
        return
    try:
        handle.proc.terminate()
        try:
            handle.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
    finally:
        try:
            handle.logf.close()
        except Exception:
            pass
        print(f"[proxy] stopped (:{handle.port})", flush=True)


# ----------------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------------

def run_query(name: str, entry: dict, params: dict, target: str, user: str):
    import psycopg2  # deferred so --list works without it

    port = IAM_PROXY_PORTS[target]
    timeout_ms = int(entry.get("timeout_s", DEFAULT_TIMEOUT_S)) * 1000
    conn = psycopg2.connect(
        host="127.0.0.1", port=port, dbname="ndo", user=user,
        # No password: the IAM proxy injects the operator's OAuth token.
        options=(
            f"-c search_path=dietitian "
            f"-c default_transaction_read_only=on "
            f"-c statement_timeout={timeout_ms}"
        ),
        connect_timeout=10,
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(entry["sql"], params)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()
    return cols, rows


def write_csv(name: str, cols: list[str], rows: list[tuple]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{name}_{ts}.csv"
    with open(path, "w", newline="") as fd:
        w = csv.writer(fd)
        w.writerow(cols)
        for row in rows:
            w.writerow(["" if v is None else v for v in row])
    return path


def print_sample(cols: list[str], rows: list[tuple], show: int, max_cell: int = MAX_CELL_WIDTH) -> None:
    def fmt(v: Any) -> str:
        s = "∅" if v is None else str(v)
        return s if len(s) <= max_cell else s[: max_cell - 1] + "…"

    sample = [tuple(fmt(v) for v in r) for r in rows[:show]]
    widths = [len(c) for c in cols]
    for r in sample:
        widths = [max(w, len(v)) for w, v in zip(widths, r)]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in sample:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def guidance_for(exc: Exception) -> str:
    msg = str(exc)
    if "permission denied" in msg:
        return (
            "Your IAM login authenticated but lacks a grant. Either the "
            "dietitian schema grants haven't been applied on this env "
            "(db/sqls/dietitian_read_surface.sql, Sections A+B), or your "
            "account isn't a member of dietitian_ro yet (Section A.3)."
        )
    if "does not exist" in msg and "role" in msg:
        return (
            "Your IAM DB login doesn't exist on this instance yet — it's "
            "created by Terraform (google_sql_user in ndo-db.tf). Ask platform "
            "eng to add you."
        )
    if "connection refused" in msg.lower():
        return "The proxy isn't listening — rerun (the runner auto-starts it)."
    return ""


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="catalog query name (see --list)")
    ap.add_argument("--list", action="store_true", help="list catalogued queries and exit")
    ap.add_argument("--param", action="append", default=[], metavar="K=V",
                    help="query parameter, repeatable (e.g. --param ids=1,2,3)")
    ap.add_argument("--target", choices=("prod", "dev"), default="prod",
                    help="environment (default prod — reads are grant-gated)")
    ap.add_argument("--show", type=int, default=DEFAULT_SHOW_ROWS,
                    help=f"sample rows to print (default {DEFAULT_SHOW_ROWS}, max {MAX_SHOW_ROWS})")
    ap.add_argument("--show-sql", action="store_true", help="print the SQL before running")
    ap.add_argument("--keep-proxy", action="store_true",
                    help="leave a proxy this run started running (faster follow-ups)")
    args = ap.parse_args(argv)

    catalog = load_catalog()

    if args.list or not args.query:
        print(f"{len(catalog)} catalogued queries (target with: ndo_query.py <name> --param k=v):\n")
        for qname, entry in catalog.items():
            print(f"  {qname}")
            print(f"      {entry.get('description', '').strip()}")
            for pname, spec in (entry.get("params") or {}).items():
                bits = [spec.get("type", "str")]
                if spec.get("required"):
                    bits.append("required")
                if "default" in spec:
                    bits.append(f"default={spec['default']}")
                if "max" in spec:
                    bits.append(f"max={spec['max']}")
                print(f"      --param {pname}=<{', '.join(bits)}>  {spec.get('help', '')}")
        return 0

    if args.query not in catalog:
        sys.exit(f"error: unknown query {args.query!r} — run with --list")
    entry = catalog[args.query]

    raw_params: dict[str, str] = {}
    for kv in args.param:
        if "=" not in kv:
            sys.exit(f"error: --param must be K=V (got {kv!r})")
        k, _, v = kv.partition("=")
        raw_params[k.strip()] = v.strip()

    specs = entry.get("params") or {}
    unknown = set(raw_params) - set(specs)
    if unknown:
        sys.exit(f"error: unknown param(s) {sorted(unknown)} for {args.query} — run with --list")
    params = {name: coerce_param(name, spec, raw_params.get(name)) for name, spec in specs.items()}

    if args.show_sql:
        print(entry["sql"].strip(), "\n")

    user = iam_account()
    show = min(max(args.show, 0), MAX_SHOW_ROWS)
    handle = start_iam_proxy_if_needed(args.target)
    try:
        started = time.monotonic()
        try:
            cols, rows = run_query(args.query, entry, params, args.target, user)
        except Exception as exc:  # noqa: BLE001 — always append actionable guidance
            hint = guidance_for(exc)
            sys.exit(f"error: query failed: {exc}" + (f"\n{hint}" if hint else ""))
        elapsed = time.monotonic() - started
    finally:
        if not args.keep_proxy:
            stop_proxy(handle)
        elif handle is not None:
            print(f"[proxy] left running on :{handle.port} (--keep-proxy)")

    print(f"\n[{args.query}] target={args.target} user={user} rows={len(rows)} elapsed={elapsed:.1f}s")
    if rows:
        path = write_csv(args.query, cols, rows)
        print(f"[full results] {path}\n")
        print_sample(cols, rows, show)
        if len(rows) > show:
            print(f"\n… {len(rows) - show} more rows in the CSV (not echoed).")
    else:
        print("(no rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
