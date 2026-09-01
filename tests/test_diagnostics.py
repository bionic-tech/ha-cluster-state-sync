"""Diagnostic entity tests (AR-0019, AR-0032).

The review's third theme was that the integration is "built to fail safe, but
blind while doing it": AR-0001 and a dead backend both looked like a healthy
green failover, because nothing was measurable. `entities_tracked` is the
specific number that would have made AR-0001 obvious in week one -- a snapshot
of 3 entities on a system tracking 200 is not subtle once it is on screen.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import logging
import pathlib
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cluster_state_sync.const import (
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_FILESET_ENABLED,
    CONF_NODE_ID,
    CONF_REDIS_HOST,
    CONF_SNAPSHOT_INTERVAL,
    DATA_FILESET,
    DEGRADED_MARKER_NAME,
    DOMAIN,
)

from .fakes import FakeBackend


@pytest.fixture(autouse=True)
def _no_stray_degraded_marker(hass: HomeAssistant) -> Generator[None]:
    """Guard against exactly the kind of invisible cross-test failure this
    task's alarm exists to catch in production -- turned inward.

    `pytest_homeassistant_custom_component`'s `hass` fixture points every test
    in the whole suite at the same fixed `testing_config/` directory rather
    than a fresh tmp dir per test. A marker left behind by one test would
    silently raise `fileset_degraded` for every *other* test anywhere in the
    suite that sets up this integration afterwards -- not just here.
    """
    marker = pathlib.Path(hass.config.path(DEGRADED_MARKER_NAME))
    marker.unlink(missing_ok=True)
    try:
        yield
    finally:
        marker.unlink(missing_ok=True)


INTERVAL = 5
SECRET = "cluster-secret-under-test"

# Resolved from the entity registry by unique_id rather than hardcoded.
# Entity IDs are derived from the device name and the translated entity name,
# so asserting on a literal would be testing Home Assistant's slug algorithm
# rather than this integration.
TRACKED_KEY = "entities_tracked"
AGE_KEY = "last_snapshot_age"
RESTORED_KEY = "entities_restored"
BACKEND_KEY = "backend"
FILESET_AGE_KEY = "fileset_age"
FILESET_DEGRADED_KEY = "fileset_degraded"


def entity_id_for(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """Look up an entity_id by the unique_id suffix the integration assigns."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_{key}"
    for reg_entry in registry.entities.values():
        if reg_entry.unique_id == unique_id:
            return reg_entry.entity_id
    raise AssertionError(f"no entity registered with unique_id {unique_id!r}")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


