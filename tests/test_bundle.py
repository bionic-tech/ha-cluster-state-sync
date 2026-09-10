"""Host-side bundle generation tests (ADR-001 wizard step 5).

These artifacts are installed on the Docker hosts and two of them manipulate
firewall rules on machines running other services. A generated file that *looks*
authoritative but is subtly wrong is worse than no file at all, so the assertions
here are about substance -- that the operator's actual subnets and uid reach the
rules, that the follower ruleset is genuinely more restrictive than the leader's,
and that nothing claims to be validated when it cannot be.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time

import pytest

from custom_components.cluster_state_sync.bundle import (
    INSTALL_DIR,
    MANAGED_FILENAMES,
    PULL_MOUNTPOINT,
    VALKEY_PASSWORD_FILENAME,
    build_bundle,
    write_bundle,
)
from custom_components.cluster_state_sync.const import (
    CONF_CLUSTER_SECRET,
    CONF_FILESET_ENABLED,
    CONF_FILESET_STALE_AFTER,
    CONF_HA_CONFIG_PATH,
    CONF_HA_CONTAINER_IP,
    CONF_NODE_ID,
    CONF_REDIS_DB,
    CONF_REDIS_PASSWORD,
    CONF_REDIS_TLS_CA_CERTS,
    CONF_REDIS_USE_TLS,
    CONF_REDIS_USERNAME,
    DEGRADED_MARKER_NAME,
    DOCKER_ALL,
    DOCKER_BRIDGE,
    DOCKER_HOST,
    DOCKER_MACVLAN,
    STAGED_DIR_NAME,
    TOPOLOGY_COLD,
    TOPOLOGY_WARM,
)
from custom_components.cluster_state_sync.crypto import derive_fileset_key

COLD = {
    "topology_model": TOPOLOGY_COLD,
    "node_id": "tiger1",
    "peer_host": "tiger2.lan",
    "ha_container": "homeassistant",
    "cluster_namespace": "default",
}

WARM = {
    **COLD,
    "topology_model": TOPOLOGY_WARM,
    "iot_subnets": "192.168.50.0/24, 10.20.0.0/16",
    "block_discovery": True,
    "docker_network": DOCKER_HOST,
    "ha_uid": 1000,
    "ha_container_ip": "192.168.1.60",
    "settle_delay": 15,
    "leadership_source": "lease",
}


def files(**overrides: object) -> dict[str, str]:
    cfg = {**COLD, **overrides}
    return build_bundle(cfg)


# -- cold model -------------------------------------------------------------


def test_cold_bundle_has_no_firewall_rules() -> None:
    """ADR-001: the cold model needs no firewall.

    Side-effect suppression comes from the standby's Home Assistant not
    running. Emitting nftables rules anyway would hand the operator a
    dangerous file with no purpose.
    """
    bundle = files()
    assert not [name for name in bundle if name.endswith(".nft")]
    assert not [name for name in bundle if "bridge" in name]


def test_cold_notify_scripts_start_and_stop_home_assistant() -> None:
    """Cold promotion is: start the container. Demotion is: stop it.

    The stop must carry an explicit `-t`. Docker's default grace is 10s, and
    Home Assistant writes `.storage` as it shuts down (AR-0034), so a default
    stop SIGKILLs it mid-write. Measured on node-b, 2026-09-04: every
    demotion ended `Container failed to exit within 10s of signal 15 - using
    the force`, exit 137, which was misread as the standby crashing.
    """
    from custom_components.cluster_state_sync.bundle import (
        DOCKER_STOP_TIMEOUT_SECONDS,
    )

    bundle = files()
    stop = f"docker stop -t {DOCKER_STOP_TIMEOUT_SECONDS} homeassistant"
    assert "docker start homeassistant" in bundle["notify_master.sh"]
    assert stop in bundle["notify_backup.sh"]
    assert stop in bundle["notify_fault.sh"]
    assert DOCKER_STOP_TIMEOUT_SECONDS > 10, "Docker's default is what broke this"


def test_promotion_claims_hardware_before_the_preflight() -> None:
    """Ordering is the whole point of the pre-start hook, and BOTH orderings
    matter for different reasons.

    Before `docker start`, because a container's /dev is a snapshot taken at
    start and a device attached afterwards never appears inside it (GOTCHAS 17).
    Before the device pre-flight, because the pre-flight DISABLES config entries
    whose hardware is absent -- so claiming afterwards would let a promoted node
    disable the very radios it had just acquired.

    Production change this catches: moving the hook below the pre-flight or
    below `docker start`, either of which reads as harmless.
    """
    script = files()["notify_master.sh"]
    hook = script.index("pre-start.d")
    preflight = script.index("ha-device-preflight.py")
    start = script.index("docker start")
    assert hook < preflight, "hardware must be claimed before the pre-flight"
    assert preflight < start, "pre-flight must precede the container start"


def test_demotion_releases_hardware_after_the_container_stops() -> None:
    """Release only once nothing is using the device."""
    script = files()["notify_backup.sh"]
    assert script.index("docker stop") < script.index("post-stop.d")


def test_a_hook_failure_never_blocks_promotion() -> None:
    """D4's rule, applied to hardware: network-attached devices work whatever
    happened to a radio, so refusing to promote over an unattached one turns a
    partial outage into a total one."""
    script = files()["notify_master.sh"]
    block = script[script.index("pre-start.d") : script.index("ha-device-preflight.py")]
    assert "DEGRADED" in block, "a failed hook must say the node is degraded"
    assert "exit 1" not in block, "a failed hook must not abort the promotion"
    assert "rc=$?" in block, "capture rc first, or $? reports basename's success"


def test_hooks_are_declared_but_never_installed() -> None:
    """The common case -- USB passed straight through -- must cost nothing and
    ship no vendor's script. The directory is consulted, not populated."""
    bundle = files()
    assert not any("pre-start.d/" in name for name in bundle), (
        "no hook may ship in the bundle by default"
    )
    assert '[[ -d "$HOOKS" ]]' in bundle["notify_master.sh"], (
        "an absent hook directory must be the ordinary, silent case"
    )


def test_fault_is_treated_as_demotion() -> None:
    """notify_fault must demote, not be left as a no-op.

    A node in VRRP fault state is not healthy enough to be leader. Leaving
    fault unhandled is how a broken node keeps writing.
    """
    bundle = files()
    assert bundle["notify_fault.sh"].strip()
    assert "stop" in bundle["notify_fault.sh"]


# -- warm model: firewall ---------------------------------------------------


def test_warm_bundle_emits_firewall_rules() -> None:
    bundle = files(**WARM)
    assert "leader-host.nft" in bundle
    assert "follower-host.nft" in bundle


def test_operator_subnets_reach_the_rules() -> None:
    """The IoT CIDRs the operator typed must actually appear in the ruleset.

    Production change that would make this fail: emitting a template with a
    placeholder subnet the operator is expected to edit by hand -- which is
    precisely the hand-rolled configuration the wizard exists to remove.
    """
    bundle = files(**WARM)
    follower = bundle["follower-host.nft"]
    assert "192.168.50.0/24" in follower
    assert "10.20.0.0/16" in follower


def test_host_mode_filters_by_uid() -> None:
    """In host network mode HA shares the host's stack, so uid is the only handle."""
    bundle = files(**WARM)
    assert "skuid 1000" in bundle["follower-host.nft"]


def test_follower_ruleset_is_more_restrictive_than_the_leader() -> None:
    """The whole point: the follower drops what the leader allows.

    Production change that would make this fail: emitting identical rulesets,
    which would leave the warm standby talking to the IoT network while
    demoted -- the exact side effect the model exists to suppress.
    """
    bundle = files(**WARM)
    assert "drop" in bundle["follower-host.nft"]
    assert bundle["leader-host.nft"] != bundle["follower-host.nft"]


def test_discovery_ports_are_blocked_on_the_follower_when_requested() -> None:
    """mDNS 5353 and SSDP 1900 -- the HomeKit/Matter fabric-collision guard."""
    bundle = files(**WARM)
    follower = bundle["follower-host.nft"]
    assert "5353" in follower
    assert "1900" in follower


def test_discovery_ports_are_absent_when_not_requested() -> None:
    """An operator who turns discovery blocking off must not get it anyway."""
    bundle = files(**{**WARM, "block_discovery": False})
    assert "5353" not in bundle["follower-host.nft"]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (DOCKER_HOST, ["leader-host.nft"]),
        (DOCKER_MACVLAN, ["leader-macvlan.nft"]),
        (DOCKER_BRIDGE, ["leader-bridge.sh"]),
    ],
)
def test_each_docker_mode_emits_its_own_variant(mode: str, expected: list[str]) -> None:
    bundle = files(**{**WARM, "docker_network": mode})
    for name in expected:
        assert name in bundle


def test_all_mode_emits_every_variant() -> None:
    """With the network mode unknown, emit all three rather than guess.

    Guessing wrong here produces rules that silently do nothing -- a follower
    that believes it is firewalled and is not.
    """
    bundle = files(**{**WARM, "docker_network": DOCKER_ALL})
    for name in (
        "leader-host.nft",
        "follower-host.nft",
        "leader-macvlan.nft",
        "follower-macvlan.nft",
        "leader-bridge.sh",
        "follower-bridge.sh",
    ):
        assert name in bundle, f"{name} missing from the all-modes bundle"


def test_macvlan_rules_use_the_egress_hook() -> None:
    """ADR-001 flags this: macvlan egress can bypass the host filter hook.

    Production change that would make this fail: reusing the host-mode filter
    hook for macvlan. The rules would load without error and drop nothing --
    the worst failure mode available, since it looks like it worked.
    """
    bundle = files(**{**WARM, "docker_network": DOCKER_MACVLAN})
    assert "egress" in bundle["follower-macvlan.nft"]
    assert "192.168.1.60" in bundle["follower-macvlan.nft"]


def test_bridge_mode_uses_the_docker_user_chain() -> None:
    """Docker rewrites its own chains but never touches DOCKER-USER."""
    bundle = files(**{**WARM, "docker_network": DOCKER_BRIDGE})
    assert "DOCKER-USER" in bundle["follower-bridge.sh"]


# -- safety -----------------------------------------------------------------


def test_a_safety_revert_script_is_included_for_warm() -> None:
    """Loading a bad ruleset on a remote host can lock out SSH.

    The revert script is the way back in: it restores the previous ruleset on a
    timer unless explicitly cancelled.
    """
    bundle = files(**WARM)
    assert "nft-safety-revert.sh" in bundle
    body = bundle["nft-safety-revert.sh"]
    assert "nft list ruleset" in body


def test_generated_firewall_rules_are_labelled_unvalidated() -> None:
    """These were generated without access to the hosts and must say so.

    Production change that would make this fail: dropping the warning. Output
    that looks machine-generated reads as tested; this was not, and the
    operator needs to know before pointing it at a box running other services.
    """
    bundle = files(**WARM)
    assert "NOT been tested" in bundle["follower-host.nft"]


def test_scripts_carry_a_shebang_and_strict_mode() -> None:
    """An unset variable in a firewall script must abort, not expand to empty.

    `nft ... ip daddr drop` with an empty subnet is a very different rule from
    the one intended.
    """
    bundle = files(**WARM)
    for name, body in bundle.items():
        if not name.endswith(".sh"):
            continue
        assert body.startswith("#!/usr/bin/env bash"), name
        assert "set -euo pipefail" in body, name


def test_install_readme_is_present_and_names_the_model() -> None:
    bundle = files(**WARM)
    assert "INSTALL.md" in bundle
    assert "warm" in bundle["INSTALL.md"].lower()


# -- real syntax validation -------------------------------------------------
#
# The assertions above check that the right *content* reaches the files. These
# check the files are actually loadable, by running the real parsers. A bundle
# that asserts cleanly in Python and then fails to parse on the host has helped
# nobody.


def _write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body)
    return path


@pytest.mark.parametrize("model", [TOPOLOGY_COLD, TOPOLOGY_WARM])
def test_every_generated_script_is_valid_bash(tmp_path, model: str) -> None:
    """Run bash's own parser over every script we emit.

    Production change that would make this fail: any quoting or heredoc error
    in the generated shell. These run as root from Keepalived during a
    failover -- the worst possible moment to discover a syntax error.
    """
    import subprocess

    bundle = files(**{**WARM, "topology_model": model})
    scripts = {n: b for n, b in bundle.items() if n.endswith(".sh")}
    assert scripts, "expected at least one script"

    for name, body in scripts.items():
        path = _write(tmp_path, name, body)
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name} is not valid bash:\n{result.stderr}"


@pytest.mark.parametrize(
    "name", ["leader-host.nft", "follower-host.nft", "leader-macvlan.nft", "follower-macvlan.nft"]
)
def test_every_generated_ruleset_parses(tmp_path, name: str) -> None:
    """Run nft's own parser over each ruleset.

    Production change that would make this fail: a malformed set literal, a bad
    hook spec, or an empty `ip daddr` from a missing subnet.

    `nft -c` checks syntax without loading, so this is safe to run anywhere and
    needs no privileges. It does not prove the rules *do* the right thing --
    nothing available here can, without the real hosts -- but it proves the
    operator will not be handed a file that fails to parse.
    """
    import shutil
    import subprocess

    nft = shutil.which("nft")
    if nft is None:
        pytest.skip("nft not installed")

    bundle = files(**{**WARM, "docker_network": DOCKER_ALL})
    path = _write(tmp_path, name, bundle[name])
    result = subprocess.run([nft, "-c", "-f", str(path)], capture_output=True, text=True)

    if "cache initialization failed" in result.stderr:
        # `nft -c` still opens a netlink socket to read the live ruleset, so it
        # needs CAP_NET_ADMIN even though it changes nothing. Unprivileged
        # containers cannot run it at all.
        #
        # Skipping rather than passing: a green tick here would claim these
        # rulesets were syntax-checked when they were not. That claim is exactly
        # what the UNVALIDATED header in every generated ruleset exists to
        # prevent, and the test should not undermine it.
        pytest.skip(
            "nft cannot initialise netlink here (needs CAP_NET_ADMIN) — "
            "ruleset syntax is UNVERIFIED on this machine"
        )

    assert result.returncode == 0, f"{name} does not parse:\n{result.stderr}"


def test_fileset_pull_systemd_units_have_the_required_sections() -> None:
    """The tier-1 sync units this test used to check are gone entirely (see the
    tier-1 removal tests below); this is the systemd pair that replaced them."""
    bundle = build_bundle(FS)
    assert "[Service]" in bundle["cluster-fileset-pull.service"]
    assert "[Timer]" in bundle["cluster-fileset-pull.timer"]
    assert "[Install]" in bundle["cluster-fileset-pull.timer"]


# -- the notify scripts must reference files that actually exist ------------


@pytest.mark.parametrize(
    ("mode", "expected_ref"),
    [
        (DOCKER_HOST, "leader-host.nft"),
        (DOCKER_MACVLAN, "leader-macvlan.nft"),
        (DOCKER_BRIDGE, "leader-bridge.sh"),
    ],
)
def test_warm_notify_master_references_the_variant_it_generated(
    mode: str, expected_ref: str
) -> None:
    """A notify script must point at a file the bundle actually contains.

    Production change that would make this fail: hardcoding `leader.nft` while
    generating `leader-host.nft`. Keepalived would run `nft -f` against a
    missing path, `set -e` would abort the promotion, and the failover would
    silently not happen -- discovered during an outage, not before one.
    """
    bundle = files(**{**WARM, "docker_network": mode})
    assert expected_ref in bundle["notify_master.sh"]
    assert expected_ref in bundle


def test_bridge_mode_notify_invokes_the_script_not_nft() -> None:
    """Bridge mode emits an iptables script; `nft -f` cannot run it."""
    bundle = files(**{**WARM, "docker_network": DOCKER_BRIDGE})
    master = bundle["notify_master.sh"]
    assert "leader-bridge.sh" in master
    assert "nft -f" not in master


def test_all_mode_emits_a_dispatcher_the_notify_scripts_call() -> None:
    """With the mode unknown, the notify scripts must not guess a variant.

    They call a dispatcher carrying a single clearly-marked setting instead, so
    the operator makes one deliberate choice rather than editing two Keepalived
    hooks by hand.
    """
    bundle = files(**{**WARM, "docker_network": DOCKER_ALL})
    assert "apply-leader.sh" in bundle
    assert "apply-follower.sh" in bundle
    assert "apply-leader.sh" in bundle["notify_master.sh"]
    assert "apply-follower.sh" in bundle["notify_backup.sh"]
    assert "NETWORK_MODE=" in bundle["apply-leader.sh"]


def test_every_file_referenced_by_a_notify_script_exists_in_the_bundle() -> None:
    """Nothing in the bundle may point at a file the bundle does not ship.

    Sweeps all four network-mode choices rather than trusting the three
    parametrised cases above to have caught every path.
    """
    import re

    for mode in (DOCKER_HOST, DOCKER_MACVLAN, DOCKER_BRIDGE, DOCKER_ALL):
        bundle = files(**{**WARM, "docker_network": mode})
        for name, body in bundle.items():
            if not name.endswith(".sh"):
                continue
            for ref in re.findall(r"/etc/cluster-sync/([\w.-]+)", body):
                assert ref in bundle, (
                    f"{name} (mode={mode}) references {ref}, which the bundle does not contain"
                )


