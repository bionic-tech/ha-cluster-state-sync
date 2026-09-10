"""The alert router: what reaches somebody who is not looking at the screen.

The load-bearing tests here are the ones about *not* alerting. An alarm that
fires on every Home Assistant restart is worse than no alarm, because it
teaches the operator to swipe the notification away without reading it -- and
the one they swipe away unread will be the real failover.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.cluster_state_sync.alerts import (
    ALERT_MIN_INTERVAL,
    MAX_PENDING_ALERTS,
    AlertRouter,
)
from custom_components.cluster_state_sync.const import (
    DEFAULT_NOTIFY_CONDITIONS,
    NOTIFY_CONDITIONS,
    NOTIFY_FILESET_DEGRADED,
    NOTIFY_PROMOTED,
    NOTIFY_RECOVERED,
    NOTIFY_STATISTICS_GAP,
)


def _router(hass: HomeAssistant, *, conditions=None, services=("notify.tester",), node="tiger1"):
    return AlertRouter(
        hass,
        conditions=DEFAULT_NOTIFY_CONDITIONS if conditions is None else conditions,
        services=services,
        node_id=node,
    )


def _cards(hass: HomeAssistant) -> set[str]:
    """The persistent notifications on screen. These are not entities."""
    return set(_async_get_or_create_notifications(hass))


@pytest.fixture
def pushes(hass: HomeAssistant):
    """Every call that reached a `notify.` service."""
    return async_mock_service(hass, "notify", "tester")


# -- the edge ---------------------------------------------------------------


async def test_raise_pushes_once_not_once_per_pass(hass: HomeAssistant, pushes) -> None:
    """The repair sites raise on EVERY coordinator pass. We speak on the edge.

    This is the difference between an integration that tells you the fileset is
    degraded and one that tells you every thirty seconds, forever.
    """
    router = _router(hass)
    for _ in range(5):
        await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "The go-bag is stale")
    await hass.async_block_till_done()
    assert len(pushes) == 1


async def test_clear_pushes_the_all_clear(hass: HomeAssistant, pushes) -> None:
    """Silence is not evidence -- AR-0040's lesson, applied to alerting.

    An alarm that stops without a word is indistinguishable from an alarm
    nobody sent. If we interrupted someone's evening, we owe them the ending.
    """
    router = _router(hass)
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh again")
    await hass.async_block_till_done()
    assert len(pushes) == 2
    assert pushes[1].data["title"] == "Recovered"


async def test_clear_without_a_raise_says_nothing(hass: HomeAssistant, pushes) -> None:
    """Clearing runs on every pass too. Only a real transition is news."""
    router = _router(hass)
    for _ in range(3):
        await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fine")
    await hass.async_block_till_done()
    assert pushes == []


async def test_recovery_can_be_switched_off_on_its_own(hass: HomeAssistant, pushes) -> None:
    """Some operators want the bad news only. The raise still fires."""
    router = _router(hass, conditions=(NOTIFY_FILESET_DEGRADED,))
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh")
    await hass.async_block_till_done()
    assert [c.data["title"] for c in pushes] == ["Degraded"]


async def test_a_condition_not_chosen_never_pushes(hass: HomeAssistant, pushes) -> None:
    """The statistics conditions are off by default: they can wait until Saturday."""
    router = _router(hass)
    assert NOTIFY_STATISTICS_GAP not in DEFAULT_NOTIFY_CONDITIONS
    await router.async_raise(NOTIFY_STATISTICS_GAP, "Gap", "six hours missing")
    await hass.async_block_till_done()
    assert pushes == []


async def test_an_unchosen_condition_still_raises_its_card(hass: HomeAssistant, pushes) -> None:
    """Not choosing to be INTERRUPTED is not choosing to be uninformed.

    The persistent notification is the floor. Declining the push must not also
    silently delete the record.
    """
    router = _router(hass)
    await router.async_raise(NOTIFY_STATISTICS_GAP, "Gap", "six hours missing")
    await hass.async_block_till_done()
    assert pushes == []
    assert any("statistics_gap" in card for card in _cards(hass))


# -- the flap guard ---------------------------------------------------------


async def test_a_flapping_condition_does_not_spam(hass: HomeAssistant, pushes) -> None:
    """Raise/clear/raise inside the window interrupts once, not twice."""
    router = _router(hass, conditions=(NOTIFY_FILESET_DEGRADED,))
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh")
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale again")
    await hass.async_block_till_done()
    assert len(pushes) == 1


async def test_the_flap_window_expires(hass: HomeAssistant, pushes, monkeypatch) -> None:
    """Suppression is a rate limit, not a mute. The next hour's fault speaks."""
    router = _router(hass, conditions=(NOTIFY_FILESET_DEGRADED,))
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    router._last_sent[f"{NOTIFY_FILESET_DEGRADED}:raise"] -= ALERT_MIN_INTERVAL + 1
    await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh")
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale again")
    await hass.async_block_till_done()
    assert len(pushes) == 2


