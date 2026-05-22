"""Pre-flight inspection of inputs vs DB state for the ndo-run skill.

Before any prod write, the runner can ask the DB "given this input set,
what will actually change?" and surface a structured report with per-bucket
counts and reasons. This catches the silent-skip surprises we hit during
CLI-844 sub-task 1 — items with `ingredients_text=NULL`, already-categorized
rows that the command skips via `overwrite=False`, IDs that don't exist
in IPM, etc.

Architecture:

* A `PREFLIGHT_REGISTRY` maps command names to functions of shape
  `(conn, ids) -> PreflightReport`. Commands not in the registry simply
  don't have preflight today (v0 ships with one canary impl).

* The report is a dict-of-buckets so it's easy to JSON-serialize for the
  `--summary-out` payload (Dagster ops parse this).

* DB access is read-only via `psycopg2`. The runner constructs the right
  `DATABASE_URL` from meltano's `.env` before calling preflight, so we
  inherit the same dev/prod routing the actual command will use.

* Health probes are intentionally out of scope for v0; the ticket
  ([ENG-938](https://linear.app/foodhealthco/issue/ENG-938)) splits them as a follow-on. They add network
  calls and timing complexity; the SQL-side preflight already eliminates
  the highest-frequency surprises.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@dataclass
class Bucket:
    """One row in the preflight report.

    `kind`:
      - "update" = will write something
      - "skip"   = silent no-op; surfaced so the operator knows
      - "block"  = precondition fails; would emit an error mid-run
    """

    label: str
    count: int
    kind: str  # "update" | "skip" | "block"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "count": self.count,
            "kind": self.kind,
            "reason": self.reason,
        }


@dataclass
class PreflightReport:
    command: str
    target: str
    input_count: int
    buckets: list[Bucket] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def will_update_count(self) -> int:
        return sum(b.count for b in self.buckets if b.kind == "update")

    @property
    def will_skip_count(self) -> int:
        return sum(b.count for b in self.buckets if b.kind == "skip")

    @property
    def will_block_count(self) -> int:
        return sum(b.count for b in self.buckets if b.kind == "block")

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "target": self.target,
            "input_count": self.input_count,
            "will_update": self.will_update_count,
            "will_skip": self.will_skip_count,
            "will_block": self.will_block_count,
            "buckets": [b.to_dict() for b in self.buckets],
            "notes": list(self.notes),
        }

    def format(self) -> str:
        """Human-readable text report for stdout."""
        border = "═" * 72
        lines = [
            "",
            border,
            f"Pre-flight: {self.command}",
            f"  Target: {'🔴 NDO PROD' if self.target == 'prod' else '🟢 NDO dev'}",
            f"  Input: {self.input_count} product_ids",
            border,
            "",
        ]
        update_buckets = [b for b in self.buckets if b.kind == "update"]
        skip_buckets = [b for b in self.buckets if b.kind == "skip"]
        block_buckets = [b for b in self.buckets if b.kind == "block"]

        if update_buckets:
            lines.append("Will WRITE:")
            for b in update_buckets:
                suffix = f"  ({b.reason})" if b.reason else ""
                lines.append(f"  ✓ {b.count:>6}  {b.label}{suffix}")
            lines.append("")

        if skip_buckets:
            lines.append("Will SKIP (no-op):")
            for b in skip_buckets:
                suffix = f"  ({b.reason})" if b.reason else ""
                lines.append(f"  ↻ {b.count:>6}  {b.label}{suffix}")
            lines.append("")

        if block_buckets:
            lines.append("Will SKIP (precondition fails):")
            for b in block_buckets:
                suffix = f"  ({b.reason})" if b.reason else ""
                lines.append(f"  ✗ {b.count:>6}  {b.label}{suffix}")
            lines.append("")

        if self.notes:
            lines.append("Notes:")
            for n in self.notes:
                lines.append(f"  • {n}")
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input extraction (run BEFORE upload so we can preflight raw inputs)
# ---------------------------------------------------------------------------


def extract_ids_for_preflight(
    args, spec: dict
) -> tuple[Optional[list[int]], Optional[str]]:
    """Read the input IDs locally (no Spaces side effects) for preflight.

    Returns (ids, reason_for_skip). If we can't reasonably extract IDs (e.g.
    --spaces-key, source-only command), returns (None, reason) and the caller
    skips preflight.
    """
    if spec.get("input") in ("source", "vendor", "none"):
        return None, f"{args.command} doesn't use per-product input"
    if args.spaces_key:
        return None, "preflight skipped for --spaces-key (would require Spaces fetch)"
    if not (args.ids or args.csv):
        return None, "no per-product input provided"

    if args.ids:
        try:
            return [int(p.strip()) for p in args.ids.split(",") if p.strip()], None
        except ValueError:
            return None, "preflight: --ids contains non-integer values"

    # CSV path
    local_path = Path(args.csv).expanduser().resolve()
    if not local_path.exists():
        return None, f"preflight: {local_path} does not exist"

    ids: list[int] = []
    id_columns = (spec.get("csv_schema") or {}).get("id_columns") or []
    # Prefer columns that look like an integer IPM id; fall back to anything
    # in the recognized list.
    preferred_cols = ["product_id", "id", "product_match_id"]
    try:
        with open(local_path) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return None, "preflight: CSV has no header row"
            id_col = next(
                (c for c in preferred_cols if c in reader.fieldnames),
                next((c for c in id_columns if c in reader.fieldnames), None),
            )
            if not id_col:
                return None, (
                    "preflight: CSV header has no recognized integer id column "
                    "(product_id / id / product_match_id)"
                )
            for row in reader:
                raw = (row.get(id_col) or "").strip().replace(",", "")
                if not raw:
                    continue
                try:
                    ids.append(int(raw))
                except ValueError:
                    # Non-integer (e.g. gtin/upc strings) — skip silently;
                    # preflight only works for IPM ids today
                    return None, (
                        f"preflight: CSV column `{id_col}` has non-integer values; "
                        "preflight only supports product_id / id / product_match_id"
                    )
    except OSError as e:
        return None, f"preflight: failed to read CSV: {e}"

    return ids, None


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------


def open_connection(ndo_env: dict[str, str]):
    """Open a psycopg2 connection using DATABASE_URL from the translated env.

    The runner builds `ndo_env` with the right DATABASE_URL for the chosen
    target (dev/prod) — preflight piggybacks on that so it can't accidentally
    inspect a different DB than the command will write to.
    """
    db_url = ndo_env.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("preflight: DATABASE_URL missing from runner env")
    return psycopg2.connect(db_url, connect_timeout=10)


# ---------------------------------------------------------------------------
# Per-command preflight implementations
# ---------------------------------------------------------------------------


def extract_id_aux_pairs_for_preflight(
    args, spec: dict, aux_columns: list[str]
) -> tuple[Optional[list[tuple]], Optional[str]]:
    """Read product_id + auxiliary CSV columns (e.g. `fhs`) without uploading.

    Used by commands like `approve_scores` whose preflight needs to compare
    each input row against the DB (does the input fhs match the stored SR fhs?).
    `--ids` can't carry aux columns, so we only support `--csv` here. Returns
    a list of tuples `(product_id, *aux_values)` in column order or (None, reason).
    """
    if args.ids:
        return None, "preflight: --ids cannot carry aux columns; pass --csv with all required columns"
    if args.spaces_key:
        return None, "preflight skipped for --spaces-key (would require Spaces fetch)"
    if not args.csv:
        return None, "no CSV provided"

    local_path = Path(args.csv).expanduser().resolve()
    if not local_path.exists():
        return None, f"preflight: {local_path} does not exist"

    preferred_id_cols = ["product_id", "id", "product_match_id"]
    rows: list[tuple] = []
    try:
        with open(local_path) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return None, "preflight: CSV has no header row"
            id_col = next(
                (c for c in preferred_id_cols if c in reader.fieldnames), None
            )
            if not id_col:
                return None, "preflight: CSV needs product_id / id / product_match_id"
            missing_aux = [c for c in aux_columns if c not in reader.fieldnames]
            if missing_aux:
                return None, f"preflight: CSV missing required columns: {missing_aux}"
            for row in reader:
                raw_id = (row.get(id_col) or "").strip().replace(",", "")
                if not raw_id:
                    continue
                try:
                    pid = int(raw_id)
                except ValueError:
                    return None, f"preflight: non-integer id in CSV column `{id_col}`"
                rows.append((pid, *(row.get(c) for c in aux_columns)))
    except OSError as e:
        return None, f"preflight: failed to read CSV: {e}"

    return rows, None


# ---------------------------------------------------------------------------
# Shared SQL helpers
# ---------------------------------------------------------------------------


def _fetchone_dict(conn, sql: str, params) -> dict:
    """Run a single-row aggregate query and return a name→value dict."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
    return dict(zip(cols, row))