# -- regeneration must not leave stale artifacts ---------------------------


def test_switching_warm_to_cold_removes_the_stale_firewall_rules(tmp_path) -> None:
    """Regenerating after a model change must clean up what no longer applies.

    Production change that would make this fail: writing files without pruning.

    An operator who moves warm -> cold would be left with follower-host.nft
    still sitting in /etc/cluster-sync while INSTALL.md tells them the cold
    model needs no firewall. The next person to look at that directory has to
    guess which files are live -- and a stale ruleset that still loads is
    exactly the kind of thing that gets loaded.
    """
    from custom_components.cluster_state_sync.bundle import write_bundle

    write_bundle(str(tmp_path), {**WARM, "docker_network": DOCKER_ALL})
    assert (tmp_path / "follower-host.nft").exists()

    write_bundle(str(tmp_path), COLD)

    assert not (tmp_path / "follower-host.nft").exists()
    assert not list(tmp_path.glob("*.nft"))
    assert (tmp_path / "notify_master.sh").exists()


def test_pruning_leaves_unrelated_files_alone(tmp_path) -> None:
    """Only our own artifacts are removed.

    The operator may keep notes or a local override in that directory, and
    regenerating a bundle is not licence to delete someone else's file.
    """
    from custom_components.cluster_state_sync.bundle import write_bundle

    (tmp_path / "my-notes.txt").write_text("do not delete me")
    (tmp_path / "site-specific.nft").write_text("# hand-written")

    write_bundle(str(tmp_path), COLD)

    assert (tmp_path / "my-notes.txt").read_text() == "do not delete me"
    assert (tmp_path / "site-specific.nft").exists()


# -- ruleset swap semantics -------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "leader-host.nft",
        "follower-host.nft",
        "leader-macvlan.nft",
        "follower-macvlan.nft",
    ],
)
def test_rulesets_replace_rather_than_merge(name: str) -> None:
    """Each ruleset must clear its own table before defining it.

    Production change that would make this fail: emitting a bare
    `table inet cluster_sync { ... }` with no delete.

    `nft -f` MERGES into an existing table, it does not replace it. Without the
    delete, loading leader-host.nft over a live follower ruleset leaves every
    follower drop rule in place -- the promoted node stays firewalled off from
    the IoT network it was just promoted to talk to. The rules load cleanly,
    the promotion reports success, and nothing works.

    Verified by actually loading them in sequence -- see
    `test_promotion_actually_clears_the_follower_rules`, which is the test that
    caught this. A parse check cannot: both versions parse perfectly.
    """
    bundle = files(**{**WARM, "docker_network": DOCKER_ALL})
    body = bundle[name]
    family = "netdev" if "macvlan" in name else "inet"

    assert f"delete table {family} cluster_sync" in body, (
        f"{name} would merge into a live ruleset instead of replacing it"
    )
    # The bare declaration must come first so the delete cannot fail on a host
    # where the table does not exist yet.
    assert body.index(f"table {family} cluster_sync\n") < body.index(
        f"delete table {family} cluster_sync"
    )


