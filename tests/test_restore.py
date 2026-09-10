"""Restore-path tests.

The restore path is where attacker-controllable data from the shared hash
reaches `hass.states.async_set()`. The v1 review rated the combination P0: a
poisoned entry is applied verbatim, and a future-dated timestamp defeats both
of the guards that exist (AR-0035).

Integrity checking itself (AR-0005, HMAC) is Phase 3. These tests cover the
guards that must hold regardless of whether an entry is authentic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from unittest.mock import patch

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync.backend import SnapshotEntry
from custom_components.cluster_state_sync.const import (
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_LEADERSHIP_ENTITY,
    CONF_NODE_ID,
    CONF_REDIS_HOST,
    CONF_RESTORE_MAX_AGE,
    DOMAIN,
    MAX_ATTRIBUTE_BYTES,
)

from .fakes import FakeBackend

NODE_ID = "node-a"
PEER_ID = "node-b"
SECRET = "cluster-secret-under-test"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


def peer_entry(
    entity_id: str = "input_boolean.quiet",
    state: str = "on",
    *,
    age_seconds: float = 10.0,
    source_node: str = PEER_ID,
    attributes: dict[str, Any] | None = None,
    last_updated: str | None = None,
) -> SnapshotEntry:
    """Build an entry as the peer node would have written it."""
    stamp = last_updated or (dt_util.utcnow() - timedelta(seconds=age_seconds)).isoformat()
    return SnapshotEntry(
        entity_id=entity_id,
        state=state,
        attributes=attributes if attributes is not None else {},
        last_changed=stamp,
        last_updated=stamp,
        source_node=source_node,
    )


async def boot_with_snapshot(
    hass: HomeAssistant,
    backend: FakeBackend,
    entries: dict[str, SnapshotEntry],
    *,
    snapshot_age_seconds: float = 5.0,
    **overrides: object,
) -> MockConfigEntry:
    """Set up the integration through the *boot* path and run the restore.

    Restore is wired to EVENT_HOMEASSISTANT_START, which only fires when HA is
    still starting -- the window before the automation engine runs. The test
    harness hands us a already-running `hass`, so we wind it back first.
    """
    backend.stored = dict(entries)
    # The meta the real backend would have written alongside those entries.
    # `snapshot_age_seconds` lets a test age the SNAPSHOT, which is what the
    # restore's age gate now reads -- as distinct from ageing an individual
    # entry, which it deliberately no longer cares about.
    backend.meta = {
        "schema_version": 1,
        "source_node": PEER_ID,
        "entry_count": len(entries),
        "last_snapshot_at": (
            datetime.now(tz=UTC) - timedelta(seconds=snapshot_age_seconds)
        ).isoformat(),
    }
    hass.set_state(CoreState.not_running)

    data = {
        CONF_REDIS_HOST: "valkey.invalid",
        CONF_CLUSTER_NAMESPACE: "testns",
        CONF_NODE_ID: NODE_ID,
        CONF_CLUSTER_SECRET: SECRET,
        **overrides,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.async_start()
        await hass.async_block_till_done()
    return entry


# -- the guards that already existed ---------------------------------------


async def test_peer_state_is_restored(hass: HomeAssistant, backend: FakeBackend) -> None:
    """The happy path: a fresh entry from the peer seeds local state."""
    await boot_with_snapshot(hass, backend, {"input_boolean.quiet": peer_entry(state="on")})
    assert hass.states.get("input_boolean.quiet").state == "on"


async def test_own_node_entries_are_not_restored(hass: HomeAssistant, backend: FakeBackend) -> None:
    """We only learn from the peer; our own writes are not news."""
    await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.quiet": peer_entry(state="on", source_node=NODE_ID)},
    )
    assert hass.states.get("input_boolean.quiet") is None


async def test_an_ancient_snapshot_is_not_restored(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """A node returning after a week must not resurrect ancient state.

    The gate is on the SNAPSHOT's age, which is what `restore_max_age` says it
    is. Nothing has written to the shared hash for a week, so none of it is
    trustworthy.
    """
    await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.quiet": peer_entry(age_seconds=7 * 24 * 3600)},
        snapshot_age_seconds=7 * 24 * 3600,
        **{CONF_RESTORE_MAX_AGE: 1800},
    )
    assert hass.states.get("input_boolean.quiet") is None


async def test_a_stable_entity_in_a_fresh_snapshot_is_restored(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """Production change that would make this fail: putting the age gate back
    on each entry.

    Observed on node-b, 2026-09-04: "Restored NOTHING from a snapshot that
    held 28 entries. Skipped: 28 too-old." A healthy cluster, idle half an
    hour, restoring nothing — because every entry's own `last_updated` had
    aged past the cutoff together.

    That inverted the intent. A setpoint that has held all day is exactly the
    state worth carrying across a failover; a sensor that flickered ten seconds
    ago is the one that matters least. Here the peer is still writing — the
    snapshot is seconds old — while the entity itself has not changed in a day.
    """
    await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.quiet": peer_entry(age_seconds=24 * 3600)},
        snapshot_age_seconds=5,
        **{CONF_RESTORE_MAX_AGE: 1800},
    )
    restored = hass.states.get("input_boolean.quiet")
    assert restored is not None, "a stable entity must survive a fresh snapshot"
    assert restored.state == "on"


# -- AR-0035: future-dated timestamps --------------------------------------


async def test_ar_0035_future_dated_entry_cannot_beat_fresher_local_state(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0035 — a future timestamp must not defeat the freshness guard.

    Production change that would make this fail: accepting entries stamped
    beyond `CLOCK_SKEW_TOLERANCE` instead of refusing them.

    Note this asserts the *security property*, not the mechanism the review
    prescribed. The review said to clamp future stamps to now — but a stamp
    clamped to now is still newer than every pre-existing local state, so the
    poisoned value goes on winning and this test still fails. Refusing
    implausible stamps outright is what actually holds the property; honest
    skew inside the tolerance is clamped instead (see the test below).
    """
    hass.states.async_set("alarm_control_panel.house", "armed_away")
    await hass.async_block_till_done()

    far_future = (dt_util.utcnow() + timedelta(days=365 * 1000)).isoformat()
    await boot_with_snapshot(
        hass,
        backend,
        {
            "alarm_control_panel.house": peer_entry(
                "alarm_control_panel.house", "disarmed", last_updated=far_future
            )
        },
    )

    assert hass.states.get("alarm_control_panel.house").state == "armed_away", (
        "a future-dated snapshot entry must not override fresher local state"
    )


