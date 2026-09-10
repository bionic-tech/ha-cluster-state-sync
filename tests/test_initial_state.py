"""The restore honours an automation's `initial_state`.

`initial_state` is Home Assistant's documented way of saying "force this
automation on or off at every startup, *regardless of what was restored*". In
HA's own code it is applied in `async_added_to_hass` and deliberately beats
`async_get_last_state()`:

    if state := await self.async_get_last_state():
        enable_automation = state.state == STATE_ON
    ...
    if self._initial_state is not None:
        enable_automation = self._initial_state      # <- wins

Our restore runs later — on `EVENT_HOMEASSISTANT_START`, after entities are
added — and applies automations by calling `automation.turn_on`/`turn_off`
(AR: a state write on an automation is a lie). A service call is a command, and
a command lands after the pin has been applied — so without a guard **we win,
and we should not**. `_initial_state_is_pinned` is that guard.

Found 2026-09-10 while checking a *different* claim, which was that pinned
automations were not covered by the restore. They are covered. That is the
problem: an operator who wrote `initial_state: false` opted that automation out
of restore, and we put it back.

Concretely, on the estate this was found on:

    - id: network_firewall_cooling_preempt
      alias: "Network — firewall cooling preempt (DISABLED)"
      initial_state: false        # ships disabled

pinned off because the hardware it commands is not wired yet. If the peer's
snapshot ever records it `on` — one manual toggle is enough — every subsequent
promotion re-enables it.

Fixed 2026-09-10 by reading the private `_initial_state`, a deliberate and
documented exception to this repo's public-APIs-only rule: Home Assistant
exposes `initial_state` on no public API at all, so the choice was to read it
or to keep overriding operators. The last test here is the tripwire for that
exception — if the attribute ever moves, it fails loudly rather than letting
the guard silently go back to doing nothing.
"""

from __future__ import annotations

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

from tests.test_restore import FakeBackend, boot_with_snapshot, peer_entry


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


async def _automations(hass: HomeAssistant) -> None:
    """One pinned off, one pinned on, one free — otherwise identical."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "pinned_off",
                    "alias": "Pinned off",
                    "initial_state": False,
                    "trigger": {"platform": "event", "event_type": "cluster_test_never"},
                    "action": {"event": "cluster_test_fired"},
                },
                {
                    "id": "pinned_on",
                    "alias": "Pinned on",
                    "initial_state": True,
                    "trigger": {"platform": "event", "event_type": "cluster_test_never"},
                    "action": {"event": "cluster_test_fired"},
                },
                {
                    "id": "free",
                    "alias": "Free",
                    "trigger": {"platform": "event", "event_type": "cluster_test_never"},
                    "action": {"event": "cluster_test_fired"},
                },
            ]
        },
    )


async def test_a_pinned_automation_is_left_alone_by_the_restore(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """Pinned off, peer says on — the pin wins, because the operator meant it.

    Before the fix this came back `on`, which would have armed the estate's
    `network_firewall_cooling_preempt` — an automation shipped disabled because
    the hardware it commands is not wired.
    """
    hass.set_state(CoreState.not_running)
    await _automations(hass)

    await boot_with_snapshot(
        hass,
        backend,
        {"automation.pinned_off": peer_entry("automation.pinned_off", "on")},
        include_domains=["automation"],
    )

    pinned = hass.states.get("automation.pinned_off")
    assert pinned is not None
    assert pinned.state == "off", (
        "the restore overrode `initial_state` — an operator's explicit opt-out "
        "of restore. Check `_initial_state_is_pinned` still finds the attribute."
    )


async def test_an_unpinned_automation_is_restored_as_intended(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """The control. Without a pin, taking the peer's value is the whole point."""
    hass.set_state(CoreState.not_running)
    await _automations(hass)

    await boot_with_snapshot(
        hass,
        backend,
        {"automation.free": peer_entry("automation.free", "on")},
        include_domains=["automation"],
    )

    free = hass.states.get("automation.free")
    assert free is not None and free.state == "on"


async def test_home_assistant_still_has_no_public_initial_state_api() -> None:
    """Why the fix is not a one-liner, pinned so a bump tells us if it changes.

    `initial_state` reaches the entity as the private `_initial_state` and is
    absent from `extra_state_attributes`. Honouring it therefore means reading
    a private attribute — which is why the fix is a decision, not a patch. If
    Home Assistant ever exposes it publicly this test fails and the fix gets
    cheap.
    """
    from homeassistant.components.automation import AutomationEntity

    assert not hasattr(AutomationEntity, "initial_state"), (
        "Home Assistant now exposes initial_state publicly — honour it properly"
    )
    assert "_initial_state" in AutomationEntity.__init__.__code__.co_names, (
        "the private attribute moved; any fix reading it needs revisiting"
    )


async def test_the_guard_is_about_the_pin_not_the_direction(
    hass: HomeAssistant, backend: FakeBackend
) -> None:
    """Pinned ON, peer says OFF — still the pin, not the cluster.

    The symmetric case. A guard that only protected `initial_state: false`
    would look correct on the estate that prompted it and be wrong by half.
    """
    hass.set_state(CoreState.not_running)
    await _automations(hass)

    await boot_with_snapshot(
        hass,
        backend,
        {"automation.pinned_on": peer_entry("automation.pinned_on", "off")},
        include_domains=["automation"],
    )

    pinned = hass.states.get("automation.pinned_on")
    assert pinned is not None and pinned.state == "on"