async def test_a_fault_that_clears_quickly_still_announces_it(hass: HomeAssistant, pushes) -> None:
    """The two edges of one condition get separate flap windows.

    Sharing one would mean a fault that clears inside five minutes never says
    that it cleared -- which is exactly when the all-clear is worth most,
    because the person it woke is still holding their phone.
    """
    router = _router(hass)  # defaults: both the raise and `recovered` are on
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh")
    await hass.async_block_till_done()
    assert [c.data["title"] for c in pushes] == ["Degraded", "Recovered"]


async def test_a_flapping_recovery_does_not_spam_either(hass: HomeAssistant, pushes) -> None:
    """The all-clear is rate-limited too. Two faults, two clears, no third."""
    router = _router(hass)
    for _ in range(3):
        await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
        await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh")
    await hass.async_block_till_done()
    assert len(pushes) == 2


async def test_one_conditions_recovery_does_not_mute_anothers(hass: HomeAssistant, pushes) -> None:
    """🚨 Recoveries are gated by ONE choice but limited PER CONDITION.

    Keyed on `recovered` alone, a flapping statistics gap would silently
    swallow the all-clear for a degraded fileset -- two unrelated faults, one
    of them reported and the other not, with nothing saying why.
    """
    router = _router(hass, conditions=(NOTIFY_STATISTICS_GAP, NOTIFY_RECOVERED))
    await router.async_raise(NOTIFY_STATISTICS_GAP, "Gap", "six hours")
    await router.async_clear(NOTIFY_STATISTICS_GAP, "Gap closed", "caught up")
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await router.async_clear(NOTIFY_FILESET_DEGRADED, "Fileset whole", "fresh")
    await hass.async_block_till_done()
    assert "Fileset whole" in [c.data["title"] for c in pushes]


# -- promotion, and the restarts that must NOT look like one ----------------


async def test_promotion_announces_who_it_took_over_from(hass: HomeAssistant, pushes) -> None:
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger2")
    await router.async_check_promotion(leader="tiger2", snapshot_source="tiger1")
    await hass.async_block_till_done()
    assert len(pushes) == 1
    assert "tiger2" in pushes[0].data["message"]
    assert "tiger1" in pushes[0].data["message"]


async def test_an_ordinary_restart_of_the_leader_is_silent(hass: HomeAssistant, pushes) -> None:
    """🚨 The one that matters.

    On the cold-standby model a promoted node STARTS Home Assistant, so its
    first leadership resolution is always "leader" -- and so is the primary's,
    every time it is restarted for maintenance. In-memory edge detection cannot
    tell those apart. `snapshot_source` can: the primary's own name is on the
    newest snapshot, so nothing moved.
    """
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger1")
    await router.async_check_promotion(leader="tiger1", snapshot_source="tiger1")
    await hass.async_block_till_done()
    assert pushes == []


async def test_a_fresh_cluster_does_not_alarm_at_install(hass: HomeAssistant, pushes) -> None:
    """An alarm during setup is an alarm nobody believes at 3am."""
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger1")
    await router.async_check_promotion(leader="tiger1", snapshot_source=None)
    await hass.async_block_till_done()
    assert pushes == []


async def test_a_follower_never_announces_a_promotion(hass: HomeAssistant, pushes) -> None:
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger2")
    await router.async_check_promotion(leader="tiger1", snapshot_source="tiger1")
    await hass.async_block_till_done()
    assert pushes == []


