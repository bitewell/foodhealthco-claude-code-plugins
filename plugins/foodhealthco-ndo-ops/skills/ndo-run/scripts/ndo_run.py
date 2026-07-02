#!/usr/bin/env python3
"""ndo-run: invoke nutrition-data-ops management commands.

Reads credentials from a .env (see `discover_env_file` for the chain),
optionally uploads a CSV of product IDs to the btw-nutrition DigitalOcean
Spaces bucket, then shells out to `poetry run python manage.py <command>` in
the nutrition-data-ops checkout. Streams stdout live.

Usage (driven by Claude via the skill; humans can also invoke directly):

    ndo_run.py <command> \\
        [--ids 12,34,56 | --csv /path/to/ids.csv | --spaces-key existing/key.csv] \\
        [--source SRC] [--target dev|prod] [--db ndo|platform] \\
        [--sync true|false] [--dry-run] [--force] \\
        [-- <extra manage.py args passed through>]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from dotenv import dotenv_values

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CATALOG_PATH = SKILL_DIR / "catalog.yaml"

HERODB_GAP_TICKET = "ENG-897"

# Batch-size guardrail (ENG-965 incident): a manual `retrieve_data_cache -a 10000`
# overran the fhs-app's per-request query and dropped the DB connection
# (psycopg2 "SSL SYSCALL error: EOF detected"). Per-command defaults are safe
# (250/256), but an explicit override can still blow past what a single IN-list
# can carry. Any batch flag above NDO_RUN_MAX_BATCH is clamped to the ceiling
# with a loud warning unless --allow-large-batch is passed. Applies to manual
# AND Dagster-shelled runs (dagster_ndo/jobs/scoring_chain.py shells this runner).
DEFAULT_MAX_BATCH = int(os.environ.get("NDO_RUN_MAX_BATCH", "1000"))
BATCH_FLAGS = frozenset({"-a", "--amount", "-b", "--batch", "-bs", "--batch_size", "-l", "--limit"})


# ---------------------------------------------------------------------------
# Repo + .env discovery
# ---------------------------------------------------------------------------
# The skill needs the nutrition-data-ops checkout (where `manage.py` lives)
# and one .env. Each is resolved via a chain so the skill works regardless of
# cwd. Override either with the matching env var.


def _find_dir_walking_up(start: Path, name: str, max_levels: int = 8) -> Optional[Path]:
    """Walk up from `start` looking for a sibling directory named `name`.

    Returns the absolute path if found, else None. Used to auto-discover
    `nutrition-data-ops` or `fhs-app` from CWD or the skill dir.
    """
    here = start.resolve()
    for _ in range(max_levels):
        candidate = here / name
        if candidate.is_dir():
            return candidate
        if here.parent == here:
            return None
        here = here.parent
    return None


def discover_ndo_root() -> Optional[Path]:
    """Find nutrition-data-ops via env var → walk-up from CWD/skill → ~/Code heuristic."""
    if env := os.environ.get("NDO_ROOT"):
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    for start in (Path.cwd(), SCRIPT_DIR):
        if found := _find_dir_walking_up(start, "nutrition-data-ops"):
            return found
    fallback = Path.home() / "Code" / "nutrition-data-ops"
    return fallback if fallback.is_dir() else None


def discover_fhs_app_root() -> Optional[Path]:
    """Find fhs-app via env var → sibling of NDO → walk-up → ~/Code heuristic.

    Used only by catalog entries with `tool: fhs_app` (currently:
    `generate_qa_report`). fhs-app uses its own poetry env and its own .env,
    so we just need the path to `cd` into.
    """
    if env := os.environ.get("FHS_APP_ROOT"):
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    if (ndo := discover_ndo_root()) and (ndo.parent / "fhs-app").is_dir():
        return ndo.parent / "fhs-app"
    for start in (Path.cwd(), SCRIPT_DIR):
        if found := _find_dir_walking_up(start, "fhs-app"):
            return found
    fallback = Path.home() / "Code" / "fhs-app"
    return fallback if fallback.is_dir() else None


def discover_env_file() -> Optional[Path]:
    """Locate the .env to load. Discovery chain (first hit wins):

    1. $NDO_RUN_ENV (explicit path override)
    2. <plugins-repo>/.env  (recommended home if installed via marketplace)
    3. <nutrition-data-ops>/.env  (when NDO is checked out locally)
    4. ~/.config/ndo-run/.env  (XDG-ish per-user)
    """
    # 1. Explicit override
    if env := os.environ.get("NDO_RUN_ENV"):
        p = Path(env).expanduser().resolve()
        return p if p.is_file() else None

    # 2. Plugins repo (skill's own home if installed via marketplace).
    # The skill's own directory is the closest hint of which repo it lives in.
    # Walk up looking for the plugins repo root (contains .claude-plugin/ at root level).
    candidate_plugin_repo_marker = None
    for parent in [SKILL_DIR, *SKILL_DIR.parents]:
        if (parent / ".claude-plugin").is_dir() and (parent / "plugins").is_dir():
            candidate_plugin_repo_marker = parent
            break
    if candidate_plugin_repo_marker and (candidate_plugin_repo_marker / ".env").is_file():
        return candidate_plugin_repo_marker / ".env"

    # 3. NDO checkout
    if (ndo := discover_ndo_root()) and (ndo / ".env").is_file():
        return ndo / ".env"

    # 4. XDG-ish
    xdg = Path.home() / ".config" / "ndo-run" / ".env"
    if xdg.is_file():
        return xdg

    return None


# Resolved at import time so callers can rely on stable values.
NDO_ROOT: Optional[Path] = discover_ndo_root()


def load_catalog() -> dict:
    with open(CATALOG_PATH) as f:
        raw = yaml.safe_load(f)
    flat: dict[str, dict] = {}
    for _group, commands in raw.items():
        for cmd_name, spec in commands.items():
            flat[cmd_name] = spec
    return flat


def load_meltano_env() -> dict[str, str]:
    """Load the discovered .env into a dict. Kept name for backward compat."""
    env_path = discover_env_file()
    if not env_path:
        sys.exit(
            "error: no .env found. Discovery chain:\n"
            "  1. $NDO_RUN_ENV (set to an explicit path)\n"
            "  2. <foodhealthco-claude-code-plugins>/.env (if installed via marketplace)\n"
            "  3. <nutrition-data-ops>/.env\n"
            "  4. ~/.config/ndo-run/.env\n"
            "Create one and rerun, or set NDO_RUN_ENV to override."
        )
    values = dotenv_values(env_path)
    return {k: v for k, v in values.items() if v is not None}


def build_ndo_env(meltano_env: dict[str, str], target: str, db: str) -> dict[str, str]:
    """Translate .env names to what nutrition-data-ops expects."""
    base = os.environ.copy()
    env_path = discover_env_file()  # for error messages; load already succeeded

    if db == "platform":
        hero_url = meltano_env.get("FHS_HUB_DATABASE_URL")
        if not hero_url:
            sys.exit(
                f"error: --db platform requested but FHS_HUB_DATABASE_URL is not set in {env_path}"
            )
        base["DATABASE_URL"] = hero_url
        base["HERO_DATABASE_URL"] = hero_url
    else:
        key = "NDO_PROD_DATABASE_URL" if target == "prod" else "NDO_DEV_DATABASE_URL"
        url = meltano_env.get(key)
        if not url:
            sys.exit(f"error: {key} not set in {env_path}")
        base["DATABASE_URL"] = url
        if meltano_env.get("FHS_HUB_DATABASE_URL"):
            base["HERO_DATABASE_URL"] = meltano_env["FHS_HUB_DATABASE_URL"]

    # Shell env takes precedence over .env so the user can override sensitive
    # creds (e.g. prod btw-nutrition keys) without mutating the file.
    name_map = {
        "DO_SPACES_ACCESS_KEY": "DO_ACCESS_KEY",
        "DO_SPACES_SECRET_KEY": "DO_SECRET_KEY",
        "DO_SPACES_REGION": "DO_REGION_NAME",
    }
    for src, dst in name_map.items():
        val = os.environ.get(src) or meltano_env.get(src)
        if val:
            base[dst] = val
    base["DO_BUCKET_NAME"] = os.environ.get("DO_BUCKET_NAME", "btw-nutrition")

    for passthrough in (
        "FHS_API_URL",
        "FHS_API_TOKEN",
        # BentoML category-prediction endpoint used by `backfill_categories`
        "CATEGORY_ENDPOINT_URL",
        "CATEGORY_ENDPOINT_TOKEN",
        # Tagging config key in DO Spaces (e.g. `t2t_v4.csv`). NDO defaults to
        # `t2t.csv` if unset, but prod runs on `t2t_v4.csv` — mismatch would
        # tag with stale rules. Set this in your .env to match prod.
        "DEFAULT_TAGGING_FILE",
        # OpenSearch connection vars consumed by `index_scored_view_command`
        # (via NDO's settings.py → OpenSearchClientService). Only needed when
        # `approve_scores --with-reindex` chains the reindex step; if unset,
        # the chain skips.
        "DO_OPENSEARCH_URL",
        "DO_OPENSEARCH_PORT",
        "DO_OPENSEARCH_USERNAME",
        "DO_OPENSEARCH_PASSWORD",
        "DO_OPENSEARCH_USE_SSL",
    ):
        val = os.environ.get(passthrough) or meltano_env.get(passthrough)
        if val:
            base[passthrough] = val

    return base


def write_temp_csv(ids: list[str]) -> Path:
    fd, path = tempfile.mkstemp(prefix="ndo-run-", suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["product_id"])
        for pid in ids:
            writer.writerow([pid])
    return Path(path)


def validate_csv_schema(csv_path: Path, spec: dict) -> None:
    schema = spec.get("csv_schema") or {}
    if not schema:
        return
    with open(csv_path) as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            sys.exit(f"error: {csv_path} is empty")
    header_set = {col.strip() for col in header}
    required = schema.get("required_columns") or []
    missing = [col for col in required if col not in header_set]
    if missing:
        sys.exit(
            f"error: CSV {csv_path} missing required columns: {missing}. "
            f"Header was: {header}"
        )
    id_columns = schema.get("id_columns") or []
    if id_columns and not any(col in header_set for col in id_columns):
        sys.exit(
            f"error: CSV {csv_path} has no recognized product id column. "
            f"Expected one of: {id_columns}. Header was: {header}"
        )


def extract_ids_from_csv(csv_path: Path, spec: dict) -> list[str]:
    """Return the values of the first id column found in the CSV.

    Looks at `csv_schema.id_columns` (in declaration order) and picks the first
    one that appears in the header. Empty cells are skipped. Used by fhs-app
    commands which take a newline-separated txt file rather than a CSV, so
    the runner extracts the id column and writes it back out as plain lines.
    """
    schema = spec.get("csv_schema") or {}
    id_columns = schema.get("id_columns") or []
    with open(csv_path) as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return []
        header_idx = {col.strip(): i for i, col in enumerate(header)}
        chosen = next((c for c in id_columns if c in header_idx), None)
        if chosen is None:
            sys.exit(
                f"error: {csv_path} has no recognized id column. "
                f"Expected one of: {id_columns}. Header was: {header}"
            )
        idx = header_idx[chosen]
        return [row[idx].strip() for row in reader if len(row) > idx and row[idx].strip()]


def count_csv_rows(csv_path: Path) -> int | None:
    """Return data-row count (excluding header), or None on read error."""
    try:
        with open(csv_path) as f:
            reader = csv.reader(f)
            try:
                next(reader)  # skip header
            except StopIteration:
                return 0
            return sum(1 for _ in reader)
    except OSError:
        return None


def resolve_input(args: argparse.Namespace, spec: dict) -> tuple[str | None, int | None]:
    """Resolve input.

    Returns (spaces_key, input_count). spaces_key is None for source-only or vendor-only
    commands. input_count is the row count uploaded (or pasted) when applicable, else None.
    """
    input_mode = spec.get("input")

    if input_mode in ("source", "vendor"):
        return None, None

    provided = [
        bool(args.ids),
        bool(args.csv),
        bool(args.spaces_key),
    ]
    if sum(provided) > 1:
        sys.exit("error: pass only one of --ids, --csv, --spaces-key")

    if args.spaces_key:
        # Caller-provided existing key — we don't know its row count without downloading.
        return args.spaces_key, None

    if input_mode == "file" and not (args.ids or args.csv):
        sys.exit(
            f"error: `{args.command}` requires a file input. "
            "Provide --ids, --csv, or --spaces-key."
        )

    if input_mode == "file_or_source" and not (args.ids or args.csv or args.source):
        sys.exit(
            f"error: `{args.command}` requires --source OR a file input "
            "(--ids, --csv, or --spaces-key)."
        )

    if not (args.ids or args.csv):
        return None, None

    if args.csv:
        local_path = Path(args.csv).expanduser().resolve()
        if not local_path.exists():
            sys.exit(f"error: {local_path} does not exist")
        validate_csv_schema(local_path, spec)
        cleanup = False
    else:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        if not ids:
            sys.exit("error: --ids produced no values after parsing")
        local_path = write_temp_csv(ids)
        cleanup = True
        # Validate the auto-generated CSV against the command's schema. For
        # commands that need columns beyond product_id (e.g. approve_scores
        # needs `fhs`, backfill_proxy_match needs `copy_from_id`), --ids alone
        # isn't sufficient — caller should use --csv with the full schema.
        try:
            validate_csv_schema(local_path, spec)
        except SystemExit:
            local_path.unlink(missing_ok=True)
            raise

    input_count = count_csv_rows(local_path)

    if args.dry_run:
        print(f"[dry-run] would upload {local_path} to Spaces")
        if cleanup:
            local_path.unlink()
        return f"ops-skill/DRYRUN-{args.command}.csv", input_count

    from upload import upload as upload_fn  # local import; upload.py is a sibling

    key = upload_fn(local_path, args.command)
    print(f"uploaded: s3://btw-nutrition/{key}")
    if cleanup:
        local_path.unlink()
    return key, input_count


def build_manage_cmd(
    args: argparse.Namespace,
    spec: dict,
    spaces_key: str | None,
) -> list[str]:
    cmd = ["poetry", "run", "python", "manage.py", args.command]

    if spaces_key is not None:
        # Most commands use `-f` for the input file key; a few (e.g. text2tag_qa)
        # use `-i`. Per-command override comes from catalog.yaml's `file_flag`.
        file_flag = spec.get("file_flag", "-f")
        cmd.extend([file_flag, spaces_key])

    # Forward --source either when the input mode implies it (source-only or
    # file_or_source), or when an `input: file` command explicitly declares a
    # singular --source in its catalog args (e.g. `bulk_create_products`, which
    # stamps every created row with a source). Plural `--sources` (nargs='+')
    # also accepts a single value, so this stays safe for those callers.
    source_in_spec_args = any(
        a in spec.get("args", []) for a in ("-s", "--source")
    )
    if args.source and (
        spec.get("input") in ("source", "file_or_source") or source_in_spec_args
    ):
        cmd.extend(["-s", args.source])

    sync_flag = spec.get("sync_flag")
    if sync_flag and args.sync is not None:
        cmd.extend([sync_flag, "true" if args.sync else "false"])

    if args.extra:
        cmd.extend(args.extra)

    return cmd


def confirm_prod(args: argparse.Namespace, spec: dict) -> None:
    if args.target != "prod" or args.force or args.dry_run:
        return
    print(
        f"⚠ You are about to run `{args.command}` against NDO PROD.\n"
        f"   {spec.get('help', '')}\n"
        "Type the word `prod` to continue: ",
        end="",
        flush=True,
    )
    typed = sys.stdin.readline().strip()
    if typed != "prod":
        sys.exit("aborted: prod confirmation not received")


def warn_herodb(args: argparse.Namespace) -> None:
    if args.db != "platform":
        return
    border = "=" * 72
    lines = [
        "",
        border,
        "WARNING: --db platform",
        f"  HeroDB routing is NOT YET wired through NDO management commands",
        f"  (tracked in {HERODB_GAP_TICKET}). This flag currently swaps the",
        f"  DATABASE_URL env var so Django's DEFAULT alias points at HeroDB.",
        f"  Functional for a single-DB-target run, but does NOT give",
        f"  concurrent NDO+HeroDB behavior.",
        border,
        "",
    ]
    print("\n".join(lines), file=sys.stderr)
    if not args.force:
        print(f"Re-run with --force to proceed anyway, or wait for {HERODB_GAP_TICKET}.", file=sys.stderr)
        sys.exit(2)


def run(cmd: list[str], env: dict[str, str], dry_run: bool, cwd: Optional[str] = None) -> int:
    cwd = cwd or str(NDO_ROOT)
    pretty = " ".join(shlex.quote(c) for c in cmd)
    print(f"\n$ (cd {cwd} && {pretty})\n")
    if dry_run:
        print("[dry-run] not executing")
        return 0
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    return proc.wait()


# ---------------------------------------------------------------------------
# Post-approve propagation chains (--with-reindex / --send-to-clients)
# ---------------------------------------------------------------------------
# The consumer search index is rebuilt from the *approved*-scores view
# (`index_scored_view_command` reads that view), so propagation belongs to
# `approve_scores`, not to scoring. `backfill_fhs` only writes UNAPPROVED
# ScoringResult/ASR rows — those legitimately shouldn't reach consumer search
# until an operator approves them. So:
#
#   backfill_fhs            → writes unapproved scores (no reindex)
#   approve_scores          → the approval gate; --with-reindex here refreshes
#                             the index materialized views and pushes the
#                             approved-scores view to OpenSearch, and
#                             --send-to-clients then publishes to clients.
#
# Both chains are opt-in (default off) and prod-only (dev OpenSearch isn't
# wired, and client publish is a real external side effect).


REINDEX_CHAIN_COMMANDS: dict[str, list[str]] = {
    # primary command → list of chained manage.py commands (in order)
    "approve_scores": [
        "refresh_fhs_view_for_index_command",
        "index_scored_view_command",
    ],
}

# Scoring commands that write unapproved scores. Used only for a loud reminder
# pointing operators at the approve_scores --with-reindex next step.
BACKFILL_FHS_COMMANDS: set[str] = {
    "backfill_fhs",
    "backfill_fhs_and_refresh_view_command",
}


def run_reindex_chain(
    args: argparse.Namespace, ndo_env: dict[str, str]
) -> tuple[list[dict], Optional[str]]:
    """Chain refresh + reindex after a successful approve_scores run.

    Returns (steps, skip_reason). At most one is meaningful:
      - steps non-empty, skip_reason None → chain ran (each step records
        command/exit_code/elapsed_s; first non-zero exit aborts the chain)
      - steps empty, skip_reason str → chain was skipped (printed by caller)

    Auto-skips when --target dev (dev OpenSearch isn't wired the same as prod)
    or when DO_OPENSEARCH_URL is unset in the resolved env. The skip is loud
    so operators can spot it; the audit also lands in --summary-out.
    """
    chain = REINDEX_CHAIN_COMMANDS.get(args.command)
    if not chain:
        return [], f"--with-reindex not applicable to `{args.command}`"

    if args.target == "dev":
        return [], "--target dev (dev OpenSearch not wired)"
    if not ndo_env.get("DO_OPENSEARCH_URL"):
        return [], "DO_OPENSEARCH_URL not set in resolved env"

    steps: list[dict] = []
    for cmd_name in chain:
        chain_cmd = [
            "poetry", "run", "python", "manage.py", cmd_name, "-sy", "true",
        ]
        print(f"\n[chain] --with-reindex: running {cmd_name}", flush=True)
        t0 = time.monotonic()
        chain_exit = run(chain_cmd, ndo_env, args.dry_run)
        elapsed = round(time.monotonic() - t0, 2)
        steps.append({
            "command": cmd_name,
            "exit_code": chain_exit,
            "elapsed_s": elapsed,
        })
        if chain_exit != 0:
            print(
                f"[chain] {cmd_name} failed (exit={chain_exit}); aborting chain",
                file=sys.stderr,
                flush=True,
            )
            break
    return steps, None


def run_send_to_clients_chain(
    args: argparse.Namespace, ndo_env: dict[str, str], spaces_key: Optional[str]
) -> tuple[list[dict], Optional[str]]:
    """Publish approved scores to clients after a successful approve_scores.

    Only fires when `--send-to-clients` is set. Reuses the same Spaces key that
    approve_scores just consumed (it carries product_id, which send_to_clients
    accepts as an id column). Modes:
      - `all`    → send_to_clients -f <key>            (all clients on the rows)
      - `select` → send_to_clients -f <key> -c <id>    (one named client)
      - `requested` is rejected at parse time (no NDO concept for it yet).

    Returns (steps, skip_reason) with the same contract as run_reindex_chain.
    Auto-skips on --target dev — publishing to clients is a real external side
    effect we only do against prod.
    """
    mode = args.send_to_clients
    if not mode:
        return [], None
    if args.target == "dev":
        return [], "--target dev (client publish is prod-only)"
    if spaces_key is None:
        return [], "no input file to publish"

    cmd = ["poetry", "run", "python", "manage.py", "send_to_clients", "-f", spaces_key]
    label = "all clients on the approved rows"
    if mode == "select":
        cmd += ["-c", args.client_id]
        label = f"client {args.client_id}"

    print(f"\n[chain] --send-to-clients: publishing to {label}", flush=True)
    t0 = time.monotonic()
    send_exit = run(cmd, ndo_env, args.dry_run)
    step = {
        "command": "send_to_clients",
        "mode": mode,
        "client_id": args.client_id if mode == "select" else None,
        "exit_code": send_exit,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    if send_exit != 0:
        print(
            f"[chain] send_to_clients failed (exit={send_exit})",
            file=sys.stderr,
            flush=True,
        )
    return [step], None


# ---------------------------------------------------------------------------
# fhs-app shell-out (separate path from NDO `manage.py`)
# ---------------------------------------------------------------------------
# Catalog entries with `tool: fhs_app` follow a simpler flow than the NDO
# default: no Spaces upload (fhs-app reads files from its local input_scores/
# directory), no NDO env var translation (fhs-app reads its own .env), no
# preflight (no SQL inspection available — fhs-app calls the FHS API
# directly). Postflight is still wired via POSTFLIGHT_REGISTRY.


def write_fhs_app_input_file(ids: list[str], fhs_app_root: Path, source: str) -> Path:
    """Write IDs to a newline-separated .txt in fhs-app/input_scores/.

    fhs-app's generate_scores.py reads file paths from input_scores/ (one
    product id per line). The filename includes a timestamp + source so
    repeated runs don't clobber each other and operators can reproduce later.
    """
    input_dir = fhs_app_root / "input_scores"
    input_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = input_dir / f"ops_skill_{source}_{stamp}.txt"
    with open(path, "w") as f:
        for pid in ids:
            f.write(f"{pid}\n")
    return path


def resolve_fhs_app_input(args: argparse.Namespace, spec: dict, fhs_app_root: Path, source: str) -> tuple[Path, int]:
    """Return (input_file_path_relative_to_input_scores, id_count).

    fhs-app's `-f` is interpreted relative to its `input_scores/` directory
    (see get_full_path() in fhs-app/generate_scores.py). The runner accepts
    --ids or --csv and writes a sidecar .txt, returning the bare filename.
    """
    if args.spaces_key:
        sys.exit("error: --spaces-key is not supported for fhs-app commands")
    if not (args.ids or args.csv):
        sys.exit(
            f"error: `{args.command}` requires --ids or --csv "
            "(a list of product ids to score)"
        )
    if args.ids and args.csv:
        sys.exit("error: pass only one of --ids, --csv")

    if args.csv:
        local_path = Path(args.csv).expanduser().resolve()
        if not local_path.exists():
            sys.exit(f"error: {local_path} does not exist")
        validate_csv_schema(local_path, spec)
        ids = extract_ids_from_csv(local_path, spec)
    else:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        if not ids:
            sys.exit("error: --ids produced no values after parsing")

    if not ids:
        sys.exit("error: no product ids resolved from input")

    if args.dry_run:
        # Don't pollute fhs-app/input_scores/ on a dry-run. Synthesize the
        # would-be path so the printed shell-out is realistic.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dry_path = fhs_app_root / "input_scores" / f"ops_skill_{source}_{stamp}.txt"
        print(
            f"[dry-run] would write {len(ids)} ids to {dry_path} "
            f"(first 3: {ids[:3]})"
        )
        return dry_path, len(ids)

    out = write_fhs_app_input_file(ids, fhs_app_root, source)
    return out, len(ids)


def main_fhs_app(args: argparse.Namespace, spec: dict, started_at: str) -> tuple[int, dict]:
    """Run an fhs-app shell-out command. Returns (exit_code, run_meta).

    run_meta carries postflight-relevant fields (source, input_count,
    input_file) so the caller can build the summary JSON.
    """
    fhs_app_root = discover_fhs_app_root()
    if fhs_app_root is None or not fhs_app_root.exists():
        sys.exit(
            "error: fhs-app checkout not found.\n"
            "Searched: $FHS_APP_ROOT, sibling of nutrition-data-ops, walk-up from CWD, ~/Code/fhs-app.\n"
            "Clone it: git clone git@github.com:foodhealthco/fhs-app.git ~/Code/fhs-app\n"
            "Or set: FHS_APP_ROOT=/path/to/fhs-app"
        )

    # fhs-app's generate_scores.py defaults to source="wkbk_1" if -s is
    # omitted; we mirror that default so the postflight scan knows which
    # filename prefix to look for.
    source = args.source or "wkbk_1"

    input_file, input_count = resolve_fhs_app_input(args, spec, fhs_app_root, source)
    relative_name = input_file.name  # fhs-app prepends "input_scores/" itself

    # `--directory` forces poetry to resolve against fhs-app's own pyproject.toml
    # regardless of where the runner was invoked from. Without it, when the
    # runner runs inside another project's poetry env, the inherited
    # POETRY_ACTIVE / VIRTUAL_ENV vars cause `poetry run` to reuse that venv
    # — which probably lacks xlsxwriter (an fhs-app-only dep).
    cmd = [
        "poetry", "--directory", str(fhs_app_root),
        "run", "python", "generate_scores.py",
        "-s", source, "-f", relative_name,
    ]
    if args.extra:
        cmd.extend(args.extra)

    # Strip env vars that pin poetry to the parent env. Otherwise --directory
    # is honored for project resolution but poetry still sees an active venv
    # and tries to reuse it.
    fhs_app_env = os.environ.copy()
    for leak in ("POETRY_ACTIVE", "VIRTUAL_ENV"):
        fhs_app_env.pop(leak, None)

    exit_code = run(cmd, fhs_app_env, args.dry_run, cwd=str(fhs_app_root))

    return exit_code, {
        "source": source,
        "input_count": input_count,
        "input_file": str(input_file),
        "fhs_app_root": str(fhs_app_root),
    }


def clamp_batch_flags(
    extra: list[str], ceiling: int, allow_large: bool
) -> tuple[list[str], list[dict]]:
    """Clamp passthrough batch-size flags to a safe ceiling.

    Scans `extra` (tokens after `--`) for batch flags (`-a/--amount`,
    `-b/--batch`, `-bs/--batch_size`, `-l/--limit`) and their integer values.
    Any value > ceiling is clamped to the ceiling (unless allow_large), so an
    oversized batch can't drop the fhs-app DB connection. Handles both
    `-a 10000` and `-a=10000` forms. Returns (possibly-rewritten extra, audit).
    """
    out = list(extra)
    clamps: list[dict] = []
    i = 0
    while i < len(out):
        tok = out[i]
        flag: Optional[str] = None
        raw_val: Optional[str] = None
        joined = False
        if tok in BATCH_FLAGS and i + 1 < len(out):
            flag, raw_val = tok, out[i + 1]
        elif "=" in tok and tok.split("=", 1)[0] in BATCH_FLAGS:
            flag, raw_val = tok.split("=", 1)
            joined = True
        if flag is None:
            i += 1
            continue
        try:
            n = int(raw_val)
        except (TypeError, ValueError):
            i += 1
            continue
        if n > ceiling:
            clamps.append(
                {"flag": flag, "requested": n,
                 "applied": n if allow_large else ceiling,
                 "clamped": not allow_large}
            )
            if not allow_large:
                out[i] = f"{flag}={ceiling}" if joined else out[i]
                if not joined:
                    out[i + 1] = str(ceiling)
        i += 1 if joined else 2
    return out, clamps


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Invoke an NDO management command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", help="manage.py command name (see catalog.yaml)")
    parser.add_argument("--ids", help="Comma-separated product IDs")
    parser.add_argument("--csv", help="Path to a local CSV to upload")
    parser.add_argument("--spaces-key", help="Existing key in btw-nutrition to use as-is")
    parser.add_argument("--source", help="NDO source (for file_or_source or source-only commands)")
    parser.add_argument(
        "--target", choices=("dev", "prod"), default="dev", help="Which NDO DB to target"
    )
    parser.add_argument(
        "--db",
        choices=("ndo", "platform"),
        default="ndo",
        help="ndo (default) or platform (HeroDB; see ENG-897)",
    )
    parser.add_argument(
        "--sync",
        default="true",
        choices=("true", "false"),
        help="Pass through as --sync/-sy (default: true so no Celery worker is needed)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; no changes")
    parser.add_argument(
        "--force", action="store_true", help="Skip prod confirmation and ENG-897 block"
    )
    parser.add_argument(
        "--summary-out",
        help=(
            "Write a JSON run-summary file at this path on exit (success or failure). "
            "Used by Dagster ops in the scoring chain to surface structured metadata."
        ),
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help=(
            "Skip the pre-flight DB inspection. Pre-flight inspects the input set "
            "against the target DB and reports per-bucket counts (will-update / "
            "will-skip / will-block) before any prod write. By default it runs "
            "whenever a preflight implementation exists for the command (see "
            "preflight.PREFLIGHT_REGISTRY). For --target prod it also prompts "
            "for opt-in. Use --no-preflight to bypass entirely (audited in "
            "--summary-out)."
        ),
    )
    parser.add_argument(
        "--with-reindex",
        action="store_true",
        help=(
            "After a successful `approve_scores`, chain "
            "`refresh_fhs_view_for_index_command` + `index_scored_view_command` "
            "so the approved scores propagate to OpenSearch consumer search. "
            "The index is built from the approved-scores view, so this belongs "
            "to approval, not scoring — `backfill_fhs` no longer reindexes. "
            "Auto-skipped on --target dev (dev OpenSearch not wired) or when "
            "DO_OPENSEARCH_URL is unset. Audited in --summary-out."
        ),
    )
    parser.add_argument(
        "--send-to-clients",
        choices=["all", "requested", "select"],
        default=None,
        help=(
            "After a successful `approve_scores`, also publish the approved "
            "scores to clients (chains `send_to_clients`). `all` = every client "
            "on the approved rows; `select` = one client (requires --client-id). "
            "`requested` (only clients who requested the product) is NOT yet "
            "supported — NDO has no requested-client concept; tracked as a "
            "future feature (ENG-895 area). Prod-only; auto-skipped on --target "
            "dev. Audited in --summary-out."
        ),
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="Client id for `--send-to-clients select` (a single client).",
    )
    parser.add_argument(
        "--allow-large-batch",
        action="store_true",
        help=(
            "Override the batch-size guardrail. By default any -a/-b/-bs/-l value "
            f"above NDO_RUN_MAX_BATCH (default {DEFAULT_MAX_BATCH}) is clamped to the "
            "ceiling — a large IN-list can drop the fhs-app DB connection (SSL EOF). "
            "Pass this to send the full value anyway; the clamp/override is audited "
            "in --summary-out."
        ),
    )
    ns, unknown = parser.parse_known_args(argv)
    ns.sync = ns.sync == "true"
    # Strip the literal `--` separator if present, then pass the rest through
    ns.extra = [a for a in unknown if a != "--"]

    # --with-reindex / --send-to-clients now attach to the approval step only.
    # Scoring no longer reindexes directly (the index is built from the
    # approved-scores view), so reject the old backfill_fhs --with-reindex usage
    # with a pointer to the new flow rather than silently no-op'ing.
    if ns.with_reindex and ns.command not in REINDEX_CHAIN_COMMANDS:
        allowed = ", ".join(sorted(REINDEX_CHAIN_COMMANDS))
        parser.error(
            f"--with-reindex is only valid for: {allowed} (got `{ns.command}`). "
            f"Scoring no longer reindexes directly — approve first, then run "
            f"`approve_scores --with-reindex`."
        )
    if ns.send_to_clients:
        if ns.command != "approve_scores":
            parser.error(
                f"--send-to-clients is only valid for approve_scores "
                f"(got `{ns.command}`)."
            )
        if ns.send_to_clients == "requested":
            parser.error(
                "--send-to-clients requested is not supported yet: NDO has no "
                "'clients who requested' concept. Tracked as a future NDO "
                "feature (ENG-895 area). Use `all` or `select` for now."
            )
        if ns.send_to_clients == "select" and not ns.client_id:
            parser.error("--send-to-clients select requires --client-id <id>.")
    return ns


def write_summary(
    path: str,
    *,
    args: argparse.Namespace,
    spaces_key: str | None,
    input_count: int | None,
    exit_code: int,
    started_at: str,
    elapsed_s: float,
    preflight_payload: dict | None = None,
    preflight_skipped_reason: str | None = None,
    postflight_payload: dict | None = None,
    postflight_skipped_reason: str | None = None,
    reindex_chain_steps: list[dict] | None = None,
    reindex_chain_skipped_reason: str | None = None,
    send_clients_steps: list[dict] | None = None,
    send_clients_skipped_reason: str | None = None,
    batch_clamps: list[dict] | None = None,
) -> None:
    """Write a JSON run-summary file (best-effort; never raises)."""
    payload = {
        "command": args.command,
        "target": args.target,
        "db": args.db,
        "source": args.source,
        "spaces_key": spaces_key,
        "input_count": input_count,
        "exit_code": exit_code,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(elapsed_s, 2),
        "extra": args.extra,
        "dry_run": bool(args.dry_run),
        "with_reindex": bool(getattr(args, "with_reindex", False)),
        "send_to_clients": getattr(args, "send_to_clients", None),
        "preflight": preflight_payload,
        "preflight_skipped_reason": preflight_skipped_reason,
        "postflight": postflight_payload,
        "postflight_skipped_reason": postflight_skipped_reason,
        "reindex_chain": {
            "steps": reindex_chain_steps or [],
            "skipped_reason": reindex_chain_skipped_reason,
        },
        "send_to_clients_chain": {
            "steps": send_clients_steps or [],
            "skipped_reason": send_clients_skipped_reason,
        },
        "batch_clamps": batch_clamps or [],
    }
    try:
        with open(path, "w") as f:
            json.dump(payload, f)
    except OSError as e:
        print(f"warning: failed to write summary to {path}: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    spaces_key: str | None = None
    input_count: int | None = None
    exit_code = 1  # pessimistic default in case we exit before run()
    preflight_payload: dict | None = None
    preflight_skipped_reason: str | None = None
    postflight_payload: dict | None = None
    postflight_skipped_reason: str | None = None
    reindex_chain_steps: list[dict] = []
    reindex_chain_skipped_reason: str | None = None
    send_clients_steps: list[dict] = []
    send_clients_skipped_reason: str | None = None
    batch_clamps: list[dict] = []

    try:
        args.extra, batch_clamps = clamp_batch_flags(
            args.extra, DEFAULT_MAX_BATCH, args.allow_large_batch
        )
        for c in batch_clamps:
            if c["clamped"]:
                print(
                    f"⚠ {c['flag']} {c['requested']} exceeds the safe ceiling "
                    f"{DEFAULT_MAX_BATCH}; clamped to {DEFAULT_MAX_BATCH}. A large "
                    f"IN-list can drop the fhs-app DB connection (SSL EOF). "
                    f"Re-run with --allow-large-batch to send the full value.",
                    file=sys.stderr, flush=True,
                )
            else:
                print(
                    f"⚠ {c['flag']} {c['requested']} exceeds the safe ceiling "
                    f"{DEFAULT_MAX_BATCH} — proceeding anyway (--allow-large-batch).",
                    file=sys.stderr, flush=True,
                )
        catalog = load_catalog()
        if args.command not in catalog:
            sys.exit(
                f"error: unknown command `{args.command}`. "
                f"Known commands: {sorted(catalog.keys())}"
            )
        spec = catalog[args.command]

        # fhs-app commands take a separate path: no NDO checkout, no Spaces
        # upload, no preflight, no env translation. Postflight still runs but
        # scans the filesystem rather than opening a DB connection.
        if spec.get("tool") == "fhs_app":
            exit_code, run_meta = main_fhs_app(args, spec, started_at)
            input_count = run_meta["input_count"]
            spaces_key = None  # fhs-app doesn't use Spaces

            from postflight import run_fhs_app_postflight  # local import

            post_report, postflight_skipped_reason = run_fhs_app_postflight(
                args, spec, run_meta, started_at, exit_code
            )
            if post_report is not None:
                postflight_payload = post_report.to_dict()
                print(post_report.format(), flush=True)
            elif postflight_skipped_reason and not args.dry_run:
                print(f"[postflight] skipped: {postflight_skipped_reason}", flush=True)

            return exit_code

        if NDO_ROOT is None or not NDO_ROOT.exists():
            sys.exit(
                f"error: nutrition-data-ops checkout not found.\n"
                "Searched: $NDO_ROOT, walk-up from CWD, ~/Code/nutrition-data-ops.\n"
                "Clone it: git clone git@github.com:foodhealthco/nutrition-data-ops.git\n"
                "Or set: NDO_ROOT=/path/to/nutrition-data-ops"
            )

        warn_herodb(args)
        confirm_prod(args, spec)

        meltano_env = load_meltano_env()
        ndo_env = build_ndo_env(meltano_env, args.target, args.db)

        # Pre-flight: inspect inputs vs DB state and (for prod) prompt before
        # any write. Runs only when a preflight impl exists for the command;
        # see preflight.PREFLIGHT_REGISTRY. ENG-938.
        from preflight import run_preflight, confirm_or_abort  # local import

        report, preflight_skipped_reason = run_preflight(args, spec, ndo_env)
        if report is not None:
            preflight_payload = report.to_dict()
            if not confirm_or_abort(report, args=args):
                exit_code = 130  # SIGINT-style; "user aborted"
                return exit_code
        elif preflight_skipped_reason and not args.no_preflight:
            print(f"[preflight] skipped: {preflight_skipped_reason}", flush=True)

        spaces_key, input_count = resolve_input(args, spec)
        cmd = build_manage_cmd(args, spec, spaces_key)
        exit_code = run(cmd, ndo_env, args.dry_run)

        # Post-flight: measure what landed and compare against the preflight
        # forecast. Surfaces silent drops (rows skipped at runtime that
        # preflight didn't predict). Runs only when an impl exists for the
        # command; see postflight.POSTFLIGHT_REGISTRY.
        from postflight import run_postflight  # local import

        post_report, postflight_skipped_reason = run_postflight(
            args, spec, ndo_env, started_at, preflight_payload, exit_code
        )
        if post_report is not None:
            postflight_payload = post_report.to_dict()
            print(post_report.format(), flush=True)
        elif postflight_skipped_reason and not args.dry_run:
            print(f"[postflight] skipped: {postflight_skipped_reason}", flush=True)

        # Post-approve propagation. --with-reindex rebuilds the index views and
        # pushes the approved-scores view to OpenSearch; --send-to-clients then
        # publishes the approved scores to clients. Both fire only after a
        # successful approve_scores run (see REINDEX_CHAIN_COMMANDS).
        if args.with_reindex:
            if exit_code != 0:
                reindex_chain_skipped_reason = (
                    f"primary command exited {exit_code}; skipped reindex chain"
                )
                print(
                    f"[chain] --with-reindex: {reindex_chain_skipped_reason}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                reindex_chain_steps, reindex_chain_skipped_reason = run_reindex_chain(
                    args, ndo_env
                )
                if reindex_chain_skipped_reason:
                    print(
                        f"[chain] --with-reindex: skipped ({reindex_chain_skipped_reason})",
                        flush=True,
                    )
                else:
                    failed = next(
                        (s for s in reindex_chain_steps if s["exit_code"] != 0), None
                    )
                    if failed is not None:
                        exit_code = failed["exit_code"]

        if args.send_to_clients:
            if exit_code != 0:
                send_clients_skipped_reason = (
                    f"prior step exited {exit_code}; skipped client publish"
                )
                print(
                    f"[chain] --send-to-clients: {send_clients_skipped_reason}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                send_clients_steps, send_clients_skipped_reason = (
                    run_send_to_clients_chain(args, ndo_env, spaces_key)
                )
                if send_clients_skipped_reason:
                    print(
                        f"[chain] --send-to-clients: skipped "
                        f"({send_clients_skipped_reason})",
                        flush=True,
                    )
                else:
                    failed = next(
                        (s for s in send_clients_steps if s["exit_code"] != 0), None
                    )
                    if failed is not None:
                        exit_code = failed["exit_code"]

        # Loud reminders when a propagation step was NOT requested, so the gap
        # between "written to Postgres" and "visible to consumers/clients" is
        # never silent.
        if exit_code == 0 and not args.dry_run:
            if args.command == "approve_scores" and not args.with_reindex:
                remaining = ", ".join(REINDEX_CHAIN_COMMANDS["approve_scores"])
                extra = (
                    "" if args.send_to_clients else
                    "  Add --send-to-clients all|select to also publish to clients.\n"
                )
                print(
                    f"\nNOTE: `approve_scores` succeeded but the approved scores "
                    f"are NOT yet in OpenSearch consumer search.\n  Rerun with "
                    f"--with-reindex, or manually run: {remaining}\n{extra}",
                    flush=True,
                )
            elif args.command in BACKFILL_FHS_COMMANDS:
                print(
                    f"\nNOTE: `{args.command}` wrote scores but they are "
                    f"UNAPPROVED and not searchable.\n  Next: `approve_scores "
                    f"--with-reindex` to approve, rebuild the index, and push to "
                    f"OpenSearch (add --send-to-clients to also publish).\n",
                    flush=True,
                )

        return exit_code
    finally:
        if args.summary_out:
            write_summary(
                args.summary_out,
                args=args,
                spaces_key=spaces_key,
                input_count=input_count,
                exit_code=exit_code,
                started_at=started_at,
                elapsed_s=time.monotonic() - t0,
                preflight_payload=preflight_payload,
                preflight_skipped_reason=preflight_skipped_reason,
                postflight_payload=postflight_payload,
                postflight_skipped_reason=postflight_skipped_reason,
                reindex_chain_steps=reindex_chain_steps,
                reindex_chain_skipped_reason=reindex_chain_skipped_reason,
                send_clients_steps=send_clients_steps,
                send_clients_skipped_reason=send_clients_skipped_reason,
                batch_clamps=batch_clamps,
            )


if __name__ == "__main__":
    sys.path.insert(0, str(SCRIPT_DIR))
    raise SystemExit(main())
