"""Tests for the recorder-database RTO analyser.

These tests build a real SQLite database with the real column definitions and
run the real queries against it. Nothing here is mocked: the point of the tool
is that its SQL is correct, and a double would assert only that we called
ourselves.

Schema verified against the installed 2026.6.4 recorder rather than assumed:

* ``recorder_runs`` — ``db_schema.py:787``; ``start`` / ``end`` /
  ``closed_incorrect``. One row per Home Assistant run.
* ``states`` — ``db_schema.py:415``; ``last_updated_ts`` is a float epoch
  (``TIMESTAMP_TYPE`` → ``DOUBLE``), and ``entity_id`` on this table is a
  legacy unused column — the live entity name lives in ``states_meta``.
* ``states_meta`` — ``db_schema.py:601``; ``metadata_id`` → ``entity_id``.

``recorder_runs.start`` / ``end`` are ``FAST_PYSQLITE_DATETIME``
(``db_schema.py:171``), a subclass of SQLAlchemy's SQLite ``DATETIME``, which
stores naive UTC as ``"YYYY-MM-DD HH:MM:SS.ffffff"``.

node-a has no ``recorder:`` block in ``configuration.yaml``, so it runs the
default SQLite recorder at ``config/home-assistant_v2.db`` with the default
10-day purge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from tools.rto.ha_recorder_rto import recovery_profile, restart_gaps

SCHEMA = """
CREATE TABLE recorder_runs (
    run_id INTEGER PRIMARY KEY,
    start DATETIME,
    end DATETIME,
    closed_incorrect BOOLEAN,
    created DATETIME
);
CREATE TABLE states_meta (
    metadata_id INTEGER PRIMARY KEY,
    entity_id VARCHAR(255)
);
CREATE TABLE states (
    state_id INTEGER PRIMARY KEY,
    metadata_id INTEGER,
    state VARCHAR(255),
    last_updated_ts FLOAT
);
"""

BASE = datetime(2026, 8, 15, 9, 0, 0, tzinfo=UTC)


def _sqlite_dt(value: datetime) -> str:
    """Render a datetime the way SQLAlchemy's SQLite DATETIME binds it."""
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    return connection


def _add_run(
    conn: sqlite3.Connection,
    run_id: int,
    start: datetime,
    end: datetime | None,
    *,
    unclean: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO recorder_runs (run_id, start, end, closed_incorrect, created) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            run_id,
            _sqlite_dt(start),
            _sqlite_dt(end) if end else None,
            int(unclean),
            _sqlite_dt(start),
        ),
    )
    conn.commit()