def preflight_backfill_categories(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Inspect the input set against IPM for the categorize step.

    Buckets:
    - "Will categorize" (update) — in IPM, no current category, ingredients
      present
    - "Already categorized" (skip) — overwrite=false will skip these
    - "Missing ingredients_text" (block) — BentoML pydantic rejects
    - "Not in IPM" (block) — manage.py command's ProductMatch lookup misses
    """
    sql = """
        WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
            COUNT(*) FILTER (
                WHERE pm.id IS NOT NULL
                  AND (pm.product_category IS NOT NULL AND pm.product_category != '')
            ) AS already_categorized,
            COUNT(*) FILTER (
                WHERE pm.id IS NOT NULL
                  AND (pm.product_category IS NULL OR pm.product_category = '')
                  AND (pm.ingredients_text IS NULL OR pm.ingredients_text = '')
            ) AS missing_ingredients,
            COUNT(*) FILTER (
                WHERE pm.id IS NOT NULL
                  AND (pm.product_category IS NULL OR pm.product_category = '')
                  AND pm.ingredients_text IS NOT NULL
                  AND pm.ingredients_text != ''
            ) AS will_update
        FROM input
        LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ids,))
        cols = [c[0] for c in cur.description]
        row = dict(zip(cols, cur.fetchone()))

    return PreflightReport(
        command="backfill_categories",
        target="",  # caller fills in
        input_count=row["total"],
        buckets=[
            Bucket(
                label="Will categorize",
                count=row["will_update"],
                kind="update",
                reason="no current product_category; will hit BentoML",
            ),
            Bucket(
                label="Already categorized",
                count=row["already_categorized"],
                kind="skip",
                reason="overwrite=false; pass `-- -o true` to force",
            ),
            Bucket(
                label="Missing ingredients_text",
                count=row["missing_ingredients"],
                kind="block",
                reason="BentoML pydantic rejects null; backfill ingredients first",
            ),
            Bucket(
                label="Not in IPM",
                count=row["not_in_ipm"],
                kind="block",
                reason="id not found in ingestion_productmatch",
            ),
        ],
    )


