"""Post-run verification for the ndo-run skill.

Where preflight predicts what *will* change, postflight measures what *did*
change and compares against the preflight forecast. Same dispatch shape:

* A `POSTFLIGHT_REGISTRY` maps command names to impls of shape
  `(conn, args, started_at, preflight_payload) -> PostflightReport`.
* The report mirrors preflight's `PreflightReport` so the runner can render
  both with the same formatter and include both in `--summary-out`.
* DB access is read-only via `psycopg2`, piggybacking on the runner's already-
  built `ndo_env` so we hit the same DB the command wrote to.

v0 covers `bulk_create_products` only — the first command that's both write-
heavy and easy to measure (a single COUNT against IPM). Wire additional impls
in here as creation-style commands land.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import psycopg2

from preflight import Bucket, open_connection


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
        target_tag = "🔴 NDO PROD" if self.target == "prod" else "🟢 NDO dev"
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
# Registry + entry point
# ---------------------------------------------------------------------------


PostflightImpl = Callable[..., PostflightReport]

POSTFLIGHT_REGISTRY: dict[str, PostflightImpl] = {
    "bulk_create_products": postflight_bulk_create_products,
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
