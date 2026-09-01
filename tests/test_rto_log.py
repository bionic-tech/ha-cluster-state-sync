"""Tests for the Home Assistant startup-log RTO analyser.

The log lines below are in Home Assistant's real format, and — since 2026-08-25
— in Home Assistant's real *order*. Both were checked against a live log on
node-a rather than inferred:

* format string — ``bootstrap.py:585``
  ``"%(asctime)s.%(msecs)03d %(levelname)s (%(threadName)s) [%(name)s] %(message)s"``
* ``asctime`` layout — ``const.py:988``, ``"%Y-%m-%d %H:%M:%S"``
* ``"Setup of domain %s took %.2f seconds"`` — ``setup.py:790`` (INFO)
* ``"Setup of %s is taking over %s seconds."`` — ``setup.py:403`` (WARNING)
* ``"Home Assistant initialized in %.2fs"`` — ``bootstrap.py:569`` (INFO)
* ``"Starting Home Assistant %s"`` — ``core.py:500`` (INFO)

**Ordering matters and is counter-intuitive.** ``Starting Home Assistant`` is
emitted by ``hass.async_start()``, which runs *after* bootstrap has finished
setting up integrations. So a run reads:

    Setup of domain … × N      ← first
    Home Assistant initialized in Xs
    Starting Home Assistant 2026.4.4   ← last

On node-a those landed at lines 52…1195, 1325 and 1326 respectively. An
earlier version of this parser treated ``Starting Home Assistant`` as the
*opening* of a run and silently discarded every timing line before it,
reporting a healthy instance as "never finished starting". These fixtures are
written in the real order so that mistake cannot come back.
"""

from __future__ import annotations

from datetime import datetime

from tools.rto.ha_log_rto import parse_runs

# One complete run, in the order Home Assistant actually emits it.
RUN_ONE = [
    "2026-08-15 09:14:02.113 WARNING (SyncWorker_0) [homeassistant.loader] "
    "We found a custom integration royalmail which has not been tested",
    "2026-08-15 09:14:03.501 INFO (MainThread) [homeassistant.setup] "
    "Setup of domain recorder took 1.20 seconds",
    "2026-08-15 09:14:10.004 INFO (MainThread) [homeassistant.setup] "
    "Setup of domain mqtt took 6.40 seconds",
    "2026-08-15 09:14:45.220 INFO (MainThread) [homeassistant.bootstrap] "
    "Home Assistant initialized in 43.11s",
    "2026-08-15 09:14:45.221 INFO (MainThread) [homeassistant.core] "
    "Starting Home Assistant 2026.4.0",
]

RUN_TWO = [
    "2026-08-15 18:02:00.000 WARNING (SyncWorker_0) [homeassistant.loader] "
    "We found a custom integration royalmail which has not been tested",
    "2026-08-15 18:02:05.000 INFO (MainThread) [homeassistant.setup] "
    "Setup of domain recorder took 0.90 seconds",
    "2026-08-15 18:02:31.500 INFO (MainThread) [homeassistant.bootstrap] "
    "Home Assistant initialized in 31.50s",
    "2026-08-15 18:02:31.501 INFO (MainThread) [homeassistant.core] "
    "Starting Home Assistant 2026.4.0",
]

# What a real log looks like for the 18 days after startup finishes.
RUNTIME_NOISE = [
    "2026-08-20 12:00:00.479 INFO (MainThread) [homeassistant.components.automation] "
    "Update-automation - to-do sweep: Executing step call service",
    "2026-08-21 03:14:15.000 WARNING (MainThread) [homeassistant.helpers.entity] "
    "Update of sensor.foo is taking over 10 seconds",
]


def test_collects_timing_lines_that_precede_the_starting_line() -> None:
    """The regression that real data caught.

    Every timing line is emitted before ``Starting Home Assistant``. A parser
    that opens a run on that line discards all of them.
    """
    (run,) = parse_runs(RUN_ONE)

    assert run.initialized_seconds == 43.11
    assert run.domain_seconds == {"recorder": 1.20, "mqtt": 6.40}


def test_captures_the_version_from_the_trailing_starting_line() -> None:
    """The version arrives after the run it describes has already finished."""
    (run,) = parse_runs(RUN_ONE)

    assert run.version == "2026.4.0"


