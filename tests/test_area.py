"""Filing the cluster device under its own area.

Most of what this integration creates is diagnostic and stays off the default
dashboard. The switches are deliberately not diagnostic — a control buried in
diagnostics is a control nobody finds — so they surface among the lamps unless
something files them somewhere meaningful.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.cluster_state_sync import area


class _Devices:
    def __init__(self, device):
        self._device = device
        self.updated: dict = {}

    def async_get_device(self, identifiers):  # noqa: ARG002
        return self._device

    def async_update_device(self, device_id, **kwargs):
        self.updated = {"device_id": device_id, **kwargs}


class _Areas:
    def __init__(self, existing=None):
        self._existing = existing
        self.created: list[str] = []

    def async_get_area_by_name(self, name):
        return self._existing

    def async_create(self, name, icon=None):  # noqa: ARG002
        self.created.append(name)
        return SimpleNamespace(id="area_new", name=name)


def _run(hass, entry, devices, areas, monkeypatch):
    monkeypatch.setattr(area.dr, "async_get", lambda _h: devices)
    monkeypatch.setattr(area.ar, "async_get", lambda _h: areas)
    return asyncio.run(area.async_assign_area(hass, entry))


def test_an_unfiled_device_is_put_in_the_cluster_area(monkeypatch) -> None:
    devices = _Devices(SimpleNamespace(id="dev1", area_id=None))
    areas = _Areas(existing=None)
    result = _run(object(), SimpleNamespace(entry_id="e1"), devices, areas, monkeypatch)
    assert result == "area_new"
    assert areas.created == [area.AREA_NAME]
    assert devices.updated["area_id"] == "area_new"


def test_an_existing_area_is_reused_not_duplicated(monkeypatch) -> None:
    """Two nodes, two config entries, one area — not 'Cluster' and 'Cluster 2'."""
    devices = _Devices(SimpleNamespace(id="dev1", area_id=None))
    areas = _Areas(existing=SimpleNamespace(id="area_existing", name=area.AREA_NAME))
    result = _run(object(), SimpleNamespace(entry_id="e1"), devices, areas, monkeypatch)
    assert result == "area_existing"
    assert areas.created == [], "must not create a second area with the same name"


def test_an_operator_s_own_area_is_never_overridden(monkeypatch) -> None:
    """Someone who filed this under 'Loft' or 'Servers' meant it.

    An integration that quietly moved things back on every restart would be
    worse than one that never helped at all.
    """
    devices = _Devices(SimpleNamespace(id="dev1", area_id="their_choice"))
    areas = _Areas(existing=None)
    result = _run(object(), SimpleNamespace(entry_id="e1"), devices, areas, monkeypatch)
    assert result is None
    assert devices.updated == {}, "the device must not be touched"
    assert areas.created == []


def test_a_missing_device_is_not_an_error(monkeypatch) -> None:
    """Platforms may not have created it yet. Nothing to file is not a failure."""
    devices = _Devices(None)
    areas = _Areas(existing=None)
    assert _run(object(), SimpleNamespace(entry_id="e1"), devices, areas, monkeypatch) is None


def test_failure_is_swallowed_so_setup_survives(monkeypatch) -> None:
    """Tidying a device list must never stop the cluster starting."""

    def _boom(_hass):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(area.dr, "async_get", _boom)
    assert asyncio.run(area.async_assign_area(object(), SimpleNamespace(entry_id="e1"))) is None