def preflight_backfill_tags(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Bucket input vs IPM for the tagging step.

    Buckets:
    - "Will tag" (update) — in IPM, has ingredients_text
    - "No ingredients_text" (block) — TaggingJob would yield no tags
    - "Not in IPM" (block)
    """
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND (pm.ingredients_text IS NULL OR pm.ingredients_text = '')
                ) AS no_ingredients,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND pm.ingredients_text IS NOT NULL
                      AND pm.ingredients_text != ''
                ) AS will_update
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="backfill_tags",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will tag", row["will_update"], "update",
                   "TaggingJob runs with overwrite=True"),
            Bucket("No ingredients_text", row["no_ingredients"], "block",
                   "TaggingJob yields no tags without ingredients"),
            Bucket("Not in IPM", row["not_in_ipm"], "block"),
        ],
    )


def preflight_backfill_fhs(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Bucket input vs IPM for FHS scoring.

    ScoringJob calls the FHS API per item. Items missing calories/protein/carbs/fat
    will get rejected upstream. Items not in IPM aren't fetchable.
    """
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND (pm.calories IS NULL OR pm.total_fat IS NULL
                        OR pm.protein IS NULL OR pm.carbs IS NULL)
                ) AS missing_macros,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND pm.calories IS NOT NULL AND pm.total_fat IS NOT NULL
                      AND pm.protein IS NOT NULL AND pm.carbs IS NOT NULL
                ) AS will_update
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="backfill_fhs",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will score (writes scoring_review_scoringresult)",
                   row["will_update"], "update",
                   "ScoringJob hits FHS API; previous SR rows archived"),
            Bucket("Missing macros (cal/fat/protein/carbs)", row["missing_macros"], "block",
                   "FHS API rejects without core macros"),
            Bucket("Not in IPM", row["not_in_ipm"], "block"),
        ],
    )


