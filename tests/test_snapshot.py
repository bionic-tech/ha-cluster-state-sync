"""Snapshot write-path tests.

The headline case is `test_ar_0001_snapshot_contains_every_tracked_entity`: the
v1 adversarial review's finding that the integration mirrors only the last
interval's changes rather than full state. Eight of fifteen personas found it
independently and nothing in the repository could have caught it, which is the
entire reason this file exists.
"""

from __future__ import annotations

from datetime import timedelta
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
    CONF_NODE_ID,
    CONF_REDIS_HOST,
    CONF_REDIS_PORT,
    CONF_SNAPSHOT_INTERVAL,
    DOMAIN,
)

from .fakes import FakeBackend

INTERVAL = 5
NODE_ID = "node-a"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


async def setup_integration(
    hass: HomeAssistant, backend: FakeBackend, **overrides: object
) -> MockConfigEntry:
    """Set up the integration with the Redis backend replaced by a fake."""
    data = {
        CONF_REDIS_HOST: "valkey.invalid",
        CONF_REDIS_PORT: 6379,
        CONF_CLUSTER_NAMESPACE: "testns",
        CONF_NODE_ID: NODE_ID,
        CONF_SNAPSHOT_INTERVAL: INTERVAL,
        **overrides,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def advance_one_flush(hass: HomeAssistant) -> None:
    """Advance HA's clock past one snapshot interval and let the flush run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=INTERVAL + 1))
    await hass.async_block_till_done()


async def test_ar_0001_snapshot_contains_every_tracked_entity(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0001/AR-0002 — the snapshot is full state, not the last interval's delta.

    Production change that would make this fail: reverting the flush to write
    the drained change-buffer (`DEL` + `HSET(batch)`) instead of the full
    authoritative map.

    `input_boolean.quiet` is the crux. It is set once before setup and never
    touched again, so it never produces a state-changed event. Under the v0.1
    behaviour it is absent from every snapshot ever written, and a promoted
    standby would silently come up without it.
    """
    hass.states.async_set("input_boolean.quiet", "on")
    hass.states.async_set("input_boolean.chatty", "off")
    hass.states.async_set("counter.visits", "7")
    await hass.async_block_till_done()

    await setup_integration(hass, backend)

    # Only one entity changes during this interval.
    hass.states.async_set("input_boolean.chatty", "on")
    await hass.async_block_till_done()

    await advance_one_flush(hass)

    written = backend.last_write
    assert set(written) == {
        "input_boolean.quiet",
        "input_boolean.chatty",
        "counter.visits",
    }, "snapshot must carry full tracked state, not just what changed"
    assert written["input_boolean.chatty"].state == "on"
    assert written["input_boolean.quiet"].state == "on"


async def test_untracked_domains_are_not_mirrored(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The include filter still applies to the full-state map.

    Production change that would make this fail: seeding the authoritative map
    from `hass.states.async_all()` without filtering through `_should_track`.
    """
    hass.states.async_set("input_boolean.tracked", "on")
    hass.states.async_set("sensor.untracked_temperature", "21.5")
    await hass.async_block_till_done()

    await setup_integration(hass, backend)
    await advance_one_flush(hass)

    assert "input_boolean.tracked" in backend.last_write
    assert "sensor.untracked_temperature" not in backend.last_write


async def test_flush_is_skipped_when_nothing_changed(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """An idle cluster must not rewrite an identical snapshot every interval.

    Production change that would make this fail: dropping the dirty-flag guard
    and writing the full map unconditionally.
    """
    hass.states.async_set("input_boolean.quiet", "on")
    await hass.async_block_till_done()

    await setup_integration(hass, backend)
    await advance_one_flush(hass)
    writes_after_first = len(backend.writes)

    await advance_one_flush(hass)
    await advance_one_flush(hass)

    assert len(backend.writes) == writes_after_first, (
        "idle intervals must not produce redundant writes"
    )


async def test_failed_flush_is_retried_on_the_next_interval(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0011 — a failed flush must not silently drop the pending changes.

    Production change that would make this fail: clearing the dirty flag before
    confirming the backend write succeeded.
    """
    hass.states.async_set("input_boolean.quiet", "on")
    await hass.async_block_till_done()

    await setup_integration(hass, backend)

    backend.fail_writes = True
    await advance_one_flush(hass)
    assert not backend.writes, "write was supposed to fail"

    backend.fail_writes = False
    await advance_one_flush(hass)

    assert "input_boolean.quiet" in backend.last_write, (
        "state changed while the backend was down must survive to the next flush"
    )


async def test_snapshot_entries_carry_the_source_node(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """Restore's peer-trust check depends on every entry being attributed."""
    hass.states.async_set("input_boolean.quiet", "on")
    await hass.async_block_till_done()

    await setup_integration(hass, backend)
    await advance_one_flush(hass)

    assert all(e.source_node == NODE_ID for e in backend.last_write.values())


# --- the attribute budget on the WRITE path (2026-09-08) --------------------


async def test_an_oversized_entity_is_not_written_at_all(hass) -> None:
    """The budget used to be applied on READ only.

    That meant an oversized entity was written on every flush and refused on
    every restore -- paying full write cost, forever, for something guaranteed
    unusable. Measured on a real estate: one entity carried 100,911 bytes
    against a 16,384 byte cap.
    """
    from custom_components.cluster_state_sync import StateMirror
    from custom_components.cluster_state_sync.coordinator import SyncStats

    class _Backend:
        def __init__(self):
            self.written = None

        async def write_snapshot(self, payload, node_id):
            self.written = payload
            return True

    backend = _Backend()
    stats = SyncStats()
    cfg = {"include_domains": ["input_boolean"]}
    mirror = StateMirror(hass, backend, cfg, "node-a", stats=stats)

    hass.states.async_set("input_boolean.small", "on", {"note": "fine"})
    hass.states.async_set(
        "input_boolean.huge",
        "on",
        {"blob": "x" * 40000},  # well past 16 KB
    )
    await hass.async_block_till_done()
    mirror.seed_from_current_states()
    await mirror.async_flush()

    assert backend.written is not None, "nothing was written at all"
    assert "input_boolean.small" in backend.written
    assert "input_boolean.huge" not in backend.written, (
        "an oversized entity reached the backend — write cost for an entry the "
        "restore will always refuse"
    )
    assert "input_boolean.huge" in stats.oversized, (
        "the skip was silent; an entity that never replicates must say so"
    )


async def test_an_all_oversized_flush_writes_nothing_rather_than_an_empty_map(
    hass,
) -> None:
    """Publishing an empty map claims 'this node tracks nothing'.

    That is a different and much worse statement than 'some entries did not
    fit', and a peer restoring from it would come up blank.
    """
    from custom_components.cluster_state_sync import StateMirror
    from custom_components.cluster_state_sync.coordinator import SyncStats

    class _Backend:
        def __init__(self):
            self.calls = 0

        async def write_snapshot(self, payload, node_id):
            self.calls += 1
            return True

    backend = _Backend()
    mirror = StateMirror(
        hass,
        backend,
        {"include_domains": ["input_boolean"]},
        "node-a",
        stats=SyncStats(),
    )
    hass.states.async_set("input_boolean.huge", "on", {"blob": "x" * 40000})
    await hass.async_block_till_done()
    mirror.seed_from_current_states()

    assert await mirror.async_flush() is False
    assert backend.calls == 0, "an empty snapshot was published"
