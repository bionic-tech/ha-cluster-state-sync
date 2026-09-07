"""The radio liveness sensor (GOTCHAS §18).

The blind spot: the D3 promotion probe asks Home Assistant whether it is alive,
and Home Assistant is perfectly capable of being alive while every radio behind
it is dead. On this fleet a Zigbee daemon livelocked -- 78% CPU, no log output
for 32 minutes, its healthcheck still reporting `healthy` -- and the house lost
Zigbee with nothing anywhere marking the cluster degraded.

The sensor does not fix that. It makes the silence a number an operator can see
and threshold. These tests guard the two properties that decide whether it is
trustworthy or merely decorative: it must never report a comforting value it
cannot justify, and it must not exist at all when it has nothing to watch.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync.const import CONF_RADIO_WATCH, DOMAIN
from custom_components.cluster_state_sync.sensor import RadioSilenceSensor


def _sensor(hass: HomeAssistant, patterns: list[str]) -> RadioSilenceSensor:
    entry = MockConfigEntry(domain=DOMAIN, title="cluster", data={})
    entry.add_to_hass(hass)
    sensor = RadioSilenceSensor(MagicMock(), entry, patterns)
    sensor.hass = hass
    return sensor


async def test_reports_the_age_of_the_freshest_match(hass: HomeAssistant) -> None:
    """The freshest, not the oldest.

    Any single radio still reporting means the path is alive. Taking the oldest
    would alarm on the one dead battery sensor in a working house, and an alarm
    that cries wolf is the same as no alarm at all.
    """
    hass.states.async_set("sensor.hall_rssi_numeric", "-70")
    await hass.async_block_till_done()

    sensor = _sensor(hass, ["sensor.*_rssi_numeric"])
    assert sensor.native_value == 0

    # Nothing heard for two minutes.
    later = dt_util.utcnow() + timedelta(seconds=120)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dt_util, "utcnow", lambda: later)
        assert sensor.native_value == 120


async def test_an_unmatched_glob_is_unknown_never_zero(hass: HomeAssistant) -> None:
    """Zero would read as 'heard something just now'. It is the opposite.

    A typo in the glob, a renamed entity, an integration that failed to load --
    each leaves this watching nothing. Reporting 0 there would show a perfectly
    healthy green number for a check that is not running, which is precisely
    the AR-0040 failure this project exists because of.
    """
    sensor = _sensor(hass, ["sensor.no_such_thing_*"])
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["entities_matched"] == 0


async def test_the_attributes_say_what_is_actually_being_watched(
    hass: HomeAssistant,
) -> None:
    """An operator has to be able to check the glob without re-reading the wizard.

    `entities_matched` is the number that catches the silent misconfiguration:
    a plausible-looking pattern matching nothing looks identical to a working
    one until this attribute is read.
    """
    hass.states.async_set("sensor.a_rssi_numeric", "-60")
    hass.states.async_set("sensor.b_rssi_numeric", "-61")
    hass.states.async_set("sensor.unrelated_battery", "90")
    await hass.async_block_till_done()

    sensor = _sensor(hass, ["sensor.*_rssi_numeric"])
    attrs = sensor.extra_state_attributes
    assert attrs["entities_matched"] == 2
    assert attrs["watching"] == ["sensor.*_rssi_numeric"]


async def test_several_globs_are_all_watched(hass: HomeAssistant) -> None:
    """A house has more than one radio, and they prove liveness differently.

    RFXtrx exposes signal strength per device; Z-Wave exposes a last-seen
    timestamp; a Zigbee coordinator exposes its own diagnostic. Restricting
    this to one pattern would mean watching one radio and calling it the house.
    """
    hass.states.async_set("sensor.rfx_rssi_numeric", "-70")
    hass.states.async_set("sensor.zigbee_coordinator_lqi", "180")
    await hass.async_block_till_done()

    sensor = _sensor(hass, ["sensor.*_rssi_numeric", "sensor.*_lqi"])
    assert sensor.extra_state_attributes["entities_matched"] == 2


def test_a_list_of_blanks_turns_the_feature_off() -> None:
    """Half-configured must resolve to off, not to a sensor that never reports.

    The wizard field takes free text, so a stray empty entry is ordinary. An
    entity that exists, looks configured, and reports `unknown` forever is the
    shape of check this repo keeps being bitten by -- present, green-adjacent,
    and measuring nothing.
    """
    from custom_components.cluster_state_sync.sensor import watch_patterns

    assert watch_patterns({}) == []
    assert watch_patterns({CONF_RADIO_WATCH: ["", "  "]}) == []
    assert watch_patterns({CONF_RADIO_WATCH: [" sensor.a_* ", ""]}) == ["sensor.a_*"]


async def test_a_chatty_wifi_chip_would_mask_a_dead_rf_radio(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Freshest-wins is right within a radio and wrong across radios.

    This is not hypothetical. On the fleet this was built for, `sensor.*_rssi`
    sweeps in a WLED and two Sonoff Wi-Fi RSSI sensors alongside the 39 RFXtrx
    ones -- and a Wi-Fi chip reporting every 60 seconds holds the number near
    zero through a completely dead RFXtrx. Watching more entities looks safer
    and is the exact opposite.

    The code cannot enforce this; only the operator knows which entity belongs
    to which radio. So this test pins the behaviour that makes the warning in
    the wizard and the docstring worth writing: the masking is real, and it is
    silent.
    """
    hass.states.async_set("sensor.rfx_thing_rssi_numeric", "-70")
    hass.states.async_set("sensor.wled_wi_fi_rssi", "-40")
    await hass.async_block_till_done()

    mixed = _sensor(hass, ["sensor.*_rssi_numeric", "sensor.*_rssi"])
    rf_only = _sensor(hass, ["sensor.*_rssi_numeric"])

    # Fifteen minutes pass. The RF radio says nothing; the Wi-Fi chip reports.
    freezer.tick(timedelta(seconds=900))
    hass.states.async_set("sensor.wled_wi_fi_rssi", "-41")
    await hass.async_block_till_done()

    assert mixed.native_value == 0, "the Wi-Fi sensor masked the dead RF radio"
    assert rf_only.native_value == 900, "watching one radio sees the silence"


