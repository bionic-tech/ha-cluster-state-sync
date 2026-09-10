"""Setting up with the optional features actually switched ON.

Both default to off, and every existing setup test therefore takes the branch
where they do nothing. So the bodies of `_setup_recorder_snapshots` and
`_setup_statistics_replication` -- roughly a hundred statements between them --
had never been executed by the suite at all.

🚨 That is not a coverage statistic. **Statistics replication is enabled in
production**, on the reference pair, holding 6.4 million rows. The code that
starts it was reached by no test, so a mistake there would have been found by
the house rather than by the suite. This is the AR-0040 shape once more: the
tested path and the running path are not the same path.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync.const import (
    DATA_STATISTICS,
    DOMAIN,
)
from tests.fakes import FakeBackend

SECRET = "s" * 44


def _entry(**extra: object) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": SECRET,
            "snapshot_interval": 30,
            "fileset_enabled": False,
            **extra,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return ok


# -- statistics replication (ADR-010) --------------------------------------


async def test_setup_with_statistics_enabled_starts_the_publisher(
    hass: HomeAssistant,
) -> None:
    """The production configuration, which no test had ever set up."""
    entry = _entry(statistics_enabled=True)
    assert await _setup(hass, entry)
    assert entry.runtime_data[DATA_STATISTICS] is not None, (
        "statistics replication was switched on and no publisher was created"
    )


async def test_statistics_without_a_secret_refuses_rather_than_publishing_plaintext(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """🚨 The blob is sealed with a key derived from the cluster secret.

    Without one there is no key, and the only two options are to refuse or to
    put the house's history into Valkey unsealed. It refuses, loudly, and
    leaves the slot empty rather than half-configured.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "snapshot_interval": 30,
            "fileset_enabled": False,
            "statistics_enabled": True,
        },
    )
    assert await _setup(hass, entry)
    assert entry.runtime_data[DATA_STATISTICS] is None
    assert "no cluster secret" in caplog.text


async def test_statistics_off_leaves_the_slot_explicitly_empty(hass: HomeAssistant) -> None:
    """The slot is set to None, not left absent.

    A missing key and a key holding None read the same to `.get()` and very
    differently to `[...]` -- and the diagnostics use the latter.
    """
    entry = _entry(statistics_enabled=False)
    assert await _setup(hass, entry)
    assert DATA_STATISTICS in entry.runtime_data
    assert entry.runtime_data[DATA_STATISTICS] is None


# -- recorder snapshots (ADR-010) ------------------------------------------


async def test_setup_with_recorder_snapshots_enabled(hass: HomeAssistant) -> None:
    """The other opt-in branch nothing had entered."""
    entry = _entry(recorder_snapshot_enabled=True)
    assert await _setup(hass, entry)


@pytest.mark.parametrize(
    ("requested", "reason"),
    [
        (1, "below the minimum"),
        (100000, "above the maximum"),
    ],
)
async def test_a_snapshot_interval_out_of_range_is_clamped_not_refused(
    hass: HomeAssistant, requested: int, reason: str
) -> None:
    """An absurd interval must not stop the integration starting.

    Refusing setup over a tunable would take the whole cluster down to protect
    a copy of the recorder, which is the wrong trade in both directions.
    """
    entry = _entry(recorder_snapshot_enabled=True, recorder_snapshot_minutes=requested)
    assert await _setup(hass, entry), f"setup refused an interval {reason}"


async def test_both_optional_features_on_together(hass: HomeAssistant) -> None:
    """They share the fileset's key and secret, and run in an agreed order."""
    entry = _entry(statistics_enabled=True, recorder_snapshot_enabled=True)
    assert await _setup(hass, entry)
    assert entry.runtime_data[DATA_STATISTICS] is not None


# -- the timers themselves, which are where the work actually happens ------


async def _fire(hass: HomeAssistant, minutes: int) -> None:
    """Advance past the interval so the scheduled job runs."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


async def test_the_statistics_timer_publishes_when_this_node_leads(
    hass: HomeAssistant,
) -> None:
    """Scheduling a job is not evidence the job works.

    The setup path was covered above; this is the callback it schedules -- the
    code that actually moves 6.4 million rows in production, and which nothing
    had ever executed.
    """
    entry = _entry(statistics_enabled=True, statistics_interval_minutes=5)
    assert await _setup(hass, entry)
    publisher = entry.runtime_data[DATA_STATISTICS]

    with (
        patch.object(publisher, "async_publish") as publish,
        patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=True),
    ):
        await _fire(hass, 6)

    assert publish.called, "the statistics timer fired and published nothing"


async def test_a_follower_publishes_no_statistics(hass: HomeAssistant) -> None:
    """Leader-only, for the reason the flush is: a follower's history is noise."""
    entry = _entry(statistics_enabled=True, statistics_interval_minutes=5)
    assert await _setup(hass, entry)
    publisher = entry.runtime_data[DATA_STATISTICS]

    with (
        patch.object(publisher, "async_publish") as publish,
        patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=False),
    ):
        await _fire(hass, 6)

    assert not publish.called, "a follower published its own history over the leader's"