def preflight_backfill_imputation(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Bucket by IPM existence + whether imputable fields are NULL."""
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND pm.calories IS NOT NULL AND pm.total_fat IS NOT NULL
                      AND pm.protein IS NOT NULL AND pm.carbs IS NOT NULL
                      AND pm.added_sugars IS NOT NULL
                ) AS already_complete,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND (pm.calories IS NULL OR pm.total_fat IS NULL
                        OR pm.protein IS NULL OR pm.carbs IS NULL
                        OR pm.added_sugars IS NULL)
                ) AS will_update
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="backfill_imputation",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will impute (has NULL macros/added_sugars)",
                   row["will_update"], "update"),
            Bucket("Already complete (no nulls to fill)",
                   row["already_complete"], "skip"),
            Bucket("Not in IPM", row["not_in_ipm"], "block"),
        ],
    )


def preflight_backfill_ni_profiles(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Field-copy onto IPM. Only block is `not in IPM`."""
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (WHERE pm.id IS NOT NULL) AS will_update
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="backfill_ni_profiles",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will update (CSV fields copied onto IPM)",
                   row["will_update"], "update"),
            Bucket("Not in IPM", row["not_in_ipm"], "block"),
        ],
    )


def preflight_backfill_proxy_match(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Bucket by both target id and copy_from_id existence.

    The runner extracts only product_ids for the standard preflight; to surface
    copy_from_id misses we'd need to read the CSV's copy_from_id column too. For
    now we just check target IPM existence and report copy_from coverage as a note.
    """
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (WHERE pm.id IS NOT NULL) AS targets_in_ipm
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="backfill_proxy_match",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Target IPM present (will copy from copy_from_id)",
                   row["targets_in_ipm"], "update",
                   "copy_from_id existence NOT verified at preflight"),
            Bucket("Target not in IPM", row["not_in_ipm"], "block"),
        ],
        notes=[
            "Preflight checks the target product_id only; copy_from_id rows are validated at runtime."
        ],
    )


def preflight_backfill_detailed_fhs_norms(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Needs an existing ScoringResult to attach detailed norms to."""
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM scoring_review_scoringresult sr
                          WHERE sr.product_match_id = pm.id
                      )
                ) AS no_scoringresult,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM scoring_review_scoringresult sr
                          WHERE sr.product_match_id = pm.id
                      )
                ) AS will_update
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="backfill_detailed_fhs_norms",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will backfill (has ScoringResult)", row["will_update"], "update"),
            Bucket("No ScoringResult yet", row["no_scoringresult"], "block",
                   "run backfill_fhs first"),
            Bucket("Not in IPM", row["not_in_ipm"], "block"),
        ],
    )


def preflight_remove_products_and_scores(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Destructive archive. Show what'll be removed by category."""
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM scoring_review_approvedscoringresult asr
                          WHERE asr.product_match_id = pm.id
                      )
                ) AS with_approved,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM scoring_review_approvedscoringresult asr
                          WHERE asr.product_match_id = pm.id
                      )
                ) AS without_approved
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="remove_products_and_scores",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will archive IPM + ApprovedScoringResult",
                   row["with_approved"], "update", "destructive (archived)"),
            Bucket("Will archive IPM only (no approved score)",
                   row["without_approved"], "update", "destructive (archived)"),
            Bucket("Already gone (not in IPM)", row["not_in_ipm"], "skip"),
        ],
        notes=["DESTRUCTIVE — rows are archived (not hard-deleted), but downstream queries see them as gone."],
    )


