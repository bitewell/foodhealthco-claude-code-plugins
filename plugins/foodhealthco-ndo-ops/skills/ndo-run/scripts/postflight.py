"""Post-run verification of NDO ops.

After the runner's subprocess exits 0, postflight queries the destination
table(s) and counts rows that changed since the run started. Compared to the
preflight's `will_update` projection, this surfaces silent drift — e.g. the
FHS API rejected half the products and the script exited 0 anyway because
the loop kept going.

Design:
* Each command's postflight is an explicit per-command end-state check.
  Filters on `updated_at >= run_started_at` (for updates) or `created_at >=
  run_started_at` (for new rows like ScoringResult).
* Skipped when the runner used `--sync false` (async; writes haven't landed
  yet — postflight would report false drift).
* Skipped when there's no preflight to compare against (no baseline).
* Returns a structured report; runner prints it and embeds in `--summary-out`.

The relationship between preflight and postflight:
* preflight projects: "N rows WILL update"
* postflight verifies: "N rows DID update since the run started"
* drift = preflight_expected - postflight_actual
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import psycopg2


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@dataclass
class PostflightReport:
    command: str
    target: str
    run_started_at: str  # ISO timestamp; postflight only counts rows changed AFTER this
    expected_updates: Optional[int]  # from preflight; None if preflight didn't run
    actual_updates: int  # observed change count since run_started_at
    table_inspected: str  # which table the postflight queried
    timestamp_column: str  # "updated_at" or "created_at"
    notes: list[str] = field(default_factory=list)

    @property
    def drift(self) -> Optional[int]:
        if self.expected_updates is None:
            return None
        return self.expected_updates - self.actual_updates

    @property
    def status(self) -> str:
        """'ok' | 'drift' | 'no_baseline'."""
        if self.expected_updates is None:
            return "no_baseline"
        if self.drift == 0:
            return "ok"
        return "drift"

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "target": self.target,
            "run_started_at": self.run_started_at,
            "expected_updates": self.expected_updates,
            "actual_updates": self.actual_updates,
            "drift": self.drift,
            "status": self.status,
            "table_inspected": self.table_inspected,
            "timestamp_column": self.timestamp_column,
            "notes": list(self.notes),
        }

    def format(self) -> str:
        """Human-readable text report for stdout."""
        border = "─" * 72
        lines = ["", border, f"Post-flight: {self.command}"]

        if self.status == "ok":
            icon = "✅"
            summary = f"all {self.actual_updates} expected writes landed"
        elif self.status == "drift":
            icon = "⚠"
            summary = (
                f"expected {self.expected_updates} writes, observed "
                f"{self.actual_updates} (drift: {self.drift})"
            )
        else:
            icon = "ℹ"
            summary = f"observed {self.actual_updates} writes (no preflight baseline)"

        lines.append(f"  {icon} {summary}")
        lines.append(
            f"  source: {self.table_inspected}.{self.timestamp_column} >= "
            f"{self.run_started_at}"
        )

        if self.notes:
            lines.append("")
            for n in self.notes:
                lines.append(f"  • {n}")

        lines.append(border)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared SQL helpers
# ---------------------------------------------------------------------------


def _count_changed_in_table(
    conn,
    table: str,
    timestamp_column: str,
    ids: list[int],
    run_started_at: datetime,
    id_column: str = "id",
) -> int:
    """Count rows in `table` whose `timestamp_column` is >= `run_started_at` AND
    whose id is in `ids`. Used by most postflight checks."""
    # table + columns come from a fixed whitelist per command — safe to interpolate.
    sql = f"""
        SELECT COUNT(*) FROM {table}
        WHERE {id_column} = ANY(%s::bigint[])
          AND {timestamp_column} >= %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ids, run_started_at))
        return cur.fetchone()[0]


def _count_changed_by_product_match_id(
    conn,
    table: str,
    timestamp_column: str,
    product_ids: list[int],
    run_started_at: datetime,
) -> int:
    """Variant of _count_changed_in_table for joinable tables that have
    `product_match_id` instead of `id` as the foreign key."""
    return _count_changed_in_table(
        conn, table, timestamp_column, product_ids, run_started_at,
        id_column="product_match_id",
    )


