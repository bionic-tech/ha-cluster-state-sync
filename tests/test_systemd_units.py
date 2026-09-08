"""The units in `deploy/systemd/` must match what the bundle generates.

These files run as root on every cluster host and decide when a house fails
over. They are checked in so they can be read and reviewed without running the
wizard -- which means they can also fall silently behind the generator, and a
stale copy of a unit is worse than no copy: it invites someone to "restore" a
host to it.

This test is the drift check that makes keeping them here safe.
"""

from __future__ import annotations

import pathlib

import pytest

from custom_components.cluster_state_sync import bundle

REFERENCE = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "systemd"

#: The config the reference copies are generated from. Pinned here and
#: described in `deploy/systemd/README.md` so the two cannot disagree.
REFERENCE_CFG = {
    "ha_container": "homeassistant",
    "ha_config_path": "/config",
    "redis_host": "valkey.lan",
    "redis_port": 6379,
    "redis_db": 2,
    "cluster_namespace": "prod",
    "cluster_secret": "s" * 44,
    "node_id": "node-a",
    "topology_model": "cold",
    "leadership_source": "lease",
    "fileset_enabled": True,
    "statistics_enabled": True,
}


def _generated() -> dict[str, str]:
    return {
        name: body
        for name, body in bundle.build_bundle(REFERENCE_CFG).items()
        if name.endswith((".service", ".timer"))
    }


def test_every_generated_unit_has_a_reference_copy() -> None:
    """A new unit that nobody checked in is a root-level artefact nobody read."""
    missing = set(_generated()) - {p.name for p in REFERENCE.glob("*")}
    assert not missing, (
        f"units generated but not checked in: {sorted(missing)}. Run "
        "`pytest tests/test_systemd_units.py --regenerate` or copy them into "
        "deploy/systemd/."
    )


def test_no_reference_copy_outlives_its_generator() -> None:
    """The opposite drift: a unit removed from the bundle but still on disk here,
    where an operator could find it and install a service nothing maintains."""
    stale = {p.name for p in REFERENCE.glob("*.service")} | {
        p.name for p in REFERENCE.glob("*.timer")
    }
    assert not (stale - set(_generated())), (
        f"reference copies with no generator: {sorted(stale - set(_generated()))}"
    )


@pytest.mark.parametrize("name", sorted(_generated()))
def test_the_reference_copy_matches_the_generator(name: str) -> None:
    """🚨 The whole point. A checked-in unit that no longer matches the code is
    an invitation to restore a host to a configuration this project abandoned."""
    assert (REFERENCE / name).read_text(encoding="utf-8") == _generated()[name], (
        f"deploy/systemd/{name} has drifted from bundle.py. These are generated, "
        "not authored: change the emitting function, then regenerate."
    )


def test_no_unit_reference_carries_a_secret() -> None:
    """`/etc/cluster-sync/` holds the fileset key and the Valkey password.

    Units name paths, never contents -- but this is the test that keeps it that
    way, because the cost of learning otherwise is a credential in git history.
    """
    for path in sorted(REFERENCE.glob("*")):
        body = path.read_text(encoding="utf-8")
        for marker in ("PASSWORD=", "SECRET=", "-----BEGIN", "cluster-fileset.key\n"):
            assert marker not in body, f"{path.name} appears to carry a secret ({marker!r})"