async def test_promotion_announced_once_though_polled_repeatedly(
    hass: HomeAssistant, pushes
) -> None:
    """The cluster poll can beat our first snapshot write, and often will."""
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger2")
    for _ in range(4):
        await router.async_check_promotion(leader="tiger2", snapshot_source="tiger1")
    await hass.async_block_till_done()
    assert len(pushes) == 1


async def test_a_later_failback_speaks_again(hass: HomeAssistant, pushes) -> None:
    """tiger2 takes over, tiger1 comes back and takes it again. Both are news."""
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger1")
    await router.async_check_promotion(leader="tiger1", snapshot_source="tiger1")
    await router.async_check_promotion(leader="tiger2", snapshot_source="tiger2")
    await router.async_check_promotion(leader="tiger1", snapshot_source="tiger2")
    await hass.async_block_till_done()
    assert len(pushes) == 1
    assert "took over from tiger2" in pushes[0].data["message"]


# -- alerting must never be able to break the cluster -----------------------


async def test_a_missing_service_does_not_raise(hass: HomeAssistant) -> None:
    """A typo in the options must cost an alert, never a replication cycle."""
    router = _router(hass, services=("notify.does_not_exist",))
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await hass.async_block_till_done()


async def test_a_missing_service_is_logged_once_not_per_pass(hass: HomeAssistant, caplog) -> None:
    router = _router(hass, services=("notify.does_not_exist",))
    for condition in (NOTIFY_FILESET_DEGRADED, NOTIFY_PROMOTED, NOTIFY_RECOVERED):
        await router.async_raise(condition, "T", "M")
    await hass.async_block_till_done()
    assert caplog.text.count("does not exist -- alerts will not reach it") == 1


async def test_one_dead_service_does_not_stop_the_live_one(hass: HomeAssistant) -> None:
    """The service that still works is the one the operator is holding."""
    good = async_mock_service(hass, "notify", "good")
    router = _router(hass, services=("notify.dead", "notify.good"))
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await hass.async_block_till_done()
    assert len(good) == 1


async def test_a_service_that_throws_does_not_propagate(hass: HomeAssistant) -> None:
    async def _boom(call):
        raise RuntimeError("push token expired")

    hass.services.async_register("notify", "flaky", _boom)
    router = _router(hass, services=("notify.flaky",))
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await hass.async_block_till_done()


async def test_zero_configured_services_still_tells_somebody(hass: HomeAssistant) -> None:
    """The persistent notification is why an empty service list is not a fault."""
    router = _router(hass, services=())
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "the go-bag is stale")
    await hass.async_block_till_done()
    assert any("fileset_degraded" in card for card in _cards(hass))


async def test_clearing_removes_the_card(hass: HomeAssistant) -> None:
    """A cleared condition must not leave a card demanding attention."""
    router = _router(hass, services=())
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh")
    await hass.async_block_till_done()
    assert not any("fileset_degraded" in card for card in _cards(hass))


# -- the register itself ----------------------------------------------------


async def test_every_default_is_a_real_condition() -> None:
    """A default naming a condition nothing raises is a silently dead default."""
    assert set(DEFAULT_NOTIFY_CONDITIONS) <= set(NOTIFY_CONDITIONS)


# -- the boot race ----------------------------------------------------------


async def test_a_push_raised_before_ha_finishes_starting_is_held_not_dropped(
    hass: HomeAssistant,
) -> None:
    """🚨 The bug that would have silenced the flagship alert.

    "This node just took over" is raised during OUR setup -- and on a cold
    standby that is a Home Assistant which began booting seconds ago. The
    `notify.` service the operator chose belongs to another integration that
    may not have loaded yet. Dispatching immediately finds no such service,
    warns about a name that is perfectly correct, and drops the one
    notification the entire feature exists to send.
    """
    hass.set_state(CoreState.starting)
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger2")
    router.async_arm()

    await router.async_check_promotion(leader="tiger2", snapshot_source="tiger1")
    await hass.async_block_till_done()

    # The service only exists now -- exactly the ordering being defended against.
    pushes = async_mock_service(hass, "notify", "tester")
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert len(pushes) == 1, "the promotion alert was dropped to a boot-order race"
    assert "took over from tiger1" in pushes[0].data["message"]


