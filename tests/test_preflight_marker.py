"""The pre-flight's silence, which was the whole point of the pre-flight.

`ha_device_preflight.py` disables config entries whose hardware is absent so a
promoted node comes up rather than failing at setup. That is the right call and
this file does not argue with it.

🚨 What it did NOT do was say so. A promoted node missing three radios looked
identical, from every surface this integration offers, to a healthy one — and
the failure that shape produces is somebody buying a second transceiver for the
standby, getting the device path wrong (it embeds the unit's serial, so a
different physical unit never matches), promoting, and being told nothing.
They find out when they need the radio.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync.const import (
    DOMAIN,
    NOTIFY_DEVICES_DISABLED,
    PREFLIGHT_MARKER_NAME,
)
from custom_components.cluster_state_sync.scripts.ha_device_preflight import (
    Plan,
    _write_marker,
)
from tests.fakes import FakeBackend


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": "s" * 44,
            "snapshot_interval": 30,
            "fileset_enabled": False,
        },
    )


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = _entry(hass)
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _clear(hass: HomeAssistant) -> None:
    """Remove any marker left by another test.

    The harness can hand successive tests the same config directory, so a
    marker written by one becomes ambient state for the next — exactly the
    order-dependence that made a leadership test flap earlier today. Say what
    the filesystem should look like rather than inheriting it.
    """
    pathlib.Path(hass.config.path(".storage", PREFLIGHT_MARKER_NAME)).unlink(missing_ok=True)


def _write(hass: HomeAssistant, payload: dict) -> None:
    p = pathlib.Path(hass.config.path(".storage", PREFLIGHT_MARKER_NAME))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


# -- the host-side half -----------------------------------------------------


def test_the_preflight_records_what_it_switched_off(tmp_path: pathlib.Path) -> None:
    plan = Plan(
        to_disable=["e1"],
        details=[("e1", "rfxtrx", "/dev/serial/by-id/usb-RFXCOM_RFXtrx433_A1Z5EIPE-if00-port0")],
        present_count=2,
        untouched_count=7,
    )
    _write_marker(tmp_path, plan)

    marker = json.loads((tmp_path / PREFLIGHT_MARKER_NAME).read_text())
    assert marker["disabled"][0]["domain"] == "rfxtrx"
    assert "A1Z5EIPE" in marker["disabled"][0]["device"]


def test_applying_the_plan_writes_the_marker(tmp_path: pathlib.Path) -> None:
    """🚨 Through `apply_plan`, not by calling `_write_marker` directly.

    A test that calls the writer itself passes even when nothing calls the
    writer — which is precisely what a mutation check found here: removing the
    call from `apply_plan` left the direct test green. The wiring is the part
    that can rot.
    """
    from custom_components.cluster_state_sync.scripts.ha_device_preflight import apply_plan

    (tmp_path / "core.config_entries").write_text(
        json.dumps({"data": {"entries": [{"entry_id": "e1", "domain": "rfxtrx"}]}})
    )
    apply_plan(
        tmp_path,
        Plan(to_disable=["e1"], details=[("e1", "rfxtrx", "/dev/x")], present_count=0),
    )

    assert (tmp_path / PREFLIGHT_MARKER_NAME).exists(), (
        "apply_plan disabled an entry and left no record — the node comes up "
        "missing hardware and nothing says so"
    )
    entries = json.loads((tmp_path / "core.config_entries").read_text())["data"]["entries"]
    assert entries[0]["disabled_by"] == "integration", "the entry was not actually disabled"


def test_a_promotion_that_disabled_nothing_removes_the_marker(tmp_path: pathlib.Path) -> None:
    """🚨 A stale marker outliving the problem is how an alert stops being read.

    The radios came back; saying they are still missing trains the operator to
    ignore the next one.
    """
    (tmp_path / PREFLIGHT_MARKER_NAME).write_text('{"disabled": [{"domain": "rfxtrx"}]}')
    _write_marker(tmp_path, Plan(to_disable=[], details=[], present_count=3, untouched_count=8))
    assert not (tmp_path / PREFLIGHT_MARKER_NAME).exists()


def test_an_unwritable_marker_does_not_abort_the_promotion(tmp_path: pathlib.Path) -> None:
    """Degraded beats not starting — the pre-flight's own rule, applied to itself."""
    plan = Plan(to_disable=["e1"], details=[("e1", "rfxtrx", "/dev/x")], present_count=0)
    _write_marker(tmp_path / "does" / "not" / "exist", plan)  # must not raise


# -- the integration half ---------------------------------------------------