def preflight_archive_table(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Generic archive. Buckets vary by `-m <table>`.

    Pulled from args.extra (passthrough). Supported model_names are the same
    ones the NDO command accepts: approvedscoringresult, foringestion,
    productmatch, scoringresult.
    """
    extra = list(getattr(args, "extra", None) or [])
    model_name = None
    for i, tok in enumerate(extra):
        if tok in ("-m", "--model_name") and i + 1 < len(extra):
            model_name = extra[i + 1]
            break

    if not model_name:
        return PreflightReport(
            command="archive_table",
            target="",
            input_count=len(ids),
            buckets=[],
            notes=["preflight: -m/--model_name not found in passthrough args; skipping per-row check"],
        )

    table_map = {
        "approvedscoringresult": "scoring_review_approvedscoringresult",
        "foringestion": "ingestion_foringestion",
        "productmatch": "ingestion_productmatch",
        "scoringresult": "scoring_review_scoringresult",
    }
    table = table_map.get(model_name)
    if not table:
        return PreflightReport(
            command="archive_table",
            target="",
            input_count=len(ids),
            buckets=[],
            notes=[f"preflight: unknown model_name `{model_name}`; NDO will reject at runtime"],
        )

    # SQL identifiers can't be parameterized — table is from a fixed whitelist above.
    row = _fetchone_dict(
        conn,
        f"""
            WITH input(row_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE t.id IS NULL) AS not_found,
                COUNT(*) FILTER (WHERE t.id IS NOT NULL) AS will_archive
            FROM input
            LEFT JOIN {table} t ON t.id = input.row_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="archive_table",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket(f"Will archive in {table}", row["will_archive"], "update",
                   "destructive"),
            Bucket(f"Not found in {table}", row["not_found"], "skip"),
        ],
        notes=[
            f"-m={model_name} → table {table}. CSV `id` column = row id in that table (NOT product_id)."
        ],
    )


def preflight_approve_scores(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Bucket by IPM exists + has ScoringResult matching the input fhs.

    Requires CSV (not --ids) because we need each row's fhs. If --ids was used,
    we fall back to a coarse check (no fhs comparison).
    """
    # Try to read full (id, fhs) pairs; fall back to ids-only if --ids was used
    pairs, reason = extract_id_aux_pairs_for_preflight(args, spec, ["fhs"])
    if pairs is None:
        # Coarse: just check IPM + ScoringResult existence
        row = _fetchone_dict(
            conn,
            """
                WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                    COUNT(*) FILTER (
                        WHERE pm.id IS NOT NULL
                          AND NOT EXISTS (SELECT 1 FROM scoring_review_scoringresult sr WHERE sr.product_match_id = pm.id)
                    ) AS no_sr,
                    COUNT(*) FILTER (
                        WHERE pm.id IS NOT NULL
                          AND EXISTS (SELECT 1 FROM scoring_review_scoringresult sr WHERE sr.product_match_id = pm.id)
                    ) AS has_sr
                FROM input
                LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
            """,
            (ids,),
        )
        return PreflightReport(
            command="approve_scores",
            target="",
            input_count=row["total"],
            buckets=[
                Bucket("Has ScoringResult (fhs match NOT verified)", row["has_sr"], "update"),
                Bucket("No ScoringResult", row["no_sr"], "block"),
                Bucket("Not in IPM", row["not_in_ipm"], "block"),
            ],
            notes=[f"fhs comparison skipped: {reason}. Use --csv with fhs column for full check."],
        )

    # Full check: per-row fhs match. Pass id + expected_fhs as parallel arrays
    # and unnest both — easier than wrangling psycopg2 record-array types.
    pids: list[int] = []
    fhss: list[Optional[float]] = []
    for pid, raw_fhs in pairs:
        pids.append(pid)
        try:
            fhss.append(float(raw_fhs))
        except (TypeError, ValueError):
            fhss.append(None)

    row = _fetchone_dict(
        conn,
        """
            WITH input AS (
                SELECT
                    unnested.product_id,
                    unnested.expected_fhs
                FROM unnest(%s::bigint[], %s::double precision[])
                    AS unnested(product_id, expected_fhs)
            )
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM scoring_review_scoringresult sr WHERE sr.product_match_id = pm.id)
                ) AS no_sr,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND input.expected_fhs IS NULL
                ) AS bad_input_fhs,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND input.expected_fhs IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM scoring_review_scoringresult sr
                          WHERE sr.product_match_id = pm.id
                            AND ROUND(sr.fhs::numeric, 2) = ROUND(input.expected_fhs::numeric, 2)
                      )
                ) AS will_approve,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND input.expected_fhs IS NOT NULL
                      AND EXISTS (SELECT 1 FROM scoring_review_scoringresult sr WHERE sr.product_match_id = pm.id)
                      AND NOT EXISTS (
                          SELECT 1 FROM scoring_review_scoringresult sr
                          WHERE sr.product_match_id = pm.id
                            AND ROUND(sr.fhs::numeric, 2) = ROUND(input.expected_fhs::numeric, 2)
                      )
                ) AS fhs_mismatch
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (pids, fhss),
    )
    return PreflightReport(
        command="approve_scores",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will approve (CSV fhs matches SR.fhs ±2dp)", row["will_approve"], "update"),
            Bucket("FHS mismatch (won't approve)", row["fhs_mismatch"], "block",
                   "input fhs differs from stored ScoringResult.fhs"),
            Bucket("No ScoringResult", row["no_sr"], "block",
                   "run backfill_fhs first"),
            Bucket("Bad input fhs (non-numeric)", row["bad_input_fhs"], "block"),
            Bucket("Not in IPM", row["not_in_ipm"], "block"),
        ],
    )


