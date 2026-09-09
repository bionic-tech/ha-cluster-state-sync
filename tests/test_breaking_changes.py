"""The five contracts a node that is ALREADY DEPLOYED depends on.

Everything else in this suite asks whether the code is correct. This file asks
a different question: **would installing this version onto a running cluster
break it?** Those are not the same question, and the suite answered only the
first one until an upgrade was actually attempted.

The failure that produced this file, measured on 2026-09-08 against the live
pair: `cluster-promoter.sh` gained three arguments (`--base-grace`,
`--ha-container`, `--grace-reason-file`) and `cluster_promoter.py` gained the
options to receive them. Both were correct. Both passed every test. Install the
wrapper without the program and argparse exits 2 — **every ten seconds, forever,
on both nodes, so neither can take or renew the lease and the cluster cannot
fail over at all.** Nothing here would have caught it, because no test had ever
put an old artefact next to a new one.

A change that breaks one of these contracts is not forbidden. It is a
**breaking change**, and it has to be recorded as one in that version's release
note, which the last test enforces.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import re
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.cluster_state_sync import bundle
from custom_components.cluster_state_sync.const import (
    DOMAIN,
    fileset_blob_key,
    fileset_manifest_key,
    leader_key,
    meta_key,
    node_key,
    states_key,
    statistics_key,
    statistics_status_key,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "cluster_state_sync"


def _cfg(**overrides: Any) -> dict[str, Any]:
    """A fully-featured config, so every optional artefact is emitted."""
    base = {
        "ha_container": "homeassistant",
        "ha_config_path": "/config",
        "redis_host": "valkey.lan",
        "redis_port": 6379,
        "redis_db": 2,
        "redis_username": "cluster",
        "redis_password": "hunter2",
        "redis_use_tls": True,
        "redis_tls_ca_certs": "/etc/ssl/ca.pem",
        "cluster_namespace": "prod",
        "cluster_secret": "s" * 44,
        "node_id": "node-a",
        "peer_host": "peer.lan",
        "topology_model": "cold",
        "leadership_source": "lease",
        "fileset_enabled": True,
        "statistics_enabled": True,
    }
    base.update(overrides)
    return base


# --- Contract 1: the host wrapper and the program it runs -----------------


class _ParserCaptured(Exception):
    """Carries the parser out of `main()` before it parses anything."""


def _parser_of(module_path: str) -> argparse.ArgumentParser:
    """The real `ArgumentParser` a program builds, without running it.

    Intercepted at `parse_args` rather than reconstructed here, because a
    hand-maintained copy of the option list would be a second source of truth
    and would drift -- which is the whole class of bug this file exists for.
    """
    module = importlib.import_module(module_path)
    captured: dict[str, argparse.ArgumentParser] = {}

    def spy(self: argparse.ArgumentParser, *_a: Any, **_kw: Any) -> Any:
        captured["parser"] = self
        raise _ParserCaptured

    with patch.object(argparse.ArgumentParser, "parse_args", spy):
        try:
            module.main([])
        except _ParserCaptured:
            pass
    assert "parser" in captured, f"{module_path}.main() never built a parser"
    return captured["parser"]


def _flags_in(script: str, program: str) -> set[str]:
    """Long options the generated shell passes **to the program**.

    Scanning the whole script would sweep up `docker run`'s own flags
    (`--entrypoint`, `--network`, `--rm`), which the program neither sees nor
    should accept -- a test that failed on those would be noise, and noise is
    how a real break gets waved through.
    """
    tail = script.split(program)[-1]
    return set(re.findall(r"(?m)^\s+(--[a-z][a-z0-9-]*)", tail))


#: Every wrapper that runs a shipped program, and the program it runs. These
#: are installed as separate files and can therefore be updated separately,
#: which is exactly the hazard.
WRAPPER_PAIRS = [
    (
        "cluster-promoter.sh",
        "cluster_promoter.py",
        "custom_components.cluster_state_sync.scripts.cluster_promoter",
    ),
    (
        "cluster-fileset-pull.sh",
        "fileset_pull.py",
        "custom_components.cluster_state_sync.scripts.fileset_pull",
    ),
    (
        "cluster-statistics-pull.sh",
        "statistics_pull.py",
        "custom_components.cluster_state_sync.scripts.statistics_pull",
    ),
]


@pytest.mark.parametrize(("script_name", "program", "module_path"), WRAPPER_PAIRS)
def test_the_wrapper_never_passes_an_argument_the_program_rejects(
    script_name: str, program: str, module_path: str
) -> None:
    """🚨 The measured break. argparse exits 2 and the unit dies every tick.

    A wrapper newer than its program is the likely direction: the wrapper is
    small and gets copied, the program is large and gets forgotten.
    """
    script = bundle.build_bundle(_cfg())[script_name]
    parser = _parser_of(module_path)
    accepted = {opt for action in parser._actions for opt in action.option_strings}

    unknown = _flags_in(script, program) - accepted
    assert not unknown, (
        f"{script_name} passes {sorted(unknown)}, which {module_path} does not accept. "
        "Installed together this is fine; installed apart the program exits 2 on every "
        "run. If this is intended, it is a BREAKING CHANGE: both artefacts must ship "
        "together and the release note must say so."
    )


@pytest.mark.parametrize(("script_name", "program", "module_path"), WRAPPER_PAIRS)
def test_the_program_never_requires_an_argument_the_wrapper_omits(
    script_name: str, program: str, module_path: str
) -> None:
    """The other direction, and the quieter one.

    A program that gains a *required* argument fails identically, but the
    reviewer's eye is on the program's own tests, which pass it happily.
    """
    script = bundle.build_bundle(_cfg())[script_name]
    parser = _parser_of(module_path)
    required = {
        action.option_strings[0]
        for action in parser._actions
        if action.required and action.option_strings
    }

    missing = required - _flags_in(script, program)
    assert not missing, (
        f"{module_path} requires {sorted(missing)}, which {script_name} never passes. "
        "Every run exits 2."
    )


def test_a_new_option_defaults_rather_than_silently_changing_behaviour() -> None:
    """New program + old wrapper must degrade honestly, not quietly.

    `--base-grace` defaults to 120s while `--ha-container` decides whether the
    grace may EXTEND to 600s. An old wrapper passes neither, so the node gets a
    flat 120s where it used to have 600s -- a five-fold cut to the window a
    restarting Home Assistant is given, with no error anywhere.

    This test does not forbid that. It pins the pairing, so that changing it
    requires changing this test, which is the moment someone has to think about
    the upgrade path.
    """
    promoter = _parser_of("custom_components.cluster_state_sync.scripts.cluster_promoter")
    defaults = {
        action.option_strings[0]: action.default
        for action in promoter._actions
        if action.option_strings
    }
    assert defaults["--base-grace"] == 120.0
    assert defaults["--probe-grace"] == 600.0
    assert not defaults["--ha-container"], (
        "--ha-container must default to something FALSY. `main()` builds the docker "
        "inspector only `if args.ha_container`, so a default name would have a node "
        "inspect a container it was never told about -- and extend a promotion grace "
        "on the strength of what it found."
    )


# --- Contract 2: what the bundle installs, and what it prunes -------------


def test_every_file_the_bundle_can_emit_is_managed() -> None:
    """An unmanaged artefact is never cleaned up.

    `write_bundle` prunes `MANAGED_FILENAMES - written`. A file emitted but not
    listed survives a configuration change that should have removed it -- so
    turning statistics replication OFF would leave a timer running against a
    key nobody publishes, and switching warm->cold would leave a firewall
    ruleset on disk that still loads.
    """
    emitted: set[str] = set()
    for topology in ("cold", "warm"):
        for fileset in (True, False):
            for statistics in (True, False):
                emitted |= set(
                    bundle.build_bundle(
                        _cfg(
                            topology_model=topology,
                            fileset_enabled=fileset,
                            statistics_enabled=statistics,
                        )
                    )
                )
    unmanaged = emitted - bundle.MANAGED_FILENAMES
    assert not unmanaged, f"emitted but never pruned: {sorted(unmanaged)}"


# --- Contract 3: an existing config entry must still set up ---------------

#: The keys a v0.3.1 entry actually carries. An upgrade does not rewrite the
#: entry, so every version after this one has to boot from exactly this much
#: and no more. Adding a REQUIRED key here is a breaking change: the
#: integration would fail to start until the operator re-ran the wizard, on a
#: node whose Home Assistant may be the one running the house.
V0_3_1_ENTRY_KEYS = {
    "redis_host",
    "redis_port",
    "redis_db",
    "cluster_namespace",
    "node_id",
    "cluster_secret",
    "snapshot_interval",
    "fileset_enabled",
}


async def test_an_entry_from_the_previous_version_still_sets_up(hass) -> None:
    """🚨 No new key may be required. Upgrades do not re-run the wizard."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.fakes import FakeBackend

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "redis_port": 6379,
            "redis_db": 2,
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": "s" * 44,
            "snapshot_interval": 30,
            "fileset_enabled": False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        assert await hass.config_entries.async_setup(entry.entry_id), (
            "an entry written by the previous version no longer sets up. A new "
            "REQUIRED config key is a breaking change: the integration will not "
            "start until the operator re-runs the wizard."
        )
        await hass.async_block_till_done()


