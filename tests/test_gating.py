"""ADR-001 layers 3 and 4 — recorder writes and automations.

Layer 2 (the flush) already consumes the leadership signal. These two consume
the same signal but must not copy its *posture*.

`LeadershipMonitor.async_is_leader` fails closed: an unreachable backend
answers "not leader". For a flush that is exactly right — declining to write
costs one interval of freshness. For automations it is not: a thirty-second
Valkey blip would turn off every automation in the house, and the mechanism
meant to prevent a double-firing failover would itself be the outage.

So these layers are **opt-in**, and they only act on a *transition*. A gate that
called `automation.turn_off` every five seconds forever would be both noisy and
a good way to fight anyone using the UI.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import CoreState
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.cluster_state_sync.const import (
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_GATE_AUTOMATIONS,
    CONF_LEADERSHIP_ENTITY,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_REDIS_HOST,
    CONF_SNAPSHOT_INTERVAL,
    DOMAIN,
    LEADERSHIP_ENTITY,
)
from custom_components.cluster_state_sync.gating import ServiceGate

from .fakes import FakeBackend

AUTOMATION_OFF = ("automation", "turn_off")
AUTOMATION_ON = ("automation", "turn_on")
RECORDER_OFF = ("recorder", "disable")
RECORDER_ON = ("recorder", "enable")


@pytest.fixture
def calls(hass):
    """Record every service the gate reaches for."""
    return {
        name: async_mock_service(hass, domain, service)
        for name, (domain, service) in {
            "automation_off": AUTOMATION_OFF,
            "automation_on": AUTOMATION_ON,
            "recorder_off": RECORDER_OFF,
            "recorder_on": RECORDER_ON,
        }.items()
    }


def _gate(hass, **kw):
    return ServiceGate(hass, gate_recorder=True, gate_automations=True, **kw)


# ---------------------------------------------------------------------------
# What it does on each side of the transition
# ---------------------------------------------------------------------------


async def test_a_follower_stops_its_recorder_and_automations(hass, calls) -> None:
    """The whole point of layers 3 and 4."""
    gate = _gate(hass)

    await gate.async_apply(is_leader=False)
    await hass.async_block_till_done()

    assert len(calls["automation_off"]) == 1
    assert len(calls["recorder_off"]) == 1
    assert not calls["automation_on"]
    assert not calls["recorder_on"]


async def test_a_leader_leaves_everything_running(hass, calls) -> None:
    """Being leader is the normal case and must cost nothing."""
    gate = _gate(hass)

    await gate.async_apply(is_leader=True)
    await hass.async_block_till_done()

    assert not calls["automation_off"]
    assert not calls["recorder_off"]


async def test_promotion_turns_them_back_on(hass, calls) -> None:
    gate = _gate(hass)
    await gate.async_apply(is_leader=False)
    await hass.async_block_till_done()

    await gate.async_apply(is_leader=True)
    await hass.async_block_till_done()

    assert len(calls["automation_on"]) == 1
    assert len(calls["recorder_on"]) == 1


# ---------------------------------------------------------------------------
# Transitions only
# ---------------------------------------------------------------------------


async def test_it_acts_once_per_transition_not_once_per_interval(hass, calls) -> None:
    """The flush loop runs every few seconds; this must not follow it.

    Calling `automation.turn_off` on a timer would fight any operator using the
    UI, and fill the log with a change that is not a change.
    """
    gate = _gate(hass)

    for _ in range(5):
        await gate.async_apply(is_leader=False)
    await hass.async_block_till_done()

    assert len(calls["automation_off"]) == 1


async def test_a_flapping_signal_still_reconciles_each_way(hass, calls) -> None:
    """Self-healing: a missed transition is corrected on the next pass."""
    gate = _gate(hass)

    for leader in (False, True, False):
        await gate.async_apply(is_leader=leader)
    await hass.async_block_till_done()

    assert len(calls["automation_off"]) == 2
    assert len(calls["automation_on"]) == 1


# ---------------------------------------------------------------------------
# Opt-in, and failure posture
# ---------------------------------------------------------------------------


async def test_nothing_happens_when_the_gate_is_switched_off(hass, calls) -> None:
    """Default for an existing install must be no behaviour change at all."""
    gate = ServiceGate(hass, gate_recorder=False, gate_automations=False)

    await gate.async_apply(is_leader=False)
    await hass.async_block_till_done()

    assert not calls["automation_off"]
    assert not calls["recorder_off"]


async def test_each_layer_can_be_enabled_independently(hass, calls) -> None:
    """Layer 3 guards a shared recorder; layer 4 guards automations.

    They protect different things and a deployment may want one without the
    other — a shared Postgres with no warm standby wants 3 and not 4.
    """
    gate = ServiceGate(hass, gate_recorder=True, gate_automations=False)

    await gate.async_apply(is_leader=False)
    await hass.async_block_till_done()

    assert len(calls["recorder_off"]) == 1
    assert not calls["automation_off"]


async def test_a_failing_service_does_not_break_the_caller(hass) -> None:
    """This runs inside the flush loop. It must never take that loop down.

    A recorder that refuses to disable is a degraded gate; a raised exception
    would be a stopped snapshot loop, which is strictly worse.
    """

    async def boom(_call):
        raise RuntimeError("recorder said no")

    hass.services.async_register("recorder", "disable", boom)
    async_mock_service(hass, "automation", "turn_off")
    gate = _gate(hass)

    await gate.async_apply(is_leader=False)  # must not raise
    await hass.async_block_till_done()


async def test_a_failed_gate_is_retried_rather_than_assumed_done(hass) -> None:
    """If the call failed, the state was not reached, so do not record it as reached."""
    attempts: list[str] = []

    async def boom(_call):
        attempts.append("tried")
        raise RuntimeError("nope")

    hass.services.async_register("automation", "turn_off", boom)
    gate = ServiceGate(hass, gate_recorder=False, gate_automations=True)

    await gate.async_apply(is_leader=False)
    await gate.async_apply(is_leader=False)
    await hass.async_block_till_done()

    assert len(attempts) == 2


# ---------------------------------------------------------------------------
# The options have to survive the trip from the wizard to the runtime
# ---------------------------------------------------------------------------


def test_the_gate_options_are_offered_by_the_wizard() -> None:
    """An option nobody can set is not a feature."""
    from custom_components.cluster_state_sync.config_flow import _topology_schema

    keys = {str(k) for k in _topology_schema({}).schema}

    assert "gate_recorder" in keys
    assert "gate_automations" in keys


def test_both_gates_default_to_off() -> None:
    """Turning these on for an existing install would be a silent regression.

    ADR-003 made the same call for the leadership signal itself: never change
    the behaviour of a working single-node install on upgrade.
    """
    from custom_components.cluster_state_sync.config_flow import _topology_schema

    defaults = {str(k): k.default() for k in _topology_schema({}).schema if k.default}

    assert defaults.get("gate_recorder") is False
    assert defaults.get("gate_automations") is False


# ---------------------------------------------------------------------------
# Undoing itself — adversarial review finding, 2026-08-27
# ---------------------------------------------------------------------------


async def test_the_gate_restores_what_it_disabled(hass, calls) -> None:
    """Uninstalling must not leave the house without automations.

    A follower disables automations; the user then removes the integration. If
    the gate does not undo itself, they stay off forever and the only thing
    that would turn them back on has just been deleted.
    """
    gate = _gate(hass)
    await gate.async_apply(is_leader=False)
    await hass.async_block_till_done()

    await gate.async_restore()
    await hass.async_block_till_done()

    assert len(calls["automation_on"]) == 1
    assert len(calls["recorder_on"]) == 1


async def test_restoring_a_gate_that_never_fired_changes_nothing(hass, calls) -> None:
    """A leader that unloads must not be 'restored' into a service call."""
    gate = _gate(hass)
    await gate.async_apply(is_leader=True)
    await hass.async_block_till_done()

    await gate.async_restore()
    await hass.async_block_till_done()

    assert not calls["automation_on"]
    assert not calls["recorder_on"]


async def test_restore_is_a_no_op_when_the_gate_is_switched_off(hass, calls) -> None:
    gate = ServiceGate(hass, gate_recorder=False, gate_automations=False)

    await gate.async_restore()
    await hass.async_block_till_done()

    assert not calls["automation_on"]


async def test_unloading_the_entry_restores_the_gate(hass, calls) -> None:
    """The wiring, not just the method: unload must actually call it."""
    import custom_components.cluster_state_sync as integration

    src = (
        integration.async_unload_entry.__code__.co_consts,
        integration.async_unload_entry.__code__.co_names,
    )
    assert any("restore" in str(x).lower() for x in src[1]), (
        "async_unload_entry does not reference the gate's restore path"
    )


async def test_a_first_pass_as_leader_touches_nothing(hass, calls) -> None:
    """Adversarial review 2026-08-27, finding 3.

    On startup the gate has switched nothing off, so being leader is not a
    reason to switch anything on. `automation.turn_on` with `entity_id: all`
    would re-enable automations the operator had deliberately disabled in the
    UI — a failover component overruling a human about something it knows
    nothing about.
    """
    gate = _gate(hass)

    await gate.async_apply(is_leader=True)
    await hass.async_block_till_done()

    assert not calls["automation_on"]
    assert not calls["recorder_on"]


async def test_repeated_leader_passes_stay_silent(hass, calls) -> None:
    """The normal case for a healthy primary: the gate is inert."""
    gate = _gate(hass)

    for _ in range(10):
        await gate.async_apply(is_leader=True)
    await hass.async_block_till_done()

    assert not calls["automation_on"]
    assert not calls["automation_off"]


async def test_disabling_logs_the_manual_recovery(hass, calls, caplog) -> None:
    """Adversarial review 2026-08-27, finding 4.

    The gate's effect outlives the gate. It survives a restart, and if the
    entry is removed while a node is gated off — or if a later setup fails
    before the gate is constructed, because the backend is unreachable —
    nothing is left to undo it. The log line at the moment of disabling is the
    only place the operator will be standing, so it carries the fix.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        await _gate(hass).async_apply(is_leader=False)
        await hass.async_block_till_done()

    assert "Developer Tools" in caplog.text
    assert "entity_id: all" in caplog.text


