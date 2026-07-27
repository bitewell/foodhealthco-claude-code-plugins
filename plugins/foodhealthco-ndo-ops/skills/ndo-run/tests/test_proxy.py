"""Unit tests for the cloud-sql-proxy lifecycle gating in ndo_run.

These cover the decision logic (when NOT to start a proxy) without spawning a
real proxy — the spawn path needs gcloud/ADC and a live instance, so it's
exercised in integration, not here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ndo_run  # noqa: E402


def test_dsn_host_port_localhost():
    assert ndo_run._dsn_host_port(
        "postgresql://ndo:pw@127.0.0.1:5443/ndo?sslmode=disable"
    ) == ("127.0.0.1", 5443)


def test_dsn_host_port_none():
    assert ndo_run._dsn_host_port(None) == (None, None)


def test_platform_db_never_starts_proxy():
    # HeroDB proxy lifecycle belongs to the db-connect skill, not the runner.
    assert (
        ndo_run.start_proxy_if_needed(
            "prod", "postgresql://x@127.0.0.1:5443/herodb", "platform"
        )
        is None
    )


def test_remote_dsn_is_left_alone():
    # A non-local DSN means a direct connection — nothing to manage.
    assert (
        ndo_run.start_proxy_if_needed(
            "prod", "postgresql://ndo:pw@ndo.example.com:5432/ndo", "ndo"
        )
        is None
    )


def test_existing_proxy_is_reused_not_restarted(monkeypatch):
    # If something is already listening on the port, reuse it (return None so
    # nothing gets torn down later).
    monkeypatch.setattr(ndo_run, "_port_listening", lambda *a, **k: True)
    assert (
        ndo_run.start_proxy_if_needed(
            "prod", "postgresql://ndo:pw@127.0.0.1:5443/ndo", "ndo"
        )
        is None
    )


def test_stop_proxy_none_is_noop():
    ndo_run.stop_proxy(None)  # must not raise