def preflight_bulk_create_products(conn, ids, args=None, spec=None) -> PreflightReport:
    """Bucket CSV rows by (gtin, source) existence in IPM.

    `bulk_create_products` is a CREATE flow, so there are no integer
    product_ids to extract — the impl reads the CSV's `gtin` column directly
    and joins against `(gtin, source)` in IPM. The runner skips the standard
    int-id extractor for commands listed in `PREFLIGHT_SKIPS_ID_EXTRACTION`.
    """
    source = getattr(args, "source", None)
    if not source:
        return PreflightReport(
            command="bulk_create_products",
            target="",
            input_count=0,
            buckets=[],
            notes=["preflight: --source is required for bulk_create_products"],
        )

    local_path = Path(args.csv).expanduser().resolve()
    gtins_in_csv: list[str] = []
    rows_without_gtin = 0
    seen: set[str] = set()
    duplicate_in_csv = 0

    try:
        with open(local_path) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "gtin" not in reader.fieldnames:
                return PreflightReport(
                    command="bulk_create_products",
                    target="",
                    input_count=0,
                    buckets=[],
                    notes=["preflight: CSV missing `gtin` column"],
                )
            for row in reader:
                raw = (row.get("gtin") or "").strip()
                if not raw:
                    rows_without_gtin += 1
                    continue
                if raw in seen:
                    duplicate_in_csv += 1
                    continue
                seen.add(raw)
                gtins_in_csv.append(raw)
    except OSError as e:
        return PreflightReport(
            command="bulk_create_products",
            target="",
            input_count=0,
            buckets=[],
            notes=[f"preflight: failed to read CSV: {e}"],
        )

    will_create = 0
    already_in_source = 0
    if gtins_in_csv:
        row = _fetchone_dict(
            conn,
            """
                WITH input(gtin) AS (SELECT unnest(%s::text[]))
                SELECT
                    COUNT(*) FILTER (WHERE pm.gtin IS NULL) AS will_create,
                    COUNT(*) FILTER (WHERE pm.gtin IS NOT NULL) AS already_in_source
                FROM input
                LEFT JOIN ingestion_productmatch pm
                  ON pm.gtin = input.gtin AND pm.source = %s
            """,
            (gtins_in_csv, source),
        )
        will_create = row["will_create"]
        already_in_source = row["already_in_source"]

    return PreflightReport(
        command="bulk_create_products",
        target="",
        input_count=len(gtins_in_csv) + rows_without_gtin + duplicate_in_csv,
        buckets=[
            Bucket("Will create new IPM rows", will_create, "update",
                   f"source={source}, api_match_stage=manual_review"),
            Bucket(f"Skip: gtin+source already in IPM ({source})",
                   already_in_source, "skip",
                   "idempotent — rerunning is safe"),
            Bucket("Skip: duplicate gtin within CSV",
                   duplicate_in_csv, "skip"),
            Bucket("Block: row missing gtin", rows_without_gtin, "block",
                   "every row needs a gtin (synthetic prefix OK)"),
        ],
    )


def preflight_send_to_clients(conn, ids: list[int], args=None, spec=None) -> PreflightReport:
    """Publishing requires an ApprovedScoringResult."""
    row = _fetchone_dict(
        conn,
        """
            WITH input(product_id) AS (SELECT unnest(%s::bigint[]))
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE pm.id IS NULL) AS not_in_ipm,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM scoring_review_approvedscoringresult asr
                          WHERE asr.product_match_id = pm.id
                      )
                ) AS no_approved,
                COUNT(*) FILTER (
                    WHERE pm.id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM scoring_review_approvedscoringresult asr
                          WHERE asr.product_match_id = pm.id
                      )
                ) AS will_publish
            FROM input
            LEFT JOIN ingestion_productmatch pm ON pm.id = input.product_id
        """,
        (ids,),
    )
    return PreflightReport(
        command="send_to_clients",
        target="",
        input_count=row["total"],
        buckets=[
            Bucket("Will publish (has approved score)", row["will_publish"], "update"),
            Bucket("No approved score yet", row["no_approved"], "block",
                   "run approve_scores first"),
            Bucket("Not in IPM", row["not_in_ipm"], "block"),
        ],
    )