def test_statistics_replication_defaults_off_for_an_upgrading_node() -> None:
    """An upgrade must not switch on a feature that needs a manual seed.

    Turning itself on would have the leader publish, the standby report
    `not_seeded`, and a repair appear on a cluster whose owner changed nothing.
    """
    from custom_components.cluster_state_sync.const import (
        DEFAULT_RECORDER_SNAPSHOT_ENABLED,
        DEFAULT_STATISTICS_ENABLED,
    )

    assert DEFAULT_STATISTICS_ENABLED is False
    assert DEFAULT_RECORDER_SNAPSHOT_ENABLED is False


# --- Contract 4: the Valkey keys, which both nodes must agree on ----------


def test_the_valkey_key_names_are_pinned() -> None:
    """Both nodes address the same store, and they upgrade one at a time.

    Renaming a key does not fail -- it makes the upgraded node read an empty
    key and the other write one nobody reads. The cluster reports itself
    healthy while the two halves silently stop talking, which is the worst
    shape of failure this project has.
    """
    ns = "prod"
    assert states_key(ns) == "ha:cluster_state_sync:prod:states"
    assert meta_key(ns) == "ha:cluster_state_sync:prod:meta"
    assert leader_key(ns) == "ha:cluster_state_sync:prod:leader"
    assert node_key(ns, "node-a") == "ha:cluster_state_sync:prod:nodes:node-a"
    assert fileset_manifest_key(ns) == "ha:cluster_state_sync:prod:fileset:manifest"
    assert fileset_blob_key(ns, "abc") == "ha:cluster_state_sync:prod:fileset:blob:abc"
    assert statistics_key(ns) == "ha:cluster_state_sync:prod:statistics:window"
    assert statistics_status_key(ns) == "ha:cluster_state_sync:prod:statistics:follower"


