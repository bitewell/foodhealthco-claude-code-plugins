"""Post-run verification for the ndo-run skill.

Where preflight predicts what *will* change, postflight measures what *did*
change and compares against the preflight forecast. Two dispatch tracks:

* `POSTFLIGHT_REGISTRY` — NDO `manage.py` commands. Impls have shape
  `(conn, args, started_at, preflight_payload) -> PostflightReport` and get
  a psycopg2 connection built from the runner's already-resolved `ndo_env`.

* `FHS_APP_POSTFLIGHT_REGISTRY` — commands with `tool: fhs_app` in catalog.
  Impls have shape `(args, run_meta, started_at) -> PostflightReport` and
  scan the filesystem (no DB connection) — fhs-app writes xlsx files to
  `output_scores/`, so postflight just lists what landed.

Both kinds of report flow through the same `PostflightReport` shape so the
runner can render them with one formatter and include both in `--summary-out`.

v0 NDO coverage: `bulk_create_products` only. v0 fhs-app coverage:
`generate_qa_report`. Wire additional impls in here as commands land.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import psycopg2

from preflight import Bucket, extract_ids_for_preflight, open_connection


# ---------------------------------------------------------------------------
# Report shape (mirrors PreflightReport — shared formatter friendly)
# ---------------------------------------------------------------------------


@dataclass
class PostflightReport:
    command: str
    target: str
    expected_count: Optional[int]
    actual_count: int
    buckets: list[Bucket] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def gap(self) -> Optional[int]:
        if self.expected_count is None:
            return None
        return self.actual_count - self.expected_count

    @property
    def is_ok(self) -> bool:
        return self.expected_count is None or self.actual_count == self.expected_count

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "target": self.target,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "gap": self.gap,
            "is_ok": self.is_ok,
            "buckets": [b.to_dict() for b in self.buckets],
            "notes": list(self.notes),
        }

    def format(self) -> str:
        border = "═" * 72
        status = "✓ matches preflight" if self.is_ok else f"⚠ gap={self.gap:+d}"
        if self.target == "prod":
            target_tag = "🔴 NDO PROD"
        elif self.target:
            target_tag = "🟢 NDO dev"
        else:
            # fhs-app runs have no NDO target; suppress the misleading tag.
            target_tag = "fhs-app (local filesystem)"
        lines = [
            "",
            border,
            f"Post-flight: {self.command}",
            f"  Target: {target_tag}",
            f"  Expected (preflight): {self.expected_count if self.expected_count is not None else 'n/a'}",
            f"  Actual: {self.actual_count}",
            f"  Status: {status}",
            border,
            "",
        ]
        if self.buckets:
            for b in self.buckets:
                suffix = f"  ({b.reason})" if b.reason else ""
                lines.append(f"  • {b.count:>6}  {b.label}{suffix}")
            lines.append("")
        if self.notes:
            lines.append("Notes:")
            for n in self.notes:
                lines.append(f"  • {n}")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-command postflight implementations
# ---------------------------------------------------------------------------


def _expected_will_create(preflight_payload: Optional[dict]) -> Optional[int]:
    """Pull preflight's 'Will create...' bucket count, or None if unavailable."""
    if not preflight_payload:
        return None
    for b in preflight_payload.get("buckets") or []:
        if b.get("kind") == "update":
            return int(b.get("count") or 0)
    return None


def postflight_bulk_create_products(
    conn, args, started_at: str, preflight_payload: Optional[dict]
) -> PostflightReport:
    """Count IPM rows created since the run started for the given source."""
    source = getattr(args, "source", None)
    if not source:
        return PostflightReport(
            command="bulk_create_products",
            target="",
            expected_count=None,
            actual_count=0,
            notes=["postflight: --source missing; cannot scope row count"],
        )

    with conn.cursor() as cur:
        cur.execute(
            """
                SELECT COUNT(*)
                FROM ingestion_productmatch
                WHERE source = %s
                  AND created_at >= %s
            """,
            (source, started_at),
        )
        (actual,) = cur.fetchone()

    expected = _expected_will_create(preflight_payload)
    buckets = [
        Bucket(
            label=f"IPM rows created (source={source}, since run start)",
            count=actual,
            kind="ok" if expected is None or actual == expected else "warn",
        )
    ]
    if expected is not None and actual != expected:
        buckets.append(
            Bucket(
                label="Gap vs preflight forecast",
                count=actual - expected,
                kind="warn",
                reason="rows may have been skipped at runtime (dup, validation, exception)",
            )
        )

    return PostflightReport(
        command="bulk_create_products",
        target="",
        expected_count=expected,
        actual_count=actual,
        buckets=buckets,
    )


# ---------------------------------------------------------------------------
# Helpers for the per-IPM backfill postflights (Phases 4–7, 12 in the demo)
# ---------------------------------------------------------------------------


def _expected_will_update(preflight_payload: Optional[dict]) -> Optional[int]:
    """Pull preflight's 'Will update'-class bucket counts. Sums all 'update' kind."""
    if not preflight_payload:
        return None
    total = 0
    found = False
    for b in preflight_payload.get("buckets") or []:
        if b.get("kind") == "update":
            total += int(b.get("count") or 0)
            found = True
    return total if found else None