async def test_a_disabled_device_raises_a_repair_and_pushes(hass: HomeAssistant) -> None:
    """The failure this whole file exists for: coming up short and saying nothing."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    pushes = async_mock_service(hass, "notify", "tester")
    _write(
        hass,
        {
            "disabled": [
                {
                    "entry_id": "e1",
                    "domain": "rfxtrx",
                    "device": "/dev/serial/by-id/usb-X_A1Z5-if00",
                }
            ]
        },
    )
    entry = _entry(hass)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"notify_services": ["notify.tester"]})
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, "devices_disabled") is not None
    assert any("rfxtrx" in c.data.get("message", "") for c in pushes), (
        "the node came up without hardware and nobody was told"
    )


async def test_no_marker_means_no_alarm(hass: HomeAssistant) -> None:
    """The ordinary case must cost nothing and say nothing."""
    _clear(hass)
    await _setup(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "devices_disabled") is None


async def test_an_empty_disabled_list_clears_a_previous_alarm(hass: HomeAssistant) -> None:
    """The issue registry is storage-backed, so raising is only half the job."""
    _write(hass, {"disabled": []})
    await _setup(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "devices_disabled") is None


async def test_an_unreadable_marker_is_reported_not_silently_ignored(
    hass: HomeAssistant, caplog
) -> None:
    """🚨 A marker that exists and cannot be read is NOT the same as no marker.

    Treating them alike is how the alarm this file adds gets lost again, by the
    one route nobody would look at.
    """
    p = pathlib.Path(hass.config.path(".storage", PREFLIGHT_MARKER_NAME))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    await _setup(hass)
    assert "Could not read the device pre-flight marker" in caplog.text


def test_the_condition_is_pushed_by_default() -> None:
    """Losing radios changes what the house can do. That earns an interruption."""
    from custom_components.cluster_state_sync.const import DEFAULT_NOTIFY_CONDITIONS

    assert NOTIFY_DEVICES_DISABLED in DEFAULT_NOTIFY_CONDITIONS


# --------------------------------------------------------------------------
# A clean pre-flight must clear the PREVIOUS marker (GOTCHAS 31)
# --------------------------------------------------------------------------


def _storage_with(tmp_path: pathlib.Path, entries: list[dict]) -> pathlib.Path:
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text(
        json.dumps({"data": {"entries": entries}}), encoding="utf-8"
    )
    return storage


def _run_preflight(storage: pathlib.Path, by_id: pathlib.Path, apply: bool) -> int:
    from custom_components.cluster_state_sync.scripts.ha_device_preflight import main

    argv = ["--storage", str(storage), "--by-id", str(by_id)]
    if apply:
        argv.append("--apply")
    return main(argv)


def test_a_clean_preflight_clears_a_marker_left_by_the_peer(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 The marker that outlives its problem is usually not this node's own.

    The go-bag carries `.storage` wholesale, so a promotion installs the
    PEER's marker along with everything else, and the promoted node then
    reports the peer's missing radios as its own.

    Observed on the reference pair 2026-09-20: node-a held all five devices
    with every config entry enabled, and raised `devices_disabled` describing
    node-b's promotion six hours earlier. Its own pre-flight had said
    "Every referenced device is present. Nothing to disable" -- and then
    returned without touching the marker, because `_write_marker` is reached
    through `apply_plan`, which a clean run never calls.

    An alarm that is wrong often enough stops being read at all, which costs
    you the time it is right.
    """
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    device = by_id / "usb-RADIO_present-if00-port0"
    device.write_text("")  # present, so there is nothing to disable

    storage = _storage_with(
        tmp_path,
        [{"entry_id": "abc", "domain": "rfxtrx", "disabled_by": None,
          "data": {"device": str(device)}}],
    )

    # The peer's marker, arriving via the go-bag.
    stale = storage / PREFLIGHT_MARKER_NAME
    stale.write_text(
        json.dumps({"disabled_at": "20260918T104916", "present_count": 6,
                    "untouched_count": 149,
                    "disabled": [{"entry_id": "other", "domain": "rfxtrx",
                                  "device": "/dev/serial/by-id/usb-RADIO_peer-if00-port0"}]}),
        encoding="utf-8",
    )
    assert stale.exists()

    assert _run_preflight(storage, by_id, apply=True) == 0
    assert not stale.exists(), (
        "a clean pre-flight left the peer's marker standing; this node will "
        "report the peer's missing radios as its own"
    )


def test_a_dry_run_never_touches_the_marker(tmp_path: pathlib.Path) -> None:
    """A dry run changes nothing, the marker included.

    Without the `--apply` guard, merely *inspecting* a node would clear a
    genuine record of a degraded promotion.
    """
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    device = by_id / "usb-RADIO_present-if00-port0"
    device.write_text("")

    storage = _storage_with(
        tmp_path,
        [{"entry_id": "abc", "domain": "rfxtrx", "disabled_by": None,
          "data": {"device": str(device)}}],
    )
    marker = storage / PREFLIGHT_MARKER_NAME
    marker.write_text(json.dumps({"disabled_at": "x", "disabled": []}), encoding="utf-8")

    assert _run_preflight(storage, by_id, apply=False) == 0
    assert marker.exists(), "a dry run removed the marker"
