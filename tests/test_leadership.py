"""Leadership tests (AR-0017).

ADR-001 makes leadership the single source of truth for every gating layer.
This covers the layer the integration can enforce from inside an unprivileged
container: whether *this* node writes to the shared snapshot at all.

The interim guard from Phase 1 (no `DEL`, so concurrent writers merge instead
of erasing each other) stays in place underneath. These tests are about not
having two writers in the first place.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cluster_state_sync.const import (
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_LEADERSHIP_ENTITY,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_REDIS_HOST,
    CONF_SNAPSHOT_INTERVAL,
    DOMAIN,
    LEADERSHIP_ALWAYS,
    LEADERSHIP_ENTITY,
    LEADERSHIP_LEASE,
)

from .fakes import FakeBackend

INTERVAL = 5
SECRET = "cluster-secret-under-test"
NODE_ID = "node-a"
LEADER_FLAG = "input_boolean.cluster_leader"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


async def setup_integration(
    hass: HomeAssistant, backend: FakeBackend, **overrides: object
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: NODE_ID,
            CONF_CLUSTER_SECRET: SECRET,
            CONF_SNAPSHOT_INTERVAL: INTERVAL,
            **overrides,
        },
    )
    entry.add_to_hass(hass)
    # 🚨 Pin the core state, or the AR-0065 restore gate makes these tests
    # order-dependent.
    #
    # The gate holds the flush until the boot restore has run. Setting up while
    # `hass` is RUNNING is the "added to a live instance" path, where no restore
    # is coming and the gate opens at once; setting up while it is starting
    # leaves the flush held until EVENT_HOMEASSISTANT_START. These tests are
    # about leadership, not about boot, so they want the former — and left to
    # whatever core state the previous test happened to leave behind, they got
    # whichever. Observed as `assert writes_while_leader` failing on one
    # ordering and passing on every other.
    hass.set_state(CoreState.running)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def advance(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=INTERVAL + 1))
    await hass.async_block_till_done()


async def test_default_is_always_leader_for_backwards_compatibility(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """An entry with no leadership config keeps flushing, as it always did.

    Silently making existing single-node installs stop writing would be a far
    worse regression than the dual-writer race this guards against.
    """
    hass.states.async_set("input_boolean.one", "on")
    await hass.async_block_till_done()

    await setup_integration(hass, backend, **{CONF_LEADERSHIP_SOURCE: LEADERSHIP_ALWAYS})
    await advance(hass)

    assert backend.writes


async def test_ar_0017_follower_does_not_write(hass: HomeAssistant, backend: FakeBackend) -> None:
    """AR-0017 — a node that is not leader must not touch the shared snapshot.

    Production change that would make this fail: flushing without consulting
    leadership.

    Both nodes running the flush loop is the dual-writer condition. Phase 1
    removed the `DEL` so concurrent writes merge rather than erase, but merging
    two nodes' views of the world is damage control, not correctness -- the
    follower's state is by definition the stale one.
    """
    hass.states.async_set("input_boolean.one", "on")
    hass.states.async_set(LEADER_FLAG, "off")
    await hass.async_block_till_done()

    await setup_integration(
        hass,
        backend,
        **{
            CONF_LEADERSHIP_SOURCE: LEADERSHIP_ENTITY,
            CONF_LEADERSHIP_ENTITY: LEADER_FLAG,
        },
    )
    await advance(hass)

    assert not backend.writes, "a follower must not write to the shared snapshot"


async def test_promotion_starts_writing(hass: HomeAssistant, backend: FakeBackend) -> None:
    """Flipping the leadership signal promotes without a restart.

    This is the failover path: Keepalived's notify_master flips the flag and
    the newly-promoted node must start publishing.
    """
    hass.states.async_set("input_boolean.one", "on")
    hass.states.async_set(LEADER_FLAG, "off")
    await hass.async_block_till_done()

    await setup_integration(
        hass,
        backend,
        **{
            CONF_LEADERSHIP_SOURCE: LEADERSHIP_ENTITY,
            CONF_LEADERSHIP_ENTITY: LEADER_FLAG,
        },
    )
    await advance(hass)
    assert not backend.writes

    hass.states.async_set(LEADER_FLAG, "on")
    await hass.async_block_till_done()
    await advance(hass)

    assert backend.writes, "a promoted node must start publishing"


async def test_demotion_stops_writing(hass: HomeAssistant, backend: FakeBackend) -> None:
    """A demoted node must go quiet immediately, not at the next restart."""
    hass.states.async_set("input_boolean.one", "on")
    hass.states.async_set(LEADER_FLAG, "on")
    await hass.async_block_till_done()

    await setup_integration(
        hass,
        backend,
        **{
            CONF_LEADERSHIP_SOURCE: LEADERSHIP_ENTITY,
            CONF_LEADERSHIP_ENTITY: LEADER_FLAG,
        },
    )
    await advance(hass)
    writes_while_leader = len(backend.writes)
    assert writes_while_leader

    hass.states.async_set(LEADER_FLAG, "off")
    hass.states.async_set("input_boolean.one", "off")
    await hass.async_block_till_done()
    await advance(hass)

    assert len(backend.writes) == writes_while_leader


async def test_missing_leadership_entity_is_treated_as_follower(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """An absent or unknown signal must fail closed.

    Production change that would make this fail: defaulting to leader when the
    entity is missing. If the flag has not been created yet, or a typo points
    at nothing, the safe reading is "I am not the leader" -- assuming
    leadership is how you get two of them.
    """
    hass.states.async_set("input_boolean.one", "on")
    await hass.async_block_till_done()

    await setup_integration(
        hass,
        backend,
        **{
            CONF_LEADERSHIP_SOURCE: LEADERSHIP_ENTITY,
            CONF_LEADERSHIP_ENTITY: "input_boolean.does_not_exist",
        },
    )
    await advance(hass)

    assert not backend.writes


async def flush_until_written(hass: HomeAssistant, backend, limit: int = 10) -> int:
    """Advance the clock until the leader has written, or give up loudly.

    🚨 Third attempt, and the first two are worth recording because both were
    wrong in instructive ways.

    The first added a post-setup state change and a second `advance`, getting
    the failure rate from roughly one in twelve to one in twenty. The second —
    mine — waited for leadership to resolve, which is not what gates the flush
    at all (`_scheduled_flush` returns early on `DATA_RESTORE_DONE`, AR-0065's
    guard), and whose repeated `async_block_till_done()` could let a flush
    consume the pending revision before the test made its change. It took the
    rate to about one in three: a fix that made it worse while looking like
    diligence.

    Both were attempts to make a race unlikely. This waits for the outcome
    instead, with a bound, so it is either deterministic or it fails saying so.
    """
    for _ in range(limit):
        if backend.writes:
            return len(backend.writes)
        await advance(hass)
    return len(backend.writes)


async def test_lease_holder_writes(hass: HomeAssistant, backend: FakeBackend) -> None:
    """With the Valkey lease, the node holding it writes."""
    hass.states.async_set("input_boolean.one", "on")
    await hass.async_block_till_done()
    backend.lease_holder = NODE_ID

    await setup_integration(hass, backend, **{CONF_LEADERSHIP_SOURCE: LEADERSHIP_LEASE})

    # Change something AFTER setup, so there is unambiguously an unflushed
    # revision when the interval fires. `async_flush` skips when nothing has
    # changed, and whether setup's own capture lands before or after the first
    # flush is a race -- measured at roughly one failure in twelve runs before
    # this line existed.
    hass.states.async_set("input_boolean.one", "off")
    await hass.async_block_till_done()
    # Two intervals, deliberately. The claim under test is "the lease holder
    # writes", not "writes within exactly one interval": if the first flush
    # fires before the lease check has resolved leadership, writing nothing is
    # correct behaviour, not a regression. Measured at ~1 failure in 20 runs
    # with a single advance, including run in isolation.
    await flush_until_written(hass, backend)

    assert backend.writes


async def test_lease_lost_to_peer_stops_writes(hass: HomeAssistant, backend: FakeBackend) -> None:
    """Losing the lease to the peer must stop this node writing.

    This is the split-brain guard ADR-001 leans on: VRRP alone can dual-master
    under a partition, so the lease is the tiebreak that keeps a second writer
    off the shared hash.
    """
    hass.states.async_set("input_boolean.one", "on")
    await hass.async_block_till_done()
    backend.lease_holder = NODE_ID

    await setup_integration(hass, backend, **{CONF_LEADERSHIP_SOURCE: LEADERSHIP_LEASE})

    # As above: guarantee an unflushed change rather than racing setup.
    hass.states.async_set("input_boolean.one", "off")
    await hass.async_block_till_done()
    writes_while_leader = await flush_until_written(hass, backend)
    assert writes_while_leader, "the leader never wrote, even given ten intervals"

    backend.lease_holder = "node-b"
    hass.states.async_set("input_boolean.one", "off")
    await hass.async_block_till_done()
    await advance(hass)

    assert len(backend.writes) == writes_while_leader


async def test_lease_failure_is_treated_as_not_leader(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """A backend that cannot answer must not be read as a yes."""
    hass.states.async_set("input_boolean.one", "on")
    await hass.async_block_till_done()
    backend.lease_raises = True

    await setup_integration(hass, backend, **{CONF_LEADERSHIP_SOURCE: LEADERSHIP_LEASE})
    await advance(hass)

    assert not backend.writes