def test_the_host_side_programs_agree_with_the_integration_on_key_names() -> None:
    """The scripts re-declare these because they run with no package on the path.

    Two copies of a constant is the arrangement, deliberately (ADR-005). This
    is the test that keeps them equal.
    """
    from custom_components.cluster_state_sync.scripts import statistics_pull

    ns = "prod"
    assert statistics_pull._statistics_key(ns) == statistics_key(ns)
    assert statistics_pull._status_key(ns) == statistics_status_key(ns)


# --- Contract 5: entity identity ------------------------------------------

#: Every entity key this integration has ever shipped. Removing or renaming one
#: orphans the entity in the user's registry: their dashboards, automations and
#: history all reference `sensor.<node>_<key>`, and a rename leaves a dead
#: entity behind and a new one nobody is watching.
SHIPPED_ENTITY_KEYS = {
    "backend",
    "cluster_leader",
    "clock_skew",
    "cluster_members",
    "entities_restored",
    "entities_tracked",
    "fileset_age",
    "fileset_degraded",
    "is_leader",
    "last_snapshot_age",
    "maintenance_hold",
    "oversized_entities",
    "radio_silence",
    "recorder_snapshot_age",
    "shared_snapshot_age",
    "statistics_age",
    "statistics_seed",
    "unreplicated_references",
}


def test_no_entity_key_is_ever_removed_or_renamed() -> None:
    """Additions are free. Removals and renames break users' dashboards."""
    keys: set[str] = set()
    for name in ("sensor.py", "binary_sensor.py", "switch.py", "button.py"):
        source = (COMPONENT / name).read_text(encoding="utf-8")
        keys |= set(re.findall(r'_attr_translation_key = "([a-z_]+)"', source))

    removed = SHIPPED_ENTITY_KEYS - keys
    assert not removed, (
        f"entity keys no longer produced: {sorted(removed)}. Users' dashboards, "
        "automations and history all reference these. Removing one is a BREAKING "
        "CHANGE and needs a migration, not a deletion."
    )


# --- Captured according to version ----------------------------------------


def test_this_version_has_a_release_note() -> None:
    """ "Captured according to version" is only true if it cannot be skipped.

    A breaking change recorded nowhere is one an operator meets during an
    upgrade, at the moment it costs the most.
    """
    version = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))["version"]
    note = REPO / "docs" / f"RELEASE-{version}.md"
    assert note.exists(), (
        f"manifest.json says {version} but {note.relative_to(REPO)} does not exist. "
        "Every version gets a release note, and any change to the contracts in this "
        "file gets a 'Breaking changes' section in it."
    )