def test_promotion_actually_clears_the_follower_rules(tmp_path) -> None:
    """Load follower, then leader, and assert the drops are really gone.

    This is a behavioural test against the real nftables engine, not a string
    check. It needs CAP_NET_ADMIN, so it skips on unprivileged machines and
    runs in CI.
    """
    import shutil
    import subprocess

    nft = shutil.which("nft")
    if nft is None:
        pytest.skip("nft not installed")

    bundle = files(**{**WARM, "docker_network": DOCKER_HOST})
    follower = _write(tmp_path, "follower-host.nft", bundle["follower-host.nft"])
    leader = _write(tmp_path, "leader-host.nft", bundle["leader-host.nft"])

    probe = subprocess.run([nft, "list", "ruleset"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(
            "nft cannot reach netlink here (needs CAP_NET_ADMIN) — "
            "ruleset swap behaviour is UNVERIFIED on this machine"
        )

    subprocess.run([nft, "-f", str(follower)], check=True, capture_output=True)
    after_demote = subprocess.run(
        [nft, "list", "table", "inet", "cluster_sync"], capture_output=True, text=True
    ).stdout
    assert "drop" in after_demote, "follower ruleset should be dropping"

    subprocess.run([nft, "-f", str(leader)], check=True, capture_output=True)
    after_promote = subprocess.run(
        [nft, "list", "table", "inet", "cluster_sync"], capture_output=True, text=True
    ).stdout

    subprocess.run([nft, "delete", "table", "inet", "cluster_sync"], capture_output=True)

    assert "drop" not in after_promote, (
        "promotion left the follower's drop rules in place — the new leader is "
        "still firewalled off from the IoT network"
    )


# ---------------------------------------------------------------------------
# The promotion pre-flight ships with the bundle
# ---------------------------------------------------------------------------


def test_the_bundle_ships_the_device_preflight() -> None:
    """A script that exists only in the repo protects nobody.

    The probe is what lets a promoted node start with absent radios disabled
    rather than failing at integration setup. If the operator never receives
    it, the standby comes up broken in exactly the way it was written to
    prevent.
    """
    bundle = build_bundle(COLD)

    assert "ha-device-preflight.py" in bundle


def test_the_shipped_preflight_is_the_real_script_not_a_stub() -> None:
    """Emitted content must be the tested module, not a re-typed copy."""
    body = build_bundle(COLD)["ha-device-preflight.py"]

    assert "def classify_attachment" in body
    assert "vhci_hcd" in body
    assert body.lstrip().startswith('"""')


def test_promotion_runs_the_preflight_before_starting_home_assistant() -> None:
    """Ordering is the whole point.

    Attaching and probing after Home Assistant has started is useless — the
    integrations have already failed at setup and stay failed until a restart.
    """
    script = build_bundle(COLD)["notify_master.sh"]

    assert "ha-device-preflight.py" in script
    assert script.index("ha-device-preflight.py") < script.index("docker start")


def test_the_warm_promotion_also_runs_the_preflight() -> None:
    script = build_bundle(WARM)["notify_master.sh"]

    assert "ha-device-preflight.py" in script


def test_the_preflight_is_pruned_like_every_other_managed_file() -> None:
    """Regeneration must be able to remove what it previously wrote."""
    assert "ha-device-preflight.py" in MANAGED_FILENAMES


def test_the_cold_promotion_makes_the_preflight_verify_ha_is_stopped() -> None:
    """Adversarial review 2026-08-27: trust, but pass --container so it checks."""
    script = build_bundle(COLD)["notify_master.sh"]

    assert "--container homeassistant" in script
    assert "--apply" in script


def test_the_warm_promotion_never_applies() -> None:
    """Home Assistant is already running in the warm model.

    Editing core.config_entries under a live process is lost or corrupting.
    The warm path reports and changes nothing.
    """
    script = build_bundle(WARM)["notify_master.sh"]

    assert "ha-device-preflight.py" in script
    preflight = script[script.index("ha-device-preflight.py") :]
    assert "--apply" not in preflight.split("\n\n")[0]


# -- AR-0042: the host config path is a fact, not an assumption ---------------


def test_ar_0042_the_config_path_is_configurable() -> None:
    """The generated bundle must not hardcode where Home Assistant lives.

    Production change that would make this fail: writing
    `/opt/homeassistant/config` into the scripts instead of reading it from
    the config entry.

    Measured on the real deployment this was built for:

        bundle assumes   /opt/homeassistant/config/
        tiger1 actually  /mnt/docker_data/homeassistant/config/

    Every artefact was affected — the pre-flight invocation in both notify
    scripts, and the rsync SOURCE and DEST. Not one of them would have found
    anything. This is F4 ("the two nodes' /config mounts differ") seen from
    a third angle: the bundle's assumed path matches *neither* node, so the
    unit fails on the first run rather than doing something subtly wrong,
    which is at least the better failure.
    """
    cfg = {**WARM, CONF_HA_CONFIG_PATH: "/mnt/docker_data/homeassistant/config"}
    for name, body in build_bundle(cfg).items():
        assert "/opt/homeassistant/config" not in body, (
            f"{name} hardcodes the default path instead of using the configured one"
        )


def test_ar_0042_the_configured_path_reaches_every_artefact_that_needs_it() -> None:
    """Production change that would make this fail: threading the setting into
    one artefact but not another.

    The pre-flight invocation in `notify_master.sh` matters most: it decides
    which devices a promoted node may claim, and pointed at a path that does
    not exist it reports "nothing to disable" — a clean, confident, wrong
    answer, which is worse than an error. `cluster-fileset-pull.sh` and
    `cluster-fileset-swap.sh` need the same setting to find `.storage` on the
    host at all -- the artefacts that took over from ADR-001 tier 1's rsync,
    which used to be checked here too.

    `notify_backup.sh` deliberately does not appear here. Demoting only stops
    the container; it reads no storage. An earlier version of this test
    asserted it did, which would have driven a change to make the code match
    a wrong belief about it.
    """
    path = "/srv/ha/config"
    for label, base in (("cold", COLD), ("warm", WARM)):
        bundle = build_bundle({**base, CONF_HA_CONFIG_PATH: path})
        assert f"{path}/.storage" in bundle["notify_master.sh"], label

    fileset_bundle = build_bundle({**FS, CONF_HA_CONFIG_PATH: path})
    assert f"CONFIG={path}\n" in fileset_bundle["cluster-fileset-pull.sh"]
    assert f"CONFIG={path}\n" in fileset_bundle["cluster-fileset-swap.sh"]


# -- Fileset replication artefacts -------------------------------------------
#
# The pull runs on the HOST, borrowed into the Home Assistant image, and
# stages into `.cluster_sync_staged` -- it never touches live `.storage`. The
# swap is the only thing that moves staged into place, and it runs at
# promotion, before Home Assistant starts.

_PACKAGE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "cluster_state_sync"
)

FS = {**COLD, CONF_FILESET_ENABLED: True, CONF_CLUSTER_SECRET: "s3cr3t-for-tests"}


def test_the_fileset_artefacts_appear_only_when_enabled() -> None:
    """Production change that would make this fail: emitting the pull unit
    unconditionally. An operator who did not opt in would find a timer on their
    host reaching for a Valkey key that does not exist."""
    assert "cluster-fileset-pull.sh" not in build_bundle(COLD)
    assert "cluster-fileset-pull.sh" in build_bundle(FS)


def test_the_key_file_is_the_only_artefact_carrying_the_derived_key() -> None:
    """`write_bundle` used to promise the bundle carried the cluster secret
    nowhere. It now carries exactly one file that lets a host decrypt.

    That file holds the *derived* fileset key, not the raw secret --
    `derive_fileset_key` is an HKDF derivation (crypto.py: "the two uses fail
    independently"), which is one-way. That makes the brief's literal
    assertion -- that the raw string "s3cr3t-for-tests" appears in the key
    file -- impossible by construction: hex output is drawn only from
    `0-9a-f`, and the test secret contains characters (s, r, t, o, ...) that
    are not hex digits, so it can never be a substring of any hex string,
    regardless of implementation. Verified directly:
    `derive_fileset_key("s3cr3t-for-tests").hex()` contains no such substring.

    So this checks the property that is actually true and actually matters:
    the derived key material appears in exactly the one artefact meant to
    carry it, and the raw secret string appears in none -- not even the file
    that can decrypt with it, which is a *stronger* guarantee than the brief's
    literal wording asked for.
    """
    bundle = build_bundle(FS)
    derived_hex = derive_fileset_key(FS[CONF_CLUSTER_SECRET]).hex()

    carriers = [name for name, body in bundle.items() if derived_hex in body]
    assert carriers == ["cluster-fileset.key"]

    assert not any("s3cr3t-for-tests" in body for body in bundle.values())


def test_the_key_file_is_written_owner_only(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: writing it 0644 like the
    other artefacts. It decrypts every credential in the config directory."""
    import stat

    write_bundle(str(tmp_path), FS)
    mode = (tmp_path / "cluster-fileset.key").stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH)


def test_the_pull_stages_rather_than_writing_live_storage() -> None:
    """Production change that would make this fail: pulling straight into
    `.storage`. Under a running Home Assistant that is unsafe -- HA holds the
    files in memory and overwrites you -- and under a stopped one it destroys
    the rollback copy."""
    body = build_bundle(FS)["cluster-fileset-pull.sh"]
    assert ".cluster_sync_staged" in body
    assert "/.storage " not in body


def test_the_pull_only_runs_on_a_follower() -> None:
    """The leader is the source. A leader that pulled would overwrite its own
    identity with an older copy of itself."""
    body = build_bundle(FS)["cluster-fileset-pull.sh"]
    assert "vrrp-state" in body
    assert "MASTER" in body


def test_the_swap_writes_a_marker_when_the_go_bag_is_missing() -> None:
    """Decision D4: promote anyway, alarm loudly. That makes the marker the
    only safety net, so it must be written before the container starts."""
    body = build_bundle(FS)["cluster-fileset-swap.sh"]
    assert ".cluster_sync_degraded.json" in body
    assert "no_staged_fileset" in body


def test_the_swap_refuses_on_a_failed_verification_but_not_on_staleness() -> None:
    """A corrupt go-bag is worse than an old one; an old one still beats none."""
    body = build_bundle(FS)["cluster-fileset-swap.sh"]
    assert "verify_failed" in body
    assert "stale" in body


def test_the_bundle_ships_the_fileset_pull_program_and_its_crypto_dependency() -> None:
    """`fileset_pull.py`'s host-path import falls back to a bare `crypto`
    module beside it on `sys.path[0]` -- Python puts a script's own directory
    there. Shipping the pull without `crypto.py` ships something that cannot
    import, and it fails at the worst possible moment: during a promotion.

    `resp.py` is the same hazard one level further down: `fileset_pull.py`
    falls back to a bare `resp` beside it the same way, for `ValkeyClient`.
    It was split out of `fileset_pull.py` specifically so the lease promoter
    could use it without dragging in `crypto.py`'s `cryptography` dependency
    -- shipping the pull without it reintroduces exactly the ImportError that
    split was meant to avoid, just on the pull's side instead of the
    promoter's.

    Byte-identical, not merely present: a drifted copy is a silent failure --
    nothing exercises the bundled text until a real promotion needs it.
    """
    bundle = build_bundle(FS)
    assert bundle["fileset_pull.py"] == (_PACKAGE_DIR / "scripts" / "fileset_pull.py").read_text(
        encoding="utf-8"
    )
    assert bundle["crypto.py"] == (_PACKAGE_DIR / "crypto.py").read_text(encoding="utf-8")
    assert bundle["resp.py"] == (_PACKAGE_DIR / "scripts" / "resp.py").read_text(encoding="utf-8")


def test_the_fileset_artefacts_are_all_pruneable() -> None:
    """Regeneration must be able to remove what it previously wrote -- e.g. an
    operator who disables the fileset feature after trying it."""
    for name in (
        "fileset_pull.py",
        "crypto.py",
        "cluster-fileset-pull.sh",
        "cluster-fileset-pull.service",
        "cluster-fileset-pull.timer",
        "cluster-fileset-swap.sh",
        "cluster-fileset-identity.py",
        "cluster-fileset.key",
    ):
        assert name in MANAGED_FILENAMES, name


def test_disabling_the_fileset_after_trying_it_removes_its_artefacts(
    tmp_path: pathlib.Path,
) -> None:
    """Mirrors `test_switching_warm_to_cold_removes_the_stale_firewall_rules`:
    an operator who turns the feature off must not be left with a stale timer
    still reaching for a Valkey key, or a stale key file still on disk."""
    write_bundle(str(tmp_path), FS)
    assert (tmp_path / "cluster-fileset.key").exists()

    write_bundle(str(tmp_path), COLD)

    for name in (
        "cluster-fileset-pull.sh",
        "cluster-fileset-pull.service",
        "cluster-fileset-pull.timer",
        "cluster-fileset-swap.sh",
        "cluster-fileset-identity.py",
        "cluster-fileset.key",
        "fileset_pull.py",
        "crypto.py",
    ):
        assert not (tmp_path / name).exists(), name


def test_install_readme_documents_the_key_file_when_fileset_is_enabled() -> None:
    """The brief's requirement: the key file's purpose is documented in
    INSTALL.md, never inline in the key file itself -- which must contain
    only the hex key, because `fileset_pull.py` parses it with
    `bytes.fromhex(path.read_text().strip())`."""
    readme = build_bundle(FS)["INSTALL.md"]
    assert "cluster-fileset.key" in readme
    assert "0600" in readme


def test_the_key_file_contains_only_the_hex_key() -> None:
    """No header, no comment -- `fileset_pull.py` reads it with
    `bytes.fromhex(path.read_text().strip())`, which raises on anything else."""
    body = build_bundle(FS)["cluster-fileset.key"]
    assert body == derive_fileset_key(FS[CONF_CLUSTER_SECRET]).hex() + "\n"
    bytes.fromhex(body.strip())  # raises ValueError if this is not pure hex


# -- The Valkey credential the host-side pull needs (Task 12) ---------------
#
# `fileset_pull.py` now speaks RESP itself, because the Home Assistant image
# does not ship `redis`. Speaking it means authenticating, which means the
# password has to reach the host -- and it must not reach it through argv,
# which `ps` shows to every user on the box.

FS_AUTH = {
    **FS,
    CONF_REDIS_PASSWORD: "valkey-p4ssword",
    CONF_REDIS_USERNAME: "ha-cluster-sync",
    CONF_REDIS_DB: 2,
}


def test_the_valkey_password_reaches_the_host_in_its_own_file() -> None:
    """The pull cannot authenticate without it, and the wizard is the only
    place that knows it."""
    bundle = build_bundle(FS_AUTH)
    assert VALKEY_PASSWORD_FILENAME in bundle
    assert "valkey-p4ssword" in bundle[VALKEY_PASSWORD_FILENAME]


def test_the_password_file_is_the_only_artefact_carrying_the_password() -> None:
    """Mirrors `test_the_key_file_is_the_only_artefact_carrying_the_derived_key`.

    In particular it must NOT be in `cluster-fileset-pull.sh`: that file is
    installed 0755 and read by anyone, and inlining the password there would
    also put it into the `docker run` command line, where `ps` publishes it.
    """
    bundle = build_bundle(FS_AUTH)
    carriers = [name for name, body in bundle.items() if "valkey-p4ssword" in body]
    assert carriers == [VALKEY_PASSWORD_FILENAME]


def test_the_password_file_is_written_owner_only(tmp_path: pathlib.Path) -> None:
    """0600, exactly as `cluster-fileset.key` is. It is the credential to the
    store that holds every mirrored `.storage` file."""
    import stat

    write_bundle(str(tmp_path), FS_AUTH)
    mode = (tmp_path / VALKEY_PASSWORD_FILENAME).stat().st_mode
    assert not mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH)


def test_the_password_file_is_a_shell_assignment_the_pull_script_can_source() -> None:
    """It is `.`-sourced under `set -euo pipefail`, so it has to be exactly one
    assignment and nothing that could execute."""
    body = build_bundle(FS_AUTH)[VALKEY_PASSWORD_FILENAME]
    assert body.startswith("CLUSTER_SYNC_VALKEY_PASSWORD=")
    assert len(body.strip().splitlines()) == 1


def test_a_password_with_shell_metacharacters_survives_being_sourced(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: interpolating the password
    raw. A password containing `$`, a quote or a backtick would then be
    expanded or executed by the shell that sources it -- at best a wrong
    password and a failed pull, at worst arbitrary code run as root from a
    systemd timer."""
    nasty = "p4ss'w`hostname`$USER\"d"
    body = build_bundle({**FS_AUTH, CONF_REDIS_PASSWORD: nasty})[VALKEY_PASSWORD_FILENAME]
    env_file = tmp_path / VALKEY_PASSWORD_FILENAME
    env_file.write_text(body, encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; . "{env_file}"; printf %s "$CLUSTER_SYNC_VALKEY_PASSWORD"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == nasty


def test_no_password_configured_emits_no_password_file() -> None:
    """An empty 0600 file would be one more thing to explain in INSTALL.md and
    one more thing to get wrong. A `requirepass`-less Valkey needs nothing."""
    assert VALKEY_PASSWORD_FILENAME not in build_bundle(FS)


def test_the_pull_script_sources_and_exports_the_password_rather_than_passing_it() -> None:
    """`ps` shows every process's argv to every user on the host, and this runs
    from a systemd timer on a machine that runs other things. `docker run -e
    NAME` (no `=value`) passes the variable through by name, so the value never
    appears on a command line either."""
    body = build_bundle(FS_AUTH)["cluster-fileset-pull.sh"]
    assert VALKEY_PASSWORD_FILENAME in body
    assert "export CLUSTER_SYNC_VALKEY_PASSWORD" in body
    assert "-e CLUSTER_SYNC_VALKEY_PASSWORD \\" in body
    assert "--password" not in body
    assert "CLUSTER_SYNC_VALKEY_PASSWORD=" not in body


def test_the_pull_script_names_the_database_the_publisher_writes_to() -> None:
    """The second Task 12 defect: the pull connected to db 0 while the
    publisher wrote to db 2, so it reported "no fileset manifest published"
    against a Valkey that had one."""
    assert "--db 2" in build_bundle(FS_AUTH)["cluster-fileset-pull.sh"]
    assert "--db 7" in build_bundle({**FS_AUTH, CONF_REDIS_DB: 7})["cluster-fileset-pull.sh"]


def test_the_pull_script_passes_the_acl_username() -> None:
    """ADD 24 §4.2 provisions a named user, and `AUTH <password>` alone
    silently authenticates as `default` instead."""
    body = build_bundle(FS_AUTH)["cluster-fileset-pull.sh"]
    assert "--username ha-cluster-sync" in body
    assert "--username" not in build_bundle(FS)["cluster-fileset-pull.sh"]


def test_the_pull_script_turns_tls_on_and_mounts_the_ca() -> None:
    """The production Valkey is TLS-only (ADD 24 §4.1 sets `--port 0`), so a
    pull without `--tls` cannot connect at all -- and one with `--tls` but no
    CA would verify a step-ca certificate against the system store and fail."""
    body = build_bundle(
        {**FS_AUTH, CONF_REDIS_USE_TLS: True, CONF_REDIS_TLS_CA_CERTS: "/ssl/valkey-ca.crt"}
    )["cluster-fileset-pull.sh"]
    assert "--tls \\" in body
    assert "--tls-ca-file /ca" in body
    assert "/ssl/valkey-ca.crt:/ca:ro" in body


def test_without_tls_the_pull_script_neither_asks_for_it_nor_mounts_a_ca() -> None:
    body = build_bundle(FS_AUTH)["cluster-fileset-pull.sh"]
    assert "--tls" not in body
    assert ":/ca:ro" not in body


def test_tls_without_a_ca_uses_the_system_trust_store_rather_than_no_verification() -> None:
    """There is no insecure toggle anywhere in this path, and this is where
    someone would try to add one. Omitting the CA is still verification."""
    body = build_bundle({**FS_AUTH, CONF_REDIS_USE_TLS: True})["cluster-fileset-pull.sh"]
    assert "--tls \\" in body
    assert "--tls-ca-file" not in body
    assert ":/ca:ro" not in body


def test_the_password_file_is_pruneable_like_every_other_artefact() -> None:
    assert VALKEY_PASSWORD_FILENAME in MANAGED_FILENAMES


def test_removing_the_valkey_password_removes_the_file_it_was_written_to(
    tmp_path: pathlib.Path,
) -> None:
    """An operator who clears the password -- or disables the fileset -- must
    not be left with the old credential sitting on the host."""
    write_bundle(str(tmp_path), FS_AUTH)
    assert (tmp_path / VALKEY_PASSWORD_FILENAME).exists()

    write_bundle(str(tmp_path), FS)
    assert not (tmp_path / VALKEY_PASSWORD_FILENAME).exists()


def test_install_readme_documents_the_password_file_and_its_mode() -> None:
    readme = build_bundle(FS_AUTH)["INSTALL.md"]
    assert VALKEY_PASSWORD_FILENAME in readme
    assert "0600" in readme


# -- Fileset swap/pull: executed under bash, not just grepped ---------------
#
# Review finding: every test above checks that the right *strings* appear in
# the generated script. Neither of the two real bugs the review caught --
# `mark swap_failed` not actually stopping the swap, and the follower check
# failing OPEN on an unknown VRRP state -- changed which strings were present.
# Both bugs are entirely about control flow, so the only test that can catch
# them (or a regression back to them) actually runs the generated script
# under `bash` and inspects what it did to a real, disposable directory tree.


def _fileset_cfg(config_dir: pathlib.Path, **overrides: object) -> dict[str, object]:
    cfg: dict[str, object] = {
        **COLD,
        CONF_FILESET_ENABLED: True,
        CONF_CLUSTER_SECRET: "s3cr3t-for-tests",
        CONF_HA_CONFIG_PATH: str(config_dir),
    }
    cfg.update(overrides)
    return cfg


def _write_script(tmp_path: pathlib.Path, cfg: dict, artefact: str) -> pathlib.Path:
    body = build_bundle(cfg)[artefact]
    path = tmp_path / artefact
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _install_swap(tmp_path: pathlib.Path, cfg: dict) -> pathlib.Path:
    """Write the swap script AND the identity helper it execs beside it.

    They sit together in `/etc/cluster-sync` in production, and the swap finds
    the helper with `$(dirname "$0")` — so a test that wrote only the swap
    would be rehearsing a deployment that cannot happen.
    """
    _write_script(tmp_path, cfg, "cluster-fileset-identity.py")
    return _write_script(tmp_path, cfg, "cluster-fileset-swap.sh")


def _entries_after(config: pathlib.Path) -> list[dict]:
    raw = (config / ".storage" / "core.config_entries").read_text(encoding="utf-8")
    return json.loads(raw)["data"]["entries"]


def _stub_bin(bin_dir: pathlib.Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)


def _run(
    script: pathlib.Path, bin_dir: pathlib.Path, *, env: dict | None = None
) -> subprocess.CompletedProcess:
    # logger is stubbed everywhere: the real binary works fine here (verified
    # separately), but a hermetic test should not depend on syslog being
    # writable in whatever environment eventually runs this.
    _stub_bin(bin_dir, "logger", "#!/bin/sh\nexit 0\n")
    full_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", **(env or {})}
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=full_env, timeout=30
    )


def _entries_doc(entries: list[dict]) -> str:
    """A `core.config_entries` store file, shaped as Home Assistant writes it."""
    return json.dumps(
        {
            "version": 1,
            "minor_version": 1,
            "key": "core.config_entries",
            "data": {"entries": entries},
        },
        indent=2,
    )


def _cluster_entry(node_id: str) -> dict:
    """This integration's own config entry -- the half that must NOT be inherited.

    Every per-node key is present, not just `node_id`: `peer_host` inherited
    makes a promoted node its own peer, and the two nodes' `ha_config_path`
    are already known to differ (finding F4).
    """
    return {
        "entry_id": f"entry-{node_id}",
        "domain": "cluster_state_sync",
        "title": f"default@{node_id}",
        "unique_id": f"default@{node_id}",
        "data": {
            "node_id": node_id,
            "peer_host": f"{node_id}-peer.lan",
            "ha_container_ip": "10.0.0.1" if node_id.endswith("A") else "10.0.0.2",
            "ha_config_path": f"/srv/{node_id}/config",
            "cluster_secret": "s3cr3t-for-tests",
            "redis_password": "valkey-p4ssword",
        },
        "options": {},
    }


def _foreign_entries(owner: str) -> list[dict]:
    """Everything else in the same file, which MUST be inherited wholesale."""
    return [
        {
            "entry_id": f"mobile-{owner}",
            "domain": "mobile_app",
            "title": f"{owner} phone",
            "data": {"device_name": f"{owner} phone", "webhook_id": f"wh-{owner}"},
            "options": {},
        },
        {
            "entry_id": f"zha-{owner}",
            "domain": "zha",
            "title": f"{owner} zigbee",
            "data": {"radio_type": "ezsp"},
            "options": {},
        },
    ]


def _make_live_storage(config_dir: pathlib.Path, node_id: str = "node-B") -> pathlib.Path:
    """This node's own `.storage`, as it stands the instant before a promotion.

    Every swap test needs one: a node running this integration has a
    `core.config_entries` naming itself, and the swap now has to carry that
    entry across while inheriting everything else.
    """
    live = config_dir / ".storage"
    live.mkdir(parents=True, exist_ok=True)
    (live / "auth").write_text(f"{node_id}-own-tokens", encoding="utf-8")
    (live / "core.config_entries").write_text(
        _entries_doc([_cluster_entry(node_id), *_foreign_entries(node_id)]), encoding="utf-8"
    )
    return live


def _make_staged_storage(config_dir: pathlib.Path, *, age_seconds: float) -> pathlib.Path:
    """A complete, well-formed staged go-bag -- everything a real pull would
    have produced, and everything the swap is supposed to install."""
    staged = config_dir / STAGED_DIR_NAME
    storage = staged / "storage"
    (storage / ".storage").mkdir(parents=True)
    (storage / ".storage" / "auth").write_text("refresh-token-data")
    # The leader's config entries: its own `cluster_state_sync` entry, which
    # the promoted node must not inherit, alongside the `mobile_app`
    # registrations it must.
    (storage / ".storage" / "core.config_entries").write_text(
        _entries_doc([_cluster_entry("node-A"), *_foreign_entries("node-A")]), encoding="utf-8"
    )
    (storage / "custom_components" / "cluster_state_sync").mkdir(parents=True)
    (storage / "custom_components" / "cluster_state_sync" / "__init__.py").write_text("# stub")
    (storage / "www").mkdir(parents=True)
    (storage / "www" / "icon.png").write_text("fake-png")
    (storage / "blueprints").mkdir(parents=True)
    (storage / "blueprints" / "example.yaml").write_text("blueprint: true\n")
    (storage / "configuration.yaml").write_text("homeassistant:\n")
    status = staged / "status.json"
    status.write_text('{"generation": 1}')
    mtime = time.time() - age_seconds
    os.utime(status, (mtime, mtime))
    return staged


def _marker(config_dir: pathlib.Path) -> dict:
    return json.loads((config_dir / DEGRADED_MARKER_NAME).read_text(encoding="utf-8"))


def test_swap_installs_a_fresh_staged_copy_and_clears_any_marker(
    tmp_path: pathlib.Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    _make_staged_storage(config, age_seconds=5)
    _make_live_storage(config)
    cfg = _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 1800})
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert (config / ".storage" / "auth").read_text() == "refresh-token-data"
    assert (config / "custom_components" / "cluster_state_sync" / "__init__.py").exists()
    assert (config / "www" / "icon.png").exists()
    assert (config / "blueprints" / "example.yaml").exists()
    assert (config / "configuration.yaml").exists()
    assert not (config / DEGRADED_MARKER_NAME).exists()


def test_swap_preserves_the_pre_existing_storage_group_across_a_reinstall(
    tmp_path: pathlib.Path,
) -> None:
    """Real consequence flagged by the whole-branch review, 2026-08-30: the pull
    runs as root inside the borrowed Home Assistant image and `cp -a` preserves
    that, so on a non-root Home Assistant deployment (`CONF_HA_UID`, a
    supported configuration) a promoted node could no longer read its own
    `.storage` -- partially true before (0644 files stayed readable), total
    now that the staging directories added earlier in this wave are 0700.

    A genuine uid mismatch cannot be fabricated here without root (`chown` to
    another user needs `CAP_CHOWN`), so this proves the mechanism on the GROUP
    instead: the file's own owner may `chgrp` it to any group they belong to,
    with no special privilege, which makes a real, non-root reproduction
    possible. `_make_staged_storage` builds the go-bag under this process's
    own default group; without capturing and restoring the pre-existing
    group, `cp -a` would silently leave that default behind on the
    reinstalled `.storage` -- exactly the failure this test pins.
    """
    my_gid = os.getgid()
    alt_gid = next((g for g in os.getgroups() if g != my_gid), None)
    if alt_gid is None:
        pytest.skip("test user belongs to only one group; cannot exercise a group change")

    config = tmp_path / "config"
    config.mkdir()
    _make_staged_storage(config, age_seconds=5)
    live = _make_live_storage(config)
    os.chown(live, -1, alt_gid)
    assert os.stat(live).st_gid == alt_gid, "setup did not actually change the group"

    cfg = _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 1800})
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert not (config / DEGRADED_MARKER_NAME).exists()
    assert os.stat(config / ".storage").st_gid == alt_gid, (
        "the reinstalled .storage lost the deployment's pre-existing group ownership"
    )


def test_swap_preserves_the_pre_existing_custom_components_group_across_a_reinstall(
    tmp_path: pathlib.Path,
) -> None:
    """The severe case the review escalated this to, 2026-08-30: `cp -a`
    overwrites an EXISTING destination's ownership too, not just a freshly
    created one -- verified directly against a real `cp -a`, not assumed. So on
    the wizard's own default (`CONF_HA_UID=1000`; non-root Home Assistant is a
    supported deployment), every fileset promotion left the ENTIRE
    `custom_components` tree unreadable by that uid, including this
    integration's own directory. A promoted node that cannot read its own
    `custom_components/cluster_state_sync` cannot load the integration that
    was supposed to have just promoted it -- a guaranteed outage on the
    configuration the wizard steers people toward, not a conditional one.
    """
    my_gid = os.getgid()
    alt_gid = next((g for g in os.getgroups() if g != my_gid), None)
    if alt_gid is None:
        pytest.skip("test user belongs to only one group; cannot exercise a group change")

    config = tmp_path / "config"
    config.mkdir()
    _make_staged_storage(config, age_seconds=5)
    _make_live_storage(config)
    live_cc = config / "custom_components" / "cluster_state_sync"
    live_cc.mkdir(parents=True)
    (live_cc / "__init__.py").write_text("# pre-existing")
    os.chown(config / "custom_components", -1, alt_gid)
    os.chown(live_cc, -1, alt_gid)

    cfg = _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 1800})
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert os.stat(config / "custom_components").st_gid == alt_gid, (
        "custom_components lost its pre-existing group -- the failover integration "
        "would be unreadable by a non-root Home Assistant after this promotion"
    )
    assert os.stat(live_cc).st_gid == alt_gid, (
        "the -R restore did not reach the directory this integration actually lives in"
    )


