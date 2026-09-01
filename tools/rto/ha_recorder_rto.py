"""Measure real Home Assistant restarts from the recorder database.

Sharper than the log for two reasons.

**Sample size.** Home Assistant defaults to ``backupCount=1``
(``bootstrap.py:686``), so ``home-assistant.log`` holds the current run and the
previous one. The recorder holds every run inside the purge window — ten days
by default, which is dozens of restarts on real hardware.

**Fidelity.** The log's ``Home Assistant initialized in Xs`` fires when setup
returns. That is not when the system became *useful*. This module measures the
time until a quorum of the entities that were present before the restart have
written a real state again, which is much closer to what a failover has to
deliver.

Two measurements:

``restart_gaps``
    Downtime between consecutive runs, from the ``recorder_runs`` table. One
    row per Home Assistant run, so this is the empirical distribution of every
    restart already performed.

``recovery_profile``
    Time from a run's start until a quorum of the pre-restart entity set is
    reporting again.

Schema, verified against Home Assistant 2026.6.4:

* ``recorder_runs`` (``db_schema.py:787``) — ``start`` / ``end`` /
  ``closed_incorrect``. Stored by ``FAST_PYSQLITE_DATETIME``
  (``db_schema.py:171``) as naive UTC ``"YYYY-MM-DD HH:MM:SS.ffffff"``.
* ``states`` (``db_schema.py:415``) — ``last_updated_ts`` is a float epoch.
  Note ``states.entity_id`` is a legacy unused column; the live name is in
  ``states_meta``.
* ``states_meta`` (``db_schema.py:601``) — ``metadata_id`` → ``entity_id``.

The database is opened **read-only**, so this cannot disturb a running
recorder. It is still worth running against a copy.

Usage::

    python -m tools.rto.ha_recorder_rto \\
        /mnt/docker_data/homeassistant/config/home-assistant_v2.db

Standard library only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import math
from pathlib import Path
import sqlite3
import sys

UNREPORTED_STATES = ("unavailable", "unknown")


@dataclass(frozen=True)
class RestartGap:
    """The interval between one run ending and the next beginning."""

    previous_end: datetime
    next_start: datetime
    downtime_seconds: float
    unclean: bool


@dataclass(frozen=True)
class RecoveryProfile:
    """How long after a restart the system had a usable set of entities."""

    run_start: datetime
    baseline_entities: int
    recovered: int
    seconds_to_quorum: float | None
    quorum: float


def _parse_sqlite_datetime(raw: str | None) -> datetime | None:
    """Parse a SQLAlchemy SQLite DATETIME, which is naive UTC."""
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        return None


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open the recorder database without any possibility of writing to it."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def restart_gaps(conn: sqlite3.Connection) -> list[RestartGap]:
    """Downtime between each pair of consecutive runs, oldest first.

    The currently open run has no ``end`` and so contributes no gap.

    A gap whose *preceding* run is flagged ``unclean`` overstates the real
    downtime: the recorder never wrote a shutdown time, so ``end`` is backfilled
    from the last state it managed to persist. That is also the case a failover
    resembles most, which is why the flag is surfaced rather than filtered.
    """
    rows = conn.execute(
        "SELECT start, end, closed_incorrect FROM recorder_runs ORDER BY start"
    ).fetchall()

    runs = [
        (_parse_sqlite_datetime(start), _parse_sqlite_datetime(end), bool(unclean))
        for start, end, unclean in rows
    ]

    gaps: list[RestartGap] = []
    for (_, previous_end, previous_unclean), (next_start, _, _) in zip(
        runs, runs[1:], strict=False
    ):
        if previous_end is None or next_start is None:
            continue
        gaps.append(
            RestartGap(
                previous_end=previous_end,
                next_start=next_start,
                downtime_seconds=(next_start - previous_end).total_seconds(),
                unclean=previous_unclean,
            )
        )
    return gaps


def recovery_profile(
    conn: sqlite3.Connection,
    run_start: datetime,
    *,
    quorum: float = 0.9,
    window_seconds: float = 900.0,
    baseline_window_seconds: float = 3600.0,
) -> RecoveryProfile:
    """Seconds from ``run_start`` until ``quorum`` of the prior entities report.

    The baseline is the set of entities seen in ``baseline_window_seconds``
    before the restart — what the system had, and therefore what it owes. An
    entity that appears only *after* the restart is not evidence of recovery,
    and one that comes back ``unavailable`` has not come back.

    Returns ``seconds_to_quorum=None`` if quorum was never reached inside the
    window. That is a result, not an error: it is what a failover that did not
    complete looks like.
    """
    start_ts = run_start.timestamp()

    baseline = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT sm.entity_id FROM states s "
            "JOIN states_meta sm USING (metadata_id) "
            "WHERE s.last_updated_ts >= ? AND s.last_updated_ts < ?",
            (start_ts - baseline_window_seconds, start_ts),
        )
    }

    if not baseline:
        return RecoveryProfile(run_start, 0, 0, None, quorum)

    needed = math.ceil(quorum * len(baseline))
    placeholders = ", ".join("?" for _ in UNREPORTED_STATES)
    rows = conn.execute(
        "SELECT sm.entity_id, s.last_updated_ts FROM states s "
        "JOIN states_meta sm USING (metadata_id) "
        "WHERE s.last_updated_ts >= ? AND s.last_updated_ts <= ? "
        f"AND s.state IS NOT NULL AND s.state NOT IN ({placeholders}) "
        "ORDER BY s.last_updated_ts",
        (start_ts, start_ts + window_seconds, *UNREPORTED_STATES),
    )

    seen: set[str] = set()
    seconds_to_quorum: float | None = None
    for entity_id, timestamp in rows:
        if entity_id not in baseline:
            continue
        seen.add(entity_id)
        if seconds_to_quorum is None and len(seen) >= needed:
            seconds_to_quorum = round(timestamp - start_ts, 3)

    return RecoveryProfile(
        run_start=run_start,
        baseline_entities=len(baseline),
        recovered=len(seen),
        seconds_to_quorum=seconds_to_quorum,
        quorum=quorum,
    )


def run_starts(conn: sqlite3.Connection) -> list[datetime]:
    """Every run start in the database, oldest first."""
    rows = conn.execute("SELECT start FROM recorder_runs ORDER BY start").fetchall()
    return [parsed for (raw,) in rows if (parsed := _parse_sqlite_datetime(raw)) is not None]


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Sample sizes here do not warrant interpolation."""
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def format_report(
    gaps: list[RestartGap],
    profiles: list[RecoveryProfile],
    *,
    budget_seconds: float,
) -> str:
    """Render both measurements as text.

    These are figures from a *warm* host restarting itself. A cold promotion
    additionally pays container start and reconnection to every device, so the
    report says so rather than letting the number stand alone.
    """
    lines: list[str] = ["=== Downtime between runs (recorder_runs) ==="]

    if not gaps:
        lines.append("  No completed restart found — needs at least two runs in the window.")
    else:
        downtimes = [gap.downtime_seconds for gap in gaps]
        lines.append(f"  restarts observed  {len(gaps)}")
        lines.append(f"  best               {min(downtimes):.1f}s")
        lines.append(f"  median             {_percentile(downtimes, 0.5):.1f}s")
        lines.append(f"  p90                {_percentile(downtimes, 0.9):.1f}s")
        lines.append(f"  worst              {max(downtimes):.1f}s")

        unclean = sum(1 for gap in gaps if gap.unclean)
        if unclean:
            lines.append(
                f"  {unclean} of {len(gaps)} followed an unclean stop, so their downtime "
                "is overstated — see the module docstring."
            )
        lines.append("")
        lines.append("  Most recent restarts:")
        lines.extend(
            f"    {gap.previous_end:%Y-%m-%d %H:%M:%S} -> {gap.next_start:%H:%M:%S}  "
            f"{gap.downtime_seconds:8.1f}s{'  [unclean]' if gap.unclean else ''}"
            for gap in gaps[-10:]
        )

    lines.append("")
    lines.append("=== Time to a usable entity set (states) ===")

    measured = [p for p in profiles if p.seconds_to_quorum is not None]
    if not measured:
        lines.append("  No run reached quorum inside the measurement window.")
    else:
        times = [p.seconds_to_quorum for p in measured if p.seconds_to_quorum is not None]
        lines.append(f"  quorum             {measured[0].quorum * 100:.0f}% of prior entities")
        lines.append(f"  runs measured      {len(measured)} of {len(profiles)}")
        lines.append(f"  best               {min(times):.1f}s")
        lines.append(f"  median             {_percentile(times, 0.5):.1f}s")
        lines.append(f"  worst              {max(times):.1f}s")
        lines.append("")
        lines.extend(
            f"    {p.run_start:%Y-%m-%d %H:%M:%S}  {p.seconds_to_quorum:8.1f}s  "
            f"({p.recovered}/{p.baseline_entities} entities)"
            for p in measured[-10:]
        )
        lines.append("")

        worst = max(times)
        lines.append(f"Worst time-to-usable: {worst:.1f}s against a {budget_seconds:.0f}s budget.")
        if worst >= budget_seconds:
            lines.append(
                "VERDICT: restarts already exceed the failover budget on this hardware. "
                "Cold standby cannot meet it."
            )
        else:
            lines.append(
                f"VERDICT: {budget_seconds - worst:.0f}s of headroom, but these are warm "
                "restarts of a running host. A cold promotion also pays container start "
                "and reconnection to every device, which these figures do not include."
            )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure real restart times from the Home Assistant recorder database.",
    )
    parser.add_argument("database", type=Path, help="path to home-assistant_v2.db")
    parser.add_argument(
        "--budget",
        type=float,
        default=150.0,
        help="failover budget in seconds (default: 150, i.e. 2.5 minutes)",
    )
    parser.add_argument(
        "--quorum",
        type=float,
        default=0.9,
        help="fraction of the pre-restart entity set that counts as usable",
    )
    args = parser.parse_args(argv)

    if not args.database.exists():
        sys.stderr.write(f"No such database: {args.database}\n")
        return 1

    conn = open_readonly(args.database)
    try:
        gaps = restart_gaps(conn)
        # The first run has no predecessor, so there is nothing to recover from.
        profiles = [
            recovery_profile(conn, start, quorum=args.quorum) for start in run_starts(conn)[1:]
        ]
    finally:
        conn.close()

    sys.stdout.write(format_report(gaps, profiles, budget_seconds=args.budget))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