async def test_a_failed_publish_is_named_in_the_log(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """🚨 Rewritten 2026-09-10, because the original asserted something false.

    It was called `test_a_failed_publish_does_not_kill_the_timer` and claimed
    the guard stops one transient failure ending replication for the life of
    the process. It does not, and the test passed with the guard removed --
    exactly the "passes either way" test this suite exists to avoid. Found by a
    subagent reading `_TrackTimeInterval`, which calls `_schedule_timer()`
    BEFORE running the job and runs it as a background task, so an escaping
    exception never reaches the timer at all.

    What the guard actually buys is a NAMED failure. Without it the error
    arrives as an anonymous unretrieved-task traceback whenever the garbage
    collector gets to it, detached from the thing that caused it. That is what
    is asserted now.
    """
    entry = _entry(statistics_enabled=True, statistics_interval_minutes=5)
    assert await _setup(hass, entry)
    publisher = entry.runtime_data[DATA_STATISTICS]

    with (
        patch.object(publisher, "async_publish", side_effect=RuntimeError("valkey went away")),
        patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=True),
    ):
        await _fire(hass, 6)

    assert "Statistics publish failed" in caplog.text, (
        "the failure was swallowed anonymously — an unretrieved-task traceback "
        "at GC time is not a diagnosable fault"
    )
    assert "valkey went away" in caplog.text, "the original error must survive into the log"

    # And the timer does keep running -- not because of the guard, but because
    # `_TrackTimeInterval` re-arms before it runs the job.
    with (
        patch.object(publisher, "async_publish") as publish,
        patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=True),
    ):
        await _fire(hass, 12)
    assert publish.called


async def test_the_recorder_snapshot_timer_runs_on_the_leader(hass: HomeAssistant) -> None:
    """The `VACUUM INTO` job, which must go to an executor and never the loop."""
    entry = _entry(recorder_snapshot_enabled=True, recorder_snapshot_minutes=5)
    assert await _setup(hass, entry)

    with (
        patch("custom_components.cluster_state_sync.recorder_snapshot.take_snapshot") as take,
        patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=True),
    ):
        take.return_value = type("R", (), {"ok": True, "error": None})()
        await _fire(hass, 6)

    assert take.called, "the recorder-snapshot timer fired and took no snapshot"


async def test_a_shared_database_logs_debug_not_an_error_every_interval(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A Postgres install has no SQLite file. That is ordinary, not broken.

    Logging it as an error would have a perfectly valid configuration report a
    failure on every single interval, for ever.
    """
    import logging

    entry = _entry(recorder_snapshot_enabled=True, recorder_snapshot_minutes=5)
    assert await _setup(hass, entry)

    with (
        patch("custom_components.cluster_state_sync.recorder_snapshot.take_snapshot") as take,
        patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=True),
        caplog.at_level(logging.DEBUG, logger="custom_components.cluster_state_sync"),
    ):
        take.return_value = type("R", (), {"ok": False, "error": "no SQLite database"})()
        await _fire(hass, 6)

    assert "Recorder snapshot skipped" in caplog.text
    assert "ERROR" not in caplog.text.split("Recorder snapshot skipped")[0][-200:]


async def test_an_interval_below_the_minimum_is_clamped_to_five_minutes(
    hass: HomeAssistant,
) -> None:
    """Asked for one minute, you get five.

    Found by writing the timer tests: a request of 1 was silently clamped, so a
    test that fired at +2 minutes saw nothing and looked like a broken timer
    rather than a clamped interval. Worth pinning -- the clamp protects a
    `VACUUM INTO` that took ten seconds on a 2.2 GB database from being asked
    to run every sixty.
    """
    entry = _entry(statistics_enabled=True, statistics_interval_minutes=1)
    assert await _setup(hass, entry)
    publisher = entry.runtime_data[DATA_STATISTICS]

    with (
        patch.object(publisher, "async_publish") as publish,
        patch.object(entry.runtime_data["leadership"], "async_is_leader", return_value=True),
    ):
        await _fire(hass, 2)
        assert not publish.called, "a 1-minute interval was honoured rather than clamped"
        await _fire(hass, 6)
        assert publish.called