# --- AR-0043: nothing operator-supplied reaches root-run shell unchecked ---


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("ha_container", 'homeassistant"; touch /tmp/PWNED; #'),
        ("redis_host", "valkey.lan$(touch /tmp/PWNED)"),
        ("redis_username", "user`id`"),
        ("ha_config_path", "/config; rm -rf /"),
        ("peer_host", "10.0.0.1 && curl evil.example"),
        ("ha_container_ip", '1.2.3.4"; nft flush ruleset; #'),
        ("node_id", "node-a\nmalicious line"),
        ("redis_tls_ca_certs", "/ca.pem|sh"),
        # AR-0056: the five the first version of this guard missed, because
        # they look like numbers and were not on a hand-written field list.
        ("redis_port", "6379; touch /tmp/PWNED"),
        ("redis_db", "2 || touch /tmp/PWNED"),
        ("fileset_stale_after", "1800; touch /tmp/PWNED"),
        ("iot_subnets", "10.0.0.0/8; touch /tmp/PWNED"),
        ("ha_uid", "1000; touch /tmp/PWNED"),
    ],
)
def test_no_config_value_can_inject_into_root_run_shell(field: str, payload: str) -> None:
    """🚨 AR-0043, verified by execution before it was fixed.

    Every artefact the bundle emits is run by systemd **as root, every ten
    seconds, on both hosts**. Until 2026-09-08 none of these values was quoted
    or validated, and two payloads were confirmed to execute:

        CONTAINER="homeassistant"; touch ./PWNED; #"   -> ran
        --redis valkey.lan$(touch ./PWNED):6379        -> ran

    The boundary is Home Assistant admin -> root on the host, in a project
    whose premise is that the host does not need touching.
    """
    cfg = _cfg(**{field: payload})
    with pytest.raises(bundle.UnsafeBundleValue):
        bundle.build_bundle(cfg)


def test_ordinary_values_are_untouched_by_the_guard() -> None:
    """The guard must not cost a legitimate install its bundle."""
    emitted = bundle.build_bundle(
        _cfg(
            ha_container="home-assistant-2",
            ha_config_path="/mnt/docker_data/homeassistant",
            redis_host="valkey-cluster-state.example.dev",
            redis_username="cluster_state_sync",
            redis_tls_ca_certs="/mnt/docker_data/homeassistant/root.crt",
        )
    )
    assert "cluster-promoter.sh" in emitted