async def setup_integration(
    hass: HomeAssistant, backend: FakeBackend, *, fileset_enabled: bool = False
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: "node-a",
            CONF_CLUSTER_SECRET: SECRET,
            CONF_SNAPSHOT_INTERVAL: INTERVAL,
            CONF_FILESET_ENABLED: fileset_enabled,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_entry_with_fileset(
    hass: HomeAssistant, backend: FakeBackend | None = None
) -> MockConfigEntry:
    """`setup_integration`, with fileset replication turned on.

    The degraded-marker tests don't need a `FilesetPublisher` -- the marker is
    a host-level artifact, read regardless of this entry's own configuration
    -- but the age sensor's publisher-backed fallback does, so every fileset
    diagnostics test sets up through this one helper rather than half of them
    reaching for `setup_integration` directly and getting a `None` publisher
    by surprise.
    """
    return await setup_integration(hass, backend or FakeBackend(), fileset_enabled=True)


async def advance(hass: HomeAssistant, seconds: int = INTERVAL + 1) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_ar_0032_entities_tracked_is_exposed(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0032 — the number that would have caught AR-0001 is on screen.

    Production change that would make this fail: removing the sensor, or
    sourcing it from anything other than the authoritative mirror.
    """
    hass.states.async_set("input_boolean.one", "on")
    hass.states.async_set("input_boolean.two", "off")
    hass.states.async_set("counter.three", "5")
    hass.states.async_set("sensor.not_tracked", "21.5")
    await hass.async_block_till_done()

    entry = await setup_integration(hass, backend)

    tracked = entity_id_for(hass, entry, TRACKED_KEY)
    assert hass.states.get(tracked).state == "3"


async def test_entities_tracked_follows_new_entities(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The count is live, not a boot-time snapshot of itself."""
    hass.states.async_set("input_boolean.one", "on")
    await hass.async_block_till_done()
    entry = await setup_integration(hass, backend)
    tracked = entity_id_for(hass, entry, TRACKED_KEY)
    assert hass.states.get(tracked).state == "1"

    hass.states.async_set("input_boolean.two", "on")
    await hass.async_block_till_done()
    await advance(hass)

    assert hass.states.get(tracked).state == "2"


async def test_ar_0019_backend_connectivity_is_exposed(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0019 — `health()` was implemented and never called.

    Production change that would make this fail: leaving `health()` unwired.
    A dead backend is exactly the condition that must not look like a healthy
    green failover.
    """
    entry = await setup_integration(hass, backend)
    backend_entity = entity_id_for(hass, entry, BACKEND_KEY)
    assert hass.states.get(backend_entity).state == "on"

    backend.connected = False
    await advance(hass, seconds=61)

    assert hass.states.get(backend_entity).state == "off"


async def test_last_snapshot_age_grows_between_flushes(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """A stalled flush loop must be visible as a rising age."""
    hass.states.async_set("input_boolean.one", "on")
    await hass.async_block_till_done()
    entry = await setup_integration(hass, backend)
    age = entity_id_for(hass, entry, AGE_KEY)

    await advance(hass)
    assert hass.states.get(age).state not in ("unknown", "unavailable")
    first = float(hass.states.get(age).state)

    # No state changes, so no flush happens and the age must climb.
    await advance(hass, seconds=120)
    assert float(hass.states.get(age).state) > first


async def test_last_snapshot_age_is_unknown_before_any_flush(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """Never-flushed must read as unknown, not as zero.

    Zero would say "the snapshot is perfectly fresh", which is the exact
    opposite of the truth and precisely the kind of reassuring-but-wrong signal
    the review objected to.
    """
    entry = await setup_integration(hass, backend)
    assert hass.states.get(entity_id_for(hass, entry, AGE_KEY)).state == "unknown"


async def test_restored_count_is_exposed(hass: HomeAssistant, backend: FakeBackend) -> None:
    """A failover that restored nothing must be distinguishable from one that worked."""
    entry = await setup_integration(hass, backend)
    assert hass.states.get(entity_id_for(hass, entry, RESTORED_KEY)).state == "0"


async def test_diagnostic_entities_are_categorised_as_diagnostic(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """These belong in the diagnostics section, not on the user's dashboard."""
    from homeassistant.helpers import entity_registry as er

    config_entry = await setup_integration(hass, backend)
    registry = er.async_get(hass)

    for key in (
        TRACKED_KEY,
        AGE_KEY,
        RESTORED_KEY,
        BACKEND_KEY,
        FILESET_AGE_KEY,
        FILESET_DEGRADED_KEY,
    ):
        entity_id = entity_id_for(hass, config_entry, key)
        reg_entry = registry.async_get(entity_id)
        assert reg_entry is not None, f"{entity_id} was never registered"
        assert reg_entry.entity_category == er.EntityCategory.DIAGNOSTIC


# ---------------------------------------------------------------------------
# The degraded-fileset alarm (Task 10) -- AR-0040's shape, one layer up.
#
# Decision D4 has the standby promote on a stale or missing go-bag rather than
# refuse to start. That makes what follows the *only* safety net: every HTTP
# health check on a node in this state is green. AR-0040 already proved what
# happens when a defect of exactly this shape is left to an INFO log line --
# it was live for the whole project and 305 passing tests never caught it.
# ---------------------------------------------------------------------------


async def test_a_degraded_marker_raises_a_repair_issue(
    hass: HomeAssistant, tmp_path: pathlib.Path
) -> None:
    """Production change that would make this fail: logging the marker instead
    of registering an issue. AR-0040's only symptom was an INFO line, for the
    entire life of the project."""
    from homeassistant.helpers import issue_registry as ir

    marker = pathlib.Path(hass.config.path(".cluster_sync_degraded.json"))
    marker.write_text('{"reason": "no_staged_fileset", "age_s": null}')
    await _setup_entry_with_fileset(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "fileset_degraded") is not None


async def test_an_unreadable_marker_says_so_rather_than_vanishing(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt marker was indistinguishable from no marker, and logged
    nothing.

    This is the only safety net on decision D4's "promote anyway": the swap
    wrote the file *because* something went wrong. A truncated write or a
    permission problem turned the alarm off and left the node looking clean --
    which is AR-0040's exact shape, one layer further out. It still fails open
    (a node that cannot read a marker must still boot), but not in silence.
    """
    marker = pathlib.Path(hass.config.path(".cluster_sync_degraded.json"))
    marker.write_text('{"reason": "stale", "age_s": tr')  # truncated mid-write

    with caplog.at_level(logging.WARNING, logger="custom_components.cluster_state_sync"):
        await _setup_entry_with_fileset(hass)

    assert "degraded-fileset marker" in caplog.text
    assert ".cluster_sync_degraded.json" in caplog.text


async def test_no_marker_means_no_issue(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir

    await _setup_entry_with_fileset(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "fileset_degraded") is None


async def test_a_cleared_marker_removes_the_repair_issue(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """Recovery, not just detection.

    The issue registry is storage-backed and survives restarts, so
    `ir.async_create_issue` alone leaves the issue standing forever -- even
    after a clean swap and full recovery. Worse, `strings.json` tells the
    operator that reloading the integration once a fresh fileset is staged
    clears it; without a matching `async_delete_issue` that promise is false.

    Production change that would make this fail: never calling
    `ir.async_delete_issue` when no marker is present, or calling it only on
    first setup rather than every setup/reload.
    """
    from homeassistant.helpers import issue_registry as ir

    marker = pathlib.Path(hass.config.path(".cluster_sync_degraded.json"))
    marker.write_text('{"reason": "stale", "age_s": 10, "at": "2020-01-01T00:00:00+00:00"}')
    entry = await _setup_entry_with_fileset(hass, backend)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "fileset_degraded") is not None

    marker.unlink()
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, "fileset_degraded") is None


async def test_fileset_degraded_binary_sensor_off_with_no_marker(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The common case: nothing wrong, nothing to report."""
    entry = await setup_integration(hass, backend)
    sensor_id = entity_id_for(hass, entry, FILESET_DEGRADED_KEY)
    assert hass.states.get(sensor_id).state == "off"


async def test_fileset_degraded_binary_sensor_on_with_marker(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The alarm itself: a marker on disk must flip this on.

    Production change that would make this fail: raising the repair issue but
    never wiring the marker through to an entity. The repair issue can be
    dismissed from Settings; this binary sensor -- and the blueprint condition
    that watches it -- cannot.
    """
    marker = pathlib.Path(hass.config.path(".cluster_sync_degraded.json"))
    marker.write_text('{"reason": "stale", "age_s": 4000, "at": "2020-01-01T00:00:00+00:00"}')
    entry = await _setup_entry_with_fileset(hass, backend)
    sensor_id = entity_id_for(hass, entry, FILESET_DEGRADED_KEY)
    assert hass.states.get(sensor_id).state == "on"


async def test_fileset_age_climbs_past_the_markers_captured_age(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """A degraded marker's `age_s` is a snapshot taken at swap time, before the
    container even started -- it is not re-measured afterwards. Reporting it
    verbatim forever would freeze the sensor at boot, repeating for the
    fileset exactly the "reassuring but wrong" number that
    `LastSnapshotAgeSensor` exists to avoid for the state hash: a value that
    looks current long after it has stopped being true.
    """
    written_at = dt_util.utcnow() - timedelta(seconds=100)
    marker = pathlib.Path(hass.config.path(".cluster_sync_degraded.json"))
    marker.write_text(f'{{"reason": "stale", "age_s": 4000, "at": "{written_at.isoformat()}"}}')
    entry = await _setup_entry_with_fileset(hass, backend)
    age_id = entity_id_for(hass, entry, FILESET_AGE_KEY)
    assert float(hass.states.get(age_id).state) >= 4100


async def test_fileset_age_is_unknown_when_the_marker_has_no_age(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """`no_staged_fileset` carries a null `age_s` -- there was no fileset to
    measure the age of. That must read as unknown, not as a misleading zero,
    for the same reason `last_snapshot_age` does before the first flush.
    """
    marker = pathlib.Path(hass.config.path(".cluster_sync_degraded.json"))
    marker.write_text('{"reason": "no_staged_fileset", "age_s": null}')
    entry = await _setup_entry_with_fileset(hass, backend)
    age_id = entity_id_for(hass, entry, FILESET_AGE_KEY)
    assert hass.states.get(age_id).state == "unknown"


async def test_fileset_age_climbs_after_a_publish_with_no_marker(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """No marker and a completed publish: fresh right afterwards, but the
    reading must keep climbing if nothing publishes again.

    A publish loop that silently stops after one success is this project's
    own history (AR-0011/AR-0019's shape, one layer up) -- a value hardcoded
    to zero forever would be indistinguishable from a healthy, continuously
    refreshing fileset. `LastSnapshotAgeSensor` tracks a real timestamp and
    climbs when its flush stalls; this must too, or it is not a gauge.

    Production change that would make this fail: `native_value` returning a
    literal `0.0` on success rather than elapsed time since a recorded
    timestamp -- the one-shot check this replaced could not tell the two
    apart.
    """
    entry = await _setup_entry_with_fileset(hass, backend)
    publisher = hass.data[DOMAIN][entry.entry_id][DATA_FILESET]
    assert publisher is not None
    await publisher.async_publish()
    # FilesetAgeSensor is coordinator-backed, like the other diagnostics here;
    # it re-renders on the health coordinator's own poll rather than on a
    # per-publish push, so a publish alone does not yet reach the entity.
    await advance(hass, seconds=61)

    age_id = entity_id_for(hass, entry, FILESET_AGE_KEY)
    assert hass.states.get(age_id).state not in ("unknown", "unavailable")
    first = float(hass.states.get(age_id).state)

    # Nothing publishes again. `native_value` uses real wall-clock time (like
    # `LastSnapshotAgeSensor`/`StateMirror` already do), and this harness only
    # simulates HA's scheduler clock, not the system clock -- so, matching
    # `test_last_snapshot_age_grows_between_flushes`'s own idiom, this checks
    # for growth rather than a specific magnitude. The old hardcoded `0.0`
    # fails this exactly as it should: 0.0 is never greater than 0.0.
    await advance(hass, seconds=120)
    second = float(hass.states.get(age_id).state)
    assert second > first, (first, second)


async def test_fileset_age_is_unknown_with_no_marker_and_no_publish(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """Fileset replication is on but nothing has published yet, and nothing is
    degraded either. There is genuinely nothing to report."""
    entry = await _setup_entry_with_fileset(hass, backend)
    age_id = entity_id_for(hass, entry, FILESET_AGE_KEY)
    assert hass.states.get(age_id).state == "unknown"


async def test_fileset_degraded_survives_a_backend_health_coordinator_failure(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The alarm's visibility must not ride on an unrelated fact.

    `FilesetDegradedBinarySensor` is `CoordinatorEntity`-backed like every
    other diagnostic here, but its data -- the marker read once at setup --
    has nothing to do with whether `BackendHealthCoordinator`'s *own* poll
    happened to succeed. Inheriting `available` from the coordinator anyway
    would mean a coordinator update failure -- for any reason, including one
    unrelated to the fileset -- hides the one entity whose entire job is to
    stay visible when something has already gone wrong. `BackendConnectivitySensor`
    in this same module documents exactly this trade for the same reason.
    """
    from custom_components.cluster_state_sync.const import DATA_COORDINATOR

    marker = pathlib.Path(hass.config.path(".cluster_sync_degraded.json"))
    marker.write_text('{"reason": "stale", "age_s": 10, "at": "2020-01-01T00:00:00+00:00"}')
    entry = await _setup_entry_with_fileset(hass, backend)
    sensor_id = entity_id_for(hass, entry, FILESET_DEGRADED_KEY)

    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(sensor_id).state == "on"