async def test_a_card_is_never_held(hass: HomeAssistant) -> None:
    """persistent_notification is core and up before we are. Holding it would
    delay the one surface that works with no configuration at all."""
    hass.set_state(CoreState.starting)
    router = _router(hass, services=())
    router.async_arm()
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    await hass.async_block_till_done()
    assert any("fileset_degraded" in card for card in _cards(hass))


async def test_nothing_is_held_when_added_to_a_running_instance(
    hass: HomeAssistant, pushes
) -> None:
    """Added to a live Home Assistant: there is nothing to wait for."""
    router = _router(hass, conditions=(NOTIFY_PROMOTED,), node="tiger2")
    router.async_arm()
    await router.async_check_promotion(leader="tiger2", snapshot_source="tiger1")
    await hass.async_block_till_done()
    assert len(pushes) == 1


async def test_the_boot_queue_is_bounded(hass: HomeAssistant, pushes) -> None:
    """A held queue fed from a polling loop must not grow without bound.

    The oldest are kept deliberately: the promotion alert is raised first and
    is the one worth keeping.
    """
    hass.set_state(CoreState.starting)
    router = _router(hass, conditions=tuple(NOTIFY_CONDITIONS))
    router.async_arm()
    for i in range(MAX_PENDING_ALERTS + 10):
        await router._push(f"alert {i}", "m")
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert len(pushes) == MAX_PENDING_ALERTS
    assert pushes[0].data["title"] == "alert 0"


async def test_unloading_before_boot_completes_pushes_nothing(hass: HomeAssistant, pushes) -> None:
    """An entry unloaded mid-boot must not fire from a listener nobody owns."""
    hass.set_state(CoreState.starting)
    router = _router(hass)
    unsub = router.async_arm()
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    unsub()  # what async_unload_entry does with every DATA_UNSUB entry
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert pushes == []


# -- the last resort: even the notification surfaces can fail ---------------


async def test_a_broken_persistent_notification_does_not_propagate(
    hass: HomeAssistant, pushes
) -> None:
    """If even the card cannot be raised, the push must still go.

    The two surfaces exist so that one failing leaves the other. Letting the
    card's failure escape would take the push with it and make the pair
    strictly worse than either alone.
    """
    with patch(
        "homeassistant.components.persistent_notification.async_create",
        side_effect=RuntimeError("notification store is broken"),
    ):
        router = _router(hass)
        await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
        await hass.async_block_till_done()
    assert len(pushes) == 1, "a broken card took the push down with it"


async def test_a_broken_dismiss_does_not_propagate(hass: HomeAssistant, pushes) -> None:
    """Same on the way out: the all-clear matters more than tidying the card."""
    router = _router(hass)
    await router.async_raise(NOTIFY_FILESET_DEGRADED, "Degraded", "stale")
    with patch(
        "homeassistant.components.persistent_notification.async_dismiss",
        side_effect=RuntimeError("notification store is broken"),
    ):
        await router.async_clear(NOTIFY_FILESET_DEGRADED, "Recovered", "fresh")
        await hass.async_block_till_done()
    assert len(pushes) == 2, "a failed dismiss swallowed the all-clear"


# -- AR-0065: the alarm for the failure that has now been silent twice ------


async def test_restored_nothing_is_pushed_by_default(hass: HomeAssistant, pushes) -> None:
    """🚨 It was a WARNING and nothing else, and nobody read it for hours.

    A live cluster failed over into an empty state machine, logged this, and
    every health surface stayed green. A log line has twice proved to be no
    surface at all — once as AR-0040, once as AR-0065.
    """
    from custom_components.cluster_state_sync.const import NOTIFY_RESTORED_NOTHING

    assert NOTIFY_RESTORED_NOTHING in DEFAULT_NOTIFY_CONDITIONS, (
        "the failure this product exists to prevent must be on by default"
    )
    router = _router(hass)
    await router.async_event(
        NOTIFY_RESTORED_NOTHING, "Failover restored NOTHING", "0 of 28 entities"
    )
    await hass.async_block_till_done()
    assert len(pushes) == 1
    assert "28" in pushes[0].data["message"]
