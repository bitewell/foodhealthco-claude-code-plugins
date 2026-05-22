#!/usr/bin/env python3
"""ndo-run: invoke nutrition-data-ops management commands from meltano-elt-pipelines.

Reads credentials from meltano-elt-pipelines/.env, optionally uploads a CSV of
product IDs to the btw-nutrition DigitalOcean Spaces bucket, then shells out to
`poetry run python manage.py <command>` in the sibling nutrition-data-ops
checkout. Streams stdout live.

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


# ---------------------------------------------------------------------------
# Repo + .env discovery
# ---------------------------------------------------------------------------
# The skill needs two paths and one .env file. Each is resolved via a chain so
# the skill works whether it lives in `meltano-elt-pipelines/.claude/skills/`
# (legacy) or in `foodhealthco-claude-code-plugins/plugins/.../skills/` (new).
#
#   meltano-elt-pipelines/  ← has the canonical .env today
#   nutrition-data-ops/     ← sibling; where manage.py lives
#
# Override any of these with the matching env var.


def _find_dir_walking_up(start: Path, name: str, max_levels: int = 8) -> Optional[Path]:
    """Walk up from `start` looking for a sibling directory named `name`.

    Returns the absolute path if found, else None. Used to auto-discover
    `meltano-elt-pipelines` or `nutrition-data-ops` from CWD or the skill dir.
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


def discover_meltano_root() -> Optional[Path]:
    """Find meltano-elt-pipelines via env var → walk-up from CWD → walk-up from skill → ~/Code heuristic."""
    if env := os.environ.get("MELTANO_ROOT"):
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    for start in (Path.cwd(), SCRIPT_DIR):
        if found := _find_dir_walking_up(start, "meltano-elt-pipelines"):
            return found
    fallback = Path.home() / "Code" / "meltano-elt-pipelines"
    return fallback if fallback.is_dir() else None


def discover_ndo_root() -> Optional[Path]:
    """Find nutrition-data-ops via env var → sibling of meltano → walk-up → ~/Code heuristic."""
    if env := os.environ.get("NDO_ROOT"):
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None
    if (mr := discover_meltano_root()) and (mr.parent / "nutrition-data-ops").is_dir():
        return mr.parent / "nutrition-data-ops"
    for start in (Path.cwd(), SCRIPT_DIR):
        if found := _find_dir_walking_up(start, "nutrition-data-ops"):
            return found
    fallback = Path.home() / "Code" / "nutrition-data-ops"
    return fallback if fallback.is_dir() else None


def discover_env_file() -> Optional[Path]:
    """Locate the .env to load. Discovery chain (first hit wins):

    1. $NDO_RUN_ENV (explicit path override)
    2. <plugins-repo>/.env  (the new home — if skill is installed under foodhealthco-claude-code-plugins)
    3. <meltano-elt-pipelines>/.env  (legacy / current)
    4. ~/.config/ndo-run/.env  (XDG-ish per-user)
    """
    # 1. Explicit override
    if env := os.environ.get("NDO_RUN_ENV"):
        p = Path(env).expanduser().resolve()
        return p if p.is_file() else None

    # 2. Plugins repo (skill's own home if installed via marketplace)
    # The skill's own directory is the closest hint of which repo it lives in.
    # Walk up looking for the plugins repo root (contains .claude-plugin/ at root level).
    candidate_plugin_repo_marker = None
    for parent in [SKILL_DIR, *SKILL_DIR.parents]:
        if (parent / ".claude-plugin").is_dir() and (parent / "plugins").is_dir():
            candidate_plugin_repo_marker = parent
            break
    if candidate_plugin_repo_marker and (candidate_plugin_repo_marker / ".env").is_file():
        return candidate_plugin_repo_marker / ".env"

    # 3. Meltano (legacy)
    if (mr := discover_meltano_root()) and (mr / ".env").is_file():
        return mr / ".env"

    # 4. XDG-ish
    xdg = Path.home() / ".config" / "ndo-run" / ".env"
    if xdg.is_file():
        return xdg

    return None


# Resolved at import time so callers can rely on stable values.
MELTANO_ROOT: Optional[Path] = discover_meltano_root()
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
            "  3. <meltano-elt-pipelines>/.env (legacy)\n"
            "  4. ~/.config/ndo-run/.env\n"
            "Create one and rerun, or set MELTANO_ROOT/NDO_RUN_ENV to override."
        )
    values = dotenv_values(env_path)
    return {k: v for k, v in values.items() if v is not None}


def build_ndo_env(meltano_env: dict[str, str], target: str, db: str) -> dict[str, str]:
    """Translate meltano .env names to what nutrition-data-ops expects."""
    base = os.environ.copy()

    if db == "platform":
        hero_url = meltano_env.get("FHS_HUB_DATABASE_URL")
        if not hero_url:
            sys.exit(
                "error: --db platform requested but FHS_HUB_DATABASE_URL is not set in "
                f"{MELTANO_ROOT}/.env"
            )
        base["DATABASE_URL"] = hero_url
        base["HERO_DATABASE_URL"] = hero_url
    else:
        key = "NDO_PROD_DATABASE_URL" if target == "prod" else "NDO_DEV_DATABASE_URL"
        url = meltano_env.get(key)
        if not url:
            sys.exit(f"error: {key} not set in {MELTANO_ROOT}/.env")
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
        # tag with stale rules. Set this in meltano .env to match prod.
        "DEFAULT_TAGGING_FILE",
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


def run(cmd: list[str], env: dict[str, str], dry_run: bool) -> int:
    cwd = str(NDO_ROOT)
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Invoke an NDO management command from meltano-elt-pipelines.",
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
    ns, unknown = parser.parse_known_args(argv)
    ns.sync = ns.sync == "true"
    # Strip the literal `--` separator if present, then pass the rest through
    ns.extra = [a for a in unknown if a != "--"]
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
        "preflight": preflight_payload,
        "preflight_skipped_reason": preflight_skipped_reason,
        "postflight": postflight_payload,
        "postflight_skipped_reason": postflight_skipped_reason,
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

    try:
        if NDO_ROOT is None or not NDO_ROOT.exists():
            sys.exit(
                f"error: nutrition-data-ops checkout not found.\n"
                "Searched: $NDO_ROOT, sibling of meltano-elt-pipelines, walk-up from CWD, ~/Code/nutrition-data-ops.\n"
                "Clone it: git clone git@github.com:foodhealthco/nutrition-data-ops.git\n"
                "Or set: NDO_ROOT=/path/to/nutrition-data-ops"
            )

        catalog = load_catalog()
        if args.command not in catalog:
            sys.exit(
                f"error: unknown command `{args.command}`. "
                f"Known commands: {sorted(catalog.keys())}"
            )
        spec = catalog[args.command]

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
            )


if __name__ == "__main__":
    sys.path.insert(0, str(SCRIPT_DIR))
    raise SystemExit(main())
