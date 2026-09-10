"""The go-bag really does carry what the configuration references.

`sensor.UnreplicatedReferencesSensor` spent six days reporting `12` on the very
estate it was written for, long after the fix it was measuring had landed. It
tested referenced paths against `REPLICATED_DIRS`/`REPLICATED_FILES` alone —
but those are a floor, and `fileset._candidates` also walks `includes.scan` and
adds everything it finds. Every path it flagged was already in the go-bag.

An amber warning that is always wrong is worse than none, because it teaches an
operator to ignore the colour. These tests pin the invariant the sensor should
have been asserting all along.
"""

from __future__ import annotations

import pathlib

from custom_components.cluster_state_sync.fileset import (
    REPLICATED_DIRS,
    REPLICATED_FILES,
    _candidates,
)
from custom_components.cluster_state_sync.includes import scan as scan_includes


def _estate(root: pathlib.Path) -> None:
    """A config split the way this fleet's actually is.

    `configs/*.yaml`, `packages/`, `themes/` and a credential under `secure/` —
    none of which appear in the fixed floor. This is the shape that broke a
    promotion on 2026-09-04.
    """
    (root / "configs").mkdir()
    (root / "packages").mkdir()
    (root / "themes").mkdir()
    (root / "secure").mkdir()
    (root / "configuration.yaml").write_text(
        "sensor: !include configs/sensor.yaml\n"
        "switch: !include configs/switches.yaml\n"
        "homeassistant:\n"
        "  packages: !include_dir_named packages\n"
        "frontend:\n"
        "  themes: !include_dir_merge_named themes\n",
        encoding="utf-8",
    )
    (root / "configs" / "sensor.yaml").write_text("[]\n", encoding="utf-8")
    (root / "configs" / "switches.yaml").write_text("[]\n", encoding="utf-8")
    (root / "packages" / "network_fault_recovery.yaml").write_text("{}\n", encoding="utf-8")
    (root / "themes" / "dark.yaml").write_text("{}\n", encoding="utf-8")
    (root / "secure" / "creds.json").write_text("{}\n", encoding="utf-8")


def test_everything_the_scan_resolves_is_in_the_go_bag(tmp_path: pathlib.Path) -> None:
    """🚨 The invariant the broken sensor contradicted.

    If this ever fails, the sensor's old floor-only test was right after all and
    a promoted node really would be missing files. Far more likely: someone
    changed `_candidates` to stop following includes.
    """
    _estate(tmp_path)

    scanned = scan_includes(str(tmp_path))
    carried = {rel for rel, _ in _candidates(tmp_path)}

    for rel in sorted(scanned.paths):
        target = tmp_path / rel
        if target.is_dir():
            # A referenced directory is carried as its files.
            assert any(c.startswith(f"{rel}/") for c in carried), (
                f"referenced directory {rel!r} contributed no files to the go-bag"
            )
        else:
            assert rel in carried, f"referenced file {rel!r} is not in the go-bag"


def test_the_floor_alone_would_have_missed_all_of_them(tmp_path: pathlib.Path) -> None:
    """Proves the fixture is a real test of include-following, not a tautology.

    Without this, a `_candidates` that quietly reverted to the floor could still
    pass the test above if the fixture only used floor paths.
    """
    _estate(tmp_path)

    scanned = scan_includes(str(tmp_path))
    floor_only = {
        p for p in scanned.paths if p.split("/")[0] in REPLICATED_DIRS or p in REPLICATED_FILES
    }

    assert scanned.paths - floor_only, (
        "the fixture references nothing outside the fixed floor, so it cannot "
        "detect a regression in include-following"
    )


def test_a_reference_outside_the_config_dir_is_a_real_gap(tmp_path: pathlib.Path) -> None:
    """What the sensor should have been counting: what genuinely cannot travel."""
    root = tmp_path / "config"
    root.mkdir()
    outsider = tmp_path / "elsewhere.yaml"
    outsider.write_text("[]\n", encoding="utf-8")
    (root / "configuration.yaml").write_text(f"sensor: !include {outsider}\n", encoding="utf-8")

    scanned = scan_includes(str(root))

    assert scanned.outside, "a reference escaping the config dir must be reported"
    assert not any("elsewhere.yaml" in rel for rel in scanned.paths), (
        "an escaping reference must not be presented as a replicable path"
    )


def test_a_reference_to_a_file_that_is_not_there_is_a_real_gap(tmp_path: pathlib.Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "sensor: !include configs/absent.yaml\n", encoding="utf-8"
    )

    scanned = scan_includes(str(tmp_path))

    assert scanned.missing, "a reference to a missing file must be reported"
