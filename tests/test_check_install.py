"""The guard for the mistake that took the leader down during the v0.4.2 deploy.

🚨 It has to live outside the integration. When a second directory claims our
domain Home Assistant fails to import us entirely, so no runtime check of ours
ever runs — the only place this can be caught is before or after a deploy,
from outside.
"""

from __future__ import annotations

import json
import pathlib

from scripts.check_install import check


def _integration(root: pathlib.Path, dirname: str, domain: str) -> None:
    d = root / "custom_components" / dirname
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"domain": domain}), encoding="utf-8")


def test_a_clean_config_passes(tmp_path: pathlib.Path) -> None:
    _integration(tmp_path, "cluster_state_sync", "cluster_state_sync")
    _integration(tmp_path, "hacs", "hacs")
    assert check(str(tmp_path)) == 0


def test_a_dot_prefixed_backup_is_caught(tmp_path: pathlib.Path) -> None:
    """Exactly what was written on node-a on 2026-09-09.

    A leading dot does not hide a directory from Home Assistant's scan, and the
    copy is a complete integration — manifest included, same domain.
    """
    _integration(tmp_path, "cluster_state_sync", "cluster_state_sync")
    _integration(tmp_path, ".cluster_state_sync.bak-20260909", "cluster_state_sync")
    assert check(str(tmp_path)) == 1


def test_a_renamed_copy_is_flagged_before_it_collides(tmp_path: pathlib.Path) -> None:
    """`.reolink_discovery.disabled-20260825` on the live host is this shape.

    Not a conflict today because nothing else claims that domain — but it is a
    copy of an integration sitting where Home Assistant scans, and a rename or
    a reinstall turns it into one.
    """
    _integration(tmp_path, ".reolink_discovery.disabled-20260825", "reolink_discovery")
    assert check(str(tmp_path)) == 0  # warns, does not fail


def test_a_missing_custom_components_directory_is_reported(tmp_path: pathlib.Path) -> None:
    assert check(str(tmp_path)) == 1


def test_an_unreadable_manifest_is_skipped_not_crashed_on(tmp_path: pathlib.Path) -> None:
    """A half-extracted download must not take the checker down with it."""
    _integration(tmp_path, "cluster_state_sync", "cluster_state_sync")
    broken = tmp_path / "custom_components" / "half_downloaded"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    assert check(str(tmp_path)) == 0