def test_swap_gives_a_never_before_installed_directory_the_configs_own_group(
    tmp_path: pathlib.Path,
) -> None:
    """The other detail worth getting right: a destination that has never
    existed on this node before (e.g. `www/`, if this standby never had custom
    frontend assets) has no pre-existing ownership to restore. Inheriting
    `$CONFIG`'s own group is the chosen default -- the config directory
    already belongs to whoever is meant to own everything under it -- rather
    than leaving it at whatever `cp -a` produced from the root-owned staged
    tree.
    """
    my_gid = os.getgid()
    alt_gid = next((g for g in os.getgroups() if g != my_gid), None)
    if alt_gid is None:
        pytest.skip("test user belongs to only one group; cannot exercise a group change")

    config = tmp_path / "config"
    config.mkdir()
    _make_staged_storage(config, age_seconds=5)
    _make_live_storage(config)
    os.chown(config, -1, alt_gid)
    assert not (config / "www").exists(), "setup must not pre-create www"

    cfg = _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 1800})
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert (config / "www" / "icon.png").exists()
    assert os.stat(config / "www").st_gid == alt_gid, (
        "a directory installed for the first time did not inherit $CONFIG's own group"
    )


def test_swap_preserves_the_pre_existing_yaml_file_group_across_a_reinstall(
    tmp_path: pathlib.Path,
) -> None:
    """The YAML files are copied individually with `cp -a`, not swapped as a
    tree -- verified directly, not assumed, that `cp -a` onto an EXISTING
    destination file still overwrites its ownership to match the source, so
    `configuration.yaml` needed exactly the same capture-and-restore treatment
    as the directories above, not a different one.
    """
    my_gid = os.getgid()
    alt_gid = next((g for g in os.getgroups() if g != my_gid), None)
    if alt_gid is None:
        pytest.skip("test user belongs to only one group; cannot exercise a group change")

    config = tmp_path / "config"
    config.mkdir()
    _make_staged_storage(config, age_seconds=5)
    _make_live_storage(config)
    live_yaml = config / "configuration.yaml"
    live_yaml.write_text("homeassistant:\n")
    os.chown(live_yaml, -1, alt_gid)

    cfg = _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 1800})
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert os.stat(live_yaml).st_gid == alt_gid, (
        "configuration.yaml lost its pre-existing group ownership across the swap"
    )


def test_swap_installs_a_stale_staged_copy_but_marks_it(tmp_path: pathlib.Path) -> None:
    """Stale still beats nothing (D4): the files are installed either way, but
    the marker records that the identity a promoted node just got is old."""
    config = tmp_path / "config"
    config.mkdir()
    _make_staged_storage(config, age_seconds=500)
    _make_live_storage(config)
    cfg = _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 100})
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert (config / "configuration.yaml").exists()
    marker = _marker(config)
    assert marker["reason"] == "stale"
    assert marker["age_s"] > 100


def test_swap_refuses_a_failed_verification_and_installs_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """Also exercises the ordering fix: `fileset_pull.py` creates the staging
    directory before it authenticates anything, so a first-ever pull whose
    manifest fails authentication has `verify_failed` set and no established
    `storage/` -- both the verify_failed and the no_staged_fileset checks
    would fire under the old check order. The marker must record the
    specific, accurate reason (verify_failed), not the generic one."""
    config = tmp_path / "config"
    config.mkdir()
    staged = config / STAGED_DIR_NAME
    staged.mkdir(parents=True)
    (staged / "verify_failed").write_text("manifest: blob failed authentication\n")
    cfg = _fileset_cfg(config)
    script = _write_script(tmp_path, cfg, "cluster-fileset-swap.sh")

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert not (config / "custom_components").exists()
    assert not (config / "configuration.yaml").exists()
    marker = _marker(config)
    assert marker["reason"] == "verify_failed"