# ---------------------------------------------------------------------------
# AR-0039 — when the gate first fires, relative to the automation engine
# ---------------------------------------------------------------------------


async def test_ar_0039_a_follower_is_gated_before_automations_start(hass, calls) -> None:
    """The gate must land in the same window the restore does.

    Production change that would make this fail: applying the gate only from
    the scheduled flush, so the first application waits a full snapshot
    interval.

    The restore is deliberately wired to `EVENT_HOMEASSISTANT_START` because
    that is the window after integrations load and before the automation
    engine attaches its triggers — the whole of AR-0012 is about not seeding
    state into a system whose automations are already live. Layer 4 decides
    whether those automations should be running *at all*, and it was only
    consulted from the flush timer, one interval later.

    So on a follower the sequence was: restore a batch of peer states, start
    every automation, let them act on what was just restored, and only then
    turn them off. The two halves disagreed about when "before automations"
    is. The interval is configurable up to 300 seconds, so this is not
    bounded by the 5-second default.
    """
    backend = FakeBackend()
    backend.stored = {}
    hass.set_state(CoreState.not_running)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: "node-a",
            CONF_CLUSTER_SECRET: "cluster-secret-under-test",
            CONF_GATE_AUTOMATIONS: True,
            # Fails closed to "follower": the entity does not exist.
            CONF_LEADERSHIP_SOURCE: LEADERSHIP_ENTITY,
            CONF_LEADERSHIP_ENTITY: "input_boolean.ha_is_master",
            # Long enough that a timer-driven gate could not have fired.
            CONF_SNAPSHOT_INTERVAL: 300,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.async_start()
        await hass.async_block_till_done()

    assert calls["automation_off"], (
        "a follower must be gated in the pre-automation window, not one "
        "snapshot interval after the automations have already run"
    )


async def test_ar_0039_added_to_a_live_follower_gates_immediately(hass, calls) -> None:
    """The other half: installed on a running system, not at boot.

    Production change that would make this fail: only registering the
    `EVENT_HOMEASSISTANT_START` listener, which on an already-running Home
    Assistant never fires again.

    This is deliberately the opposite call from AR-0012. The restore refuses to
    run on a live system because seeding state would make automations fire;
    the gate acts on a live system for exactly the same reason — the
    automations the operator wants quiet are the ones already running. An
    operator who installs this on their standby and sees nothing happen for
    five minutes has been given no reason to believe it works.
    """
    backend = FakeBackend()
    backend.stored = {}
    assert hass.state is CoreState.running

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: "node-a",
            CONF_CLUSTER_SECRET: "cluster-secret-under-test",
            CONF_GATE_AUTOMATIONS: True,
            CONF_LEADERSHIP_SOURCE: LEADERSHIP_ENTITY,
            CONF_LEADERSHIP_ENTITY: "input_boolean.ha_is_master",
            CONF_SNAPSHOT_INTERVAL: 300,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert calls["automation_off"], (
        "installing on a live follower must gate now, not one interval later"
    )