def _add_state(
    conn: sqlite3.Connection,
    entity_id: str,
    state: str,
    when: datetime,
) -> None:
    row = conn.execute(
        "SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        cursor = conn.execute("INSERT INTO states_meta (entity_id) VALUES (?)", (entity_id,))
        metadata_id = cursor.lastrowid
    else:
        metadata_id = row[0]
    conn.execute(
        "INSERT INTO states (metadata_id, state, last_updated_ts) VALUES (?, ?, ?)",
        (metadata_id, state, when.timestamp()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# restart_gaps — downtime between consecutive runs
# --------------------------------------------------------------------------


def test_measures_downtime_between_consecutive_runs(conn: sqlite3.Connection) -> None:
    """This is the empirical RTO of every restart in the retention window."""
    _add_run(conn, 1, BASE, BASE + timedelta(minutes=10))
    _add_run(conn, 2, BASE + timedelta(minutes=11), BASE + timedelta(minutes=20))

    (gap,) = restart_gaps(conn)

    assert gap.downtime_seconds == 60.0


def test_flags_a_run_that_did_not_shut_down_cleanly(conn: sqlite3.Connection) -> None:
    """An unclean stop is the failover case, and its `end` is only approximate.

    The recorder backfills `end` from the last write it managed, so the
    downtime is overstated by however long the write gap was. Worth knowing
    which figures carry that caveat.
    """
    _add_run(conn, 1, BASE, BASE + timedelta(minutes=10), unclean=True)
    _add_run(conn, 2, BASE + timedelta(minutes=11), BASE + timedelta(minutes=20))

    (gap,) = restart_gaps(conn)

    assert gap.unclean is True


def test_ignores_the_currently_open_run(conn: sqlite3.Connection) -> None:
    """The live run has no `end`, so it yields no gap."""
    _add_run(conn, 1, BASE, BASE + timedelta(minutes=10))
    _add_run(conn, 2, BASE + timedelta(minutes=11), None)

    assert len(restart_gaps(conn)) == 1


def test_orders_gaps_oldest_first(conn: sqlite3.Connection) -> None:
    _add_run(conn, 1, BASE, BASE + timedelta(minutes=10))
    _add_run(conn, 2, BASE + timedelta(minutes=11), BASE + timedelta(minutes=20))
    _add_run(conn, 3, BASE + timedelta(minutes=25), BASE + timedelta(minutes=30))

    gaps = restart_gaps(conn)

    assert [g.downtime_seconds for g in gaps] == [60.0, 300.0]


def test_skips_the_gap_after_a_run_that_never_recorded_an_end(
    conn: sqlite3.Connection,
) -> None:
    """A crashed run leaves `end` NULL, so the downtime after it is unknowable.

    Distinct from the currently-open run: this one has a successor, so it is
    reached as the *previous* half of a pair.
    """
    _add_run(conn, 1, BASE, BASE + timedelta(minutes=10))
    _add_run(conn, 2, BASE + timedelta(minutes=11), None)
    _add_run(conn, 3, BASE + timedelta(minutes=20), BASE + timedelta(minutes=30))

    gaps = restart_gaps(conn)

    assert [g.downtime_seconds for g in gaps] == [60.0]


def test_reports_no_gaps_for_a_single_run(conn: sqlite3.Connection) -> None:
    _add_run(conn, 1, BASE, BASE + timedelta(minutes=10))

    assert restart_gaps(conn) == []


# --------------------------------------------------------------------------
# recovery_profile — time from run start to a usable system
# --------------------------------------------------------------------------


def test_measures_time_until_a_quorum_of_entities_reports(conn: sqlite3.Connection) -> None:
    """ "Usable" is a quorum of the entities that were there before, not a log line."""
    run_start = BASE + timedelta(minutes=11)
    for name in ("light.a", "light.b", "light.c", "light.d"):
        _add_state(conn, name, "on", BASE + timedelta(minutes=5))

    _add_state(conn, "light.a", "on", run_start + timedelta(seconds=10))
    _add_state(conn, "light.b", "on", run_start + timedelta(seconds=20))
    _add_state(conn, "light.c", "on", run_start + timedelta(seconds=30))
    _add_state(conn, "light.d", "on", run_start + timedelta(seconds=90))

    profile = recovery_profile(conn, run_start, quorum=0.75)

    assert profile.baseline_entities == 4
    assert profile.seconds_to_quorum == 30.0


def test_does_not_count_unavailable_entities_as_recovered(conn: sqlite3.Connection) -> None:
    """An entity that comes back `unavailable` has not come back."""
    run_start = BASE + timedelta(minutes=11)
    for name in ("light.a", "light.b"):
        _add_state(conn, name, "on", BASE + timedelta(minutes=5))

    _add_state(conn, "light.a", "on", run_start + timedelta(seconds=10))
    _add_state(conn, "light.b", "unavailable", run_start + timedelta(seconds=15))
    _add_state(conn, "light.b", "on", run_start + timedelta(seconds=60))

    profile = recovery_profile(conn, run_start, quorum=1.0)

    assert profile.seconds_to_quorum == 60.0


def test_reports_no_quorum_when_the_window_expires(conn: sqlite3.Connection) -> None:
    """Never reaching quorum is a result, not an error."""
    run_start = BASE + timedelta(minutes=11)
    for name in ("light.a", "light.b"):
        _add_state(conn, name, "on", BASE + timedelta(minutes=5))
    _add_state(conn, "light.a", "on", run_start + timedelta(seconds=10))

    profile = recovery_profile(conn, run_start, quorum=1.0, window_seconds=120)

    assert profile.seconds_to_quorum is None
    assert profile.recovered == 1


def test_baseline_ignores_entities_absent_before_the_restart(conn: sqlite3.Connection) -> None:
    """A newly added entity is not evidence of recovery."""
    run_start = BASE + timedelta(minutes=11)
    _add_state(conn, "light.a", "on", BASE + timedelta(minutes=5))
    _add_state(conn, "light.a", "on", run_start + timedelta(seconds=10))
    _add_state(conn, "light.brand_new", "on", run_start + timedelta(seconds=12))

    profile = recovery_profile(conn, run_start, quorum=1.0)

    assert profile.baseline_entities == 1
    assert profile.seconds_to_quorum == 10.0


def test_a_new_entity_reporting_first_does_not_satisfy_quorum(
    conn: sqlite3.Connection,
) -> None:
    """Recovery is owed against what was there before, not whatever turns up.

    Ordering matters here: the new entity reports *first*, so counting it would
    declare recovery 35 seconds early.
    """
    run_start = BASE + timedelta(minutes=11)
    _add_state(conn, "light.a", "on", BASE + timedelta(minutes=5))
    _add_state(conn, "light.brand_new", "on", run_start + timedelta(seconds=5))
    _add_state(conn, "light.a", "on", run_start + timedelta(seconds=40))

    profile = recovery_profile(conn, run_start, quorum=1.0)

    assert profile.seconds_to_quorum == 40.0
