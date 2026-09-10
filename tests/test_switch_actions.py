"""The two switches, which are the only controls that change the cluster.

Both were once files you had to SSH in to touch, and both are now a tap on a
dashboard. Their `async_turn_on` / `async_turn_off` handlers were untested —
including the one that **deliberately causes a failover**.

They are tested here against a real temporary directory rather than a mocked
filesystem, because the thing being asserted is that the flag the host-side
promoter reads actually appears on disk. A mock would assert that we called a
function, which is not the same claim and is not the one that matters.
"""

from __future__ import annotations

import pathlib

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync.const import DOMAIN
from custom_components.cluster_state_sync.handover import is_requested
from custom_components.cluster_state_sync.hold import hold_reason, is_held
from custom_components.cluster_state_sync.switch import (
    HandoverRequestSwitch,
    MaintenanceHoldSwitch,
)


@pytest.fixture
def config_dir(tmp_path: pathlib.Path) -> str:
    return str(tmp_path)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    return entry


def _wire(switch, hass: HomeAssistant, entity_id: str):
    """Give the entity what a platform would.

    `async_write_ha_state` needs an entity_id; these entities are constructed
    directly so that the handlers can be driven without standing up a whole
    config entry, which is the point of testing them here.
    """
    switch.hass = hass
    switch.entity_id = entity_id
    return switch


# -- the maintenance hold --------------------------------------------------


async def test_the_hold_switch_writes_and_clears_the_flag(
    hass: HomeAssistant, config_dir: str
) -> None:
    """The flag is read by a host-side timer, so it must reach the disk.

    This is the control that makes it safe to restart Home Assistant on the
    leader. Getting it wrong in the "on" direction means a restart triggers an
    unplanned failover; getting it wrong in the "off" direction means failover
    stays suspended and the cluster silently stops protecting the house.
    """
    switch = _wire(MaintenanceHoldSwitch(_entry(hass), config_dir), hass, "switch.hold")

    assert not await hass.async_add_executor_job(is_held, config_dir)

    await switch.async_turn_on()
    assert await hass.async_add_executor_job(is_held, config_dir)
    assert switch.is_on
    reason = await hass.async_add_executor_job(hold_reason, config_dir)
    assert "dashboard" in reason, "the reason must say where it came from"

    await switch.async_turn_off()
    assert not await hass.async_add_executor_job(is_held, config_dir)
    assert not switch.is_on


async def test_the_hold_switch_reads_a_hold_set_outside_home_assistant(
    hass: HomeAssistant, config_dir: str
) -> None:
    """The runbook sets this from the host. The dashboard must agree with it.

    A switch that shows "off" while the host-side hold is on tells an operator
    failover is live when it is suspended — which is exactly the wrong answer
    to have during a maintenance window.
    """
    from custom_components.cluster_state_sync.hold import set_hold

    switch = _wire(MaintenanceHoldSwitch(_entry(hass), config_dir), hass, "switch.hold")
    await hass.async_add_executor_job(set_hold, config_dir, "set from the runbook")

    await switch.async_update()
    assert switch.is_on, "a hold set on the host did not show in the dashboard"


# -- hand over to peer -----------------------------------------------------


async def test_the_handover_switch_requests_and_withdraws(
    hass: HomeAssistant, config_dir: str
) -> None:
    """🚨 This switch causes a failover on purpose.

    Turning it on asks the promoter to release the lease and stop Home
    Assistant here. Turning it off must genuinely withdraw a request the
    promoter has not yet acted on — a withdrawal that does not reach the disk
    leaves an armed request nobody believes is armed.
    """
    switch = _wire(HandoverRequestSwitch(_entry(hass), config_dir), hass, "switch.handover")

    assert not await hass.async_add_executor_job(is_requested, config_dir)

    await switch.async_turn_on()
    assert await hass.async_add_executor_job(is_requested, config_dir)
    assert switch.is_on

    await switch.async_turn_off()
    assert not await hass.async_add_executor_job(is_requested, config_dir)
    assert not switch.is_on


async def test_both_switches_are_independent(hass: HomeAssistant, config_dir: str) -> None:
    """They share a config directory and must not share a flag.

    A handover request that also raised the hold would ask the promoter to hand
    over and simultaneously forbid it from doing so.
    """
    hold = _wire(MaintenanceHoldSwitch(_entry(hass), config_dir), hass, "switch.hold")
    handover = _wire(HandoverRequestSwitch(_entry(hass), config_dir), hass, "switch.handover")

    await handover.async_turn_on()
    await hold.async_update()
    assert not hold.is_on, "requesting a handover also suspended failover"

    await hold.async_turn_on()
    await handover.async_update()
    assert handover.is_on, "raising the hold withdrew the handover request"