async def test_a_radio_repeating_itself_is_not_a_silent_radio(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """`last_changed` does not move when a sensor reports the same value again.

    The question this sensor asks is "did we hear a packet", and a radio
    re-reporting -70 dBm for the tenth time has been heard from. Keying on
    `last_changed` would show a steadily rising silence for a radio in
    continuous, healthy reception -- a false alarm, which costs the sensor
    exactly the trust it exists to earn.

    On this fleet `last_changed` and `last_reported` happen to agree today (0
    of 37 entities diverge, checked 2026-09-07). That is a property of one
    integration on one installation, not of Home Assistant, and it is not
    something to build on.
    """
    hass.states.async_set("sensor.rfx_rssi_numeric", "-70")
    await hass.async_block_till_done()
    sensor = _sensor(hass, ["sensor.*_rssi_numeric"])

    freezer.tick(timedelta(seconds=300))
    hass.states.async_set("sensor.rfx_rssi_numeric", "-70")  # same value, new packet
    await hass.async_block_till_done()

    assert sensor.native_value == 0, "a repeated reading is still a packet received"


async def test_an_unknown_entity_is_not_evidence_of_reception(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The bug this sensor shipped with, pinned so it cannot come back.

    Home Assistant stamps `last_reported` on an entity whose state is
    `unknown` exactly as it does on a real reading. The first version of this
    sensor therefore reported a confident 60 s, 120 s, 191 s on a live fleet
    while all 37 watched entities sat at `unknown` and the RFXtrx receivers
    had heard nothing for thirty hours. The number was Home Assistant's own
    state writes keeping time with themselves.

    It read exactly like a healthy radio, which makes it worse than no sensor
    at all -- and it is the same shape as AR-0040, arriving inside the very
    thing built to catch that shape.
    """
    hass.states.async_set("sensor.rfx_a_rssi_numeric", "unknown")
    hass.states.async_set("sensor.rfx_b_rssi_numeric", "unavailable")
    await hass.async_block_till_done()
    sensor = _sensor(hass, ["sensor.*_rssi_numeric"])

    freezer.tick(timedelta(seconds=120))
    # HA rewrites the unknown states -- a restore, a reload, a restart.
    hass.states.async_set("sensor.rfx_a_rssi_numeric", "unknown")
    await hass.async_block_till_done()

    assert sensor.native_value is None, "an unknown entity was counted as a reading"
    attrs = sensor.extra_state_attributes
    assert attrs["status"] == "no_reports"
    assert attrs["entities_matched"] == 2
    assert attrs["entities_reporting"] == 0


async def test_the_three_states_are_distinguishable(hass: HomeAssistant) -> None:
    """`unknown` covers two very different problems; the attribute separates them.

    Globs that match nothing is a configuration mistake the operator made.
    Globs that match entities which have never reported is a **deaf radio** --
    the loudest condition this sensor can find. Both read `unknown`, because
    "never" has no age, so the attribute has to carry the difference.
    """
    hass.states.async_set("sensor.rfx_live_rssi_numeric", "-70")
    hass.states.async_set("sensor.rfx_dead_rssi_numeric", "unknown")
    await hass.async_block_till_done()

    assert _sensor(hass, ["sensor.nothing_here_*"]).extra_state_attributes["status"] == (
        "no_matches"
    )
    assert _sensor(hass, ["sensor.rfx_dead_*"]).extra_state_attributes["status"] == ("no_reports")
    ok = _sensor(hass, ["sensor.*_rssi_numeric"])
    assert ok.extra_state_attributes["status"] == "ok"
    assert ok.extra_state_attributes["entities_reporting"] == 1
    assert ok.native_value == 0