def test_the_guard_runs_before_any_artefact_is_rendered() -> None:
    """A partial bundle is worse than none: `write_bundle` would install the
    files it managed to render and prune the rest, leaving a host running half
    of one configuration and half of another."""
    import inspect

    source = inspect.getsource(bundle.build_bundle)
    body = source.split('"""')[-1]
    first_statement = next(
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert first_statement == "validate_shell_safe(cfg)", (
        f"validate_shell_safe must be the first statement in build_bundle; found "
        f"{first_statement!r}"
    )


# --- AR-0050: the setup contract a refactor of async_setup_entry could break --


#: Every slot `async_setup_entry` must leave in `entry.runtime_data`. The
#: platforms and diagnostics read these by key; a setup path that skips one
#: does not fail — it produces an entity reading a key that was never set,
#: which is `unknown` on a dashboard and nothing in a log.
REQUIRED_RUNTIME_SLOTS = (
    "mirror",
    "unsub",
    "leadership",
    "gate",
    "fileset",
    "statistics",
    "degraded_marker",
    "coordinator",
    "cluster_view",
    "stats",
)


async def test_setup_fills_every_runtime_slot_the_platforms_read(hass) -> None:
    """🚨 Written for AR-0050's refactor, and it is the point of it.

    `async_setup_entry` was 514 lines doing eleven unrelated jobs. Splitting it
    is safe only if something checks that every slot still gets filled — and
    the failure mode of missing one is silent: an entity reads `runtime[...]`,
    gets nothing, and reports `unknown` for ever.

    This asserts the contract rather than the shape, so the function can be
    reorganised freely and this still catches a dropped assignment.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.fakes import FakeBackend

    entry = MockConfigEntry(
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
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    missing = [k for k in REQUIRED_RUNTIME_SLOTS if k not in entry.runtime_data]
    assert not missing, (
        f"async_setup_entry left these runtime slots unset: {missing}. Every one is "
        "read by a platform or a diagnostic, and the symptom of a missing slot is an "
        "entity stuck at `unknown` rather than an error."
    )


def test_the_optional_feature_setups_stay_in_their_agreed_order() -> None:
    """Order is the one thing extraction can silently change.

    The go-bag must be published before the statistics that ride on its key and
    its secret, and the degraded alarm must be raised after both so it can
    describe what the swap actually installed. Nothing else in the suite
    notices if these swap places.
    """
    import inspect

    from custom_components import cluster_state_sync

    source = inspect.getsource(cluster_state_sync.async_setup_entry)
    order = [
        source.index("await _setup_recorder_snapshots"),
        source.index("await _setup_fileset_replication"),
        source.index("await _setup_statistics_replication"),
        source.index("await _setup_degraded_alarm"),
    ]
    assert order == sorted(order), (
        "the optional feature setups have been reordered; statistics rides on the "
        "fileset's key and secret, and the degraded alarm describes what the swap did"
    )
    # And they must all still run before the platforms, which build entities
    # from what they left behind.
    assert order[-1] < source.index("async_forward_entry_setups")


def test_the_shell_guard_is_default_deny_not_a_curated_list() -> None:
    """🚨 AR-0056, and the reason it happened.

    The first version of this guard validated a hand-written list of thirteen
    fields, while its own docstring explained that a chokepoint beats a hunt
    because "the next person to add a site will forget". Curating the FIELD
    list is that same mistake one level up, and five fields were already
    missing — `--redis v:6379; touch /tmp/PWNED` was generated, and it ran.

    A config key invented tomorrow must be checked by **not** being mentioned.
    """
    unknown_key = "some_field_nobody_has_thought_of_yet"
    with pytest.raises(bundle.UnsafeBundleValue):
        bundle.build_bundle(_cfg(**{unknown_key: "x; touch /tmp/PWNED"}))


def test_every_exception_to_the_shell_guard_carries_a_reason() -> None:
    """An unexplained exemption is how a curated list rots.

    Each entry must say why that field cannot be validated, so removing it
    later is a decision rather than a guess.
    """
    for field, reason in bundle.SHELL_SAFE_EXCEPTIONS.items():
        assert isinstance(reason, str) and len(reason) > 30, (
            f"{field} is exempt from the shell guard with no real reason given"
        )


def test_the_cluster_secret_never_reaches_a_generated_artefact() -> None:
    """It is exempt from the guard, so this is what makes that exemption safe.

    The secret derives the fileset key; only the hex of that key ships. If it
    ever started being interpolated somewhere, its exemption would silently
    become an injection path.
    """
    marker = "SECRETMARKER" + "s" * 32
    for name, body in bundle.build_bundle(_cfg(cluster_secret=marker)).items():
        assert marker not in body, f"the raw cluster secret reached {name}"


def test_a_hostile_looking_password_still_builds() -> None:
    """Passwords legitimately contain $, quotes and spaces.

    Validating this field would reject valid configurations; it is safe because
    its single use wraps it in `shlex.quote` for a file the pull sources.
    """
    out = bundle.build_bundle(_cfg(redis_password='p@ss$w0rd"with`quotes;and spaces'))
    assert "cluster-fileset-valkey.env" in out


def test_glob_bearing_fields_still_build() -> None:
    """`*.bak-*` is a legitimate exclusion and contains a metacharacter."""
    out = bundle.build_bundle(
        _cfg(
            fileset_exclusions=("*.bak-*", "core.restore_state"),
            fileset_extra_paths=("packages/",),
            radio_watch="sensor.*_rssi_numeric",
        )
    )
    assert "cluster-fileset-pull.sh" in out


@pytest.mark.parametrize(
    ("subnets", "should_build"),
    [
        ("192.168.145.0/24, 192.168.1.0/24", True),
        ("10.0.0.0/8", True),
        ("fd00::/8, 2001:db8::1", True),
        ("10.0.0.0/8; touch /tmp/PWNED", False),
        ("10.0.0.0/8$(id)", False),
        # An nftables breakout rather than a shell one: this value lands inside
        # a rule loaded as root, so closing the set early matters as much as a
        # semicolon does.
        ("10.0.0.0/8 }; drop; #", False),
    ],
)
def test_subnets_are_validated_by_shape_not_by_metacharacter(
    subnets: str, should_build: bool
) -> None:
    """A comma-separated list legitimately contains spaces.

    Rejecting the space would refuse a valid firewall configuration; exempting
    the field would leave nftables input unchecked. Neither is acceptable, so
    the field's actual shape is validated — an allowlist of address
    characters, which is stricter than the generic rule, not looser.
    """
    cfg = _cfg(topology_model="warm", peer_host="peer.lan", iot_subnets=subnets)
    if should_build:
        assert bundle.build_bundle(cfg)
    else:
        with pytest.raises(bundle.UnsafeBundleValue):
            bundle.build_bundle(cfg)