def test_swap_marks_and_skips_when_nothing_is_staged(tmp_path: pathlib.Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    cfg = _fileset_cfg(config)
    script = _write_script(tmp_path, cfg, "cluster-fileset-swap.sh")

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert not (config / "configuration.yaml").exists()
    marker = _marker(config)
    assert marker["reason"] == "no_staged_fileset"


def test_swap_leaves_the_marker_in_place_after_a_partial_bulk_copy_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Regression test for review Finding 1.

    The bug: `swap_dir ... || mark swap_failed` in the bulk loop wrote the
    marker but did not stop the script, so a fresh-but-partially-broken swap
    fell through to the success path, which cleared the marker (destroying
    the only evidence anything went wrong) and logged success over an install
    that did not fully happen.

    Forces exactly one `swap_dir` call (`custom_components`) to fail by
    making its *source* directory unreadable -- independent of the bulk
    loop's own `rm -rf "$dst.new"` (which would simply delete a pre-created
    blocking file at the destination, undoing that setup before `cp -a` ever
    ran) and independent of the sibling `.storage`/`www`/`blueprints` swaps,
    which use different source paths entirely.

    This holds only under a non-root test runner: root bypasses directory
    read permission outright, so the induced failure would not occur and the
    test would pass for the wrong reason (or rather, not exercise the bug at
    all). Skips under root instead of silently proving nothing.
    """
    if os.geteuid() == 0:
        pytest.skip("permission-based failure injection does not hold as root")

    config = tmp_path / "config"
    config.mkdir()
    staged = _make_staged_storage(config, age_seconds=5)
    _make_live_storage(config)
    blocked = staged / "storage" / "custom_components"
    blocked.chmod(0o000)
    try:
        cfg = _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 1800})
        script = _install_swap(tmp_path, cfg)

        result = _run(script, tmp_path / "bin")

        assert result.returncode == 0, result.stderr
        # Best effort, not bail-on-first-failure: the unrelated, unblocked
        # entries still installed.
        assert (config / ".storage" / "auth").exists()
        assert (config / "www" / "icon.png").exists()
        # The partial install must be visible, never erased.
        assert (config / DEGRADED_MARKER_NAME).exists(), (
            "the marker was cleared despite a failed directory swap -- "
            "this is the Finding 1 regression"
        )
        marker = _marker(config)
        assert marker["reason"] == "swap_failed"
    finally:
        blocked.chmod(0o755)
        # `cp -a` sets a directory's mode to match its (unreadable) source as
        # soon as it creates it, before it fails to populate it -- so the
        # induced failure above also leaves `custom_components.new` behind
        # with mode 000. Restore it too, or neither this test nor pytest's
        # own tmp_path teardown can remove it, and it leaks onto disk.
        leftover = config / "custom_components.new"
        if leftover.exists():
            leftover.chmod(0o755)


def _stub_docker(bin_dir: pathlib.Path, run_log: pathlib.Path) -> None:
    """A `docker` stand-in on PATH.

    `inspect` answers so `IMAGE=$(docker inspect ...)` succeeds under
    `set -e` without a real image or a real daemon. `run` records that it was
    reached and exits non-zero -- these tests only assert that the script got
    past the follower guard and attempted the pull, never that the pull
    itself succeeded, so the stub does not need to do anything real.
    """
    _stub_bin(
        bin_dir,
        "docker",
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  inspect) echo 'stub-image:latest' ;;\n"
        f"  run) echo ran >> {run_log}; exit 1 ;;\n"
        "esac\n",
    )


def _run_pull(
    tmp_path: pathlib.Path, *, state: str | None
) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    """Run the generated pull script with `CLUSTER_SYNC_STATE_FILE` pointed
    at a throwaway file in `tmp_path`. `state=None` leaves that file absent
    entirely, matching a boot race before Keepalived has written anything --
    Keepalived creates this file; nothing else does, so "does not exist yet"
    is a real, reachable state, not an edge case invented for the test."""
    config = tmp_path / "config"
    config.mkdir()
    cfg = _fileset_cfg(config)
    script = _write_script(tmp_path, cfg, "cluster-fileset-pull.sh")
    bin_dir = tmp_path / "bin"
    run_log = tmp_path / "docker-run.log"
    _stub_docker(bin_dir, run_log)

    state_file = tmp_path / "vrrp-state"
    if state is not None:
        state_file.write_text(state)

    result = _run(script, bin_dir, env={"CLUSTER_SYNC_STATE_FILE": str(state_file)})
    return result, run_log


def test_pull_skips_when_the_vrrp_state_file_is_missing(tmp_path: pathlib.Path) -> None:
    """Regression test for review Finding 2.

    The bug: the original check only skipped pulling when the state file
    existed AND said MASTER, so a missing file -- a boot race, Keepalived not
    yet settled -- fell through to the pull. That is fail OPEN on an unknown
    state, exactly backwards for an action a leader must never take.
    """
    result, run_log = _run_pull(tmp_path, state=None)

    assert result.returncode == 0, result.stderr
    assert not run_log.exists(), "pulled despite a missing (unknown) VRRP state file"


def test_pull_skips_on_a_confirmed_leader(tmp_path: pathlib.Path) -> None:
    result, run_log = _run_pull(tmp_path, state="MASTER")

    assert result.returncode == 0, result.stderr
    assert not run_log.exists(), "pulled while the state file said MASTER"


def test_pull_proceeds_past_the_guard_on_a_confirmed_follower(tmp_path: pathlib.Path) -> None:
    """The mirror image of the two tests above: a state the script can
    positively confirm is NOT the leader must actually reach the pull
    attempt. Exit code 1 here is expected and correct -- the stubbed `docker
    run` deliberately fails, and the script's own `|| { ...; exit 1; }`
    handler is what produces it; this test is about reaching that call, not
    about a pull succeeding."""
    result, run_log = _run_pull(tmp_path, state="BACKUP")

    assert result.returncode == 1, result.stderr
    assert run_log.exists(), "never reached the pull attempt on a confirmed follower"


def test_pull_proceeds_on_a_confirmed_fault(tmp_path: pathlib.Path) -> None:
    """FAULT is the other state the gate must positively allow (`_notify_demote`
    records it distinctly from BACKUP so an operator can tell a broken node
    from an idle standby) -- a node in VRRP fault state is still not the
    leader, so it must still pull."""
    result, run_log = _run_pull(tmp_path, state="FAULT")

    assert result.returncode == 1, result.stderr
    assert run_log.exists(), "never reached the pull attempt on a confirmed fault"


def test_pull_skips_on_an_unrecognized_state(tmp_path: pathlib.Path) -> None:
    """The genuine fail-closed regression test. The old gate was `!= "MASTER"`,
    so any garbage that was not exactly that string made the pull RUN -- this
    is the bug the "fails CLOSED" comment claimed did not exist. The write is
    atomic, so a torn read should not happen in practice, but the gate must
    not depend on that: an allow-list of BACKUP/FAULT, not a deny-list of
    MASTER, is what makes an unrecognized value skip rather than proceed."""
    result, run_log = _run_pull(tmp_path, state="garbage-not-a-real-state")

    assert result.returncode == 0, result.stderr
    assert not run_log.exists(), "pulled on a state that was neither BACKUP nor FAULT"


def _stub_docker_recording(bin_dir: pathlib.Path, run_log: pathlib.Path) -> None:
    """`_stub_docker`, but it records what `docker run` was actually given.

    Two separate things need proving and only a real run can prove either: that
    the password arrives in the stub's *environment* (so `-e NAME` pass-through
    worked and the `export` really happened), and that it is absent from the
    stub's *argv* (so nothing put it on a command line for `ps` to publish).
    """
    _stub_bin(
        bin_dir,
        "docker",
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  inspect) echo 'stub-image:latest' ;;\n"
        f'  run) echo "argv: $*" >> {run_log}\n'
        f'       echo "env: ${{CLUSTER_SYNC_VALKEY_PASSWORD-<unset>}}" >> {run_log}\n'
        "       exit 1 ;;\n"
        "esac\n",
    )


def _run_pull_with_password(
    tmp_path: pathlib.Path, *, password_file_present: bool
) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    config = tmp_path / "config"
    config.mkdir()
    cfg = _fileset_cfg(config, **{CONF_REDIS_PASSWORD: "valkey-p4ssword"})
    script = _write_script(tmp_path, cfg, "cluster-fileset-pull.sh")
    bin_dir = tmp_path / "bin"
    run_log = tmp_path / "docker-run.log"
    _stub_docker_recording(bin_dir, run_log)

    state_file = tmp_path / "vrrp-state"
    state_file.write_text("BACKUP")  # a confirmed follower, so the pull proceeds
    password_file = tmp_path / VALKEY_PASSWORD_FILENAME
    if password_file_present:
        password_file.write_text(build_bundle(cfg)[VALKEY_PASSWORD_FILENAME], encoding="utf-8")

    result = _run(
        script,
        bin_dir,
        env={
            "CLUSTER_SYNC_STATE_FILE": str(state_file),
            "CLUSTER_SYNC_PASSWORD_FILE": str(password_file),
        },
    )
    return result, run_log


def test_the_pull_refuses_rather_than_connecting_without_the_password(
    tmp_path: pathlib.Path,
) -> None:
    """A Valkey that answers without a password is not the one this cluster
    publishes to. Staging whatever it returned would be worse than staging
    nothing -- and under `set -u` the alternative is an empty password sent to
    the real server once a minute, forever."""
    result, run_log = _run_pull_with_password(tmp_path, password_file_present=False)

    assert result.returncode == 1
    assert not run_log.exists(), "reached docker run with no password file"


def test_the_password_reaches_the_container_by_environment_and_not_by_argv(
    tmp_path: pathlib.Path,
) -> None:
    """Both halves of the claim, executed rather than grepped: the value is in
    the environment `docker run` inherits, and it is nowhere in its argv --
    which is what `ps` shows to every user on the host."""
    result, run_log = _run_pull_with_password(tmp_path, password_file_present=True)

    assert run_log.exists(), f"never reached the pull attempt: {result.stderr}"
    log = run_log.read_text()
    assert "env: valkey-p4ssword" in log
    argv_line = next(ln for ln in log.splitlines() if ln.startswith("argv: "))
    assert "valkey-p4ssword" not in argv_line, argv_line
    assert "-e CLUSTER_SYNC_VALKEY_PASSWORD" in argv_line


@pytest.mark.parametrize("base", [COLD, WARM], ids=["cold", "warm"])
def test_the_swap_runs_before_the_device_preflight(base: dict[str, object]) -> None:
    """Production change that would make this fail: appending the swap after
    the pre-flight, which reads naturally and is wrong.

    The pre-flight's `--apply` edits `.storage`. Swap afterwards and those
    edits are thrown away by the file that replaces them — so the promoted node
    claims the peer's USB, Zigbee and Bluetooth radios, and reports success.

    Parameterised over cold and warm: `_notify_master` builds the two
    branches independently, so a regression that reordered only one of them
    -- warm, say -- would pass a cold-only assertion. Review finding on
    Task 8: the original test used `FS` (cold only) and left the warm branch's
    ordering unverified, the exact hazard this test exists to pin.
    """
    cfg = {**base, CONF_FILESET_ENABLED: True, CONF_CLUSTER_SECRET: "s3cr3t-for-tests"}
    body = build_bundle(cfg)["notify_master.sh"]
    assert body.index("cluster-fileset-swap.sh") < body.index("ha-device-preflight.py")


def test_the_warm_promotion_stops_before_swap_and_starts_after_preflight() -> None:
    """Identity cannot be installed under a live Home Assistant: on shutdown
    it writes its in-memory stores back to disk, overwriting whatever the
    swap just installed. Rehearsal (Task 11) found this the hard way --
    `docker restart` lost `core.device_registry` and `http` on every run,
    and `.storage/auth` (the refresh tokens this feature exists to carry) on
    one run. Regression this guards against: swapping (or pre-flighting)
    while the container from a previous run is still up, which is exactly
    what `docker restart` did and `stop`-then-`start` does not.

    `body.index(...)` genuinely discriminates here, the same way the
    swap-before-pre-flight test does: reorder any of these four and the
    chained comparison fails.
    """
    warm = {**WARM, CONF_FILESET_ENABLED: True, CONF_CLUSTER_SECRET: "s"}
    body = build_bundle(warm)["notify_master.sh"]
    stop_at = body.index("docker stop")
    swap_at = body.index("cluster-fileset-swap.sh")
    preflight_at = body.index("ha-device-preflight.py")
    start_at = body.index("docker start")
    assert stop_at < swap_at < preflight_at < start_at


def test_the_cold_promotion_never_stops_or_restarts() -> None:
    """Cold starts from an already-stopped container; a stop is a no-op at
    best, and a restart would be a second boot. Neither belongs here."""
    body = build_bundle(FS)["notify_master.sh"]
    assert "docker stop" not in body
    assert "docker restart" not in body
    assert "docker start" in body


@pytest.mark.parametrize(
    "cfg, expect_apply",
    [
        pytest.param(
            {**WARM, CONF_FILESET_ENABLED: True, CONF_CLUSTER_SECRET: "s"},
            True,
            id="warm+fileset",
        ),
        pytest.param(WARM, False, id="warm-without-fileset"),
        pytest.param(FS, True, id="cold"),
    ],
)
def test_the_device_preflight_applies_only_when_home_assistant_is_stopped(
    cfg: dict[str, object], expect_apply: bool
) -> None:
    """The three cases genuinely differ, which is the point of the test.

    Base warm never stops Home Assistant during promotion, so the pre-flight
    stays report-only -- editing `.storage` under a live HA is unsafe there,
    which is why it was report-only to begin with. Warm-with-fileset now
    stops the container before the swap (the rehearsal fix above), so at the
    point the pre-flight runs it is exactly as safe as cold's -- and skipping
    `--apply` there would leave the promoted node with no device-claim
    protection at all, the same hazard the ordering fix exists to prevent,
    arriving by a different route. Cold already applies; asserting it here
    too guards against a fix that made the flag unconditional rather than
    keyed to whether the container is actually stopped.
    """
    body = build_bundle(cfg)["notify_master.sh"]
    assert ("--apply" in body) is expect_apply


@pytest.mark.parametrize("base", [COLD, WARM], ids=["cold", "warm"])
def test_without_the_fileset_the_promotion_is_unchanged(base: dict[str, object]) -> None:
    """Production change that would make this fail: emitting the swap call
    (or, on warm, the stop/start pair) unconditionally, so an operator who
    never opted in gets a promotion that execs a script the bundle did not
    write, or stops a container this feature never asked to touch.

    Parameterised over cold and warm for the same reason as the ordering
    test above: `swap`, `stop`, and `start` are all gated on the same
    `fileset_enabled` local in `_notify_master`, but a cold-only assertion
    would not have caught a regression that leaked any of them into warm
    specifically. `docker restart` is asserted too even though nothing in
    `_notify_master` emits it any more (Task 8 warm restarted; the
    rehearsal-driven fix replaced that with stop/start) -- a regression
    back to restart-on-warm would otherwise slip past every other
    assertion here.
    """
    body = build_bundle(base)["notify_master.sh"]
    assert "cluster-fileset-swap.sh" not in body
    assert "docker stop" not in body
    assert "docker restart" not in body


def test_install_readme_does_not_instruct_manual_wiring_of_the_swap() -> None:
    """Regression test for a correctness bug, not a wording nicety.

    The original text told the operator to "Wire it into `notify_master.sh`,
    ahead of the device pre-flight" by hand. Task 8 made the bundle emit that
    call itself, so following the old instruction runs `swap_dir` twice --
    the bundle's own call, plus the hand-added one. The second run's
    `rm -rf "$dst.previous"` destroys the one generation of rollback the
    first run just created, overwriting it with the copy the first run just
    installed -- discarding the node's original `.storage` at exactly the
    moment someone might need to restore it.

    Assert the property -- the reader is told this already happened
    automatically and is told not to repeat it -- rather than the absence of
    the old sentence's specific wording, so a differently-worded regression
    ("add this call to your promotion script") still fails this test.
    """
    section = build_bundle(FS)["INSTALL.md"].split("## Fileset replication", 1)[1]

    # Affirmative: the reader is told the wiring already happened, and where.
    assert "notify_master.sh" in section
    assert re.search(r"already (calls|wired|does this|emits)", section)

    # Prohibitive: the reader is told not to duplicate it, and told why --
    # not just that the old verb ("wire") is gone.
    assert re.search(r"do not add|must not add|never add|do not.*yourself", section.lower())
    assert "rollback" in section.lower() or ".storage.previous" in section


# -- Identity preservation across the swap (Task 13) ------------------------
#
# `.storage/core.config_entries` carries two things that must be treated
# oppositely: the `mobile_app` registrations, which the promoted node has to
# inherit or the companion app stops working, and this integration's own
# config entry, which it must NOT inherit. That entry holds `node_id`, and the
# lease that prevents two leaders compares identity -- `current == ARGV[1]` --
# so a standby promoted carrying the leader's `node_id` renews the leader's
# own lease, and the returning leader is told it still holds it. Both nodes
# then believe they lead, both publish, and each prunes the other's blobs.
# The same entry also carries `peer_host` -- removed from the wizard in
# 2026-09 and kept here on purpose: a key nothing collects any more still
# has to survive the graft, because the rule is whole-entry preservation
# rather than a list of fields. (A promoted node whose peer is
# itself), `ha_container_ip` and `ha_config_path`.


def _identity_fixture(
    tmp_path: pathlib.Path, *, local_entries: list[dict] | None = None
) -> tuple[pathlib.Path, dict]:
    """A staged go-bag from "node-A" over a live `.storage` belonging to "node-B"."""
    config = tmp_path / "config"
    config.mkdir()
    _make_staged_storage(config, age_seconds=5)
    live = _make_live_storage(config)
    if local_entries is not None:
        (live / "core.config_entries").write_text(_entries_doc(local_entries), encoding="utf-8")
    return config, _fileset_cfg(config, **{CONF_FILESET_STALE_AFTER: 1800})


def test_the_swap_keeps_this_nodes_identity_and_inherits_the_rest(
    tmp_path: pathlib.Path,
) -> None:
    """THE discriminating test.

    The go-bag says `node_id: node-A`; this node is `node-B`. After the swap
    the file must carry **node-B's** `cluster_state_sync` entry and **node-A's**
    `mobile_app` registration. Any fix that preserves the wrong half fails one
    of the two assertions, and the unmodified swap fails the first.
    """
    config, cfg = _identity_fixture(tmp_path)
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    entries = _entries_after(config)
    ours = [e for e in entries if e["domain"] == "cluster_state_sync"]
    assert [e["data"]["node_id"] for e in ours] == ["node-B"], (
        "the promoted node inherited the leader's node_id — it will renew the "
        "leader's lease and both nodes will believe they lead"
    )
    assert ours[0]["data"]["peer_host"] == "node-B-peer.lan"
    assert ours[0]["data"]["ha_config_path"] == "/srv/node-B/config"
    assert ours[0]["unique_id"] == "default@node-B"
    # ...but `entry_id` comes from the go-bag. It is the join key of the
    # `core.entity_registry` and `core.device_registry` this swap inherits in
    # the same breath, not a setting anyone chose; keeping the local one
    # orphans every row referencing it, and Home Assistant pins the orphans
    # to `unavailable` on the ids `failover_readiness.yaml` addresses.
    assert ours[0]["entry_id"] == "entry-node-A"

    inherited = {e["domain"]: e["title"] for e in entries if e["domain"] != "cluster_state_sync"}
    assert inherited == {"mobile_app": "node-A phone", "zha": "node-A zigbee"}, (
        "the go-bag's own registrations did not survive — the companion app "
        "is what this whole feature exists to carry"
    )
    # The rest of the go-bag still landed, and a clean swap clears the marker.
    assert (config / ".storage" / "auth").read_text() == "refresh-token-data"
    assert (config / "configuration.yaml").exists()
    assert not (config / DEGRADED_MARKER_NAME).exists()


def test_the_swap_rolls_back_rather_than_promote_with_the_peers_identity(
    tmp_path: pathlib.Path,
) -> None:
    """Owner's decision, and it deliberately does NOT follow D4.

    D4 ("promote anyway") covers a stale or missing go-bag, which costs
    logins. A restore that failed after `.storage` was replaced costs *two
    writers* -- both nodes renew the same lease, both publish, and each prunes
    the other's blobs -- which is corrupting rather than degrading. So the
    swap puts `.storage.previous` back and marks, and the node promotes on its
    own config: logged out, but identity-safe.

    Injected the way it would really happen: a go-bag whose
    `core.config_entries` is truncated. That is a file the swap installs and
    then cannot edit -- capture (which reads the *live* file) still succeeds,
    so this exercises the restore failure specifically and not a capture one.
    """
    config, cfg = _identity_fixture(tmp_path)
    staged_entries = config / STAGED_DIR_NAME / "storage" / ".storage" / "core.config_entries"
    staged_entries.write_text('{"data": {"entr', encoding="utf-8")
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    # Rolled back: this node's own `.storage`, not the leader's.
    assert (config / ".storage" / "auth").read_text() == "node-B-own-tokens"
    assert [e["data"]["node_id"] for e in _entries_after(config) if "node_id" in e["data"]] == [
        "node-B"
    ]
    # And nothing else was installed either -- a half-inherited node is the
    # thing being avoided, so the swap stops rather than carrying on.
    assert not (config / "configuration.yaml").exists()
    assert _marker(config)["reason"] == "identity_restore_failed"


def test_an_unconfigured_node_does_not_acquire_the_peers_identity(
    tmp_path: pathlib.Path,
) -> None:
    """Decided edge case: a node with no `cluster_state_sync` entry of its own
    has the leader's entries *removed* rather than inheriting them, and the
    promotion is marked.

    Nearly unreachable -- no config means no pull timer means no go-bag -- but
    AR-0040 lived its entire life in exactly that kind of gap. The rest of the
    go-bag still installs: being unconfigured is not a reason to throw away
    the peer's `mobile_app` registrations.
    """
    config, cfg = _identity_fixture(tmp_path, local_entries=_foreign_entries("node-B"))
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    domains = [e["domain"] for e in _entries_after(config)]
    assert "cluster_state_sync" not in domains, (
        "an unconfigured node inherited the peer's identity, and would take "
        "the peer's lease the moment someone configured it"
    )
    assert domains == ["mobile_app", "zha"]
    assert (config / "configuration.yaml").exists()
    # Its own reason, not identity_restore_failed. The two want opposite
    # things from the operator: here the go-bag *was* installed and this node
    # will simply never sync until someone configures it, where
    # identity_restore_failed means the go-bag was not installed at all and
    # the node is running its own previous config.
    assert _marker(config)["reason"] == "no_local_identity"


def test_the_swap_removes_the_captured_identity_file_afterwards(
    tmp_path: pathlib.Path,
) -> None:
    """It holds `cluster_secret` and `redis_password` in the clear, and it
    lives in a world-writable directory on a host that runs other things.
    The trap has to fire on the success path too, not only on the exits."""
    config, cfg = _identity_fixture(tmp_path)
    script = _install_swap(tmp_path, cfg)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = _run(script, tmp_path / "bin", env={"TMPDIR": str(scratch)})

    assert result.returncode == 0, result.stderr
    assert list(scratch.iterdir()) == [], "the captured identity was left behind"


def test_the_captured_identity_is_never_world_readable(tmp_path: pathlib.Path) -> None:
    """Checked on the file the swap actually creates, by freezing it in place:
    the identity helper is replaced with one that copies its `--identity`
    argument aside before the trap can remove it. Asserting the mode on a file
    the test wrote itself would prove nothing about the generated script."""
    config, cfg = _identity_fixture(tmp_path)
    script = _install_swap(tmp_path, cfg)
    kept = tmp_path / "kept.json"
    real = tmp_path / "cluster-fileset-identity.py"
    real.rename(tmp_path / "real-identity.py")
    real.write_text(
        "import shutil, subprocess, sys, pathlib\n"
        "rc = subprocess.run([sys.executable, "
        f"{str(tmp_path / 'real-identity.py')!r}, *sys.argv[1:]]).returncode\n"
        "if sys.argv[1] == 'capture':\n"
        f"    shutil.copy2(sys.argv[sys.argv.index('--identity') + 1], {str(kept)!r})\n"
        "raise SystemExit(rc)\n",
        encoding="utf-8",
    )

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert kept.exists(), "the capture step never wrote an identity file"
    assert "valkey-p4ssword" in kept.read_text(encoding="utf-8"), "wrong file captured"
    assert stat.S_IMODE(kept.stat().st_mode) & 0o077 == 0, oct(kept.stat().st_mode)


@pytest.mark.parametrize("base", [COLD, WARM], ids=["cold", "warm"])
def test_capture_precedes_the_swap_and_restore_precedes_the_preflight(
    base: dict[str, object],
) -> None:
    """The ordering hazard, pinned end to end across the two generated files.

    Capture must precede the swap -- capturing afterwards captures the
    *leader's* entry, which is the entire defect. Restore must precede the
    device pre-flight, so both edits land on the file Home Assistant will
    actually read rather than one being thrown away by the other. This is the
    same class of hazard as swap-before-pre-flight, which has already bitten
    this project once.

    Two files, one chain: the swap script owns capture -> install -> restore,
    and `notify_master.sh` owns swap -> pre-flight -> start. Parameterised
    over cold and warm because `_notify_master` builds those branches
    independently.
    """
    cfg = {**base, CONF_FILESET_ENABLED: True, CONF_CLUSTER_SECRET: "s3cr3t-for-tests"}
    swap = build_bundle(cfg)["cluster-fileset-swap.sh"]
    promote = build_bundle(cfg)["notify_master.sh"]

    tool_at = swap.index("cluster-fileset-identity.py")
    capture_at = swap.index('"$IDENTITY_TOOL" capture')
    install_at = swap.index('swap_dir "$STAGED/storage/.storage"')
    restore_at = swap.index('"$IDENTITY_TOOL" restore')
    assert tool_at < capture_at < install_at < restore_at

    assert promote.index("cluster-fileset-swap.sh") < promote.index("ha-device-preflight.py")
    assert promote.index("ha-device-preflight.py") < promote.index("docker start")


def test_the_identity_helper_ships_verbatim_from_the_package() -> None:
    """Same guarantee as `fileset_pull.py` and the device pre-flight: the
    shipped copy is the tested copy. A transcription here would drift, and it
    is only ever exercised during a promotion."""
    shipped = build_bundle(FS)["cluster-fileset-identity.py"]

    assert shipped == (_PACKAGE_DIR / "scripts" / "fileset_identity.py").read_text(encoding="utf-8")


def test_the_identity_helper_is_not_emitted_without_the_fileset() -> None:
    """It is only ever run by the swap, which the bundle only emits when the
    operator opted in. An unconditional emission would leave a file nothing
    calls in every bundle."""
    assert "cluster-fileset-identity.py" not in build_bundle(COLD)
    assert "cluster-fileset-identity.py" in build_bundle(FS)


def test_the_first_marked_reason_survives_a_later_generic_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Review finding: `mark` had to become first-wins.

    Every `mark` before this task was followed immediately by `exit 0`, so the
    script's promise that the specific reason survives held by construction.
    The identity marks are the first that let the swap carry on -- so a later
    `swap_dir` or `cp -a` failure would overwrite `no_local_identity`, which
    tells the operator to go and configure the node, with `swap_failed`, which
    tells them nothing they can act on.

    Drives both at once: an unconfigured node (marks first) whose
    `custom_components` swap then fails (would mark second).
    """
    if os.geteuid() == 0:
        pytest.skip("permission-based failure injection does not hold as root")

    config, cfg = _identity_fixture(tmp_path, local_entries=_foreign_entries("node-B"))
    blocked = config / STAGED_DIR_NAME / "storage" / "custom_components"
    blocked.chmod(0o000)
    try:
        script = _install_swap(tmp_path, cfg)

        result = _run(script, tmp_path / "bin")

        assert result.returncode == 0, result.stderr
        assert _marker(config)["reason"] == "no_local_identity", (
            "the generic swap_failed overwrote the specific reason that came first"
        )
    finally:
        blocked.chmod(0o755)
        leftover = config / "custom_components.new"
        if leftover.exists():
            leftover.chmod(0o755)


def test_a_stale_marker_from_an_earlier_promotion_does_not_win(
    tmp_path: pathlib.Path,
) -> None:
    """First-wins is scoped to this run, and it has to be.

    The marker is cleared only by a clean swap, so a node that promoted
    degraded once carries the file until it promotes cleanly. Guarding `mark`
    on the file already existing -- rather than on a variable -- would pin the
    operator to a reason from a promotion that could be days old, which is
    worse than the overwrite it was meant to prevent.
    """
    config, cfg = _identity_fixture(tmp_path)
    shutil.rmtree(config / STAGED_DIR_NAME)
    (config / DEGRADED_MARKER_NAME).write_text(
        '{"reason":"stale","age_s":99999,"at":"2020-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert _marker(config)["reason"] == "no_staged_fileset", (
        "this promotion's own reason was suppressed by a marker left over from an earlier one"
    )


