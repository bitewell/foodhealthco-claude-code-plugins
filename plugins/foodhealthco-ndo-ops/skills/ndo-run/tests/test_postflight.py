"""Tests for the postflight module — report shape, status logic, registry,
and skip conditions. DB calls aren't tested (those are integration territory)."""
from __future__ import annotations

import pytest

from postflight import (
    POSTFLIGHT_REGISTRY,
    PostflightReport,
    run_postflight,
)


# ---------------------------------------------------------------------------
# Report status + drift logic
# ---------------------------------------------------------------------------


def _make_report(expected=None, actual=0, **kw):
    defaults = {
        "command": "backfill_tags",
        "target": "dev",
        "run_started_at": "2026-05-21T00:00:00+00:00",
        "expected_updates": expected,
        "actual_updates": actual,
        "table_inspected": "ingestion_productmatch",
        "timestamp_column": "updated_at",
    }
    defaults.update(kw)
    return PostflightReport(**defaults)


def test_status_ok_when_counts_match():
    r = _make_report(expected=10, actual=10)
    assert r.status == "ok"
    assert r.drift == 0


def test_status_drift_when_short():
    r = _make_report(expected=10, actual=7)
    assert r.status == "drift"
    assert r.drift == 3


def test_status_drift_when_extra():
    """Negative drift = more rows changed than expected. Still 'drift'."""
    r = _make_report(expected=10, actual=15)
    assert r.status == "drift"
    assert r.drift == -5


def test_status_no_baseline_when_expected_is_none():
    r = _make_report(expected=None, actual=10)
    assert r.status == "no_baseline"
    assert r.drift is None


def test_format_includes_status_icon():
    assert "✅" in _make_report(expected=5, actual=5).format()
    assert "⚠" in _make_report(expected=5, actual=3).format()
    assert "ℹ" in _make_report(expected=None, actual=5).format()


def test_to_dict_round_trips():
    r = _make_report(expected=3, actual=2)
    d = r.to_dict()
    assert d["expected_updates"] == 3
    assert d["actual_updates"] == 2
    assert d["drift"] == 1
    assert d["status"] == "drift"
    assert "table_inspected" in d
    assert "timestamp_column" in d


# ---------------------------------------------------------------------------
# Registry coverage
# ---------------------------------------------------------------------------


EXPECTED_POSTFLIGHT_COMMANDS = {
    "backfill_tags", "backfill_fhs", "backfill_categories",
    "backfill_imputation", "backfill_ni_profiles", "backfill_proxy_match",
    "backfill_detailed_fhs_norms",
    "approve_scores", "send_to_clients",
    "remove_products_and_scores", "archive_table",
}


def test_registry_covers_expected_commands():
    assert set(POSTFLIGHT_REGISTRY.keys()) == EXPECTED_POSTFLIGHT_COMMANDS


@pytest.mark.parametrize("cmd", sorted(EXPECTED_POSTFLIGHT_COMMANDS))
def test_each_postflight_is_callable(cmd):
    impl = POSTFLIGHT_REGISTRY[cmd]
    assert callable(impl)


# ---------------------------------------------------------------------------
# run_postflight skip conditions (no DB connection needed)
# ---------------------------------------------------------------------------


def test_skips_when_no_postflight_flag(make_args):
    args = make_args(no_postflight=True, command="backfill_tags")
    report, reason = run_postflight(
        args, {}, {"DATABASE_URL": "x"},
        ids=[1, 2, 3], run_started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        expected_updates=3,
    )
    assert report is None
    assert "--no-postflight" in reason


def test_skips_when_sync_false(make_args):
    args = make_args(no_postflight=False, sync=False, command="backfill_tags")
    report, reason = run_postflight(
        args, {}, {"DATABASE_URL": "x"},
        ids=[1, 2, 3], run_started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        expected_updates=3,
    )
    assert report is None
    assert "--sync false" in reason or "async" in reason.lower()


def test_skips_when_no_impl(make_args):
    args = make_args(no_postflight=False, command="generate_scores")  # no postflight impl
    report, reason = run_postflight(
        args, {}, {"DATABASE_URL": "x"},
        ids=[1], run_started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        expected_updates=1,
    )
    assert report is None
    assert "no postflight implementation" in reason


def test_skips_when_no_ids(make_args):
    args = make_args(no_postflight=False, command="backfill_tags")
    report, reason = run_postflight(
        args, {}, {"DATABASE_URL": "x"},
        ids=None, run_started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        expected_updates=None,
    )
    assert report is None
    assert "no ids to verify" in reason


def test_skips_when_database_url_missing(make_args):
    args = make_args(no_postflight=False, command="backfill_tags")
    report, reason = run_postflight(
        args, {}, {},  # empty ndo_env
        ids=[1, 2, 3], run_started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        expected_updates=3,
    )
    assert report is None
    assert "DATABASE_URL" in reason