async def test_small_clock_skew_is_tolerated_not_refused(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """A peer whose clock runs slightly fast is still a peer.

    Production change that would make this fail: refusing every future stamp
    rather than only implausible ones. Two NTP-synced nodes still drift by
    milliseconds, and a restore that rejected them would throw away good state
    on every failover.
    """
    slightly_ahead = (dt_util.utcnow() + timedelta(seconds=2)).isoformat()
    await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.quiet": peer_entry(last_updated=slightly_ahead)},
    )
    assert hass.states.get("input_boolean.quiet").state == "on"


async def test_ar_0014_naive_timestamp_does_not_raise(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0014 — a tz-naive timestamp must not blow up the comparison.

    Production change that would make this fail: comparing
    `datetime.fromisoformat(...)` directly against a tz-aware cutoff without
    normalising. That raises TypeError, and one entry would abort the restore.
    """
    naive = (dt_util.utcnow() - timedelta(seconds=10)).replace(tzinfo=None).isoformat()
    await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.quiet": peer_entry(last_updated=naive)},
    )
    assert hass.states.get("input_boolean.quiet").state == "on"


# -- AR-0009: bounds --------------------------------------------------------


async def test_ar_0009_restore_is_capped_at_max_entries(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0009 — an oversized snapshot must not be applied unbounded.

    Production change that would make this fail: dropping the entry-count cap.
    Applying an attacker-sized hash blocks the event loop during exactly the
    window a failover needs it.
    """
    cap = 10  # patched down from MAX_RESTORE_ENTRIES so the test stays fast
    entries = {f"input_boolean.e{i}": peer_entry(f"input_boolean.e{i}") for i in range(cap + 5)}
    with patch("custom_components.cluster_state_sync.MAX_RESTORE_ENTRIES", cap):
        await boot_with_snapshot(hass, backend, entries)

    restored = [s for s in hass.states.async_all() if s.entity_id.startswith("input_boolean.e")]
    assert len(restored) == cap


async def test_ar_0009_oversized_attributes_are_skipped(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0009 — a single huge attribute payload is dropped, not applied."""
    fat = {"blob": "x" * (MAX_ATTRIBUTE_BYTES + 1000)}
    await boot_with_snapshot(
        hass,
        backend,
        {
            "input_boolean.fat": peer_entry("input_boolean.fat", attributes=fat),
            "input_boolean.thin": peer_entry("input_boolean.thin"),
        },
    )

    assert hass.states.get("input_boolean.fat") is None
    assert hass.states.get("input_boolean.thin") is not None, (
        "an oversized entry must not take healthy entries down with it"
    )


# -- unreadable timestamps --------------------------------------------------


async def test_entry_with_an_unreadable_timestamp_is_skipped_alone(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """A bad timestamp costs that entry, not the restore.

    The backend-level half of this — one malformed *stored value* aborting the
    whole read — is AR-0013 and is tested in test_backend.py, since a fake
    backend hands back already-parsed entries and bypasses that path entirely.
    """
    good = peer_entry("input_boolean.good")
    bad = peer_entry("input_boolean.bad")
    bad.last_updated = "not-a-timestamp"

    await boot_with_snapshot(hass, backend, {"input_boolean.good": good, "input_boolean.bad": bad})

    assert hass.states.get("input_boolean.good") is not None
    assert hass.states.get("input_boolean.bad") is None


async def test_ar_0038_the_peers_leadership_flag_is_not_restored(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The split-brain path, end to end rather than as a filter unit.

    Production change that would make this fail: not passing the leadership
    entity through to `_should_track` on the restore path — the guard can be
    correct and still be wired only into the flush half.

    This node follows `input_boolean.ha_is_master`. The peer, being the leader,
    has `on` in the snapshot. Restore runs at `EVENT_HOMEASSISTANT_START`,
    before the first leadership evaluation, so if it lands then this node reads
    its own flag as `on` and promotes itself while the peer is still live.

    The assertion is about what leadership would *conclude*, not about a filter
    decision, because that is the thing that must never happen.
    """
    entries = {
        "input_boolean.ha_is_master": peer_entry("input_boolean.ha_is_master", "on"),
        "input_boolean.quiet": peer_entry("input_boolean.quiet", "on"),
    }

    await boot_with_snapshot(
        hass,
        backend,
        entries,
        **{CONF_LEADERSHIP_ENTITY: "input_boolean.ha_is_master"},
    )

    flag = hass.states.get("input_boolean.ha_is_master")
    assert flag is None or flag.state != "on", (
        "restoring the peer's leadership flag makes this node believe it is "
        "the leader while the peer still is"
    )
    # The rest of the snapshot is unaffected — this is a targeted refusal.
    assert hass.states.get("input_boolean.quiet").state == "on"


async def test_ar_0037_a_refused_entry_is_reported_not_silently_dropped(
    hass: HomeAssistant, backend: FakeBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """Isolating the failure must not also hide it.

    Production change that would make this fail: catching the exception and
    passing, without counting it or warning.

    "Swallow and continue" is the fix for the abort, and on its own it is also
    a way to lose state quietly during a promotion. The operator's belief is
    that the snapshot was restored; the entry that was not restored is exactly
    the one they need told about, so it is a warning and it carries a count.
    """
    entries = {
        "input_boolean.first": peer_entry("input_boolean.first"),
        "input_boolean.Not A Valid Id!": peer_entry("input_boolean.Not A Valid Id!"),
    }

    with caplog.at_level(logging.WARNING):
        await boot_with_snapshot(hass, backend, entries)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("refused" in r.getMessage() for r in warnings), (
        "a refused entry must be reported at WARNING, not dropped in silence"
    )
    assert any("1" in r.getMessage() for r in warnings if "refused" in r.getMessage())


async def test_ar_0037_an_entry_state_refuses_costs_that_entry_alone(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The AR-0013 guarantee has to hold all the way to `async_set`.

    Production change that would make this fail: calling `async_set` outside a
    try, so the first entity the state machine rejects raises out of the loop.

    Every other step of this loop already isolates one bad entry — parsing,
    timestamps, size, filtering. The application step did not, and it is the
    only step that runs arbitrary Home Assistant code. `async_set` validates
    the entity_id it is handed and raises on a malformed one, so a single bad
    field name in the hash abandons every entry after it, and the summary line
    that would have said so never runs either.

    What makes it worth fixing despite needing the cluster secret to reach: the
    cost is not one wrong entity, it is a promotion that silently comes up
    cold. The whole point of AR-0013 was that a failover must not be all or
    nothing.
    """
    entries = {
        "input_boolean.first": peer_entry("input_boolean.first"),
        # A field name no legitimate node writes, but the hash is shared and
        # `async_set` is what decides what is legitimate.
        "input_boolean.Not A Valid Id!": peer_entry("input_boolean.Not A Valid Id!"),
        "input_boolean.last": peer_entry("input_boolean.last"),
    }

    await boot_with_snapshot(hass, backend, entries)

    # The bad one is refused...
    assert hass.states.get("input_boolean.Not A Valid Id!") is None
    # ...and, crucially, the entry *after* it still lands.
    assert hass.states.get("input_boolean.first") is not None
    assert hass.states.get("input_boolean.last") is not None


# -- AR-0012: runtime add ---------------------------------------------------


async def test_ar_0012_runtime_add_does_not_restore(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0012 — adding the integration to a live HA must not replay state.

    Production change that would make this fail: calling the restore directly
    when `hass.state == CoreState.running`.

    At boot the restore lands before the automation engine starts. Added at
    runtime, the same call seeds dozens of entities into a live system and
    every automation watching them fires at once.
    """
    backend.stored = {"input_boolean.quiet": peer_entry()}
    assert hass.state is CoreState.running

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: NODE_ID,
            CONF_CLUSTER_SECRET: SECRET,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("input_boolean.quiet") is None, (
        "restore must not fire into a running automation engine"
    )


# -- AR-0027: attribution ---------------------------------------------------


async def test_ar_0027_all_restored_states_share_one_context(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0027 — the whole restore is one attributable operation.

    Production change that would make this fail: dropping `context=` from
    `async_set`, which makes HA mint a fresh context per call.

    Asserting merely that *a* context exists proves nothing — HA always
    supplies one. What matters is that every entity seeded by a given restore
    shares the *same* context, so an automation or the logbook can recognise
    the batch as one restore rather than as unrelated device reports.
    """
    await boot_with_snapshot(
        hass,
        backend,
        {
            "input_boolean.one": peer_entry("input_boolean.one"),
            "input_boolean.two": peer_entry("input_boolean.two"),
            "counter.three": peer_entry("counter.three", state="3"),
        },
    )

    contexts = {
        hass.states.get(eid).context.id
        for eid in ("input_boolean.one", "input_boolean.two", "counter.three")
    }
    assert len(contexts) == 1, "one restore must be one context, not one per entity"


# -- AR-0005: no secret, no restore ----------------------------------------


async def test_ar_0005_restore_is_refused_without_a_cluster_secret(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """AR-0005 — an unauthenticatable snapshot is refused, not applied.

    Production change that would make this fail: restoring anyway when no
    secret is configured.

    Starting cold is a worse failover. Obeying a forged alarm state is a worse
    outcome, and the restore path feeds `async_set` directly -- so with no way
    to establish where an entry came from, the safe answer is to take nothing.
    """
    await boot_with_snapshot(
        hass,
        backend,
        {"alarm_control_panel.house": peer_entry("alarm_control_panel.house")},
        **{CONF_CLUSTER_SECRET: ""},
    )

    assert hass.states.get("alarm_control_panel.house") is None


# -- AR-0020: staleness semantics ------------------------------------------


async def test_ar_0020_restore_reports_the_snapshot_age_it_used(
    hass: HomeAssistant, backend: FakeBackend, caplog
) -> None:
    """AR-0020 — "how old was the snapshot we restored?" must be answerable.

    Production change that would make this fail: dropping the snapshot age
    from the restore summary.

    Previously staleness was implicit: entries older than max_age were skipped
    and nothing said how old the surviving ones were. A promotion restoring
    29-minute-old state and one restoring 3-second-old state produced identical
    logs, so an operator could not tell a healthy failover from a barely-legal
    one.
    """
    import logging

    caplog.set_level(logging.INFO)
    stamp = (dt_util.utcnow() - timedelta(seconds=600)).isoformat()
    backend.meta = {"last_snapshot_at": stamp, "source_node": PEER_ID}

    await boot_with_snapshot(hass, backend, {"input_boolean.quiet": peer_entry(age_seconds=600)})

    assert "snapshot age" in caplog.text.lower()


async def test_ar_0020_a_nearly_expired_snapshot_is_warned_about(
    hass: HomeAssistant, backend: FakeBackend, caplog
) -> None:
    """A snapshot close to max-age restores, but must not do so quietly.

    Production change that would make this fail: treating "inside max_age" as
    uniformly fine. Restoring state from 29 minutes ago is legal under a
    30-minute window and is worth saying out loud during a failover.

    The wording matters and used to be wrong. It asserted the peer's flush loop
    had stopped -- and said so about a perfectly healthy node, because
    `async_flush` skips when nothing has changed, so an idle peer stops
    advancing the timestamp with nothing at all amiss. It now names the likely
    innocent cause first.
    """
    import logging

    caplog.set_level(logging.WARNING)
    stamp = (dt_util.utcnow() - timedelta(seconds=1700)).isoformat()
    backend.meta = {"last_snapshot_at": stamp, "source_node": PEER_ID}

    await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.quiet": peer_entry(age_seconds=1700)},
        snapshot_age_seconds=1700,
        **{CONF_RESTORE_MAX_AGE: 1800},
    )

    assert "ageing snapshot" in caplog.text.lower()
    assert "quiet cluster" in caplog.text.lower(), "must not blame a healthy peer"
    assert "flush loop" not in caplog.text.lower(), (
        "the old wording accused a healthy peer of having stopped writing"
    )


async def test_fresh_snapshot_does_not_warn(
    hass: HomeAssistant, backend: FakeBackend, caplog
) -> None:
    """A healthy failover must stay quiet, or the warning means nothing."""
    import logging

    caplog.set_level(logging.WARNING)
    stamp = dt_util.utcnow().isoformat()
    backend.meta = {"last_snapshot_at": stamp, "source_node": PEER_ID}

    await boot_with_snapshot(
        hass, backend, {"input_boolean.quiet": peer_entry(age_seconds=3)}, snapshot_age_seconds=3
    )

    assert "ageing snapshot" not in caplog.text.lower()


# -- AR-0040: the freshness guard versus Home Assistant's own restore --------


async def test_ar_0040_state_ha_replayed_at_boot_does_not_beat_the_snapshot(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The finding the harness could not show and the rehearsal did.

    Production change that would make this fail: comparing the snapshot entry
    against `existing.last_updated` without accounting for when this Home
    Assistant run began.

    Every domain in the default allowlist — `input_boolean`, `input_number`,
    `input_select`, `input_text`, `input_datetime`, `counter`, `timer` — is a
    RestoreEntity. At boot Home Assistant replays each one's previous value
    from local `.storage` and stamps `last_updated` with **the boot time**,
    not the time the value actually last changed. The restore then runs at
    `EVENT_HOMEASSISTANT_START`, finds local newer than every snapshot entry,
    and skips all of them.

    Observed on two real Home Assistant containers against a real Valkey:

        Snapshot age at restore: 18s (max 1800s)
        Restored 0 entities from snapshot (skipped: 2 local-newer, ...)

    The guard cannot fire for a legitimate reason at boot, because at boot all
    local state is stamped in this run. It can only fire spuriously — and it
    fired on everything. It then compounded: the standby took the lease and
    flushed its own cold state over the peer's good snapshot, so the
    integration destroyed the snapshot it had failed to use.

    This test recreates the condition rather than the mechanism: local state
    stamped *now*, a snapshot entry a few seconds older, and the snapshot must
    win.
    """
    # Exactly what RestoreEntity leaves behind: a value stamped at boot.
    hass.states.async_set("input_boolean.holiday_mode", "off")
    await hass.async_block_till_done()

    await boot_with_snapshot(
        hass,
        backend,
        {
            "input_boolean.holiday_mode": peer_entry(
                "input_boolean.holiday_mode", "on", age_seconds=18
            )
        },
    )

    assert hass.states.get("input_boolean.holiday_mode").state == "on", (
        "an 18-second-old snapshot from the peer must beat a value Home "
        "Assistant replayed from this node's own disk at boot"
    )


async def test_ar_0040_outside_boot_local_state_still_wins(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The other side of the `at_boot` switch, called directly.

    Production change that would make this fail: deleting the local-newer
    comparison rather than qualifying it.

    No boot-path test can pin this, because at boot the guard is deliberately
    inert — which is exactly how it would come to be deleted by someone
    tidying up dead code. Off the boot path the comparison is meaningful
    again: local timestamps there were set by real events rather than by Home
    Assistant replaying its own disk, so a value changed a moment ago must not
    be overwritten by an older snapshot.
    """
    from custom_components.cluster_state_sync import _restore_from_snapshot
    from custom_components.cluster_state_sync.coordinator import SyncStats

    hass.states.async_set("input_boolean.quiet", "off")
    await hass.async_block_till_done()

    backend.stored = {
        "input_boolean.quiet": peer_entry("input_boolean.quiet", "on", age_seconds=60)
    }
    await _restore_from_snapshot(
        hass,
        backend,
        {CONF_CLUSTER_SECRET: SECRET, CONF_NODE_ID: NODE_ID},
        NODE_ID,
        SyncStats(),
        at_boot=False,
    )

    assert hass.states.get("input_boolean.quiet").state == "off", (
        "off the boot path, a minute-old snapshot must not overwrite a value that was just changed"
    )


async def test_ar_0040_restoring_nothing_from_a_full_snapshot_is_a_warning(
    hass: HomeAssistant, backend: FakeBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """The detection that was missing when the bug was live.

    Production change that would make this fail: logging the all-skipped case
    at INFO alongside every healthy restore.

    "A snapshot was there and none of it was applied" is the precise signature
    of AR-0040, and for the whole time that bug was live it read as an ordinary
    INFO line. It is the one outcome meaning the integration did nothing during
    the only event it exists for, so it has to be visible without knowing to
    look for it.
    """
    # Every entry is the node's own, so all are skipped for a legitimate
    # reason — the point is the *reporting*, not the reason.
    backend.stored = {"input_boolean.quiet": peer_entry("input_boolean.quiet", source_node=NODE_ID)}

    with caplog.at_level(logging.WARNING):
        await boot_with_snapshot(hass, backend, dict(backend.stored))

    assert any(
        "Restored NOTHING" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ), "an all-skipped restore must be a warning, not an INFO line"


# -- AR-0065: the flush must not beat the restore --------------------------


async def test_ar_0065_the_leader_must_not_publish_before_the_restore_has_run(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """🚨 The finding a live drill produced and 1,144 green tests did not.

    A promoted node resolves leadership and its flush loop publishes its own
    state to the shared snapshot **before** the restore — wired to
    `EVENT_HOMEASSISTANT_START` — reads it. Every entry then carries
    `source_node == self`, the restore skips all of them as `own-node`, and
    nothing crosses. Measured on the reference pair in both directions:

        18:08:20.786  Leadership change -> LEADER, publishing state
        18:08:20.787  snapshot written by node-b   <- overwrites the peer's
        18:08:35.703  restore -> 28 own-node -> restored 0

    `DEFAULT_SNAPSHOT_INTERVAL` is 5 seconds, so the flush wins on **default
    settings**, on every installation.

    Why the suite could not see it: `boot_with_snapshot` fires START with no
    time advance in between, so the flush loop never runs. This test advances
    the clock first, which is the whole difference.

    🚨 AR-0040's own write-up already named this — *"the standby took the lease
    and flushed its own cold state over the peer's good snapshot, so the
    integration destroyed the snapshot it had failed to use"* — as a knock-on
    of the local-newer bug. Fixing local-newer made the symptom disappear from
    the rehearsal, and this half was never closed.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    backend.stored = {
        "input_boolean.holiday_mode": peer_entry("input_boolean.holiday_mode", "on", age_seconds=5)
    }
    backend.meta = {
        "schema_version": 1,
        "source_node": PEER_ID,
        "entry_count": 1,
        "last_snapshot_at": (datetime.now(tz=UTC) - timedelta(seconds=5)).isoformat(),
    }
    backend.lease_holder = NODE_ID  # we hold the lease: we are the promoted node
    hass.set_state(CoreState.not_running)

    # This node's own conflicting value, as its local .storage would replay.
    hass.states.async_set("input_boolean.holiday_mode", "off")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: NODE_ID,
            CONF_CLUSTER_SECRET: SECRET,
            "leadership_source": "always",
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The difference from every other restore test: real time passes
        # between setup and START, so the flush loop gets to run — exactly as
        # it does on a real node, where the restore waits for the whole of
        # Home Assistant to finish starting.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()

        await hass.async_start()
        await hass.async_block_till_done()

    assert hass.states.get("input_boolean.holiday_mode").state == "on", (
        "the peer's state did not cross: the flush published this node's own "
        "state over the snapshot before the restore could read it, so every "
        "entry looked like our own and was skipped. This is the entire "
        "purpose of the integration failing silently."
    )
    from custom_components.cluster_state_sync.const import DATA_STATS

    assert entry.runtime_data[DATA_STATS].restored_count == 1


async def test_ar_0065_the_gate_opens_so_the_leader_does_eventually_publish(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """🚨 The failure mode of the FIX, which would be worse than the bug.

    Holding the flush until the restore has run is only safe if the hold is
    guaranteed to lift. A gate that never opens means the leader never
    publishes, the standby's snapshot ages out, and the next promotion restores
    *stale* state — silently, and with every health check green.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.holiday_mode": peer_entry("input_boolean.holiday_mode", "on")},
        leadership_source="always",
    )
    before = len(backend.writes)

    hass.states.async_set("input_boolean.holiday_mode", "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()

    assert len(backend.writes) > before, (
        "the leader never published after the restore completed — the gate "
        "did not lift, which starves the standby of state entirely"
    )
    assert entry is not None


async def test_a_restart_and_a_failed_promotion_no_longer_read_alike(
    hass: HomeAssistant, backend: FakeBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """🚨 The wording that cost hours during the AR-0065 investigation.

    Both cases restore nothing and both are worth seeing (AR-0040). But one is
    a leader restarting with only its own snapshot to read — entirely normal —
    and the other is a promotion that failed to take the peer's state. The
    message said *"This node has promoted with no state from its peer"* for
    both, so on a live cluster a routine restart and the failure the product
    exists to prevent were indistinguishable on the one line that separates
    them.
    """
    backend.stored = {"input_boolean.quiet": peer_entry("input_boolean.quiet", source_node=NODE_ID)}
    with caplog.at_level(logging.WARNING):
        await boot_with_snapshot(hass, backend, dict(backend.stored))

    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "Restored NOTHING" in text, "AR-0040's rule still holds: it must be visible"
    assert "no peer state to apply" in text
    assert "has promoted with no state from its peer" not in text, (
        "a restart must not claim it promoted and lost the peer's state"
    )


async def test_a_genuinely_failed_promotion_still_says_so(
    hass: HomeAssistant, backend: FakeBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half: peer state was there and none of it was applied."""
    entry = peer_entry("input_boolean.quiet", "on", source_node=PEER_ID)
    backend.stored = {"input_boolean.quiet": entry}
    # Local state stamped far in the future, so the peer's entry is refused for
    # a reason that is NOT own-node.
    hass.states.async_set("input_boolean.quiet", "off")

    with caplog.at_level(logging.WARNING):
        await boot_with_snapshot(hass, backend, dict(backend.stored), restore_max_age=1)

    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    if "Restored NOTHING" in text:
        assert "no peer state to apply" not in text, (
            "the peer's snapshot was present — this is not the benign restart case"
        )


# -- restoring a domain whose component owns its own state ------------------


async def test_an_automation_is_restored_by_service_not_by_a_state_write(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """🚨 A state write on an automation is a lie.

    `AutomationEntity.is_on` returns
    `self._async_detach_triggers is not None or self._is_enabled` — its OWN
    state, not `hass.states`. So `async_set("automation.x", "off")` shows "off"
    in the UI and to every template while the triggers stay attached and the
    automation keeps firing, until the entity next writes its state and quietly
    corrects the display back to "on".

    Telling an operator something is parked when it is running is worse than
    not replicating the domain at all.
    """
    from pytest_homeassistant_custom_component.common import async_mock_service

    off_calls = async_mock_service(hass, "automation", "turn_off")
    on_calls = async_mock_service(hass, "automation", "turn_on")

    backend.stored = {
        "automation.dangerous": peer_entry("automation.dangerous", "off"),
        "automation.wanted": peer_entry("automation.wanted", "on"),
    }
    await boot_with_snapshot(
        hass,
        backend,
        dict(backend.stored),
        include_domains=["automation"],
    )

    assert [c.data["entity_id"] for c in off_calls] == ["automation.dangerous"], (
        "the parked automation was not actually disabled — a state write would "
        "have shown 'off' while it kept firing"
    )
    assert [c.data["entity_id"] for c in on_calls] == ["automation.wanted"]


async def test_a_helper_is_still_restored_by_a_state_write(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The service route is for state-owning components only.

    `input_boolean` and friends are seeded through `async_set`, which is the
    documented public API and what every existing restore relies on. Routing
    them through services would change a seeding operation into a command.
    """
    from pytest_homeassistant_custom_component.common import async_mock_service

    calls = async_mock_service(hass, "input_boolean", "turn_on")
    await boot_with_snapshot(
        hass,
        backend,
        {"input_boolean.holiday_mode": peer_entry("input_boolean.holiday_mode", "on")},
    )
    assert hass.states.get("input_boolean.holiday_mode").state == "on"
    assert calls == [], "a helper must be seeded, not commanded"


async def test_an_unknown_automation_state_falls_back_to_a_state_write(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """`unavailable` is not something to command. Seed it and move on."""
    backend.stored = {"automation.broken": peer_entry("automation.broken", "unavailable")}
    await boot_with_snapshot(hass, backend, dict(backend.stored), include_domains=["automation"])
    assert hass.states.get("automation.broken").state == "unavailable"
