"""Tests for the reporting and command-line layer of the RTO analysers.

This layer is what actually runs on node-a, so its failure modes are the
ones that matter in practice: a crash on an unexpected log, or — worse — a
confident number with no caveat attached to it.

The verdict assertions are the point of these tests. The whole value of the
measurement is that it is *asymmetric*: it can disprove cold standby but cannot
confirm it, and a report that does not say so invites exactly the wrong
conclusion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from tests.test_rto_recorder import SCHEMA, _add_run, _add_state
from tools.rto import ha_log_rto, ha_recorder_rto

BASE = datetime(2026, 8, 15, 9, 0, 0, tzinfo=UTC)


def _log_lines(initialized_seconds: float) -> list[str]:
    """A complete run in Home Assistant's real order.

    ``Starting Home Assistant`` is emitted by ``hass.async_start()`` and so
    comes *last* — see the module docstring of ``tests/test_rto_log.py``.
    """
    return [
        "2026-08-15 09:14:02.113 WARNING (SyncWorker_0) [homeassistant.loader] "
        "We found a custom integration royalmail which has not been tested",
        "2026-08-15 09:14:03.501 INFO (MainThread) [homeassistant.setup] "
        "Setup of domain mqtt took 6.40 seconds",
        f"2026-08-15 09:14:45.220 INFO (MainThread) [homeassistant.bootstrap] "
        f"Home Assistant initialized in {initialized_seconds:.2f}s",
        "2026-08-15 09:14:45.221 INFO (MainThread) [homeassistant.core] "
        "Starting Home Assistant 2026.4.0",
    ]


# --------------------------------------------------------------------------
# Log report
# --------------------------------------------------------------------------


def test_log_report_declares_cold_standby_impossible_when_over_budget() -> None:
    """The decisive result. Startup alone exceeding the budget settles it."""
    runs = ha_log_rto.parse_runs(_log_lines(200.0))

    report = ha_log_rto.format_report(runs, budget_seconds=150.0)

    assert "cold standby cannot meet the budget" in report.lower()


def test_log_report_does_not_claim_cold_standby_works_when_under_budget() -> None:
    """Under budget is not a pass — the figure excludes most of a promotion."""
    runs = ha_log_rto.parse_runs(_log_lines(40.0))

    report = ha_log_rto.format_report(runs, budget_seconds=150.0).lower()

    assert "remains possible" in report
    assert "does not include" in report


def test_log_report_names_the_slowest_domains() -> None:
    runs = ha_log_rto.parse_runs(_log_lines(40.0))

    assert "mqtt" in ha_log_rto.format_report(runs, budget_seconds=150.0)


def test_log_report_handles_a_log_with_no_startup() -> None:
    assert "no home assistant startup" in ha_log_rto.format_report([], budget_seconds=150.0).lower()


def test_log_report_flags_a_run_that_never_finished() -> None:
    runs = ha_log_rto.parse_runs(_log_lines(40.0)[:2])

    assert "never" in ha_log_rto.format_report(runs, budget_seconds=150.0).lower()


def test_log_cli_reads_a_file_and_prints_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    logfile = tmp_path / "home-assistant.log"
    logfile.write_text("\n".join(_log_lines(40.0)), encoding="utf-8")

    assert ha_log_rto.main([str(logfile)]) == 0
    assert "40.00s" in capsys.readouterr().out


def test_log_cli_accepts_a_budget_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    logfile = tmp_path / "home-assistant.log"
    logfile.write_text("\n".join(_log_lines(40.0)), encoding="utf-8")

    ha_log_rto.main([str(logfile), "--budget", "30"])

    assert "cannot meet the budget" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------
# Recorder report
# --------------------------------------------------------------------------


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """A database with two runs and a measurable recovery."""
    path = tmp_path / "home-assistant_v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    run_start = BASE + timedelta(minutes=11)
    _add_run(conn, 1, BASE, BASE + timedelta(minutes=10))
    _add_run(conn, 2, run_start, BASE + timedelta(minutes=30))

    for name in ("light.a", "light.b"):
        _add_state(conn, name, "on", BASE + timedelta(minutes=5))
    _add_state(conn, "light.a", "on", run_start + timedelta(seconds=10))
    _add_state(conn, "light.b", "on", run_start + timedelta(seconds=20))

    conn.close()
    return path


def test_recorder_report_summarises_downtime(populated_db: Path) -> None:
    conn = ha_recorder_rto.open_readonly(populated_db)
    gaps = ha_recorder_rto.restart_gaps(conn)

    report = ha_recorder_rto.format_report(gaps, [], budget_seconds=150.0)

    assert "restarts observed  1" in report
    assert "60.0s" in report


def test_recorder_report_states_when_there_is_nothing_to_measure() -> None:
    report = ha_recorder_rto.format_report([], [], budget_seconds=150.0)

    assert "no completed restart" in report.lower()


def test_recorder_report_warns_that_a_cold_promotion_costs_more(populated_db: Path) -> None:
    """Warm-restart figures must not be read as a cold-promotion result."""
    conn = ha_recorder_rto.open_readonly(populated_db)
    profiles = [
        ha_recorder_rto.recovery_profile(conn, start, quorum=1.0)
        for start in ha_recorder_rto.run_starts(conn)[1:]
    ]

    report = ha_recorder_rto.format_report(
        ha_recorder_rto.restart_gaps(conn), profiles, budget_seconds=150.0
    ).lower()

    assert "do not include" in report


def test_recorder_cli_runs_end_to_end(
    populated_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ha_recorder_rto.main([str(populated_db)]) == 0

    out = capsys.readouterr().out
    assert "Downtime between runs" in out
    assert "Time to a usable entity set" in out


def test_recorder_cli_reports_a_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ha_recorder_rto.main([str(tmp_path / "nope.db")]) == 1
    assert "no such database" in capsys.readouterr().err.lower()


def test_recorder_cli_does_not_write_to_the_database(populated_db: Path) -> None:
    """It runs against a live recorder, so it must be incapable of writing."""
    conn = ha_recorder_rto.open_readonly(populated_db)

    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO states_meta (entity_id) VALUES ('light.x')")
