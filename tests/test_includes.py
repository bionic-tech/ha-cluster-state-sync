"""Following the configuration's includes, rather than guessing at a list.

The live failover test on 2026-09-04 promoted node-b perfectly — right
identity, 20 refresh tokens, mobile app connected — to a Home Assistant in
recovery mode, because `configs/customize.yaml` had never been replicated. The
allow-list could not know how the operator had split their configuration up.
"""

from __future__ import annotations

import pathlib

from custom_components.cluster_state_sync import includes


def _write(root: pathlib.Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_a_plain_include_is_followed(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "configuration.yaml", "sensor: !include configs/sensor.yaml\n")
    _write(tmp_path, "configs/sensor.yaml", "a: 1\n")
    assert includes.scan(str(tmp_path)).paths == {"configs/sensor.yaml"}


def test_includes_nest(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: scanning only the root file.

    `packages/` routinely includes further files, so a one-level scan finds the
    directory and misses everything its contents reference.
    """
    _write(tmp_path, "configuration.yaml", "a: !include one.yaml\n")
    _write(tmp_path, "one.yaml", "b: !include two.yaml\n")
    _write(tmp_path, "two.yaml", "c: !include three.yaml\n")
    _write(tmp_path, "three.yaml", "d: 1\n")
    assert includes.scan(str(tmp_path)).paths == {"one.yaml", "two.yaml", "three.yaml"}


def test_all_four_directory_forms_are_recognised(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: handling `!include` only.

    A form missing from the tag list is a directory that silently does not
    replicate — which is the whole failure being fixed here.
    """
    body = "".join(f"k{i}: {tag} d{i}\n" for i, tag in enumerate(includes.INCLUDE_DIR_TAGS))
    _write(tmp_path, "configuration.yaml", body)
    for i in range(len(includes.INCLUDE_DIR_TAGS)):
        _write(tmp_path, f"d{i}/x.yaml", "a: 1\n")

    found = includes.scan(str(tmp_path)).paths
    for i in range(len(includes.INCLUDE_DIR_TAGS)):
        assert f"d{i}" in found, f"{includes.INCLUDE_DIR_TAGS[i]} was not followed"


def test_files_inside_an_included_directory_are_followed(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "configuration.yaml", "p: !include_dir_named packages\n")
    _write(tmp_path, "packages/one.yaml", "a: !include ../configs/shared.yaml\n")
    _write(tmp_path, "configs/shared.yaml", "b: 1\n")
    found = includes.scan(str(tmp_path)).paths
    assert "packages" in found
    assert "configs/shared.yaml" in found


def test_an_include_outside_the_config_dir_is_reported_not_dropped(
    tmp_path: pathlib.Path,
) -> None:
    """Home Assistant's loader has no containment check — only `!secret` is
    restricted — so this resolves. It cannot be replicated, and saying so is
    the point: silently dropping references is the bug being fixed.
    """
    root = tmp_path / "config"
    root.mkdir()
    (tmp_path / "outside.yaml").write_text("a: 1\n")
    _write(root, "configuration.yaml", "x: !include ../outside.yaml\n")

    result = includes.scan(str(root))
    assert result.paths == set()
    assert len(result.outside) == 1
    assert result.has_problems


def test_a_cycle_terminates(tmp_path: pathlib.Path) -> None:
    """A configuration is a graph. Without a visited set this hangs the
    publisher, which runs every 60 seconds on the leader."""
    _write(tmp_path, "configuration.yaml", "a: !include one.yaml\n")
    _write(tmp_path, "one.yaml", "b: !include configuration.yaml\n")
    assert "one.yaml" in includes.scan(str(tmp_path)).paths


def test_unknown_tags_do_not_break_the_scan(tmp_path: pathlib.Path) -> None:
    """`!secret` and `!env_var` are everywhere. A scan that raised on them
    would never run against a real configuration."""
    _write(
        tmp_path,
        "configuration.yaml",
        "tz: !secret time_zone\nenv: !env_var HOME\nx: !include one.yaml\n",
    )
    _write(tmp_path, "one.yaml", "a: 1\n")
    result = includes.scan(str(tmp_path))
    assert result.paths == {"one.yaml"}
    assert not result.unreadable


def test_a_broken_file_is_recorded_not_raised(tmp_path: pathlib.Path) -> None:
    """One unparseable file must cost one entry, not the whole scan — the same
    posture as AR-0013's per-entry restore guards."""
    _write(tmp_path, "configuration.yaml", "a: !include good.yaml\nb: !include bad.yaml\n")
    _write(tmp_path, "good.yaml", "x: 1\n")
    _write(tmp_path, "bad.yaml", "this: [is: not: valid\n")
    result = includes.scan(str(tmp_path))
    assert "good.yaml" in result.paths
    assert len(result.unreadable) == 1


def test_a_missing_target_is_recorded(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "configuration.yaml", "a: !include absent.yaml\n")
    result = includes.scan(str(tmp_path))
    assert len(result.missing) == 1
    assert result.has_problems


def test_the_fleet_shape_is_covered(tmp_path: pathlib.Path) -> None:
    """node-a's actual layout, which recovery mode was caused by.

    Every one of these was referenced and none replicated: twelve
    `configs/*.yaml`, `themes/`, `packages/`, and a Google service-account
    credential under `secure/` that nobody had noticed at all.
    """
    _write(
        tmp_path,
        "configuration.yaml",
        "homeassistant:\n"
        "  customize: !include configs/customize.yaml\n"
        "sensor: !include configs/sensor.yaml\n"
        "themes: !include_dir_merge_named themes\n"
        "packages: !include_dir_named packages\n"
        "google:\n"
        "  service_account: !include secure/creds.json\n",
    )
    for rel in (
        "configs/customize.yaml",
        "configs/sensor.yaml",
        "themes/dark.yaml",
        "packages/p.yaml",
        "secure/creds.json",
    ):
        _write(tmp_path, rel, "a: 1\n")

    found = includes.scan(str(tmp_path)).paths
    assert {"configs/customize.yaml", "configs/sensor.yaml", "themes", "packages"} <= found
    assert "secure/creds.json" in found, "the credential nobody spotted"


# -- the operator's own list, for what no parser can see ----------------------


def test_extra_paths_are_replicated(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: dropping `extra_paths`.

    `python_scripts/` and ZHA's `zigbee.db` are opened by path at runtime and
    appear nowhere in the YAML, so the include scan cannot find them. Without
    this list they can only be replicated by luck.
    """
    from custom_components.cluster_state_sync.fileset import _candidates

    (tmp_path / "configuration.yaml").write_text("a: 1\n")
    (tmp_path / "python_scripts").mkdir()
    (tmp_path / "python_scripts/hello.py").write_text("pass\n")
    (tmp_path / "zigbee.db").write_text("not really a database\n")

    names = {rel for rel, _ in _candidates(tmp_path, ("python_scripts", "zigbee.db"))}
    assert "python_scripts/hello.py" in names
    assert "zigbee.db" in names


def test_a_missing_extra_path_warns_rather_than_raises(tmp_path: pathlib.Path, caplog) -> None:
    """A typo in the picker must cost a warning, not the publish. The leader
    publishing nothing is worse than the standby missing one file."""
    import logging

    from custom_components.cluster_state_sync.fileset import _candidates

    caplog.set_level(logging.WARNING)
    (tmp_path / "configuration.yaml").write_text("a: 1\n")
    names = {rel for rel, _ in _candidates(tmp_path, ("does_not_exist",))}
    assert "configuration.yaml" in names
    assert "does not exist" in caplog.text


def test_the_picker_offers_what_the_scan_cannot_see(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: offering everything, or
    offering things already covered.

    Suggesting a path the include scan already replicates implies it needs
    ticking, and an operator who does not tick it would reasonably conclude it
    is not carried.
    """
    from custom_components.cluster_state_sync.config_flow import _extra_path_candidates

    (tmp_path / "configuration.yaml").write_text("t: !include_dir_named packages\n")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages/p.yaml").write_text("a: 1\n")
    (tmp_path / "python_scripts").mkdir()
    (tmp_path / "custom_templates").mkdir()
    (tmp_path / "home-assistant_v2.db").write_text("x")
    (tmp_path / "backups").mkdir()

    offered = _extra_path_candidates(str(tmp_path))
    assert "python_scripts" in offered
    assert "custom_templates" in offered
    assert "packages" not in offered, "already replicated by the include scan"
    assert "home-assistant_v2.db" not in offered, "the recorder database is 2.1 GB"
    assert "backups" not in offered


def test_the_picker_survives_a_broken_configuration(tmp_path: pathlib.Path) -> None:
    """The form must still render when the config cannot be parsed — that is
    exactly when someone is trying to fix it."""
    from custom_components.cluster_state_sync.config_flow import _extra_path_candidates

    (tmp_path / "configuration.yaml").write_text("this: [is: not: valid\n")
    (tmp_path / "python_scripts").mkdir()
    assert "python_scripts" in _extra_path_candidates(str(tmp_path))


def test_the_picker_filters_the_noise_a_real_config_accumulates(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: offering every top-level
    entry unfiltered.

    Run against node-a unfiltered this offered forty entries, of which about
    twenty-five were `*.bak-*` copies, log fragments and SQLite journals. A
    suggestion list nobody can read is a suggestion list nobody uses — and the
    point of the picker is that the operator can actually see what is missing.
    """
    from custom_components.cluster_state_sync.config_flow import _extra_path_candidates

    (tmp_path / "configuration.yaml").write_text("a: 1\n")
    for name in (
        "automations.yaml.bak-20260601-173857",
        "home-assistant.log.1",
        "home-assistant_v2.db-wal",
        "home-assistant_v2.db-shm",
        "backup.tar.gz",
        "cluster_state_sync_bundle",
        "old_._storage",
    ):
        (tmp_path / name).write_text("x")
    (tmp_path / "python_scripts").mkdir()

    offered = _extra_path_candidates(str(tmp_path), ("*.bak-*",))
    assert offered == ["python_scripts"], offered


def test_the_picker_never_offers_our_own_bundle(tmp_path: pathlib.Path) -> None:
    """Replicating the bundle would ship one node's generated host config —
    including its node id — to the other, which is the identity collision
    GOTCHAS 3 describes, reached by a tick box."""
    from custom_components.cluster_state_sync.config_flow import _extra_path_candidates

    (tmp_path / "configuration.yaml").write_text("a: 1\n")
    (tmp_path / "cluster_state_sync_bundle").mkdir()
    assert "cluster_state_sync_bundle" not in _extra_path_candidates(str(tmp_path))


def test_the_picker_honours_the_operators_own_exclusions(
    tmp_path: pathlib.Path,
) -> None:
    """Offering something they have already excluded contradicts them."""
    from custom_components.cluster_state_sync.config_flow import _extra_path_candidates

    (tmp_path / "configuration.yaml").write_text("a: 1\n")
    (tmp_path / "scratch").mkdir()
    assert "scratch" in _extra_path_candidates(str(tmp_path), ())
    assert "scratch" not in _extra_path_candidates(str(tmp_path), ("scratch",))


def test_the_candidate_list_renders_as_tick_boxes(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: putting `custom_value` back
    on the candidate field.

    Home Assistant renders a multi-select with `custom_value` as a type-to-add
    chip box rather than a tick list, which defeats the point of having gone
    and found the candidates. Free text has its own field.
    """
    from homeassistant.helpers import config_validation as cv
    import voluptuous_serialize

    from custom_components.cluster_state_sync.config_flow import _fileset_schema

    fields = {
        f["name"]: f.get("selector", {}).get("select", {})
        for f in voluptuous_serialize.convert(
            _fileset_schema({}, ["python_scripts"]), custom_serializer=cv.custom_serializer
        )
    }
    assert fields["fileset_extra_paths"]["multiple"] is True
    assert not fields["fileset_extra_paths"].get("custom_value"), "tick boxes, not chips"
    assert fields["fileset_extra_custom"]["custom_value"] is True, "free text needs it"


def test_both_extra_lists_reach_the_publisher(tmp_path: pathlib.Path) -> None:
    """Ticked candidates and typed paths must both be replicated — splitting
    the field must not quietly drop one of them."""
    from custom_components.cluster_state_sync.fileset import _candidates

    (tmp_path / "configuration.yaml").write_text("a: 1\n")
    (tmp_path / "ticked").mkdir()
    (tmp_path / "ticked/x.yaml").write_text("a: 1\n")
    (tmp_path / "typed").mkdir()
    (tmp_path / "typed/y.yaml").write_text("b: 2\n")

    names = {rel for rel, _ in _candidates(tmp_path, ("ticked", "typed"))}
    assert "ticked/x.yaml" in names
    assert "typed/y.yaml" in names