def test_a_go_bag_with_a_second_entry_of_ours_rolls_back_rather_than_orphan(
    tmp_path: pathlib.Path,
) -> None:
    """The asymmetric remainder, executed to the marker.

    A go-bag carrying two `cluster_state_sync` entries against this node's one
    cannot be grafted: positional pairing re-homes ours onto the first and the
    second vanishes, orphaning every `core.entity_registry` row that joins on
    its `entry_id` — pinned `unavailable` for good, on the ids
    `failover_readiness.yaml` addresses. The identity helper refuses, and this
    test is what proves the refusal reaches the swap's rollback rather than
    stopping at an exit code nobody acts on.
    """
    config, cfg = _identity_fixture(tmp_path)
    staged_entries = config / STAGED_DIR_NAME / "storage" / ".storage" / "core.config_entries"
    staged_entries.write_text(
        _entries_doc(
            [_cluster_entry("node-A"), _cluster_entry("node-A2"), *_foreign_entries("node-A")]
        ),
        encoding="utf-8",
    )
    script = _install_swap(tmp_path, cfg)

    result = _run(script, tmp_path / "bin")

    assert result.returncode == 0, result.stderr
    assert (config / ".storage" / "auth").read_text() == "node-B-own-tokens"
    assert [e["data"]["node_id"] for e in _entries_after(config) if "node_id" in e["data"]] == [
        "node-B"
    ]
    assert not (config / "configuration.yaml").exists()
    assert _marker(config)["reason"] == "identity_restore_failed"


# -- C1: the VRRP state file, and the tier-1 rsync that must not outlive it --
#
# Both gates in this bundle -- `cluster-fileset-pull.sh` and
# `cluster-config-sync.sh` -- read `/run/cluster-sync/vrrp-state`, and until
# 2026-08-30 nothing wrote it. The pull logged "not confirmed FOLLOWER" once a
# minute forever, the go-bag was never staged, and every promotion marked
# `no_staged_fileset` and reported success: a feature that never once ran,
# whose only symptom was a log line. AR-0040 all over again.
#
# These assertions run the generated scripts rather than grepping them,
# because "the string is present" is exactly what was already true of the two
# readers while the whole mechanism was dead.
#
# `cluster-config-sync.sh` no longer exists in any bundle as of the same date
# (see the I1 section below) -- `cluster-fileset-pull.sh` is now the only
# reader, and only when fileset replication is enabled.


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("notify_master.sh", "MASTER"),
        ("notify_backup.sh", "BACKUP"),
        ("notify_fault.sh", "FAULT"),
    ],
)
@pytest.mark.parametrize("base", [COLD, WARM], ids=["cold", "warm"])
def test_the_notify_scripts_record_the_vrrp_state(
    tmp_path: pathlib.Path, base: dict[str, object], script: str, expected: str
) -> None:
    """C1. Executed, not grepped: the defect this closes was two artefacts that
    contained the path and only ever read it."""
    state = tmp_path / "run" / "vrrp-state"
    cfg = {**base, CONF_HA_CONFIG_PATH: str(tmp_path / "config")}
    path = _write_script(tmp_path, cfg, script)
    _stub_bin(tmp_path / "bin", "docker", "#!/bin/sh\nexit 0\n")
    _stub_bin(tmp_path / "bin", "nft", "#!/bin/sh\nexit 0\n")
    _stub_bin(tmp_path / "bin", "python3", "#!/bin/sh\nexit 0\n")

    result = _run(
        path,
        tmp_path / "bin",
        env={
            "CLUSTER_SYNC_STATE_FILE": str(state),
            # AR-0064 added a readiness wait before the post-start hooks.
            # There is no Home Assistant here, so cap it at one second
            # rather than have this test sit through the real 180.
            "CLUSTER_SYNC_HA_READY_TIMEOUT": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert state.read_text().strip() == expected
    # The temporary name is renamed over, never left behind: this runs on every
    # transition, forever, on a tmpfs.
    assert sorted(p.name for p in state.parent.iterdir()) == ["vrrp-state"]


@pytest.mark.parametrize("base", [COLD, WARM], ids=["cold", "warm"])
def test_the_state_is_recorded_even_when_the_promotion_then_fails(
    tmp_path: pathlib.Path, base: dict[str, object]
) -> None:
    """Production change that would make this fail: writing the state after any
    other command in the script.

    Every notify script runs under `set -euo pipefail`, so a step that aborts
    takes everything below it with it. A promotion that dies at `docker start`
    has still promoted -- Keepalived has moved the VIP -- so the file has to say
    MASTER or the follower carries on pulling onto the node that now leads.
    """
    state = tmp_path / "run" / "vrrp-state"
    cfg = {**base, CONF_HA_CONFIG_PATH: str(tmp_path / "config")}
    path = _write_script(tmp_path, cfg, "notify_master.sh")
    # Everything the script reaches for fails. Nothing survives but the write
    # that happens first.
    for name in ("docker", "nft", "python3"):
        _stub_bin(tmp_path / "bin", name, "#!/bin/sh\nexit 1\n")

    result = _run(path, tmp_path / "bin", env={"CLUSTER_SYNC_STATE_FILE": str(state)})

    assert result.returncode != 0, "the failing step should still abort the script"
    assert state.read_text().strip() == "MASTER"


def test_an_unwritable_state_directory_does_not_abort_a_failover(
    tmp_path: pathlib.Path,
) -> None:
    """The mirror of the test above: recording the state must never be the thing
    that stops a promotion.

    Production change that would make this fail: dropping the `if !` around the
    write, so `set -e` turns a read-only `/run` into a failover that does not
    happen at all.
    """
    if os.geteuid() == 0:
        pytest.skip("permission-based failure injection does not hold as root")

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    cfg = {**WARM, CONF_HA_CONFIG_PATH: str(tmp_path / "config")}
    path = _write_script(tmp_path, cfg, "notify_master.sh")
    for name in ("docker", "nft", "python3"):
        _stub_bin(tmp_path / "bin", name, "#!/bin/sh\nexit 0\n")
    try:
        result = _run(
            path,
            tmp_path / "bin",
            env={"CLUSTER_SYNC_STATE_FILE": str(blocked / "run" / "vrrp-state")},
        )
    finally:
        blocked.chmod(0o700)

    assert result.returncode == 0, result.stderr


def test_the_pull_gate_reads_the_path_the_notify_scripts_write() -> None:
    """The two halves have to name the same file. They did not fail to before —
    they simply had no writer at all — so this pins the join, in both
    directions, rather than each side in isolation."""
    bundle = build_bundle(FS)

    assert "/run/cluster-sync/vrrp-state" in bundle["notify_master.sh"]
    assert "/run/cluster-sync/vrrp-state" in bundle["cluster-fileset-pull.sh"]
    assert "/run/cluster-sync/vrrp-state" in bundle["cluster-sync-tmpfiles.conf"]


def test_the_tmpfiles_default_makes_a_freshly_booted_node_a_follower() -> None:
    """`/run` is a tmpfs, so between a reboot and the first VRRP transition the
    state file does not exist — and the fileset pull fails closed on absent,
    which means a rebooted follower would never pull. BACKUP is the direction
    that makes it pull.

    Production change that would make this fail: defaulting to MASTER, which
    would have a booting node push its own config over the live leader's.
    """
    conf = build_bundle(COLD)["cluster-sync-tmpfiles.conf"]
    directives = [ln for ln in conf.splitlines() if ln and not ln.startswith("#")]

    assert directives == [
        "d /run/cluster-sync 0755 root root -",
        "f /run/cluster-sync/vrrp-state 0644 root root - BACKUP",
    ]


def test_the_tmpfiles_default_is_emitted_whatever_the_topology() -> None:
    """The notify scripts write the state file in every model, whether or not
    fileset replication is enabled to read it, so the directory it lives in is
    not a fileset-only concern."""
    for cfg in (COLD, WARM, FS, {**WARM, **FS}):
        assert "cluster-sync-tmpfiles.conf" in build_bundle(cfg)


# -- I1, and its follow-up: tier 1 is gone outright -------------------------
#
# I1 first stopped tier 1 from being emitted alongside fileset replication,
# because running both is a two-leader bug: `cluster-config-sync.sh` rsyncs
# `.storage/***` onto the follower's LIVE config directory every two minutes,
# so the follower's `core.config_entries` becomes the leader's copy, and at
# promotion `cluster-fileset-identity.py capture` captures the *leader's*
# entry as "ours" and grafts it back -- the promoted node runs on the peer's
# `node_id`, the lease renews on identity, and both nodes lead: the exact
# defect Task 13 closed inside the swap, reintroduced from outside it. It was
# inert only while nothing wrote `vrrp-state`; the moment C1 landed it became
# live, which is why that fix and I1 shipped together.
#
# The owner then went further, 2026-08-30: tier 1 has never run on any real
# install (nothing ever wrote `vrrp-state` until C1), so there is no installed
# base to preserve it for, and fileset replication covers its whole allow-list
# properly. It is now gone in every configuration, fileset on or off.


def test_the_tier_one_rsync_is_never_emitted() -> None:
    """Owner decision 2026-08-30: not just alongside fileset replication --
    `cluster-config-sync.{sh,service,timer}` are gone from every bundle."""
    for cfg in (
        COLD,
        WARM,
        {**COLD, CONF_FILESET_ENABLED: True, CONF_CLUSTER_SECRET: "s3cr3t-for-tests"},
        {**WARM, CONF_FILESET_ENABLED: True, CONF_CLUSTER_SECRET: "s3cr3t-for-tests"},
    ):
        bundle = build_bundle(cfg)
        assert "cluster-config-sync.sh" not in bundle
        assert "cluster-config-sync.service" not in bundle
        assert "cluster-config-sync.timer" not in bundle
        # Nothing else in the bundle grew its own copy of the push either.
        #
        # This guard did its job on 2026-09-08. A recorder-history feature added
        # `rsync` here to ship a 1.59 GB snapshot between nodes, and the guard
        # caught it. The exception was NOT taken: rsync runs on the host, which
        # cannot work on Home Assistant OS at all, and it would have restored
        # the SSH trust between hosts that removing tier 1 deliberately ended.
        # The feature was reworked instead. See ADR-010.
        assert not [
            name for name, body in bundle.items() if name.endswith(".sh") and "rsync -" in body
        ]
        # Same reasoning, stated for the transport rather than the tool: nothing
        # in the bundle may require one node to reach the other directly. The
        # whole design goes through Valkey.
        assert not [
            name
            for name, body in bundle.items()
            if name.endswith(".sh")
            and any(
                line.strip().startswith("ssh ") or " ssh " in line
                for line in body.splitlines()
                if not line.strip().startswith("#")
            )
        ]


def test_regenerating_the_bundle_prunes_an_already_installed_rsync_timer(
    tmp_path: pathlib.Path,
) -> None:
    """The operator-visible half of the removal. `build_bundle` never writes
    these three names any more, but an operator's install directory can still
    hold them from a bundle generated before this change -- fileset on or off,
    since tier 1 used to be generated in both cases. `write_bundle` prunes
    anything `MANAGED_FILENAMES` lists that the current build did not write,
    which is exactly why the three names stay listed there (see the comment
    above `MANAGED_FILENAMES`) even though nothing generates them any more.
    """
    target = tmp_path / "bundle"
    target.mkdir()
    for name in (
        "cluster-config-sync.sh",
        "cluster-config-sync.service",
        "cluster-config-sync.timer",
    ):
        (target / name).write_text("# stale artefact from an earlier bundle\n")

    write_bundle(str(target), COLD)

    for name in (
        "cluster-config-sync.sh",
        "cluster-config-sync.service",
        "cluster-config-sync.timer",
    ):
        assert not (target / name).exists(), name


def test_install_md_never_enables_the_removed_rsync_timer() -> None:
    """The instruction and the artefact have to agree, in every configuration.
    `INSTALL.md` used to tell the operator to
    `systemctl enable --now cluster-config-sync.timer` when the fileset was
    off -- an instruction for a unit the bundle no longer contains -- and,
    with the fileset on, to disable one an earlier bundle might have
    installed. Both configurations now give the disable-only instruction."""
    for cfg in (COLD, WARM, FS):
        readme = build_bundle(cfg)["INSTALL.md"]
        assert "enable --now cluster-config-sync.timer" not in readme
        assert "disable --now cluster-config-sync.timer" in readme


def test_install_md_tells_the_operator_to_install_the_tmpfiles_default() -> None:
    """A bundle that generates the state default and never mentions it is the
    C1 defect with an extra step: the file exists and nothing puts it in
    place."""
    for cfg in (COLD, WARM, FS):
        readme = build_bundle(cfg)["INSTALL.md"]
        assert "cluster-sync-tmpfiles.conf" in readme
        assert "/etc/tmpfiles.d/cluster-sync.conf" in readme
        assert "systemd-tmpfiles --create" in readme


# -- C2: the pull program has to be where the pull script looks for it -------
#
# It was not. `cluster-fileset-pull.sh` ran
# `python3 /config/cluster_state_sync_bundle/fileset_pull.py`, INSTALL.md's
# `sudo cp * /etc/cluster-sync/` put it somewhere else entirely, and nothing
# replicated the wizard's output directory into /config -- it is in neither
# REPLICATED_DIRS nor REPLICATED_FILES, and the tier-1 allow-list excluded it.
# In the cold model the follower's Home Assistant is stopped, so it can never
# run the wizard to write it there either.


def _install_as_the_readme_says(tmp_path: pathlib.Path, cfg: dict) -> pathlib.Path:
    """INSTALL.md's install step, verbatim: `mkdir -p`, then `cp *`.

    Nothing selective. The whole point is that an operator who types exactly
    what the README says ends up with a pull that runs, so this test must not
    quietly copy a file the README never mentions.
    """
    bundle_dir = tmp_path / "bundle"
    write_bundle(str(bundle_dir), cfg)
    install = tmp_path / "install"
    install.mkdir()
    for item in bundle_dir.iterdir():
        shutil.copy2(item, install / item.name)
    return install


def test_every_path_the_pull_mounts_exists_after_install_md_is_followed(
    tmp_path: pathlib.Path,
) -> None:
    """C2, as a join rather than as two separate assertions about each side.

    The defect was not that either half was wrong on its own — it was that the
    invocation path and the documented install path named different places and
    nothing ever compared them.
    """
    install = _install_as_the_readme_says(tmp_path, FS)
    script = build_bundle(FS)["cluster-fileset-pull.sh"]

    mounted = re.findall(rf"-v ({re.escape(INSTALL_DIR)}/[^:]+):", script)

    assert mounted, "the pull mounts nothing from the install directory"
    for host_path in mounted:
        name = pathlib.PurePosixPath(host_path).name
        assert (install / name).is_file(), f"{host_path} is not there after `cp *`"


def test_the_installed_pull_program_runs_from_the_install_directory(
    tmp_path: pathlib.Path,
) -> None:
    """The strongest form available without Docker: execute the installed copy.

    `fileset_pull.py` imports `crypto` from its own directory, which works only
    because Python puts a script's directory on `sys.path[0]`. Running it here
    proves the two files landed together and that the standalone import arm
    resolves — the thing that would otherwise fail once a minute, on the node
    standing by to take over, with an ImportError nobody is watching for.
    """
    install = _install_as_the_readme_says(tmp_path, FS)

    proc = subprocess.run(
        [sys.executable, str(install / "fileset_pull.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "--staged" in proc.stdout


def test_the_pull_program_needs_the_crypto_module_beside_it(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: INSTALL.md naming only
    `fileset_pull.py`, or the bundle emitting only it.

    The negative half of the test above. Without it, "the program runs" could
    pass on an installation that happened to have `crypto` importable for some
    unrelated reason, and the test would stop being about this bundle at all.
    """
    install = _install_as_the_readme_says(tmp_path, FS)
    (install / "crypto.py").unlink()

    proc = subprocess.run(
        [sys.executable, str(install / "fileset_pull.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode != 0
    assert "crypto" in proc.stderr


def test_the_pull_program_needs_the_resp_module_beside_it(tmp_path: pathlib.Path) -> None:
    """The same hazard one level further down, for `resp.py`.

    `resp.py` was split out of `fileset_pull.py` so the lease promoter could
    use `ValkeyClient` without dragging in `crypto.py`'s `cryptography`
    dependency. `fileset_pull.py` still needs it beside it, the same way it
    needs `crypto` -- ship the pull without it and it fails at the worst
    possible moment: during a promotion.
    """
    install = _install_as_the_readme_says(tmp_path, FS)
    (install / "resp.py").unlink()

    proc = subprocess.run(
        [sys.executable, str(install / "fileset_pull.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode != 0
    assert "resp" in proc.stderr


def test_the_pull_runs_the_program_from_where_it_mounts_it() -> None:
    """Mount target and argv, pinned together. Either one alone can be right
    while the pair is wrong, which is exactly what happened."""
    script = build_bundle(FS)["cluster-fileset-pull.sh"]

    assert f"{PULL_MOUNTPOINT}/fileset_pull.py:ro" in script
    assert f"{PULL_MOUNTPOINT}/crypto.py:ro" in script
    assert f"{PULL_MOUNTPOINT}/resp.py:ro" in script
    assert f"    {PULL_MOUNTPOINT}/fileset_pull.py \\\n" in script
    # The old path, which nothing ever wrote.
    assert "/config/cluster_state_sync_bundle" not in script


def test_the_pull_does_not_mount_the_whole_install_directory() -> None:
    """`cluster-fileset.key` reaches the container as `/key` and the Valkey
    password reaches it in the environment. Mounting `/etc/cluster-sync`
    wholesale would put both in the container's filesystem under their real
    names, for no gain."""
    script = build_bundle(FS)["cluster-fileset-pull.sh"]

    assert f"-v {INSTALL_DIR}:" not in script
    assert f'-v "{INSTALL_DIR}:' not in script


# -- I3 ---------------------------------------------------------------------


def test_the_pull_does_not_boot_the_images_supervision_tree() -> None:
    """I3: without `--entrypoint`, `docker run "$IMAGE" python3 ...` goes through
    s6-overlay's `/init`, which boots the image's whole supervision tree before
    running the command — and in the Home Assistant image that tree is what
    starts Home Assistant.

    Once a minute, on the standby, on host networking, with `/config` mounted,
    that is how a node ends up running a second unmanaged Home Assistant that
    can load this integration and take the lease. This branch's own rehearsal
    harness already passes `--entrypoint` for the 9-23s it saves; the generated
    script needs it for a much worse reason.
    """
    script = build_bundle(FS)["cluster-fileset-pull.sh"]
    run = script[script.index("docker run --rm") :]

    assert "--entrypoint python3" in run
    # Before the image name, or Docker reads it as an argument to the command.
    assert run.index("--entrypoint python3") < run.index('"$IMAGE"')
    # And the command is no longer prefixed with a redundant interpreter.
    assert "python3 /opt/cluster-sync/fileset_pull.py" not in run


# -- I7 ---------------------------------------------------------------------


def test_the_warm_promotion_reaches_the_firewall_even_if_docker_fails(
    tmp_path: pathlib.Path,
) -> None:
    """I7. Production change that would make this fail: bare `docker stop` and
    `docker start` under `set -euo pipefail`.

    Both sit before `nft -f leader.nft`. Keepalived has already moved the VIP
    by the time this script runs, so an abort at `docker start` leaves the node
    holding the VIP with Home Assistant down *and* the FOLLOWER ruleset still
    loaded — a total outage rather than a failover. Every other risky step in
    this script is `|| logger`; these now match.

    Executed rather than grepped: the whole finding is about what `set -e` does
    to control flow, which no amount of string matching can see.
    """
    cfg = {
        **WARM,
        CONF_FILESET_ENABLED: True,
        CONF_CLUSTER_SECRET: "s3cr3t-for-tests",
        CONF_HA_CONFIG_PATH: str(tmp_path / "config"),
    }
    script = _write_script(tmp_path, cfg, "notify_master.sh")
    marker = tmp_path / "nft-was-reached"
    # Everything docker does fails; nft records that it was reached anyway.
    _stub_bin(tmp_path / "bin", "docker", "#!/bin/sh\nexit 1\n")
    _stub_bin(tmp_path / "bin", "nft", f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    _stub_bin(tmp_path / "bin", "python3", "#!/bin/sh\nexit 1\n")

    result = _run(
        script,
        tmp_path / "bin",
        env={"CLUSTER_SYNC_STATE_FILE": str(tmp_path / "run" / "vrrp-state")},
    )

    assert marker.exists(), (
        "the promotion aborted before the firewall: the node holds the VIP with the "
        "follower ruleset loaded"
    )
    assert result.returncode == 0, result.stderr


def test_the_written_bundle_directory_is_not_world_readable(tmp_path: pathlib.Path) -> None:
    """Test-sweep item: deleting `os.chmod(target, 0o750)` left the suite
    green, and that directory holds `cluster-fileset.key` — which decrypts
    every credential this integration mirrors out of `.storage` — and the
    Valkey password.

    The two files are written 0600 individually, but a 0755 directory on a host
    that runs other things still lists them and still lets anything that can
    guess a name try to open one.
    """
    target = tmp_path / "bundle"
    write_bundle(str(target), {**FS, CONF_REDIS_PASSWORD: "valkey-p4ssword"})

    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    for name in ("cluster-fileset.key", VALKEY_PASSWORD_FILENAME):
        assert stat.S_IMODE((target / name).stat().st_mode) == 0o600, name


# -- The promoter: Keepalived's replacement ---------------------------------


def test_the_promoter_artefacts_are_emitted() -> None:
    bundle = build_bundle(FS)
    for name in (
        "cluster_promoter.py",
        "lease.py",
        "cluster-promoter.sh",
        "cluster-promoter.service",
        "cluster-promoter.timer",
    ):
        assert name in bundle, name


def test_the_shipped_promoter_matches_the_package_original() -> None:
    """A hand-copied script drifts from the tested module and fails silently
    until a promotion — the same reasoning as the pull program."""
    root = _PACKAGE_DIR
    bundle = build_bundle(FS)
    assert bundle["cluster_promoter.py"] == (root / "scripts" / "cluster_promoter.py").read_text(
        encoding="utf-8"
    )
    assert bundle["lease.py"] == (root / "lease.py").read_text(encoding="utf-8")


def test_keepalived_config_is_no_longer_generated() -> None:
    """Retiring Keepalived while still shipping its config invites someone to
    install it alongside the promoter — two leadership mechanisms racing."""
    for cfg in (COLD, WARM, FS):
        assert "keepalived-cluster.conf" not in build_bundle(cfg)


def test_regenerating_prunes_an_already_installed_keepalived_config(
    tmp_path: pathlib.Path,
) -> None:
    """An operator with an older bundle has the file on disk. Dropping the name
    from MANAGED_FILENAMES would strand it there."""
    stale = tmp_path / "keepalived-cluster.conf"
    stale.write_text("vrrp_instance ...")
    write_bundle(str(tmp_path), FS)
    assert not stale.exists()


def test_install_md_does_not_instruct_installing_keepalived() -> None:
    body = build_bundle(FS)["INSTALL.md"]
    assert "enable --now keepalived" not in body
    # An upgrading operator has it installed already, so say so rather than
    # going silent — silence reads as "still required".
    # Tie the removal verb to the keepalived mention. Two independent
    # substring checks would pass on an INSTALL.md that says "disable" about
    # something else entirely and never mentions removing keepalived.
    mentions = [ln for ln in body.splitlines() if "keepalived" in ln.lower()]
    assert mentions, "must tell an upgrader to remove it, not go silent"
    assert any(v in ln.lower() for ln in mentions for v in ("rm ", "remove", "disable", "mask")), (
        mentions
    )


def test_install_md_documents_the_ha_liveness_probe_and_its_escape_hatch() -> None:
    """Design D3 changes what the promoter does on every tick it believes it
    leads. An operator who only reads INSTALL.md and never the source would
    otherwise have no way to know renewal can now fail a healthy-looking
    lease, that `--release-holddown` (M3) exists so a release does not cycle
    with the fileset swap every five minutes, that `CLUSTER_SYNC_NO_PROBE`
    exists for a host that cannot reach Home Assistant over HTTP at all, or
    that `CLUSTER_SYNC_HA_URL` (M4) can redirect the probe itself instead of
    disabling it outright."""
    body = build_bundle(FS)["INSTALL.md"]
    assert "D3" in body
    assert "--probe-grace" in body
    assert "--release-holddown" in body
    assert "CLUSTER_SYNC_NO_PROBE" in body
    assert "CLUSTER_SYNC_HA_URL" in body
    assert "http://127.0.0.1:8123/" in body


def test_the_promoter_never_receives_the_password_in_argv() -> None:
    """ps is world-readable. The password comes from the environment, as the
    pull's already does."""
    body = build_bundle(FS)["cluster-promoter.sh"]
    assert "--password" not in body
    assert "CLUSTER_SYNC_VALKEY_PASSWORD" in body


def test_the_force_master_override_defaults_to_tmpfs_not_etc() -> None:
    """Design D2's default is `/run/cluster-sync/force-master`, not
    `/etc/cluster-sync/`. The difference matters: `/run` is tmpfs and
    self-clears on reboot, `/etc` does not, and the filename is deliberately
    absent from `MANAGED_FILENAMES` so regeneration never prunes it -- an
    override forgotten under `/etc` would `SET` the leader key to this node
    every tick, forever, across reboots, permanently refusing the peer's
    legitimate renewal.
    """
    body = build_bundle(FS)["cluster-promoter.sh"]
    assert "/run/cluster-sync/force-master" in body
    assert "/etc/cluster-sync/force-master" not in body


def test_the_promoter_is_given_this_nodes_own_identity(tmp_path) -> None:
    """🚨 The single highest-risk line in this change.

    `node_id` currently reaches only `keepalived-cluster.conf` and `INSTALL.md`,
    and this task deletes the first. If it is not threaded into the promoter's
    wrapper, the promoter takes the lease under an empty identity — and both
    nodes then present the same one, renew the same lease, and both believe
    they lead. That is the exact defect that cost this project a day.
    """
    cfg = {**FS, CONF_NODE_ID: "tiger1-abc123"}
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(cfg)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir)
    assert result.returncode == 0, result.stderr
    # Not merely "appears somewhere in argv" -- that would still pass if the
    # value had been threaded into the wrong flag entirely (`--namespace
    # "$NODE_ID"`, say). One arg per line (the stub's own `printf "%s\n"
    # "$@"`), so the value immediately after `--node-id` is what the promoter
    # actually receives as its identity.
    lines = argv_log.read_text().splitlines()
    assert "--node-id" in lines, lines
    assert lines[lines.index("--node-id") + 1] == "tiger1-abc123", lines


def test_the_promoter_refuses_to_run_without_an_identity(tmp_path) -> None:
    """Belt and braces on the same hazard: if the config somehow carries no
    node_id, the generated script must fail loudly rather than pass an empty
    string that collides with the peer's empty string.
    """
    # FS inherits COLD's node_id ("tiger1"), so it is blanked out here — the
    # guard is the thing under test, not the fixture's own identity.
    cfg = {**FS, CONF_NODE_ID: ""}
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(cfg)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir)
    assert result.returncode != 0, "must refuse, not run with an empty identity"
    assert not argv_log.exists(), "the promoter must never have been invoked"


def test_the_promoter_probes_home_assistants_own_container_ip(tmp_path) -> None:
    """Design D3. Left unwired, the promoter would probe its own default
    (`127.0.0.1`) regardless of where Home Assistant's container actually
    listens, and on a macvlan or bridge deployment that answers nothing --
    every renewal would read as a dead Home Assistant and the leader would
    release its own lease out from under itself."""
    cfg = {**FS, CONF_HA_CONTAINER_IP: "172.30.0.5"}
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(cfg)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir)
    assert result.returncode == 0, result.stderr
    lines = argv_log.read_text().splitlines()
    assert "--ha-url" in lines, lines
    assert lines[lines.index("--ha-url") + 1] == "http://172.30.0.5:8123/", lines


def test_the_promoter_defaults_the_probe_to_loopback(tmp_path) -> None:
    """`network_mode: host` (READINESS I11, confirmed on both tigers): with
    no container IP configured, Home Assistant answers on the host's own
    loopback, so that is what an unconfigured deployment must probe rather
    than some other placeholder."""
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(FS)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir)
    assert result.returncode == 0, result.stderr
    lines = argv_log.read_text().splitlines()
    assert "--ha-url" in lines, lines
    assert lines[lines.index("--ha-url") + 1] == "http://127.0.0.1:8123/", lines


def test_the_no_probe_escape_hatch_is_off_by_default(tmp_path) -> None:
    """D3's whole protection would be silently absent if this leaked into the
    default invocation rather than needing an explicit operator opt-in."""
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(FS)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir)
    assert result.returncode == 0, result.stderr
    assert "--no-probe" not in argv_log.read_text().splitlines()


def test_the_no_probe_escape_hatch_is_wired_to_its_env_var(tmp_path) -> None:
    """The operator override documented in INSTALL.md for a host that cannot
    reach Home Assistant over HTTP -- unwired, there would be no way to use
    it short of hand-editing the generated script on every regeneration."""
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(FS)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir, env={"CLUSTER_SYNC_NO_PROBE": "1"})
    assert result.returncode == 0, result.stderr
    assert "--no-probe" in argv_log.read_text().splitlines()


