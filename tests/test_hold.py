"""The maintenance hold: planned downtime that must not become a failover.

Four behaviours have to hold together (hold.py). The promoter's two are in
test_cluster_promoter.py; these are the integration's, plus the flag file's own
fail-safe direction.
"""

from __future__ import annotations

import pathlib

from homeassistant.core import HomeAssistant
import pytest

from custom_components.cluster_state_sync import hold
from custom_components.cluster_state_sync.leadership import LeadershipMonitor
from tests.fakes import FakeBackend

NODE = "tiger1-abc"
PEER = "tiger2-def"


# -- the flag file itself -----------------------------------------------------


def test_no_file_means_no_hold(tmp_path: pathlib.Path) -> None:
    assert hold.is_held(str(tmp_path)) is False


def test_a_file_means_a_hold(tmp_path: pathlib.Path) -> None:
    (tmp_path / hold.HOLD_FILENAME).write_text("upgrading")
    assert hold.is_held(str(tmp_path)) is True
    assert hold.hold_reason(str(tmp_path)) == "upgrading"


def test_an_empty_hold_is_still_a_hold(tmp_path: pathlib.Path) -> None:
    """`touch` is the documented way to set this; a reason is optional."""
    (tmp_path / hold.HOLD_FILENAME).touch()
    assert hold.is_held(str(tmp_path)) is True
    assert hold.hold_reason(str(tmp_path)) == ""


def test_a_directory_is_not_a_hold(tmp_path: pathlib.Path) -> None:
    """Docker creates a directory when asked to bind-mount a missing file, and
    this project has already been bitten by that once. A directory here must
    not silently suspend failover."""
    (tmp_path / hold.HOLD_FILENAME).mkdir()
    assert hold.is_held(str(tmp_path)) is False


def test_an_unreadable_path_reads_as_no_hold(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: letting an OSError
    propagate, or treating it as held.

    A hold that switches itself on is a cluster that has quietly stopped
    failing over -- strictly worse than the bug the hold was written to fix.
    """
    assert hold.is_held(str(tmp_path / "does" / "not" / "exist")) is False


# -- the integration's follower behaviour ------------------------------------


@pytest.mark.parametrize("held", [False, True])
async def test_a_leader_keeps_its_lease_either_way(
    hass: HomeAssistant, tmp_path: pathlib.Path, held: bool
) -> None:
    """The hold changes what a node will *take*, never what it may keep."""
    if held:
        (tmp_path / hold.HOLD_FILENAME).touch()
    backend = FakeBackend()
    backend.lease_holder = NODE
    monitor = LeadershipMonitor(hass, backend, NODE, source="lease", config_dir=str(tmp_path))
    assert await monitor.async_is_leader() is True


async def test_a_held_follower_does_not_take_a_free_lease(
    hass: HomeAssistant,
    tmp_path: pathlib.Path,
) -> None:
    """The behaviour that matters: while the peer restarts, its lease may sit
    free for a moment. Taking it is the failover the hold exists to stop."""
    (tmp_path / hold.HOLD_FILENAME).touch()
    backend = FakeBackend()
    backend.lease_holder = None
    monitor = LeadershipMonitor(hass, backend, NODE, source="lease", config_dir=str(tmp_path))
    assert await monitor.async_is_leader() is False
    assert backend.lease_holder is None, "the free lease must be left alone"


async def test_an_unheld_follower_does_take_a_free_lease(
    hass: HomeAssistant,
    tmp_path: pathlib.Path,
) -> None:
    """The control: without a hold this is ordinary failover, and must still
    work. A test that only proved the hold blocks things could pass against an
    integration that had stopped leading altogether."""
    backend = FakeBackend()
    backend.lease_holder = None
    monitor = LeadershipMonitor(hass, backend, NODE, source="lease", config_dir=str(tmp_path))
    assert await monitor.async_is_leader() is True
    assert backend.lease_holder == NODE


async def test_a_held_follower_still_refuses_the_peers_lease(
    hass: HomeAssistant,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / hold.HOLD_FILENAME).touch()
    backend = FakeBackend()
    backend.lease_holder = PEER
    monitor = LeadershipMonitor(hass, backend, NODE, source="lease", config_dir=str(tmp_path))
    assert await monitor.async_is_leader() is False
    assert backend.lease_holder == PEER


async def test_the_hold_is_ignored_for_non_lease_leadership(
    hass: HomeAssistant,
    tmp_path: pathlib.Path,
) -> None:
    """`always` and `entity` have no lease to renew, so the hold has nothing to
    say about them -- and must not accidentally demote a single-node install."""
    (tmp_path / hold.HOLD_FILENAME).touch()
    monitor = LeadershipMonitor(
        hass, FakeBackend(), NODE, source="always", config_dir=str(tmp_path)
    )
    assert await monitor.async_is_leader() is True
