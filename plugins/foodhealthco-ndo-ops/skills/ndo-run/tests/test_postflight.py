"""Registry-coverage and shape checks for postflight.

Postflight is opt-in (commands have to register an impl) but every command in
POSTFLIGHT_REGISTRY must:
  - exist in catalog.yaml
  - produce a PostflightReport with the right shape when called

These tests don't hit the DB — the bulk_create_products case stubs out the
psycopg2 cursor with an in-memory fake.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from postflight import (
    FHS_APP_POSTFLIGHT_REGISTRY,
    POSTFLIGHT_REGISTRY,
    PostflightReport,
    postflight_backfill_categories,
    postflight_backfill_fhs,
    postflight_backfill_imputation,
    postflight_backfill_tags,
    postflight_bulk_create_products,
    postflight_generate_qa_report,
)


CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog.yaml"


@pytest.fixture(scope="module")
def catalog_commands() -> set[str]:
    with open(CATALOG_PATH) as f:
        raw = yaml.safe_load(f)
    names: set[str] = set()
    for _section, commands in raw.items():
        names.update(commands.keys())
    return names


def test_every_registry_entry_is_in_catalog(catalog_commands):
    """A postflight for an unknown command is unreachable dead code."""
    for cmd in POSTFLIGHT_REGISTRY:
        assert cmd in catalog_commands, (
            f"POSTFLIGHT_REGISTRY has `{cmd}` but it isn't in catalog.yaml"
        )


def test_bulk_create_products_is_registered():
    assert "bulk_create_products" in POSTFLIGHT_REGISTRY, (
        "bulk_create_products is the v0 canary; it must stay registered"
    )


class _FakeCursor:
    def __init__(self, count: int):
        self._count = count

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self._sql = sql
        self._params = params

    def fetchone(self):
        return (self._count,)


class _FakeConn:
    def __init__(self, count: int):
        self._count = count

    def cursor(self):
        return _FakeCursor(self._count)


def test_postflight_bulk_create_products_matches_preflight():
    args = SimpleNamespace(source="tyson_20260521", csv="ignored")
    preflight_payload = {
        "buckets": [
            {"kind": "update", "count": 12, "label": "Will create new IPM rows"},
            {"kind": "skip", "count": 3, "label": "Skip: already in source"},
        ],
    }
    report = postflight_bulk_create_products(
        _FakeConn(count=12), args, "2026-05-21T12:00:00+00:00", preflight_payload
    )
    assert isinstance(report, PostflightReport)
    assert report.actual_count == 12
    assert report.expected_count == 12
    assert report.is_ok
    assert report.gap == 0


def test_postflight_bulk_create_products_flags_gap():
    args = SimpleNamespace(source="tyson_20260521", csv="ignored")
    preflight_payload = {"buckets": [{"kind": "update", "count": 19}]}
    # DB only has 15 — preflight predicted 19, so 4 rows silently dropped.
    report = postflight_bulk_create_products(
        _FakeConn(count=15), args, "2026-05-21T12:00:00+00:00", preflight_payload
    )
    assert report.actual_count == 15
    assert report.expected_count == 19
    assert not report.is_ok
    assert report.gap == -4
    # The gap should be visible as a `warn` bucket so it shows up in the
    # printed report and the JSON summary.
    gap_buckets = [b for b in report.buckets if b.kind == "warn"]
    assert gap_buckets, "expected a warn bucket flagging the preflight gap"


def test_postflight_handles_missing_source():
    """If --source isn't set, postflight returns a report with a note rather
    than blowing up — the runner shouldn't crash mid-finally."""
    args = SimpleNamespace(source=None, csv="ignored")
    report = postflight_bulk_create_products(
        _FakeConn(count=0), args, "2026-05-21T12:00:00+00:00", None
    )
    assert report.actual_count == 0
    assert report.expected_count is None
    assert any("source" in n.lower() for n in report.notes)