def test_the_ha_url_has_an_operator_override(tmp_path) -> None:
    """M4. Unlike every other path this wrapper hands to the promoter
    (STATE_FILE, FORCE_FILE, PASSWORD_FILE), --ha-url had no
    CLUSTER_SYNC_-prefixed override -- an operator whose Home Assistant
    answers somewhere other than the generated default (behind a reverse
    proxy, say) had no way to point the D3 probe there short of
    hand-editing this file on every regeneration, with only the blunt
    --no-probe as an escape hatch."""
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(FS)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir, env={"CLUSTER_SYNC_HA_URL": "http://custom-ha:9999/"})
    assert result.returncode == 0, result.stderr
    lines = argv_log.read_text().splitlines()
    assert "--ha-url" in lines, lines
    assert lines[lines.index("--ha-url") + 1] == "http://custom-ha:9999/", lines


def test_probe_grace_is_actually_emitted(tmp_path) -> None:
    """Documented in INSTALL.md, but previously never emitted by the
    wrapper -- meaning a real deployment always ran under whatever default
    cluster_promoter.py happened to ship with, silently, regardless of what
    INSTALL.md told the operator to expect. Found by the rehearsal, which
    had to hand-edit the installed script to exercise D3's probe at all."""
    script = tmp_path / "cluster-promoter.sh"
    script.write_text(build_bundle(FS)["cluster-promoter.sh"], encoding="utf-8")
    bin_dir, argv_log = tmp_path / "bin", tmp_path / "argv.log"
    _stub_bin(
        bin_dir,
        "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$@" >> {argv_log}\nexit 0\n',
    )
    result = _run(script, bin_dir)
    assert result.returncode == 0, result.stderr
    lines = argv_log.read_text().splitlines()
    assert "--probe-grace" in lines, lines


# ---------------------------------------------------------------------------
# The generated Lovelace dashboard.
# ---------------------------------------------------------------------------


def test_the_dashboard_is_valid_yaml_and_calls_only_real_services() -> None:
    """A dashboard that will not parse is worse than none: the operator pastes
    it, Home Assistant rejects the whole raw config, and they have lost whatever
    was there before."""
    import yaml

    doc = yaml.safe_load(build_bundle(FS)["cluster-dashboard.yaml"])
    assert isinstance(doc, dict) and doc["views"], doc
    services = set(re.findall(r"perform_action: (\S+)", build_bundle(FS)["cluster-dashboard.yaml"]))
    assert services == {
        "cluster_state_sync.flush_snapshot",
        "cluster_state_sync.clear_degraded",
    }, services


def test_the_dashboard_hardcodes_no_entity_ids() -> None:
    """🚨 The reason every card uses a template.

    Entity ids carry the device name as a prefix, and that prefix is NOT stable
    across a promotion: the standby inherits the leader's entity registry, so a
    node called node2 serves entities named `..._node1_...` for the rest of its
    life. A dashboard with ids baked in works on the node it was generated from
    and is blank on the one you actually need it on at 2am.
    """
    body = build_bundle(FS)["cluster-dashboard.yaml"]
    assert "sensor.cluster" not in body
    assert "binary_sensor.cluster" not in body
    assert "selectattr" in body, "cards must find entities by suffix"


def test_every_dashboard_template_compiles_and_survives_an_empty_match() -> None:
    """Parsing is not enough — these blew up on a real Home Assistant while
    parsing perfectly.

    `| list | first` raises UndefinedError on an empty sequence in Home
    Assistant's strict template mode, so the `is none` guard that follows never
    runs. On a node where the integration is not set up, every card that looked
    up an entity failed outright instead of degrading. Binding through a list
    and indexing is what makes the guard reachable.
    """
    import jinja2
    import yaml

    doc = yaml.safe_load(build_bundle(FS)["cluster-dashboard.yaml"])
    env = jinja2.Environment()
    checked = 0
    for card in doc["views"][0]["cards"]:
        for inner in [card, *card.get("cards", [])]:
            if "content" not in inner:
                continue
            env.parse(inner["content"])  # raises TemplateSyntaxError on bad syntax
            assert "| list | first" not in inner["content"], (
                "raises on an empty match before the none-guard can run"
            )
            checked += 1
    assert checked >= 3, f"expected several templated cards, found {checked}"


# -- host vs container paths (owner incident, 2026-09-03) ---------------------


def _tiger2_cfg(**over):
    cfg = {
        "topology_model": "cold",
        "leadership_source": "lease",
        "node_id": "node-b",
        "cluster_namespace": "prod",
        "cluster_secret": "s" * 43,
        "ha_config_path": "/mnt/docker_data/homeassistant",
        "ha_container": "home-assistant-2",
        "redis_host": "valkey.example",
        "redis_port": 6380,
        "redis_db": 2,
        "redis_username": "cluster_state_sync",
        "redis_password": "pw",
        "redis_use_tls": True,
        "redis_tls_ca_certs": "/config/manning-madness-root.crt",
        "fileset_enabled": True,
    }
    cfg.update(over)
    return cfg