def _ids_or_skip(args) -> tuple[Optional[list[int]], Optional[str]]:
    """Best-effort ID extraction for postflight. Returns (ids, reason_if_unavailable).

    Passes an empty spec to extract_ids_for_preflight — the source/vendor/none
    short-circuit fires harmlessly for source-only invocations (returns None
    with a clear reason) and falls through for --ids/--csv input."""
    ids, reason = extract_ids_for_preflight(args, {})
    if ids is None:
        return None, reason
    if not ids:
        return None, "postflight: no input ids resolved"
    return ids, None


def _count_changed_in_ipm(conn, ids: list[int], started_at: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
                SELECT COUNT(*)
                FROM ingestion_productmatch
                WHERE id = ANY(%s)
                  AND updated_at >= %s
            """,
            (list(ids), started_at),
        )
        (n,) = cur.fetchone()
    return int(n)


def _report_ipm_writes(
    command: str,
    label: str,
    args,
    conn,
    started_at: str,
    preflight_payload: Optional[dict],
) -> PostflightReport:
    """Shared body for tags/imputation/categories — they all write to IPM.

    Per-row distinctions (which column actually changed) belong in deeper
    diagnostics; postflight v0 just verifies *some* update landed for the
    input ID set since the run began.
    """
    ids, skip_reason = _ids_or_skip(args)
    if ids is None:
        return PostflightReport(
            command=command,
            target="",
            expected_count=None,
            actual_count=0,
            notes=[skip_reason or "postflight: no ids available"],
        )

    actual = _count_changed_in_ipm(conn, ids, started_at)
    expected = _expected_will_update(preflight_payload)
    is_ok = expected is None or actual == expected
    buckets = [
        Bucket(
            label=label,
            count=actual,
            kind="ok" if is_ok else "warn",
        )
    ]
    if expected is not None and actual != expected:
        buckets.append(
            Bucket(
                label="Gap vs preflight forecast",
                count=actual - expected,
                kind="warn",
                reason="rows may have failed mid-run (API rejection, exception, etc.)",
            )
        )
    return PostflightReport(
        command=command,
        target="",
        expected_count=expected,
        actual_count=actual,
        buckets=buckets,
    )


def postflight_backfill_tags(conn, args, started_at: str, preflight_payload: Optional[dict]) -> PostflightReport:
    return _report_ipm_writes(
        "backfill_tags",
        "IPM rows updated since run start (tag columns expected)",
        args, conn, started_at, preflight_payload,
    )


def postflight_backfill_imputation(conn, args, started_at: str, preflight_payload: Optional[dict]) -> PostflightReport:
    return _report_ipm_writes(
        "backfill_imputation",
        "IPM rows updated since run start (*_imputed columns expected)",
        args, conn, started_at, preflight_payload,
    )


def postflight_backfill_categories(conn, args, started_at: str, preflight_payload: Optional[dict]) -> PostflightReport:
    return _report_ipm_writes(
        "backfill_categories",
        "IPM rows updated since run start (product_category expected)",
        args, conn, started_at, preflight_payload,
    )


def postflight_backfill_fhs(conn, args, started_at: str, preflight_payload: Optional[dict]) -> PostflightReport:
    """Count new ScoringResult rows created since the run for the input IDs."""
    ids, skip_reason = _ids_or_skip(args)
    if ids is None:
        return PostflightReport(
            command="backfill_fhs",
            target="",
            expected_count=None,
            actual_count=0,
            notes=[skip_reason or "postflight: no ids available"],
        )

    with conn.cursor() as cur:
        cur.execute(
            """
                SELECT COUNT(*)
                FROM scoring_review_scoringresult
                WHERE product_match_id = ANY(%s)
                  AND created_at >= %s
            """,
            (list(ids), started_at),
        )
        (actual,) = cur.fetchone()
    actual = int(actual)

    expected = _expected_will_update(preflight_payload)
    is_ok = expected is None or actual == expected
    buckets = [
        Bucket(
            label="New ScoringResult rows created since run start",
            count=actual,
            kind="ok" if is_ok else "warn",
        )
    ]
    if expected is not None and actual != expected:
        buckets.append(
            Bucket(
                label="Gap vs preflight forecast",
                count=actual - expected,
                kind="warn",
                reason="FHS API may have rejected some rows; check upstream logs",
            )
        )
    return PostflightReport(
        command="backfill_fhs",
        target="",
        expected_count=expected,
        actual_count=actual,
        buckets=buckets,
        notes=["ScoringJob archives prior SR rows; this counts NEW rows only."],
    )


# ---------------------------------------------------------------------------
# fhs-app postflight impls (filesystem scans, no DB)
# ---------------------------------------------------------------------------


def postflight_generate_qa_report(
    args, run_meta: dict, started_at: str
) -> PostflightReport:
    """List the xlsx files fhs-app wrote into output_scores/.

    fhs-app names outputs `{source}_all_scores_{YYYYMMDD}_part_{n}.xlsx` and
    `{source}_unscorables_for_data_entry_{YYYYMMDD}_part_{n}.xlsx`. We scan
    for both patterns and only count files whose mtime is at or after the
    run start, so reruns aren't credited to a fresh invocation.

    Returns a "no expected count" report (expected_count=None) since there's
    no preflight to compare against — we just surface what landed and let
    the operator eyeball it. Zero files written is a strong signal that
    fhs-app errored out silently.
    """
    source = run_meta.get("source") or "wkbk_1"
    fhs_app_root = Path(run_meta.get("fhs_app_root") or "")
    output_dir = fhs_app_root / "output_scores"

    # Compare mtime against started_at (ISO 8601 UTC). Files written during
    # this run will have mtime >= started_at; older files from prior runs
    # are excluded.
    try:
        run_started = datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        run_started = 0.0

    scored_pattern = f"{source}_all_scores_*_part_*.xlsx"
    unscorable_pattern = f"{source}_unscorables_for_data_entry_*_part_*.xlsx"

    if not output_dir.is_dir():
        return PostflightReport(
            command="generate_qa_report",
            target="",
            expected_count=None,
            actual_count=0,
            buckets=[],
            notes=[f"postflight: {output_dir} does not exist — fhs-app did not write outputs"],
        )

    scored = [p for p in output_dir.glob(scored_pattern) if p.stat().st_mtime >= run_started]
    unscorable = [p for p in output_dir.glob(unscorable_pattern) if p.stat().st_mtime >= run_started]
    total = len(scored) + len(unscorable)

    buckets = [
        Bucket(
            label=f"all_scores xlsx (source={source})",
            count=len(scored),
            kind="ok" if scored else "warn",
            reason="" if scored else "no scored output — fhs-app may have failed",
        ),
        Bucket(
            label=f"unscorables_for_data_entry xlsx (source={source})",
            count=len(unscorable),
            kind="ok",
            reason="empty is fine if every input scored cleanly" if not unscorable else "",
        ),
    ]

    notes: list[str] = []
    if scored:
        notes.append(f"newest scored: {sorted(scored, key=lambda p: p.stat().st_mtime)[-1].name}")
    if total == 0:
        notes.append(
            "0 xlsx files written. Check streamed output above for errors; fhs-app "
            "swallows some exceptions and continues."
        )

    return PostflightReport(
        command="generate_qa_report",
        target="",
        expected_count=None,
        actual_count=total,
        buckets=buckets,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Registry + entry points
# ---------------------------------------------------------------------------


PostflightImpl = Callable[..., PostflightReport]

POSTFLIGHT_REGISTRY: dict[str, PostflightImpl] = {
    "bulk_create_products": postflight_bulk_create_products,
    "backfill_tags": postflight_backfill_tags,
    "backfill_imputation": postflight_backfill_imputation,
    "backfill_categories": postflight_backfill_categories,
    "backfill_fhs": postflight_backfill_fhs,
}

FHS_APP_POSTFLIGHT_REGISTRY: dict[str, PostflightImpl] = {
    "generate_qa_report": postflight_generate_qa_report,
}


def run_postflight(
    args,
    spec: dict,
    ndo_env: dict[str, str],
    started_at: str,
    preflight_payload: Optional[dict],
    exit_code: int,
) -> tuple[Optional[PostflightReport], Optional[str]]:
    """Run postflight if applicable.

    Returns (report, skip_reason). Mirrors run_preflight's shape.

    Skips when:
      - The command has no postflight impl
      - --dry-run was set (nothing to count)
      - The DB connection fails

    Always runs (even on exit_code != 0) so the operator can see whether
    partial progress landed.
    """
    if args.dry_run:
        return None, "postflight skipped: --dry-run"

    impl = POSTFLIGHT_REGISTRY.get(args.command)
    if not impl:
        return None, f"no postflight implementation for `{args.command}` yet"

    try:
        with open_connection(ndo_env) as conn:
            report = impl(conn, args, started_at, preflight_payload)
    except psycopg2.Error as e:
        return None, f"postflight: DB error: {e}"
    except RuntimeError as e:
        return None, str(e)

    report.target = args.target
    return report, None


def run_fhs_app_postflight(
    args,
    spec: dict,
    run_meta: dict,
    started_at: str,
    exit_code: int,
) -> tuple[Optional[PostflightReport], Optional[str]]:
    """Run a filesystem-based postflight for catalog entries with tool: fhs_app.

    Mirrors run_postflight's return shape so the caller can handle both the
    same way. Skips on --dry-run. Always runs (even on exit_code != 0) so
    operators see whether any files landed before fhs-app crashed.
    """
    if args.dry_run:
        return None, "postflight skipped: --dry-run"

    impl = FHS_APP_POSTFLIGHT_REGISTRY.get(args.command)
    if not impl:
        return None, f"no fhs-app postflight implementation for `{args.command}` yet"

    report = impl(args, run_meta, started_at)
    # fhs-app commands don't have a target (no NDO routing); leave the field
    # empty so the formatter prints "" rather than a stale dev/prod tag.
    report.target = ""
    return report, None