def test_every_fhs_app_registry_entry_is_in_catalog(catalog_commands):
    """fhs-app postflights point at real catalog commands."""
    for cmd in FHS_APP_POSTFLIGHT_REGISTRY:
        assert cmd in catalog_commands, (
            f"FHS_APP_POSTFLIGHT_REGISTRY has `{cmd}` but it isn't in catalog.yaml"
        )


def test_generate_qa_report_is_registered():
    assert "generate_qa_report" in FHS_APP_POSTFLIGHT_REGISTRY, (
        "generate_qa_report is the v0 fhs-app canary; it must stay registered"
    )


def test_postflight_generate_qa_report_counts_new_files(tmp_path):
    """Files written at-or-after run start are counted; older files are not."""
    output_dir = tmp_path / "output_scores"
    output_dir.mkdir()
    # An "old" pre-existing scored output from a prior demo
    old = output_dir / "demo_X_all_scores_20260101_part_1.xlsx"
    old.write_text("")
    import os, time
    old_ts = time.time() - 3600  # 1 hour ago
    os.utime(old, (old_ts, old_ts))

    # The current run starts now and produces 2 fresh files. We backdate
    # `started_at` by 2 seconds to model real production timing (the subprocess
    # runs for many seconds before fhs-app writes any output) AND to absorb
    # filesystem mtime granularity differences — macOS APFS records mtime to
    # the nanosecond, but ext4 on the GitHub Linux runner can round down to
    # whole seconds, which would make a just-written file's mtime land just
    # *before* a microsecond-precision started_at and trip the postflight
    # mtime filter into excluding it as "old."
    from datetime import datetime, timezone, timedelta
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    fresh_scored = output_dir / "demo_X_all_scores_20260521_part_1.xlsx"
    fresh_scored.write_text("")
    fresh_unscorable = output_dir / "demo_X_unscorables_for_data_entry_20260521_part_1.xlsx"
    fresh_unscorable.write_text("")

    args = SimpleNamespace()
    run_meta = {"source": "demo_X", "fhs_app_root": str(tmp_path)}
    report = postflight_generate_qa_report(args, run_meta, started_at)

    assert isinstance(report, PostflightReport)
    # 2 fresh files; the old one is excluded by mtime filter
    assert report.actual_count == 2
    # No preflight forecast for this command — expected stays None
    assert report.expected_count is None
    assert report.is_ok
    # Buckets break out by file kind
    labels = " ".join(b.label for b in report.buckets)
    assert "all_scores" in labels and "unscorables" in labels


def test_postflight_generate_qa_report_warns_on_zero_files(tmp_path):
    """0 files means fhs-app silently failed; postflight should surface that."""
    (tmp_path / "output_scores").mkdir()
    args = SimpleNamespace()
    run_meta = {"source": "demo_X", "fhs_app_root": str(tmp_path)}
    report = postflight_generate_qa_report(args, run_meta, datetime_now_iso())
    assert report.actual_count == 0
    # Operator-visible note explaining what 0 means
    assert any("0 xlsx" in n or "silently" in n for n in report.notes)
    # The "scored" bucket should be flagged warn so the printed report draws the eye
    warn_buckets = [b for b in report.buckets if b.kind == "warn"]
    assert warn_buckets, "expected a warn bucket when 0 scored files were written"


def test_postflight_generate_qa_report_handles_missing_output_dir(tmp_path):
    """No output_scores/ at all → clear note, no crash."""
    args = SimpleNamespace()
    run_meta = {"source": "demo_X", "fhs_app_root": str(tmp_path)}
    report = postflight_generate_qa_report(args, run_meta, datetime_now_iso())
    assert report.actual_count == 0
    assert any("output_scores" in n or "does not exist" in n for n in report.notes)


def datetime_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_postflight_handles_missing_preflight_payload():
    """No preflight ran → expected_count is None, report is still well-formed."""
    args = SimpleNamespace(source="tyson_20260521", csv="ignored")
    report = postflight_bulk_create_products(
        _FakeConn(count=7), args, "2026-05-21T12:00:00+00:00", None
    )
    assert report.actual_count == 7
    assert report.expected_count is None
    assert report.is_ok  # no expectation to violate
    assert report.gap is None