def test_start_time_is_the_first_line_of_the_run_not_the_first_marker() -> None:
    """Boot begins before the first integration finishes setting up.

    On node-a the first log line was 0.46s ahead of the first
    ``Setup of domain``, and within 0.15s of ``initialized`` minus the reported
    duration — so the first line is the honest start.
    """
    (run,) = parse_runs(RUN_ONE)

    assert run.started_at == datetime(2026, 8, 15, 9, 14, 2, 113000)
    assert run.initialized_at == datetime(2026, 8, 15, 9, 14, 45, 220000)


def test_wall_clock_span_is_measured_independently_of_the_reported_figure() -> None:
    """HA's own figure uses monotonic(); the timestamps are a cross-check."""
    (run,) = parse_runs(RUN_ONE)

    assert run.wall_clock_seconds == 43.107


def test_splits_a_log_containing_several_runs() -> None:
    runs = parse_runs([*RUN_ONE, *RUNTIME_NOISE, *RUN_TWO])

    assert [r.initialized_seconds for r in runs] == [43.11, 31.50]


def test_runtime_output_after_a_run_does_not_create_a_phantom_run() -> None:
    """A real log is mostly the 18 days of running that follow startup."""
    runs = parse_runs([*RUN_ONE, *RUNTIME_NOISE])

    assert len(runs) == 1


def test_survives_a_log_that_opens_on_a_trailing_starting_line() -> None:
    """A rotated log routinely begins partway through a run.

    With ``backupCount=1`` the top of the file is the tail of a startup whose
    timing lines are gone, so the first thing seen can be the closing
    ``Starting Home Assistant`` of a run that cannot be measured. It must be
    discarded, not attached to the run that follows.
    """
    runs = parse_runs(
        [
            "2026-08-15 06:00:00.000 INFO (MainThread) [homeassistant.core] "
            "Starting Home Assistant 2025.9.9",
            *RUN_ONE,
        ]
    )

    assert len(runs) == 1
    assert runs[0].version == "2026.4.0"


def test_records_slow_setup_warnings() -> None:
    """These are the integrations at risk of hitting the 300s per-domain cap."""
    (run,) = parse_runs(
        [
            *RUN_ONE[:2],
            "2026-08-15 09:14:20.000 WARNING (MainThread) [homeassistant.setup] "
            "Setup of zwave_js is taking over 10 seconds.",
            *RUN_ONE[2:],
        ]
    )

    assert run.slow_setups == ["zwave_js"]


def test_does_not_mistake_a_runtime_warning_for_a_slow_setup() -> None:
    """"is taking over N seconds" also appears for entity updates at runtime.

    On node-a that phrase occurred 26,241 times against 194 real setups, so
    matching it loosely would bury the signal completely.
    """
    (run,) = parse_runs([*RUN_ONE, *RUNTIME_NOISE])

    assert run.slow_setups == []


def test_ranks_the_slowest_domains() -> None:
    (run,) = parse_runs(RUN_ONE)

    assert run.slowest(1) == [("mqtt", 6.40)]


def test_ignores_lines_it_does_not_understand() -> None:
    noisy = [
        *RUN_ONE[:2],
        "not a log line at all",
        '  File "/config/custom_components/foo/__init__.py", line 1, in <module>',
        *RUN_ONE[2:],
    ]

    (run,) = parse_runs(noisy)

    assert run.initialized_seconds == 43.11
    assert run.domain_seconds == {"recorder": 1.20, "mqtt": 6.40}


def test_reports_a_run_that_never_finished_starting() -> None:
    """A crash during setup is the case most worth seeing, not dropping."""
    (run,) = parse_runs(RUN_ONE[:3])

    assert run.initialized_seconds is None
    assert run.domain_seconds == {"recorder": 1.20, "mqtt": 6.40}


def test_wall_clock_is_unavailable_for_a_run_that_never_finished() -> None:
    (run,) = parse_runs(RUN_ONE[:3])

    assert run.wall_clock_seconds is None


def test_returns_nothing_for_a_log_with_no_startup() -> None:
    assert parse_runs(RUNTIME_NOISE) == []