# Registry of commands that have a preflight implementation.
# Add more here in follow-up tickets — see ENG-938 acceptance criteria.
PREFLIGHT_REGISTRY: dict[str, Callable[..., PreflightReport]] = {
    "backfill_categories": preflight_backfill_categories,
    "backfill_tags": preflight_backfill_tags,
    "backfill_fhs": preflight_backfill_fhs,
    "backfill_imputation": preflight_backfill_imputation,
    "backfill_ni_profiles": preflight_backfill_ni_profiles,
    "backfill_proxy_match": preflight_backfill_proxy_match,
    "backfill_detailed_fhs_norms": preflight_backfill_detailed_fhs_norms,
    "remove_products_and_scores": preflight_remove_products_and_scores,
    "archive_table": preflight_archive_table,
    "approve_scores": preflight_approve_scores,
    "send_to_clients": preflight_send_to_clients,
    "bulk_create_products": preflight_bulk_create_products,
}


# Creation-style commands skip the standard integer-id extractor (no IPM ids
# exist yet) and read the CSV themselves. Their impls receive an empty `ids`
# argument; `run_preflight` validates that a local CSV is present first.
PREFLIGHT_SKIPS_ID_EXTRACTION: set[str] = {
    "bulk_create_products",
}


# ---------------------------------------------------------------------------
# Top-level entry point used by ndo_run.py
# ---------------------------------------------------------------------------


def run_preflight(
    args, spec: dict, ndo_env: dict[str, str]
) -> tuple[Optional[PreflightReport], Optional[str]]:
    """Run preflight if applicable.

    Returns (report, skip_reason). At most one is non-None:
      - report set, skip_reason None → ran successfully, caller should print/prompt
      - report None, skip_reason str → preflight didn't run; caller logs the reason
                                       and proceeds without it
    """
    if args.no_preflight:
        return None, "preflight skipped via --no-preflight"

    impl = PREFLIGHT_REGISTRY.get(args.command)
    if not impl:
        return None, f"no preflight implementation for `{args.command}` yet"

    if args.command in PREFLIGHT_SKIPS_ID_EXTRACTION:
        # CREATE flows: the impl reads the CSV itself; just make sure one is
        # available locally before opening a DB connection.
        if args.spaces_key:
            return None, "preflight skipped for --spaces-key (would require Spaces fetch)"
        if not args.csv:
            return None, "preflight: no CSV provided"
        if not Path(args.csv).expanduser().resolve().exists():
            return None, f"preflight: {args.csv} does not exist"
        ids: list = []
    else:
        ids, reason = extract_ids_for_preflight(args, spec)
        if not ids:
            return None, reason

    try:
        with open_connection(ndo_env) as conn:
            report = impl(conn, ids, args=args, spec=spec)
    except psycopg2.Error as e:
        return None, f"preflight: DB error: {e}"
    except RuntimeError as e:
        return None, str(e)

    report.target = args.target
    return report, None


def confirm_or_abort(report: PreflightReport, *, args) -> bool:
    """Print the report and (for prod) prompt for opt-in.

    Returns True to proceed, False to abort. Honors --dry-run (no prompt),
    --force (auto-yes for prod), and TTY detection (no prompt in non-interactive).
    """
    print(report.format(), flush=True)

    if args.dry_run:
        # Preflight is the headline of a dry-run report; no prompt needed.
        return True

    if args.target != "prod":
        # Dev runs are informational; no prompt.
        return True

    if args.force:
        print("  → --force set, proceeding without prompt.\n", flush=True)
        return True

    import sys

    if not sys.stdin.isatty():
        print(
            "  ✗ aborted: --target prod requires interactive confirmation OR --force\n",
            file=sys.stderr,
            flush=True,
        )
        return False

    print(
        "Proceed?  [y]es / [N]o : ", end="", flush=True
    )
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")
