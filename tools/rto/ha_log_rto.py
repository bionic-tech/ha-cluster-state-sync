"""Extract startup timings from a Home Assistant log.

Answers one question: **how much of the failover budget does Home Assistant's
own startup consume?** That is the largest single component of a cold-standby
promotion, and unlike the rest of the RTO it can be measured from data the
running node has already written.

The result is asymmetric. A large number here *disproves* cold standby on its
own. A small one does not prove it, because this figure excludes container
start, the snapshot restore, and the device reconnection a cold node faces and
a warm one does not. See ``docs/designs/READINESS.md`` H1.

Log lines parsed, verified against Home Assistant 2026.6.4:

===============================================  =====================  =======
Message                                          Source                 Level
===============================================  =====================  =======
``Starting Home Assistant %s``                   ``core.py:500``        INFO
``Setup of domain %s took %.2f seconds``         ``setup.py:790``       INFO
``Setup of %s is taking over %s seconds.``       ``setup.py:403``       WARNING
``Home Assistant initialized in %.2fs``          ``bootstrap.py:569``   INFO
===============================================  =====================  =======

The line format is ``bootstrap.py:585``::

    %(asctime)s.%(msecs)03d %(levelname)s (%(threadName)s) [%(name)s] %(message)s

with ``asctime`` as ``"%Y-%m-%d %H:%M:%S"`` (``const.py:988``).

Usage::

    python -m tools.rto.ha_log_rto /mnt/docker_data/homeassistant/config/home-assistant.log

Standard library only, so it runs on the host without installing anything.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
import sys

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"(?P<level>\w+) +\((?P<thread>[^)]*)\) +\[(?P<logger>[^\]]*)\] +(?P<message>.*)$"
)

# `\S+$` deliberately fails to match "Starting Home Assistant core loop"
# (core.py:466), which is a different message that shares this prefix.
_STARTING = re.compile(r"^Starting Home Assistant (?P<version>\S+)$")
_DOMAIN = re.compile(r"^Setup of domain (?P<domain>\S+) took (?P<seconds>[\d.]+) seconds$")
_SLOW = re.compile(r"^Setup of (?P<domain>\S+) is taking over \d+ seconds\.?$")
_INITIALIZED = re.compile(r"^Home Assistant initialized in (?P<seconds>[\d.]+)s$")

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


@dataclass
class StartupRun:
    """One Home Assistant run, from its first log line to `initialized`."""

    version: str | None = None
    started_at: datetime | None = None
    initialized_at: datetime | None = None
    initialized_seconds: float | None = None
    domain_seconds: dict[str, float] = field(default_factory=dict)
    slow_setups: list[str] = field(default_factory=list)

    @property
    def wall_clock_seconds(self) -> float | None:
        """Elapsed time by the log's own clock.

        Home Assistant reports `initialized_seconds` from ``monotonic()``. This
        is the same span measured against wall clock, so a divergence between
        the two means the system clock moved during startup — which matters for
        a project whose restore path refuses future-dated entries.
        """
        if self.started_at is None or self.initialized_at is None:
            return None
        return round((self.initialized_at - self.started_at).total_seconds(), 3)

    @property
    def completed(self) -> bool:
        return self.initialized_seconds is not None

    def slowest(self, count: int = 10) -> list[tuple[str, float]]:
        """The domains costing the most setup time, worst first."""
        ranked = sorted(self.domain_seconds.items(), key=lambda item: item[1], reverse=True)
        return ranked[:count]


def _parse_timestamp(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, _TIMESTAMP_FORMAT)
    except ValueError:
        return None


def parse_runs(lines: Iterable[str]) -> list[StartupRun]:
    """Split a log into runs and report the startup timings of each.

    A run is delimited by its *end*, not its start. Home Assistant emits
    ``Starting Home Assistant <version>`` from ``hass.async_start()``, which
    runs after bootstrap has finished, so it is the **last** line of a startup
    rather than the first — verified against a live log on node-a, where the
    timing lines sat at 52…1195, ``initialized`` at 1325 and ``Starting`` at
    1326.

    So: timing lines accumulate into an open run, ``initialized`` records the
    result, and the trailing ``Starting`` supplies the version and closes it.
    A run is only created once a marker actually belongs to it, which keeps the
    days of runtime output that follow a startup from forming a phantom run.

    Lines that do not match Home Assistant's format — tracebacks, third-party
    output, partial writes — are skipped rather than raising. Real logs are
    full of them.
    """
    runs: list[StartupRun] = []
    current: StartupRun | None = None
    # Timestamp of the first parseable line since the last run closed. Boot
    # begins before the first integration finishes, so the run's own first
    # marker is already too late to serve as its start.
    pending_start: datetime | None = None

    def open_run() -> StartupRun:
        nonlocal current
        if current is None:
            current = StartupRun(started_at=pending_start)
            runs.append(current)
        return current

    for line in lines:
        match = _LINE.match(line.rstrip("\n"))
        if match is None:
            continue

        message = match["message"]
        timestamp = _parse_timestamp(match["ts"])

        if current is None and pending_start is None:
            pending_start = timestamp

        if (domain := _DOMAIN.match(message)) is not None:
            open_run().domain_seconds[domain["domain"]] = float(domain["seconds"])
        elif (slow := _SLOW.match(message)) is not None:
            open_run().slow_setups.append(slow["domain"])
        elif (initialized := _INITIALIZED.match(message)) is not None:
            run = open_run()
            run.initialized_seconds = float(initialized["seconds"])
            run.initialized_at = timestamp
        elif (starting := _STARTING.match(message)) is not None and current is not None:
            current.version = starting["version"]
            current = None
            pending_start = None

    return runs


def format_report(runs: list[StartupRun], *, budget_seconds: float, top: int = 10) -> str:
    """Render parsed runs as text, judged against the failover budget.

    The verdict wording is deliberate. This measurement can *disprove* cold
    standby on its own, but it cannot confirm it, because it excludes container
    start, snapshot restore and device reconnection. A report that reads as a
    pass would be worse than no report.
    """
    if not runs:
        return "No Home Assistant startup found in this log.\n"

    lines: list[str] = []
    for index, run in enumerate(runs, start=1):
        lines.append(f"Run {index}/{len(runs)} — Home Assistant {run.version or 'unknown'}")
        lines.append(f"  started            {run.started_at or 'unknown'}")

        if not run.completed:
            lines.append("  initialized        NEVER — this run did not finish starting")
            lines.append(f"  domains set up     {len(run.domain_seconds)} before it stopped")
            lines.append("")
            continue

        assert run.initialized_seconds is not None
        lines.append(f"  initialized in     {run.initialized_seconds:.2f}s")
        if run.wall_clock_seconds is not None:
            lines.append(f"  wall clock         {run.wall_clock_seconds:.3f}s")
        share = run.initialized_seconds / budget_seconds * 100
        lines.append(f"  share of budget    {share:.0f}% of {budget_seconds:.0f}s")

        if run.slow_setups:
            lines.append(f"  slow setups        {', '.join(run.slow_setups)}")

        if run.domain_seconds:
            lines.append(f"  slowest domains (top {top}):")
            lines.extend(
                f"    {seconds:8.2f}s  {domain}" for domain, seconds in run.slowest(top)
            )
        lines.append("")

    completed = [r.initialized_seconds for r in runs if r.initialized_seconds is not None]
    if completed:
        worst = max(completed)
        lines.append(
            f"Worst startup observed: {worst:.2f}s against a {budget_seconds:.0f}s budget."
        )
        if worst >= budget_seconds:
            lines.append(
                "VERDICT: cold standby cannot meet the budget. Home Assistant's own "
                "startup exceeds it before container start, snapshot restore or "
                "device reconnection are counted."
            )
        else:
            lines.append(
                f"VERDICT: cold standby remains possible — {budget_seconds - worst:.0f}s "
                "is left for container start, snapshot restore and device "
                "reconnection, which this figure does not include. Not a pass."
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure Home Assistant startup time from its log.",
    )
    parser.add_argument(
        "logfile", type=Path, nargs="?", help="path to home-assistant.log (default: stdin)"
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=150.0,
        help="failover budget in seconds (default: 150, i.e. 2.5 minutes)",
    )
    parser.add_argument("--top", type=int, default=10, help="how many slow domains to list")
    args = parser.parse_args(argv)

    if args.logfile is None:
        runs = parse_runs(sys.stdin)
    else:
        with args.logfile.open(encoding="utf-8", errors="replace") as handle:
            runs = parse_runs(handle)

    sys.stdout.write(format_report(runs, budget_seconds=args.budget, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