# -----------------------------------------------------------------------------
# Backfill postflights (tags / imputation / categories / fhs)
# -----------------------------------------------------------------------------

# These four were dropped during the PR #5 consolidation; re-added so the
# headline scoring phases get drift detection again.


def _ids_args(ids: str) -> SimpleNamespace:
    """Minimal args namespace that extract_ids_for_preflight will accept."""
    return SimpleNamespace(
        command="backfill_tags",
        ids=ids,
        csv=None,
        spaces_key=None,
    )


@pytest.mark.parametrize(
    "impl, command",
    [
        (postflight_backfill_tags, "backfill_tags"),
        (postflight_backfill_imputation, "backfill_imputation"),
        (postflight_backfill_categories, "backfill_categories"),
    ],
)
def test_ipm_writes_postflights_match_preflight(impl, command):
    """Tags/imputation/categories all read the IPM updated_at; same shape."""
    args = _ids_args("1,2,3")
    preflight = {"buckets": [{"kind": "update", "count": 3}]}
    report = impl(_FakeConn(count=3), args, "2026-05-22T12:00:00+00:00", preflight)
    assert isinstance(report, PostflightReport)
    assert report.command == command
    assert report.actual_count == 3
    assert report.expected_count == 3
    assert report.is_ok
    assert report.gap == 0


def test_ipm_writes_postflights_flag_drift():
    """Preflight predicted 5; only 3 landed → warn bucket fires."""
    args = _ids_args("1,2,3,4,5")
    preflight = {"buckets": [{"kind": "update", "count": 5}]}
    report = postflight_backfill_tags(
        _FakeConn(count=3), args, "2026-05-22T12:00:00+00:00", preflight
    )
    assert report.actual_count == 3
    assert report.expected_count == 5
    assert not report.is_ok
    assert report.gap == -2
    warn_buckets = [b for b in report.buckets if b.kind == "warn"]
    assert warn_buckets, "expected a warn bucket flagging the preflight gap"


def test_ipm_writes_postflight_skips_when_no_ids():
    """--source-only invocation can't scope by id; postflight returns a note."""
    args = SimpleNamespace(command="backfill_tags", ids=None, csv=None, spaces_key=None)
    report = postflight_backfill_tags(
        _FakeConn(count=0), args, "2026-05-22T12:00:00+00:00", None
    )
    assert report.actual_count == 0
    assert report.notes, "expected a note explaining why postflight skipped"


def test_postflight_backfill_fhs_counts_new_sr_rows():
    """backfill_fhs reports new ScoringResult rows, not IPM."""
    args = _ids_args("1599736,1599737,1599738,1599739,1599740")
    preflight = {"buckets": [{"kind": "update", "count": 5}]}
    report = postflight_backfill_fhs(
        _FakeConn(count=5), args, "2026-05-22T12:00:00+00:00", preflight
    )
    assert report.command == "backfill_fhs"
    assert report.actual_count == 5
    assert report.expected_count == 5
    assert report.is_ok
    # Note about archiving prior SR rows should be present so operators don't
    # double-count.
    assert any("archives" in n.lower() or "NEW rows" in n for n in report.notes)


def test_postflight_backfill_fhs_flags_drift_when_api_rejects():
    """FHS API silently skips some products → postflight surfaces the gap."""
    args = _ids_args("1,2,3,4,5")
    preflight = {"buckets": [{"kind": "update", "count": 5}]}
    report = postflight_backfill_fhs(
        _FakeConn(count=3), args, "2026-05-22T12:00:00+00:00", preflight
    )
    assert report.actual_count == 3
    assert report.expected_count == 5
    assert report.gap == -2
    assert not report.is_ok
    warn_buckets = [b for b in report.buckets if b.kind == "warn"]
    assert warn_buckets


def test_every_backfill_postflight_is_in_registry():
    for command in (
        "backfill_tags",
        "backfill_imputation",
        "backfill_categories",
        "backfill_fhs",
    ):
        assert command in POSTFLIGHT_REGISTRY, (
            f"{command} postflight regressed during PR #5 consolidation; "
            "guard against re-regression."
        )