def test_the_promoter_gets_a_host_path_for_the_ca_not_a_container_one() -> None:
    """Production change that would make this fail: passing the stored CA path
    straight through to cluster-promoter.sh.

    The wizard's CA field is a path inside the container, because that is the
    only filesystem the integration can validate it against. But
    cluster-promoter.sh runs python3 on the HOST, and no host here has a
    /config. Untranslated, load_verify_locations raises FileNotFoundError,
    run() catches it as an OSError, every tick exits 1, and the lease is never
    taken -- failover that never happens, with a log line the only symptom.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    promoter = build_bundle(_tiger2_cfg())["cluster-promoter.sh"]
    assert "--tls-ca-file /mnt/docker_data/homeassistant/manning-madness-root.crt" in promoter
    assert "/config/manning-madness-root.crt" not in promoter


def test_the_pull_mounts_the_ca_from_a_host_path() -> None:
    """Same defect, second victim. `docker run -v` resolves its source on the
    host; handed one that does not exist, Docker creates an empty DIRECTORY
    there and mounts it, so the pull verifies TLS against a directory.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    pull = build_bundle(_tiger2_cfg())["cluster-fileset-pull.sh"]
    assert '-v "/mnt/docker_data/homeassistant/manning-madness-root.crt:/ca:ro"' in pull
    assert '-v "/config/' not in pull


def test_tiger1_shape_translates_against_its_own_config_level() -> None:
    """The two hosts differ by a trailing /config component, so the translation
    has to use each node's own answer rather than a shared constant."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    promoter = build_bundle(_tiger2_cfg(ha_config_path="/mnt/docker_data/homeassistant/config"))[
        "cluster-promoter.sh"
    ]
    assert (
        "--tls-ca-file /mnt/docker_data/homeassistant/config/manning-madness-root.crt" in promoter
    )


def test_a_ca_outside_the_config_dir_is_left_alone() -> None:
    """A system trust-store path means the same thing on both sides; rewriting
    it would break a deployment that was already correct."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    promoter = build_bundle(_tiger2_cfg(redis_tls_ca_certs="/etc/ssl/certs/ca-certificates.crt"))[
        "cluster-promoter.sh"
    ]
    assert "--tls-ca-file /etc/ssl/certs/ca-certificates.crt" in promoter


def test_no_ha_config_path_leaves_the_ca_untranslated() -> None:
    """With nothing to translate against, a guess is worse than the original."""
    from custom_components.cluster_state_sync.bundle import _host_path

    assert _host_path({}, "/config/ca.crt") == "/config/ca.crt"
    assert _host_path({"ha_config_path": ""}, "/config/ca.crt") == "/config/ca.crt"


# -- install.sh (owner ask, 2026-09-03) ---------------------------------------


def test_the_bundle_ships_an_installer() -> None:
    from custom_components.cluster_state_sync.bundle import build_bundle

    assert "install.sh" in build_bundle(_tiger2_cfg())


def test_the_installer_adopts_before_it_enables_the_promoter() -> None:
    """Production change that would make this fail: moving --adopt after the
    timer is enabled, or dropping it.

    This is the whole reason the installer exists rather than a list of
    commands. decide() acts on a CHANGE of the lease outcome, and a fresh
    install has no vrrp-state, so the first tick calls the status quo a
    transition and runs the notify script for it. On the node that already
    leads that is notify_master.sh: a fileset swap and a device pre-flight
    rewriting .storage underneath a Home Assistant that never stopped.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    script = build_bundle(_tiger2_cfg())["install.sh"]
    adopt_at = script.index("cluster-promoter.sh --adopt")
    enable_at = script.index("systemctl enable --now cluster-promoter.timer")
    assert adopt_at < enable_at, "the installer arms the promoter before seeding its state"


def test_the_installer_refuses_a_config_path_with_no_storage() -> None:
    """The `…/homeassistant` vs `…/homeassistant/config` slip, caught at install
    time rather than at the first promotion that silently writes nowhere."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    script = build_bundle(_tiger2_cfg())["install.sh"]
    assert "/mnt/docker_data/homeassistant/.storage" in script
    assert "-type d -name .storage" in script, "offers a way to find the right one"


def test_the_installer_checks_the_ca_is_readable_from_the_host() -> None:
    """A container path that reached a host-side script is silent everywhere
    else: the promoter exits 1 every tick and never promotes."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    script = build_bundle(_tiger2_cfg())["install.sh"]
    assert "-r /mnt/docker_data/homeassistant/manning-madness-root.crt" in script


def test_the_installer_has_a_dry_run() -> None:
    """Nobody should run a stranger's install script without seeing what it
    does first -- ADR-005's argument, applied to our own install step."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    script = build_bundle(_tiger2_cfg())["install.sh"]
    assert "--dry-run" in script
    assert "DRY_RUN" in script


def test_an_installer_with_no_timers_says_so_rather_than_adopting() -> None:
    """With fileset off there is no promoter and no pull, so there is no first
    tick to protect and nothing to enable. The installer must not pretend."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    script = build_bundle(_tiger2_cfg(fileset_enabled=False))["install.sh"]
    assert "--adopt" not in script
    assert "systemctl enable --now" not in script
    assert "no promoter" in script


def test_the_generated_installer_is_valid_bash() -> None:
    """Production change that would make this fail: any quoting slip in the
    generator. One already shipped -- an unquoted `;` terminating a `-exec`,
    which would have ended the wrapper call instead of the find."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present everywhere we run
        pytest.skip("bash not available")

    from custom_components.cluster_state_sync.bundle import build_bundle

    for cfg in (
        _tiger2_cfg(),
        _tiger2_cfg(fileset_enabled=False),
        _tiger2_cfg(topology_model="warm"),
    ):
        script = build_bundle(cfg)["install.sh"]
        proc = subprocess.run(
            [bash, "-n"], input=script, text=True, capture_output=True, check=False
        )
        assert proc.returncode == 0, proc.stderr


def test_the_installer_verifies_the_d3_probe_target() -> None:
    """Production change that would make this fail: dropping the probe check.

    A node that fails the D3 probe RELEASES the lease and demotes, and
    notify_backup.sh stops the container. So a probe that cannot reach Home
    Assistant does not merely fail to protect it -- it stops it, for the
    hold-down, repeatedly. Arming the timer without checking the URL first is
    scheduling an outage.
    """
    from custom_components.cluster_state_sync.bundle import (
        PROMOTER_RELEASE_HOLDDOWN_SECONDS,
        build_bundle,
    )

    script = build_bundle(_tiger2_cfg())["install.sh"]
    assert "http://127.0.0.1:8123/" in script
    # Read from the constant, not typed again. A literal here is what let the
    # installer keep advertising 900s after the promoter moved to 180.
    assert str(int(PROMOTER_RELEASE_HOLDDOWN_SECONDS)) in script, (
        "says how long a failed probe keeps HA stopped"
    )


def test_the_installer_probes_the_container_ip_when_one_is_set() -> None:
    """A published port rather than network_mode host means loopback on the
    host is not where Home Assistant answers."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    script = build_bundle(_tiger2_cfg(ha_container_ip="10.0.0.9"))["install.sh"]
    assert "http://10.0.0.9:8123/" in script


def test_the_installers_holddown_matches_the_promoters() -> None:
    """The installer quotes this number to the operator as the cost of a failed
    probe. A drift would make it lie."""
    from custom_components.cluster_state_sync.bundle import (
        PROMOTER_PROBE_GRACE_SECONDS,
        PROMOTER_RELEASE_HOLDDOWN_SECONDS,
    )
    from custom_components.cluster_state_sync.scripts.cluster_promoter import (
        DEFAULT_PROBE_GRACE_SECONDS,
        DEFAULT_RELEASE_HOLDDOWN_SECONDS,
    )

    assert PROMOTER_RELEASE_HOLDDOWN_SECONDS == DEFAULT_RELEASE_HOLDDOWN_SECONDS
    assert PROMOTER_PROBE_GRACE_SECONDS == DEFAULT_PROBE_GRACE_SECONDS


def test_install_md_leads_with_the_installer_it_ships() -> None:
    """Production change that would make this fail: adding install.sh to the
    bundle without telling the operator it exists.

    INSTALL.md handed over sixteen sudo commands and did not mention the script
    that runs them, which is how a generated installer ends up unused.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    md = build_bundle(_tiger2_cfg())["INSTALL.md"]
    assert "./install.sh --dry-run" in md
    assert md.index("install.sh") < md.index("sudo mkdir -p"), (
        "the installer must come before the manual commands, not after"
    )
    assert "standby first" in md, "the install order between hosts is load-bearing"


def test_the_swap_refuses_while_the_container_is_running() -> None:
    """Production change that would make this fail: dropping the guard.

    The cold model's promotion path is swap -> pre-flight -> docker start, and
    every step assumes nothing holds .storage open. A standby whose Home
    Assistant is still up -- left running after setup, most likely -- keeps
    those files in memory and rewrites them on its own shutdown (AR-0034), so
    the swap lands and is then silently undone. The node comes up on its own
    old identity behind a successful promotion and a clean log.

    Refusing is the safe direction: D4 already accommodates a degraded
    promotion and marks it. Swapping under a live process is data loss.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    swap = build_bundle(_tiger2_cfg())["cluster-fileset-swap.sh"]
    assert "{{.State.Running}}" in swap
    assert 'mark "container_running"' in swap
    # Before anything *uses* the staged copy. `STAGED=` is assigned at the top
    # of the script, so the meaningful anchor is the first swap_dir call, not
    # the variable.
    assert swap.index("State.Running") < swap.index("if ! swap_dir"), (
        "the guard must come before anything touches .storage"
    )


def test_the_swap_guard_names_this_nodes_own_container() -> None:
    """Two nodes routinely differ here -- homeassistant vs home-assistant-2 --
    and a guard checking the wrong name would pass while the real container
    ran."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    swap = build_bundle(_tiger2_cfg())["cluster-fileset-swap.sh"]
    assert "CONTAINER=home-assistant-2" in swap


def test_the_swap_installs_whatever_was_staged_not_a_fixed_list() -> None:
    """Production change that would make this fail: hardcoding the install set
    again.

    This was the third allow-list in one path, and the last to be found. The
    publisher followed the config's includes, the pull staged every file
    faithfully — and the swap installed only `custom_components www blueprints`
    plus seven top-level YAMLs. node-b promoted into recovery mode with
    `configs/customize.yaml` sitting in `.cluster_sync_staged` the whole time.

    What the leader publishes varies per deployment, so the swap cannot know
    the set in advance. It has to read it.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    swap = build_bundle(_tiger2_cfg())["cluster-fileset-swap.sh"]
    assert 'for d in $(cd "$STAGED/storage"' in swap
    assert 'for f in $(cd "$STAGED/storage"' in swap
    # The old fixed lists must be gone, or a partial revert would look fine.
    assert "for d in custom_components www blueprints" not in swap
    assert "for f in configuration.yaml automations.yaml" not in swap


def test_the_swap_still_handles_storage_separately() -> None:
    """`.storage` carries the identity graft, so it must not be swept up by the
    generic directory loop and installed twice."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    swap = build_bundle(_tiger2_cfg())["cluster-fileset-swap.sh"]
    assert 'swap_dir "$STAGED/storage/.storage" "$CONFIG/.storage"' in swap
    assert '[[ "$d" == ".storage" ]] && continue' in swap


# -- compose-managed Home Assistant ----------------------------------------


def _compose_cfg(**over):
    cfg = _tiger2_cfg()
    cfg.update(
        {
            "ha_start_mode": "compose",
            "compose_file": "/srv/docker-compose.yml",
            "compose_service": "home-assistant",
        }
    )
    cfg.update(over)
    return cfg


def test_docker_start_remains_the_default() -> None:
    """The fastest, least surprising thing that can work.

    It acts on one existing container, needs no project file, and cannot be
    broken by an unrelated service elsewhere in someone's estate.
    """
    scripts = files()
    assert "docker start" in scripts["notify_master.sh"]
    assert "docker compose" not in scripts["notify_master.sh"]


def test_compose_mode_acts_on_one_service_never_the_estate() -> None:
    """Bringing up a whole estate to promote one node is not acceptable.

    Production change this catches: dropping the service name, or losing
    `--no-deps`, either of which turns a promotion into "start everything".
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    master = build_bundle(_compose_cfg())["notify_master.sh"]
    assert "docker compose" in master
    assert "up -d --no-deps home-assistant" in master, "one service, no dependencies"


def test_compose_mode_honours_a_profile() -> None:
    """A service behind a profile is invisible to compose without it."""
    from custom_components.cluster_state_sync.bundle import build_bundle

    master = build_bundle(_compose_cfg(compose_profile="automation"))["notify_master.sh"]
    assert "--profile automation" in master


def test_compose_demotion_stops_and_never_downs() -> None:
    """`down` removes containers and networks, and with a stray flag, volumes.

    A demoted node must be promotable again in ten seconds, not rebuilt.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    backup = build_bundle(_compose_cfg())["notify_backup.sh"]
    assert "compose" in backup and " stop " in backup
    assert " down" not in backup, "demotion must never tear the service down"


def test_compose_promotion_uses_up_not_start() -> None:
    """A node that has never led may have no container at all, and `start`
    cannot create one. `up -d` also applies configuration changed since --
    which is how a /dev/serial mount a promoted node needs actually arrives.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    master = build_bundle(_compose_cfg())["notify_master.sh"]
    assert "up -d" in master
    assert "compose start" not in master


def test_the_env_file_is_sourced_and_exported() -> None:
    """Compose interpolates the WHOLE project file before it filters by profile
    or service, so one unset variable in a service you are not touching aborts
    the command -- measured on this fleet, where `--profile automation` still
    failed on a pgbouncer password three services away. A promoter started by
    systemd has none of the operator's shell environment.
    """
    from custom_components.cluster_state_sync.bundle import build_bundle

    master = build_bundle(_compose_cfg(compose_env_file="/srv/.env"))["notify_master.sh"]
    assert "set -a" in master and "/srv/.env" in master, "env must be sourced AND exported"
    assert master.index("set -a") < master.index("docker compose"), "sourced before use"


# -- AR-0064: the moment our own ingress guidance needs -------------------


def test_post_start_hooks_run_after_home_assistant_answers() -> None:
    """🚨 AR-0064. The gap was a moment we recommended and did not provide.

    `GUIDE-ingress.md` tells the operator to claim a floating address only
    **after** Home Assistant answers — the opposite of the radios, because a
    container's `/dev` is a snapshot at start but an address is not. There was
    no such moment: `pre-start.d/` is before `docker start` and `post-stop.d/`
    is after the container stops.

    Firing straight after `docker start` would not have fixed it. The container
    exists in a second and the instance takes the best part of a minute; an
    address claimed in between accepts connections and returns 502, which is
    worse than no address because a health check sees a live host and stops
    looking.
    """
    script = files()["notify_master.sh"]
    assert "post-start.d" in script, "the hook point our own guidance needs does not exist"
    assert script.index("docker start") < script.index("post-start.d"), (
        "post-start hooks must run after Home Assistant is started, not before"
    )
    wait = script[script.index("docker start") : script.index("post-start.d")]
    assert "curl" in wait and "CLUSTER_SYNC_HA_READY" in wait, (
        "the hooks fire without waiting for Home Assistant to answer — which is "
        "the whole point of the hook point (AR-0064)"
    )


def test_the_readiness_wait_is_bounded_and_never_blocks_promotion() -> None:
    """D4 again: a node that is up must not be held back by a probe.

    If Home Assistant never answers, waiting for ever means the hooks never
    run and an unreachable node stays unreachable. The wait is capped and the
    hooks run regardless, told what was observed.
    """
    script = files()["notify_master.sh"]
    wait = script[script.index("CLUSTER_SYNC_HA_READY=0") : script.index("post-start.d")]
    assert "seq 1" in wait, "the wait must be bounded"
    assert "exit 1" not in wait, "a slow Home Assistant must not abort the promotion"
    assert "export CLUSTER_SYNC_HA_READY" in wait, (
        "the hook cannot decide what to do unless it is told whether HA answered"
    )


def test_a_hook_is_told_when_home_assistant_did_not_answer() -> None:
    """🚨 The honest half.

    Running the hooks blindly would claim an address for a black hole; refusing
    to run them would leave an unreachable node unreachable. So the promoter
    reports what it saw and the hook decides — the same division of labour the
    hook runner already describes.
    """
    script = files()["notify_master.sh"]
    assert "CLUSTER_SYNC_HA_READY=1" in script and "CLUSTER_SYNC_HA_READY=0" in script
    assert "did NOT answer" in script, "a timeout must be visible in the journal"


def test_the_readiness_probe_uses_the_same_url_as_the_promoter() -> None:
    """One deployment, one answer to 'where is Home Assistant'.

    `_promoter_ha_url` already handles `network_mode: host` by falling back to
    loopback. A second, hand-written URL here would drift from it and probe the
    wrong place on exactly the installs that fallback exists for.
    """
    from custom_components.cluster_state_sync.bundle import _promoter_ha_url

    cfg = {**COLD, "ha_container_ip": "10.9.9.9"}
    script = build_bundle(cfg)["notify_master.sh"]
    assert _promoter_ha_url(cfg) in script

    # ...and the loopback fallback for `network_mode: host`.
    plain = {k: v for k, v in COLD.items() if k != "ha_container_ip"}
    assert _promoter_ha_url(plain) in build_bundle(plain)["notify_master.sh"]