# ---------------------------------------------------------------------------
# Per-command postflight implementations
# ---------------------------------------------------------------------------
#
# Each takes (conn, ids, run_started_at, expected_updates, args, spec) and
# returns a PostflightReport. `expected_updates` may be None if preflight
# didn't run for this invocation; postflight still reports what it observed.


def postflight_backfill_tags(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    """Verify tag columns on IPM were updated for the input IDs."""
    actual = _count_changed_in_table(
        conn, "ingestion_productmatch", "updated_at", ids, run_started_at
    )
    return PostflightReport(
        command="backfill_tags",
        target="",  # caller fills in
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="ingestion_productmatch",
        timestamp_column="updated_at",
    )


def postflight_backfill_fhs(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    """Verify new ScoringResult rows were created for the input IDs."""
    actual = _count_changed_by_product_match_id(
        conn, "scoring_review_scoringresult", "created_at", ids, run_started_at
    )
    return PostflightReport(
        command="backfill_fhs",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="scoring_review_scoringresult",
        timestamp_column="created_at",
        notes=["ScoringJob archives prior SR rows; this count is NEW rows only."],
    )


def postflight_backfill_categories(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    """Verify product_category was set on IPM for the input IDs since the run."""
    actual = _count_changed_in_table(
        conn, "ingestion_productmatch", "updated_at", ids, run_started_at
    )
    return PostflightReport(
        command="backfill_categories",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="ingestion_productmatch",
        timestamp_column="updated_at",
    )


def postflight_backfill_imputation(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    actual = _count_changed_in_table(
        conn, "ingestion_productmatch", "updated_at", ids, run_started_at
    )
    return PostflightReport(
        command="backfill_imputation",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="ingestion_productmatch",
        timestamp_column="updated_at",
    )


def postflight_backfill_ni_profiles(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    actual = _count_changed_in_table(
        conn, "ingestion_productmatch", "updated_at", ids, run_started_at
    )
    return PostflightReport(
        command="backfill_ni_profiles",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="ingestion_productmatch",
        timestamp_column="updated_at",
    )


def postflight_backfill_proxy_match(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    actual = _count_changed_in_table(
        conn, "ingestion_productmatch", "updated_at", ids, run_started_at
    )
    return PostflightReport(
        command="backfill_proxy_match",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="ingestion_productmatch",
        timestamp_column="updated_at",
    )


def postflight_backfill_detailed_fhs_norms(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    actual = _count_changed_by_product_match_id(
        conn, "scoring_review_scoringresult", "updated_at", ids, run_started_at
    )
    return PostflightReport(
        command="backfill_detailed_fhs_norms",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="scoring_review_scoringresult",
        timestamp_column="updated_at",
    )


def postflight_approve_scores(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    """Verify new ApprovedScoringResult rows were created for the input IDs."""
    actual = _count_changed_by_product_match_id(
        conn, "scoring_review_approvedscoringresult", "created_at", ids, run_started_at
    )
    return PostflightReport(
        command="approve_scores",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="scoring_review_approvedscoringresult",
        timestamp_column="created_at",
    )


def postflight_send_to_clients(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    """Verify ApprovedScoringResult.published / date_published was updated."""
    actual = _count_changed_by_product_match_id(
        conn, "scoring_review_approvedscoringresult", "updated_at", ids, run_started_at
    )
    return PostflightReport(
        command="send_to_clients",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="scoring_review_approvedscoringresult",
        timestamp_column="updated_at",
        notes=["Counts ASR rows touched since run; published flag may be one of several updates."],
    )


def postflight_remove_products_and_scores(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    """Verify rows were archived. NDO uses `entity_archive` for the archive log."""
    # entity_archive has (entity_id, model_name, archived_at). Count rows for
    # this run's IDs across IPM + ASR archives.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM entity_archive
            WHERE entity_id::bigint = ANY(%s::bigint[])
              AND archived_at >= %s
            """,
            (ids, run_started_at),
        )
        actual = cur.fetchone()[0]
    return PostflightReport(
        command="remove_products_and_scores",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="entity_archive",
        timestamp_column="archived_at",
        notes=["Counts archive records across IPM + ASR archived under these product IDs."],
    )


def postflight_archive_table(conn, ids, run_started_at, expected_updates, args=None, spec=None):
    """Verify rows were archived in entity_archive for the given -m model_name."""
    extra = list(getattr(args, "extra", None) or [])
    model_name = None
    for i, tok in enumerate(extra):
        if tok in ("-m", "--model_name") and i + 1 < len(extra):
            model_name = extra[i + 1]
            break

    with conn.cursor() as cur:
        if model_name:
            cur.execute(
                """
                SELECT COUNT(*) FROM entity_archive
                WHERE entity_id::bigint = ANY(%s::bigint[])
                  AND model_name = %s
                  AND archived_at >= %s
                """,
                (ids, model_name, run_started_at),
            )
        else:
            cur.execute(
                """
                SELECT COUNT(*) FROM entity_archive
                WHERE entity_id::bigint = ANY(%s::bigint[])
                  AND archived_at >= %s
                """,
                (ids, run_started_at),
            )
        actual = cur.fetchone()[0]

    notes = []
    if model_name:
        notes.append(f"Filtered to model_name='{model_name}'.")
    else:
        notes.append("No -m model_name in args; counted across all model_names.")

    return PostflightReport(
        command="archive_table",
        target="",
        run_started_at=run_started_at.isoformat(),
        expected_updates=expected_updates,
        actual_updates=actual,
        table_inspected="entity_archive",
        timestamp_column="archived_at",
        notes=notes,
    )


# Registry of commands that have a postflight implementation.
POSTFLIGHT_REGISTRY: dict[str, Callable[..., PostflightReport]] = {
    "backfill_tags": postflight_backfill_tags,
    "backfill_fhs": postflight_backfill_fhs,
    "backfill_categories": postflight_backfill_categories,
    "backfill_imputation": postflight_backfill_imputation,
    "backfill_ni_profiles": postflight_backfill_ni_profiles,
    "backfill_proxy_match": postflight_backfill_proxy_match,
    "backfill_detailed_fhs_norms": postflight_backfill_detailed_fhs_norms,
    "approve_scores": postflight_approve_scores,
    "send_to_clients": postflight_send_to_clients,
    "remove_products_and_scores": postflight_remove_products_and_scores,
    "archive_table": postflight_archive_table,
}


# ---------------------------------------------------------------------------
# Top-level entry point used by ndo_run.py
# ---------------------------------------------------------------------------


def run_postflight(
    args,
    spec: dict,
    ndo_env: dict[str, str],
    ids: Optional[list[int]],
    run_started_at: datetime,
    expected_updates: Optional[int],
) -> tuple[Optional[PostflightReport], Optional[str]]:
    """Run postflight if applicable.

    Returns (report, skip_reason). At most one is non-None.

    Skip conditions:
    - `--no-postflight` (mirrors --no-preflight)
    - `--sync false` (async writes haven't landed yet)
    - command has no postflight impl
    - input is source/vendor/none (no per-id list to verify)
    - subprocess exited non-zero (caller's responsibility to check first)
    """
    if getattr(args, "no_postflight", False):
        return None, "postflight skipped via --no-postflight"

    # If --sync is explicitly false, async writes are still in flight
    if hasattr(args, "sync") and args.sync is False:
        return None, "postflight skipped: --sync false (async writes still in flight)"

    impl = POSTFLIGHT_REGISTRY.get(args.command)
    if not impl:
        return None, f"no postflight implementation for `{args.command}` yet"

    if not ids:
        return None, "postflight: no ids to verify (source/vendor mode or empty input)"

    # Defer the DB import + open: caller is expected to provide ndo_env with DATABASE_URL.
    db_url = ndo_env.get("DATABASE_URL")
    if not db_url:
        return None, "postflight: DATABASE_URL missing from runner env"

    try:
        with psycopg2.connect(db_url, connect_timeout=10) as conn:
            report = impl(conn, ids, run_started_at, expected_updates, args=args, spec=spec)
    except psycopg2.Error as e:
        return None, f"postflight: DB error: {e}"

    report.target = args.target
    return report, None
