"""Host-side bundle generation (ADR-001 wizard step 5).

ADR-001's "easy to use" requirement: the operator answers a few questions and
the integration generates the Keepalived / nftables / rsync artifacts, instead
of documenting them and leaving the operator to hand-write the lot.

The integration runs in an unprivileged container, so it cannot apply host
nftables rules or start and stop its own container. It emits the artifacts; the
operator installs them. That split is what makes this a generator rather than a
controller.

**These files are generated blind.** Nothing here has been executed against a
real host, and the correct filter point genuinely depends on how Home Assistant
is networked. Every firewall artifact says so in its own header — output that
looks machine-generated reads as tested, and this is not.
"""

from __future__ import annotations

import pathlib
import shlex
from typing import Any

from .const import (
    CONF_BLOCK_DISCOVERY,
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_DOCKER_NETWORK,
    CONF_FILESET_ENABLED,
    CONF_FILESET_STALE_AFTER,
    CONF_HA_CONFIG_PATH,
    CONF_HA_CONTAINER,
    CONF_HA_CONTAINER_IP,
    CONF_HA_UID,
    CONF_IOT_SUBNETS,
    CONF_NODE_ID,
    CONF_REDIS_DB,
    CONF_REDIS_HOST,
    CONF_REDIS_PASSWORD,
    CONF_REDIS_PORT,
    CONF_REDIS_TLS_CA_CERTS,
    CONF_REDIS_USE_TLS,
    CONF_REDIS_USERNAME,
    CONF_SETTLE_DELAY,
    CONF_TOPOLOGY_MODEL,
    DEFAULT_FILESET_STALE_AFTER,
    DEFAULT_HA_CONFIG_PATH,
    DEFAULT_REDIS_DB,
    DEFAULT_REDIS_PORT,
    DEFAULT_SETTLE_DELAY,
    DEGRADED_MARKER_NAME,
    DOCKER_ALL,
    DOCKER_BRIDGE,
    DOCKER_HOST,
    DOCKER_MACVLAN,
    LEASE_TTL_SECONDS,
    STAGED_DIR_NAME,
    TOPOLOGY_WARM,
)
from .crypto import derive_fileset_key

#: The file the Valkey password is written to on the host, 0600, beside
#: `cluster-fileset.key`.
#:
#: It exists because the password must not travel in argv: `ps` shows every
#: process's command line to every user on the box, and the pull runs from a
#: systemd timer on a host that runs other things. `cluster-fileset-pull.sh`
#: sources this and exports the variable, and `docker run -e NAME` passes it
#: through by name, so the value never reaches a command line at all.
VALKEY_PASSWORD_FILENAME = "cluster-fileset-valkey.env"

#: The environment variable both the pull and the promoter read the password
#: from. Must match `scripts/resp.py:PASSWORD_ENV`.
VALKEY_PASSWORD_ENV = "CLUSTER_SYNC_VALKEY_PASSWORD"

#: Artefacts written 0600 rather than the usual 0644. Both carry a
#: credential: one decrypts every mirrored `.storage` file, the other opens
#: the store they are mirrored to.
SECRET_FILENAMES: frozenset[str] = frozenset({"cluster-fileset.key", VALKEY_PASSWORD_FILENAME})

SHEBANG = "#!/usr/bin/env bash\n"
# An unset variable expanding to empty inside a firewall rule produces a very
# different rule from the one intended, so every script aborts instead.
STRICT = "set -euo pipefail\n"

#: Where this deployment records which VRRP state the node is in, and the file
#: every other artefact gates on.
#:
#: Until 2026-08-30 *nothing in the bundle ever wrote it.* Both readers --
#: `cluster-fileset-pull.sh` and `cluster-config-sync.sh` -- only ever read it,
#: and the three scripts that actually know the transition emitted nothing but
#: `logger` lines. So on a stock install the pull logged "not confirmed
#: FOLLOWER" once a minute forever, the go-bag was never staged, and every
#: promotion marked `no_staged_fileset` and reported success. The whole feature
#: was inert and its only symptom was a log line: AR-0040's shape exactly.
#:
#: The Keepalived notify scripts are the only things in this deployment that
#: learn the transition, so they are what write it.
#:
#: `cluster-config-sync.sh` is gone as of the same date, in every
#: configuration -- see `build_bundle`. `cluster-fileset-pull.sh` is now this
#: file's only reader.
VRRP_STATE_DIR = "/run/cluster-sync"
VRRP_STATE_PATH = f"{VRRP_STATE_DIR}/vrrp-state"

#: Where INSTALL.md has the operator install this bundle. Every generated
#: artefact that references a sibling by absolute path uses this.
INSTALL_DIR = "/etc/cluster-sync"

#: Where `cluster-fileset-pull.sh` mounts the pull program inside the borrowed
#: Home Assistant image.
#:
#: Not `/config`. The pull used to be invoked as
#: `python3 /config/cluster_state_sync_bundle/fileset_pull.py`, a path nothing
#: ever wrote: INSTALL.md's `sudo cp * /etc/cluster-sync/` puts it in
#: `INSTALL_DIR`, neither `REPLICATED_DIRS` nor `REPLICATED_FILES` carries the
#: wizard's output directory, and in the cold model the follower's Home
#: Assistant is stopped so it can never run the wizard to write it there. The
#: pull therefore failed on every host that followed INSTALL.md verbatim.
#:
#: `fileset_pull.py` imports `crypto` and `resp` from its own directory
#: (`sys.path[0]`), so all three files mount here and nothing else does.
PULL_MOUNTPOINT = "/opt/cluster-sync"

UNVALIDATED = """# ============================================================================
# GENERATED BY cluster_state_sync — AND NOT VALIDATED
#
# These rules have NOT been tested against a running host. They were generated
# from your wizard answers alone, without access to the machine they target.
#
# This host runs other services. A wrong ruleset can cut them off, and can lock
# you out of SSH. Before loading anything here:
#
#   1. Read it.
#   2. Dry-run it:      nft -c -f <this-file>
#   3. Load it with the safety net, not by hand:
#                       ./nft-safety-revert.sh arm 120
#                       nft -f <this-file>
#                       ./nft-safety-revert.sh cancel      # only if still alive
#
# Step 3 restores the previous ruleset automatically if you lose the box.
# ============================================================================
"""


def _subnets(cfg: dict[str, Any]) -> list[str]:
    raw = str(cfg.get(CONF_IOT_SUBNETS) or "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _nft_set(values: list[str]) -> str:
    """Render an nftables anonymous set, or a single value bare."""
    if len(values) == 1:
        return values[0]
    return "{ " + ", ".join(values) + " }"


def _apply_command(cfg: dict[str, Any], *, leader: bool) -> str:
    """The command a notify script runs to swap the ruleset.

    Must name a file the bundle actually ships. An earlier version hardcoded
    `leader.nft` while generating `leader-host.nft`; Keepalived would have run
    `nft -f` against a missing path, `set -e` would abort the promotion, and
    the failover would simply not happen — discovered during an outage.
    """
    role = "leader" if leader else "follower"
    mode = cfg.get(CONF_DOCKER_NETWORK, DOCKER_ALL)
    if mode == DOCKER_HOST:
        return f"nft -f /etc/cluster-sync/{role}-host.nft"
    if mode == DOCKER_MACVLAN:
        return f"nft -f /etc/cluster-sync/{role}-macvlan.nft"
    if mode == DOCKER_BRIDGE:
        # iptables, not nftables — `nft -f` cannot run this.
        return f"/etc/cluster-sync/{role}-bridge.sh"
    return f"/etc/cluster-sync/apply-{role}.sh"


def _dispatcher(cfg: dict[str, Any], *, leader: bool) -> str:
    """Emitted when the Docker network mode is unknown.

    Rather than guessing a variant — which would produce rules that load
    cleanly and match nothing — the notify scripts call this, and the operator
    makes one deliberate choice in one place.
    """
    role = "leader" if leader else "follower"
    container = cfg.get(CONF_HA_CONTAINER, "homeassistant")
    return (
        f"{SHEBANG}{STRICT}"
        f"# Applies the {role} ruleset for whichever Docker network mode Home\n"
        "# Assistant actually uses. All three variants were generated because\n"
        "# the mode was not known at setup time.\n"
        "#\n"
        "# SET THIS ONCE, on both hosts. Find the answer with:\n"
        f"#     docker inspect -f '{{{{.HostConfig.NetworkMode}}}}' {container}\n"
        "\n"
        "NETWORK_MODE=          # <-- REQUIRED: host | macvlan | bridge\n"
        "\n"
        'case "$NETWORK_MODE" in\n'
        f"  host)    nft -f /etc/cluster-sync/{role}-host.nft ;;\n"
        f"  macvlan) nft -f /etc/cluster-sync/{role}-macvlan.nft ;;\n"
        f"  bridge)  /etc/cluster-sync/{role}-bridge.sh ;;\n"
        "  *)\n"
        "    logger -t cluster-sync 'NETWORK_MODE unset — refusing to guess'\n"
        "    echo 'Set NETWORK_MODE in this script first.' >&2\n"
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
    )


def _device_preflight() -> str:
    """The promotion device probe, read from the package rather than re-typed.

    Shipping a copy transcribed into a string here would be a second source of
    truth that drifts from the tested module the moment either is edited. The
    file is read at bundle time, which is safe because `write_bundle` is
    documented as blocking and its only caller runs it in an executor.
    """
    return (pathlib.Path(__file__).parent / "scripts" / "ha_device_preflight.py").read_text(
        encoding="utf-8"
    )


def _fileset_identity_program() -> str:
    """The identity-preserving surgery on `core.config_entries`.

    Read from the package for the same reason `_device_preflight()` is: a
    transcribed copy here would drift from the tested module, and this one is
    only ever exercised during a promotion -- the worst place to discover a
    drifted copy.
    """
    return (pathlib.Path(__file__).parent / "scripts" / "fileset_identity.py").read_text(
        encoding="utf-8"
    )


def _fileset_pull_program() -> str:
    """The follower-side pull, read from the package rather than re-typed.

    Mirrors `_device_preflight()` above: a hand-copied string here would drift
    from the tested module the moment either changed, and a drifted copy fails
    silently until the one moment it is actually exercised -- a promotion.
    """
    return (pathlib.Path(__file__).parent / "scripts" / "fileset_pull.py").read_text(
        encoding="utf-8"
    )


def _fileset_crypto_module() -> str:
    """`crypto.py`, shipped beside `fileset_pull.py` in the bundle.

    `fileset_pull.py` imports with a two-arm fallback: `from ..crypto import
    ...` when it runs as part of the package (these tests), `from crypto
    import ...` when it runs as a standalone script on the host. The second
    arm works only because Python puts a script's own directory on
    `sys.path[0]` -- which requires `crypto.py` to actually be sitting next to
    it. Ship the pull program without this and it cannot import, and it fails
    at the worst possible moment: during a promotion.
    """
    return (pathlib.Path(__file__).parent / "crypto.py").read_text(encoding="utf-8")


def _resp_module() -> str:
    """`resp.py`, the stdlib-only Valkey/RESP client shared by the pull and
    the promoter, shipped beside both in the bundle.

    Both `fileset_pull.py` and `cluster_promoter.py` import it with the same
    two-arm fallback `fileset_pull.py` already uses for `crypto.py`: `from
    ..resp import ...` inside the package, `from resp import ...` as a
    standalone script -- which only works because Python puts a script's own
    directory on `sys.path[0]`, and that requires `resp.py` to actually be
    sitting next to it. Ship either program without this and it cannot
    import: `fileset_pull.py` fails during a promotion, and
    `cluster-promoter.sh` fails on every tick.
    """
    return (pathlib.Path(__file__).parent / "scripts" / "resp.py").read_text(encoding="utf-8")


def _config_path(cfg: dict[str, Any]) -> str:
    """Home Assistant's config directory on the HOST (AR-0042).

    Not knowable from inside the container: `/config` in here is a bind mount
    and tells you nothing about the path outside it. The measured example is
    the deployment this was written for — the bundle assumed
    `/opt/homeassistant/config` and the host actually used
    `/mnt/docker_data/homeassistant/config`, so every generated artefact
    pointed at nothing.

    Trailing slashes are stripped so callers can append `/...` without
    producing `//`, which rsync treats differently from `/`.
    """
    return str(cfg.get(CONF_HA_CONFIG_PATH) or DEFAULT_HA_CONFIG_PATH).rstrip("/")


def build_bundle(cfg: dict[str, Any]) -> dict[str, str]:
    """Render every artifact for this deployment as {filename: content}."""
    warm = cfg.get(CONF_TOPOLOGY_MODEL) == TOPOLOGY_WARM
    fileset = bool(cfg.get(CONF_FILESET_ENABLED))
    bundle: dict[str, str] = {
        "ha-device-preflight.py": _device_preflight(),
        "notify_master.sh": _notify_master(cfg, warm),
        "notify_backup.sh": _notify_demote(cfg, warm, "backup"),
        "notify_fault.sh": _notify_demote(cfg, warm, "fault"),
        # The boot default for the file the notify scripts above write. Emitted
        # in every configuration, because both gates read it.
        "cluster-sync-tmpfiles.conf": _tmpfiles_conf(),
    }
    # ADR-001 tier 1 is gone -- in EVERY configuration, not only alongside the
    # fileset. Owner decision, 2026-08-30.
    #
    # The rsync copied `.storage/***` leader -> follower every two minutes, onto
    # the follower's LIVE config directory. It was inert on every install that
    # ever ran it, because nothing wrote `vrrp-state` until the notify scripts
    # above were fixed to write it (the C1 defect) -- and once they were, the
    # rsync would have come alive for the first time, straight into a
    # two-leader bug: the follower's `core.config_entries` becomes the leader's
    # copy, so at promotion `cluster-fileset-identity.py capture` captures the
    # *leader's* cluster_state_sync entry as "ours", grafts it back after the
    # swap, and the promoted node comes up holding the peer's node_id. The
    # lease renews on identity, so both nodes then lead -- the exact defect
    # Task 13 closed inside the swap, reintroduced from outside it. On a WARM
    # standby that `.storage` belongs to a RUNNING Home Assistant, which the
    # rsync's own former header called unsafe: HA holds those files in memory
    # and overwrites them.
    #
    # Fileset replication covers the same allow-list (`.storage`,
    # `custom_components`, `www`, `blueprints`, the seven top-level YAML files)
    # properly -- encrypted, identity-preserving, no SSH trust between hosts --
    # so the rsync has nothing left to contribute. Nobody depends on it: it has
    # never run, on any install, ever. Design §5 already called it "replaced in
    # practice"; it is now replaced outright.
    #
    # The three filenames stay in MANAGED_FILENAMES below even though nothing
    # here ever emits them, so `write_bundle` prunes a copy an earlier bundle
    # installed rather than stranding it on disk (ADR-005).
    if warm:
        bundle.update(_firewall(cfg))
        bundle["nft-safety-revert.sh"] = _safety_revert()
        if cfg.get(CONF_DOCKER_NETWORK, DOCKER_ALL) == DOCKER_ALL:
            bundle["apply-leader.sh"] = _dispatcher(cfg, leader=True)
            bundle["apply-follower.sh"] = _dispatcher(cfg, leader=False)
    if fileset:
        bundle["fileset_pull.py"] = _fileset_pull_program()
        bundle["crypto.py"] = _fileset_crypto_module()
        # The stdlib-only Valkey/RESP client, shared by the pull above and
        # the promoter below -- one shipped copy, so it cannot drift between
        # what the two are tested against.
        bundle["resp.py"] = _resp_module()
        bundle["cluster-fileset-pull.sh"] = _fileset_pull(cfg)
        bundle["cluster-fileset-pull.service"] = _fileset_pull_service()
        bundle["cluster-fileset-pull.timer"] = _fileset_pull_timer()
        bundle["cluster-fileset-swap.sh"] = _fileset_swap(cfg)
        # Shipped beside the swap script, because the swap finds it with
        # `$(dirname "$0")` -- the same way notify_master.sh finds the device
        # pre-flight. Separated from the swap rather than inlined as a heredoc
        # so the shipped copy is the copy the suite tests.
        bundle["cluster-fileset-identity.py"] = _fileset_identity_program()
        bundle["cluster-fileset.key"] = _fileset_key(cfg)
        # Only when there is a password to carry. An empty 0600 file would be
        # one more artefact to explain and one more to get wrong, and a Valkey
        # without `requirepass` or an ACL user needs none.
        if _valkey_password(cfg):
            bundle[VALKEY_PASSWORD_FILENAME] = _fileset_valkey_password(cfg)
        # The promoter -- Keepalived's replacement (ADR-005: emitted, never
        # applied). Gated on the same opt-in as the rest of this block, and
        # the coupling is structural, not arbitrary: the promoter's Valkey
        # credentials come from VALKEY_PASSWORD_FILENAME, which is itself a
        # fileset artefact. It is also the only case that matters in
        # practice -- a cold standby with no go-bag has nothing to promote
        # *to*, since the fileset is what makes promoting while Home
        # Assistant is stopped possible in the first place. Decoupling the
        # promoter's own Valkey config from the fileset's is future work; see
        # TODO.md.
        bundle["cluster_promoter.py"] = _promoter_program()
        bundle["lease.py"] = _lease_module()
        bundle["cluster-promoter.sh"] = _promoter_sh(cfg)
        bundle["cluster-promoter.service"] = _promoter_service()
        bundle["cluster-promoter.timer"] = _promoter_timer()
    bundle["INSTALL.md"] = _install_readme(cfg, warm)
    return bundle


# -- Keepalived notify scripts ---------------------------------------------


def _record_vrrp_state(state: str) -> str:
    """The block every notify script opens with. Nothing else writes this file.

    Three properties, each load-bearing:

    * **First.** It precedes every other command in the script, so nothing
      under `set -euo pipefail` can abort before the transition is recorded.
      A promotion that dies on a failed `docker stop` must still leave
      `MASTER` behind, or the follower carries on pulling onto a node that is
      now the leader and the leader never pushes.
    * **Non-fatal.** The write is the condition of an `if`, which `set -e`
      exempts, so a read-only or full `/run` logs and carries on rather than
      aborting a failover over a status file.
    * **Atomic.** The reader (`cluster-fileset-pull.sh`, when fileset
      replication is enabled) `cat`s this from a timer that fires once a
      minute and can catch a write in progress; a torn value matches none of
      `MASTER`, `BACKUP` or `FAULT`, and the reader's own gate fails closed on
      that for real -- see its allow-list of BACKUP/FAULT.
    """
    return (
        "# Record the VRRP state FIRST, before anything below can abort.\n"
        "# `cluster-fileset-pull.sh` gates on this file when fileset replication\n"
        "# is enabled, and nothing else in the deployment writes it -- so a script\n"
        "# that aborts before this point leaves the whole cluster acting on the\n"
        "# previous state. Written to a temporary name and renamed, because that\n"
        "# reader runs from a timer that can catch a write in progress and a torn\n"
        "# value matches none of MASTER, BACKUP or FAULT.\n"
        "#\n"
        "# Overridable via CLUSTER_SYNC_STATE_FILE for the same reason the pull's\n"
        "# is: so a test can point it at a throwaway file and exercise this for\n"
        "# real rather than grepping for the string. Nothing sets it outside a\n"
        "# test harness.\n"
        'STATE_FILE="${CLUSTER_SYNC_STATE_FILE:-' + VRRP_STATE_PATH + '}"\n'
        'if ! { mkdir -p "$(dirname "$STATE_FILE")" &&\n'
        "       printf '" + state + '\\n\' > "$STATE_FILE.$$" &&\n'
        '       mv -f "$STATE_FILE.$$" "$STATE_FILE"; }; then\n'
        '    rm -f "$STATE_FILE.$$" || true\n'
        "    logger -t cluster-sync 'could not record VRRP state " + state + "'\n"
        "fi\n"
        "\n"
    )


def _tmpfiles_conf() -> str:
    """The boot-time default, so a freshly-rebooted node is a follower.

    `/run` is a tmpfs, so the state directory does not survive a reboot and
    the notify scripts cannot create it before Keepalived first calls them.
    Between boot and the first VRRP transition the state file would otherwise
    be absent, and the reader fails closed on absent -- so a node that
    rebooted and correctly stayed BACKUP would never pull and never stage a
    go-bag, which is the C1 defect again in a narrower window.

    `BACKUP` is the safe default. It is the state that makes this node pull
    rather than push, and Keepalived overwrites it within a second of deciding
    otherwise. `f` writes the argument only when the file does not already
    exist, so a `systemd-tmpfiles --create` run by hand mid-flight cannot
    demote a live leader.

    The same directory this creates is also where `cluster-promoter.sh`
    defaults its `force-master` override to (design D2) -- deliberately, so a
    forgotten override self-clears on the same reboot that clears everything
    else here, rather than surviving on disk under `/etc` forever.
    """
    return (
        "# systemd-tmpfiles — the VRRP state directory and its boot default.\n"
        "#\n"
        "# Install as /etc/tmpfiles.d/cluster-sync.conf. See INSTALL.md.\n"
        "#\n"
        "# BACKUP is deliberate: it is the state that makes this node PULL rather\n"
        "# than push, and Keepalived overwrites it within a second of deciding\n"
        "# otherwise. `f` only writes the argument when the file does not already\n"
        "# exist, so running systemd-tmpfiles by hand cannot demote a live leader.\n"
        "#\n"
        "# This directory is also where the promoter's force-master override\n"
        "# (design D2) defaults to living -- on tmpfs, deliberately, so a\n"
        "# forgotten override does not survive a reboot. See INSTALL.md.\n"
        f"d {VRRP_STATE_DIR} 0755 root root -\n"
        f"f {VRRP_STATE_PATH} 0644 root root - BACKUP\n"
    )


def _notify_master(cfg: dict[str, Any], warm: bool) -> str:
    container = cfg.get(CONF_HA_CONTAINER, "homeassistant")
    settle = cfg.get(CONF_SETTLE_DELAY, DEFAULT_SETTLE_DELAY)
    fileset_enabled = bool(cfg.get(CONF_FILESET_ENABLED))
    # 0. Install the replicated fileset BEFORE anything reads or edits it. The
    # device pre-flight below rewrites .storage to drop the entries this node
    # must not claim; swapping afterwards would throw that work away and the
    # node would take the peer's USB radios while looking perfectly healthy.
    # Gated on the fileset opt-in: an operator who never enabled it must not
    # get a promotion script that execs a file the bundle never wrote.
    swap = (
        (
            "# 0. Install the replicated fileset BEFORE anything reads or edits\n"
            "#    it. The device pre-flight below rewrites .storage to drop the\n"
            "#    entries this node must not claim; swapping afterwards would\n"
            "#    throw that work away and the node would take the peer's USB\n"
            "#    radios while looking perfectly healthy.\n"
            "/etc/cluster-sync/cluster-fileset-swap.sh || \\\n"
            "  logger -t cluster-sync 'fileset swap failed; promoting anyway'\n"
            "\n"
        )
        if fileset_enabled
        else ""
    )
    if not warm:
        return (
            f"{SHEBANG}{STRICT}"
            "# Keepalived notify_master — COLD standby promotion.\n"
            "#\n"
            "# Two steps: settle the hardware, then start Home Assistant. It\n"
            f"# boots against the {'swapped-in' if fileset_enabled else 'rsynced'} "
            "config and seeds its state from\n"
            "# the shared snapshot before the automation engine starts. No\n"
            "# firewall is involved, because a process that is not running has\n"
            "# no side effects to suppress.\n"
            "\n"
            f"{_record_vrrp_state('MASTER')}"
            f"logger -t cluster-sync 'Promoting to MASTER — starting {container}'\n"
            "\n"
            f"{swap}"
            "# Device pre-flight, BEFORE Home Assistant starts. Radios delivered\n"
            "# over the network are claimed on promotion, and anything still\n"
            "# absent has its config entry disabled so the integration does not\n"
            "# fail at setup and stay failed until someone restarts it by hand.\n"
            "# Running this afterwards would be useless: by then the damage is\n"
            "# done and the container looks perfectly healthy.\n"
            f'python3 "$(dirname "$0")/ha-device-preflight.py" \\\n'
            f"  --storage {_config_path(cfg)}/.storage \\\n"
            f"  --container {container} --apply || \\\n"
            "  logger -t cluster-sync 'device pre-flight failed; starting anyway'\n"
            "\n"
            f"docker start {container}\n"
        )
    # Warm cannot install identity under a live Home Assistant. This instance
    # is already running and holds .storage (and the other stores the swap
    # replaces) in memory; on shutdown it writes them straight back to disk,
    # overwriting whatever the swap just installed underneath it. `docker
    # restart` does exactly that shutdown, which is why it is wrong here.
    # Found in rehearsal (Task 11), the hard way: every run that swapped
    # under a live HA lost core.device_registry and http to this race, and
    # one run lost .storage/auth -- the refresh tokens this feature exists to
    # carry across a promotion. So warm goes through the same stopped window
    # cold does -- stop, swap, pre-flight, start -- which costs roughly what
    # a cold boot costs. Warm keeps its real advantage (nftables gating, no
    # side effects during the overlap window); it no longer keeps an RTO edge
    # over cold. Gated on the same opt-in as the swap: an operator who never
    # enabled the fileset gets the original, stop-free warm promotion.
    stop = (
        (
            "# Stop Home Assistant BEFORE the fileset swap below runs. This\n"
            "# instance is already running and holds .storage and its\n"
            "# siblings in memory; on shutdown it writes them straight back\n"
            "# to disk, overwriting whatever the swap just installed\n"
            "# underneath it. Found in rehearsal (Task 11): every run that\n"
            "# swapped under a live HA lost core.device_registry and http\n"
            "# this way, and one run lost .storage/auth -- the refresh\n"
            "# tokens this feature exists to carry across a promotion. Warm\n"
            "# now goes through the same stopped window cold does: stop,\n"
            "# swap, pre-flight, start. That costs roughly what a cold boot\n"
            "# costs -- warm keeps its nftables gating and its lack of side\n"
            "# effects during the overlap window, but not its RTO edge over\n"
            "# cold.\n"
            "#\n"
            "# `|| logger`, like every other risky step in this script. Under\n"
            "# `set -euo pipefail` a bare `docker stop` that fails aborts the\n"
            "# whole promotion -- before the swap, before the pre-flight, and\n"
            "# before `nft -f leader.nft`. Keepalived has already moved the\n"
            "# VIP by then, so the node would hold the VIP with the FOLLOWER\n"
            "# ruleset still loaded: a total outage rather than a failover.\n"
            "# Carrying on is the better failure: the swap below refuses to\n"
            "# install under a live Home Assistant anyway (that is what\n"
            "# --container guards), so the node promotes on its own .storage.\n"
            f"docker stop {container} || \\\n"
            f"  logger -t cluster-sync 'docker stop {container} failed; promoting anyway'\n"
            "\n"
        )
        if fileset_enabled
        else ""
    )
    start = (
        (
            "# Home Assistant comes back up with the swapped identity and\n"
            "# the peer's radios already disabled by the pre-flight above.\n"
            "#\n"
            "# `|| logger` for the same reason the stop above has it, and the\n"
            "# stakes here are higher: this sits BEFORE `nft -f leader.nft`, so\n"
            "# a bare `docker start` that fails would abort holding the VIP\n"
            "# with Home Assistant down AND the follower ruleset still loaded.\n"
            "# Reaching the firewall step with a failed start is strictly\n"
            "# better than not reaching it at all.\n"
            f"docker start {container} || \\\n"
            f"  logger -t cluster-sync 'docker start {container} failed; opening the "
            "ruleset anyway'\n"
            "\n"
        )
        if fileset_enabled
        else ""
    )
    # With the fileset enabled, the container was just stopped for the swap
    # above -- so, unlike the base warm model, editing .storage here is exactly
    # as safe as it is on cold, and skipping --apply would leave this node
    # with no device-claim protection at all: the promoted node could claim
    # the peer's USB, Zigbee and Bluetooth radios, precisely the hazard this
    # task exists to prevent. Without the fileset, Home Assistant is still
    # running throughout warm promotion, so the pre-flight stays report-only,
    # exactly as before.
    preflight = (
        (
            "# 1. Device pre-flight, WITH --apply. The container was just\n"
            "#    stopped above for the fileset swap, so editing .storage\n"
            "#    here is exactly as safe as it is on cold -- nothing holds\n"
            "#    it in memory to overwrite this. Skipping --apply here\n"
            "#    would leave this node with no device-claim protection at\n"
            "#    all: it would start still claiming the peer's USB, Zigbee\n"
            "#    and Bluetooth radios. --container is passed as a second\n"
            "#    guard: if it is somehow still running, the script refuses\n"
            "#    to --apply rather than race it, and reports instead.\n"
            f'python3 "$(dirname "$0")/ha-device-preflight.py" \\\n'
            f"  --storage {_config_path(cfg)}/.storage \\\n"
            f"  --container {container} --apply || \\\n"
            "  logger -t cluster-sync 'device pre-flight failed; starting anyway'\n"
            "\n"
        )
        if fileset_enabled
        else (
            "# 1. Device pre-flight — REPORT ONLY in the warm model.\n"
            "#\n"
            "#    Home Assistant is already running here, and it rewrites\n"
            "#    core.config_entries from memory on any config-entry change and on\n"
            "#    shutdown. Editing that file underneath a live process is lost at\n"
            "#    best and corrupting at worst, so this reports what is missing and\n"
            "#    changes nothing. Disabling an entry on a running instance is a job\n"
            "#    for the Home Assistant API, not a text editor.\n"
            'python3 "$(dirname "$0")/ha-device-preflight.py" \\\n'
            f"  --storage {_config_path(cfg)}/.storage 2>&1 | \\\n"
            "  logger -t cluster-sync || true\n"
            "\n"
        )
    )
    return (
        f"{SHEBANG}{STRICT}"
        "# Keepalived notify_master — WARM standby promotion.\n"
        "#\n"
        "# Order matters. Open the network first so integrations can reconnect,\n"
        "# then wait out the settle delay before letting automations act. Firing\n"
        "# automations against half-reconnected devices is how a failover causes\n"
        "# the incident it was supposed to prevent.\n"
        "\n"
        f"{_record_vrrp_state('MASTER')}"
        "logger -t cluster-sync 'Promoting to MASTER'\n"
        "\n"
        f"{stop}"
        f"{swap}"
        f"{preflight}"
        f"{start}"
        "# 2. Layer 1 — open the ruleset.\n"
        f"{_apply_command(cfg, leader=True)}\n"
        "\n"
        "# 3. Layers 2 and 3 — the integration reads leadership from its own\n"
        "#    configured signal (a Home Assistant entity, or the Valkey lease).\n"
        "#    If you chose the entity signal, flip it here.\n"
        "#    Example, using the HA CLI inside the container:\n"
        f"#    docker exec {container} \\\n"
        "#      ha service call input_boolean.turn_on \\\n"
        "#      --arguments entity_id=input_boolean.cluster_leader\n"
        "#\n"
        "#    The entity you name as the leadership signal is never mirrored to\n"
        "#    the shared snapshot and never restored from it, whatever the\n"
        "#    include/exclude lists say (AR-0038). It is a statement about this\n"
        "#    node, so replicating it would hand the standby the leader's flag\n"
        "#    and both would promote. Set it on each host independently.\n"
        "\n"
        "# 4. Layer 4 — settle, then enable automations.\n"
        f"sleep {settle}\n"
        "logger -t cluster-sync 'Settle window elapsed — automations may run'\n"
    )


def _notify_demote(cfg: dict[str, Any], warm: bool, reason: str) -> str:
    container = cfg.get(CONF_HA_CONTAINER, "homeassistant")
    # notify_fault gets the same treatment as notify_backup on purpose: a node
    # in VRRP fault state is not healthy enough to be leader, and leaving fault
    # unhandled is how a broken node carries on writing.
    header = (
        f"# Keepalived notify_{reason} — demote this node.\n"
        "#\n"
        "# fault is handled identically to backup: a node in VRRP fault state is\n"
        "# not healthy enough to lead, and an unhandled fault leaves a broken node\n"
        "# still writing to the shared snapshot.\n"
        if reason == "fault"
        else f"# Keepalived notify_{reason} — demote this node.\n"
    )
    # BACKUP and FAULT are recorded distinctly rather than both as BACKUP. The
    # fileset pull's gate allows either one and proceeds identically for both
    # today — but this file is what an operator reads at 3am to find out what
    # the node thinks it is, and collapsing a fault into a healthy standby
    # throws away the one bit that says the node is broken rather than idle.
    state = "FAULT" if reason == "fault" else "BACKUP"
    if not warm:
        return (
            f"{SHEBANG}{STRICT}{header}"
            "\n"
            f"{_record_vrrp_state(state)}"
            f"logger -t cluster-sync 'Demoting to {reason.upper()} — stopping {container}'\n"
            f"docker stop {container}\n"
        )
    return (
        f"{SHEBANG}{STRICT}{header}"
        "\n"
        f"{_record_vrrp_state(state)}"
        f"logger -t cluster-sync 'Demoting to {reason.upper()}'\n"
        "\n"
        "# Close the network first, so nothing escapes while the rest catches up.\n"
        f"{_apply_command(cfg, leader=False)}\n"
        "\n"
        "# Then drop leadership. If you use the Valkey lease the integration\n"
        "# releases it on its own; if you use the entity signal, clear it here.\n"
    )


# -- Fileset replication: pull and swap -------------------------------------


def _valkey_password(cfg: dict[str, Any]) -> str:
    """The Valkey password, or "" when this deployment has none."""
    return str(cfg.get(CONF_REDIS_PASSWORD) or "")


def _fileset_pull(cfg: dict[str, Any]) -> str:
    container = cfg.get(CONF_HA_CONTAINER, "homeassistant")
    redis_host = cfg.get(CONF_REDIS_HOST, "valkey.lan")
    redis_port = cfg.get(CONF_REDIS_PORT, DEFAULT_REDIS_PORT)
    namespace = cfg.get(CONF_CLUSTER_NAMESPACE, "default")
    db = cfg.get(CONF_REDIS_DB, DEFAULT_REDIS_DB)
    username = str(cfg.get(CONF_REDIS_USERNAME) or "")
    use_tls = bool(cfg.get(CONF_REDIS_USE_TLS))
    ca_certs = str(cfg.get(CONF_REDIS_TLS_CA_CERTS) or "") if use_tls else ""

    # The password never appears here. It is sourced from a 0600 file into the
    # environment and handed to `docker run` by name, because argv is world
    # readable through `ps` -- see VALKEY_PASSWORD_FILENAME.
    password_block = (
        (
            "# The Valkey password. Kept out of this file, and out of the docker\n"
            "# command line below, because `ps` shows argv to every user on this\n"
            "# host and this runs from a timer on a machine that runs other things.\n"
            "#\n"
            "# Overridable via CLUSTER_SYNC_PASSWORD_FILE for the same reason\n"
            "# CLUSTER_SYNC_STATE_FILE above is: so a test can point it at a\n"
            "# throwaway file and exercise the missing-file and sourced-file\n"
            "# branches for real, rather than only grepping for the strings.\n"
            "# Nothing sets it outside a test harness.\n"
            'PASSWORD_FILE="${CLUSTER_SYNC_PASSWORD_FILE:-/etc/cluster-sync/'
            f'{VALKEY_PASSWORD_FILENAME}}}"\n'
            'if [[ ! -f "$PASSWORD_FILE" ]]; then\n'
            "    # Refuse rather than pull unauthenticated: a Valkey that answers\n"
            "    # without a password is not the one this cluster publishes to, and\n"
            "    # staging whatever it returns would be worse than staging nothing.\n"
            '    logger -t cluster-sync "missing $PASSWORD_FILE — cannot authenticate '
            'to Valkey"\n'
            "    exit 1\n"
            "fi\n"
            "# shellcheck source=/dev/null\n"
            '. "$PASSWORD_FILE"\n'
            f"export {VALKEY_PASSWORD_ENV}\n"
            "\n"
        )
        if _valkey_password(cfg)
        else ""
    )
    # `-e NAME` with no `=value` passes the variable through from this shell's
    # environment; `-e NAME=value` would put the value in the command line.
    password_flag = f"    -e {VALKEY_PASSWORD_ENV} \\\n" if _valkey_password(cfg) else ""
    ca_mount = f'    -v "{ca_certs}:/ca:ro" \\\n' if ca_certs else ""
    username_flag = f"        --username {username} \\\n" if username else ""
    tls_flags = "        --tls \\\n" if use_tls else ""
    tls_flags += "        --tls-ca-file /ca \\\n" if ca_certs else ""

    return (
        f"{SHEBANG}{STRICT}"
        "# Fileset replication — pull. Follower only.\n"
        "#\n"
        "# Runs on the HOST, not inside Home Assistant, because in the cold model the\n"
        "# standby's container is stopped and there is no integration there to receive.\n"
        "#\n"
        "# It needs AES-GCM, which this host may not have and the Home Assistant image\n"
        "# does. It does NOT need a Valkey client: the image has no `redis` (that is a\n"
        "# custom-component requirement Home Assistant pip-installs into the RUNNING\n"
        "# container, so a fresh `docker run` has never seen it), and fileset_pull.py\n"
        "# therefore speaks RESP out of the standard library. The image tag is read\n"
        "# straight off the container -- `docker inspect` works on a stopped one too --\n"
        "# rather than guessed at bundle-generation time.\n"
        "#\n"
        "# Writes to .incoming and renames into place only when the whole manifest\n"
        "# verifies. A failed pull leaves the last good staged copy untouched: never a\n"
        "# partial go-bag.\n"
        "\n"
        "# Overridable via CLUSTER_SYNC_STATE_FILE so a test can point this at a\n"
        "# throwaway file and exercise every branch below for real; nothing sets that\n"
        "# variable outside a test harness, so production always gets the real path.\n"
        'STATE_FILE="${CLUSTER_SYNC_STATE_FILE:-/run/cluster-sync/vrrp-state}"\n'
        f"CONFIG={_config_path(cfg)}\n"
        f'CONTAINER="{container}"\n'
        "\n"
        "# Only a FOLLOWER pulls, and this genuinely fails CLOSED: the condition\n"
        '# below is a positive match on BACKUP or FAULT, not merely "not MASTER",\n'
        "# so a missing state file (a boot race, Keepalived not yet settled), a torn\n"
        "# read, or any value this deployment never wrote are all read the same way\n"
        "# -- unconfirmed, and unconfirmed means skip. Proceeding on an *unknown*\n"
        "# state is exactly how a leader that has not yet written vrrp-state would\n"
        '# overwrite its own identity with an older copy of itself; "probably a\n'
        '# follower" is not a good enough guarantee to act on. (The write above is\n'
        "# atomic, so a torn read should not actually happen -- this gate does not\n"
        "# rely on that being true.)\n"
        'STATE="$(cat "$STATE_FILE" 2>/dev/null || true)"\n'
        'if [[ "$STATE" != "BACKUP" ]] && [[ "$STATE" != "FAULT" ]]; then\n'
        "    logger -t cluster-sync 'not confirmed FOLLOWER — skipping fileset pull'\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        f"{password_block}"
        "IMAGE=$(docker inspect -f '{{.Config.Image}}' \"$CONTAINER\")\n"
        "\n"
        "# The program is mounted from where INSTALL.md puts it, not from /config.\n"
        "# It used to be run from the config directory, where nothing ever put it:\n"
        f"# `sudo cp * {INSTALL_DIR}/` installs it here, no rsync or fileset entry\n"
        "# replicated it into /config, and in the cold model the follower's Home\n"
        "# Assistant is stopped so it can never run the wizard to write it there.\n"
        "# The invocation and the documented install path now name the same file.\n"
        "#\n"
        "# Two single-file mounts rather than the whole directory: this container\n"
        f"# has no business seeing {INSTALL_DIR}/cluster-fileset.key by any name but\n"
        "# /key, nor the Valkey password file at all.\n"
        "#\n"
        "# --entrypoint python3, because the image's own entrypoint is s6-overlay's\n"
        "# /init. It does run the command and does propagate the exit code, but it\n"
        "# boots the whole supervision tree first -- measured at 9-23s per\n"
        "# invocation -- and in this image that tree is what starts Home Assistant.\n"
        "# Once a minute, on host networking, with /config mounted, that is how a\n"
        "# standby ends up running a second unmanaged Home Assistant that can load\n"
        "# this integration and take the lease.\n"
        "docker run --rm \\\n"
        "    --entrypoint python3 \\\n"
        '    -v "$CONFIG:/config" \\\n'
        f"    -v {INSTALL_DIR}/cluster-fileset.key:/key:ro \\\n"
        f"    -v {INSTALL_DIR}/fileset_pull.py:{PULL_MOUNTPOINT}/fileset_pull.py:ro \\\n"
        f"    -v {INSTALL_DIR}/crypto.py:{PULL_MOUNTPOINT}/crypto.py:ro \\\n"
        # resp.py too: fileset_pull.py's two-arm import falls back to `from
        # resp import ...` inside this container (its own package is not
        # importable from a bare `python3 fileset_pull.py`), and that fallback
        # only resolves because Python puts a script's own directory on
        # `sys.path[0]` -- which requires resp.py to actually be mounted here.
        # Omit this and the pull fails on every run with an ImportError, once
        # a minute, on the node standing by to take over.
        f"    -v {INSTALL_DIR}/resp.py:{PULL_MOUNTPOINT}/resp.py:ro \\\n"
        f"{ca_mount}"
        f"{password_flag}"
        "    --network host \\\n"
        '    "$IMAGE" \\\n'
        f"    {PULL_MOUNTPOINT}/fileset_pull.py \\\n"
        f"        --redis {redis_host}:{redis_port} \\\n"
        f"        --namespace {namespace} \\\n"
        "        --key-file /key \\\n"
        f"        --staged /config/{STAGED_DIR_NAME} \\\n"
        f"        --db {db} \\\n"
        f"{username_flag}"
        f"{tls_flags}"
        "    || { logger -t cluster-sync 'fileset pull failed; staged copy unchanged'; exit 1; }\n"
        "\n"
        "logger -t cluster-sync 'Fileset pull complete'\n"
    )


def _fileset_pull_service() -> str:
    return (
        "[Unit]\n"
        "Description=Cluster State Sync — pull the replicated fileset onto this "
        "follower\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/etc/cluster-sync/cluster-fileset-pull.sh\n"
    )


def _fileset_pull_timer() -> str:
    return (
        "[Unit]\n"
        "Description=Run the cluster fileset pull, so a follower's staged go-bag "
        "stays close to current\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=1min\n"
        "OnUnitActiveSec=1min\n"
        "AccuracySec=10s\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _fileset_swap(cfg: dict[str, Any]) -> str:
    stale_after = cfg.get(CONF_FILESET_STALE_AFTER, DEFAULT_FILESET_STALE_AFTER)
    return (
        f"{SHEBANG}"
        "set -uo pipefail\n"
        "# Fileset replication — swap. Runs at promotion, BEFORE the container starts\n"
        "# and BEFORE the device pre-flight edits what this installs.\n"
        "#\n"
        "# Decision D4: a stale or missing go-bag does not stop a promotion. Every path\n"
        "# below exits 0. That makes the marker this writes the only signal that\n"
        "# anything is wrong, so it is written on every degraded path, removed only by a\n"
        "# clean swap, and never overwritten by a later, less specific reason once this\n"
        "# run has already marked -- see `mark` below, which enforces that rather than\n"
        "# leaving it to every caller remembering to exit.\n"
        "\n"
        f"CONFIG={_config_path(cfg)}\n"
        f'STAGED="$CONFIG/{STAGED_DIR_NAME}"\n'
        f'MARKER="$CONFIG/{DEGRADED_MARKER_NAME}"\n'
        f"STALE_AFTER={stale_after}\n"
        "FAILED=0\n"
        "MARKED=0\n"
        "\n"
        "# First reason wins, for this run. The header above promises the specific\n"
        "# reason survives, and that used to hold by construction because every mark\n"
        "# was followed immediately by `exit 0`. The identity marks below are not --\n"
        "# they let the rest of the swap proceed -- so a later `swap_dir` or `cp -a`\n"
        "# failure would overwrite the consequential reason with the generic one.\n"
        "#\n"
        "# Guarded on a variable, NOT on the marker file already existing: the marker\n"
        "# is removed only by a clean swap, so a node that promoted degraded once\n"
        "# carries the file until it promotes cleanly. Testing the file would pin the\n"
        "# operator to a reason from a promotion that may be days old.\n"
        "mark() {\n"
        "    (( MARKED )) && return 0\n"
        "    MARKED=1\n"
        '    printf \'{"reason":"%s","age_s":%s,"at":"%s"}\\n\' \\\n'
        '        "$1" "${2:-null}" "$(date -Is)" > "$MARKER"\n'
        '    logger -t cluster-sync "fileset degraded: $1"\n'
        "}\n"
        "\n"
        "# 1. The pull writes this when a blob failed to authenticate. Checked BEFORE\n"
        '#    the "missing" case below: fileset_pull.py creates the staging directory\n'
        "#    before it authenticates anything, so a first-ever pull whose manifest\n"
        "#    fails authentication trips both checks. The specific reason has to win --\n"
        "#    it is what gets read at 3am -- so verify_failed goes first. Either way, a\n"
        "#    corrupt go-bag is worse than an old one: install nothing, keep what is here.\n"
        'if [[ -f "$STAGED/verify_failed" ]]; then\n'
        "    mark verify_failed\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "# 2. Nothing usable staged. Promote anyway (D4), but say so loudly: this node\n"
        "#    is about to come up with whatever .storage it already had, which after F3\n"
        "#    is a cleared-down instance nobody can log into.\n"
        'if [[ ! -d "$STAGED/storage" ]] || [[ ! -f "$STAGED/status.json" ]]; then\n'
        "    mark no_staged_fileset\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        'AGE=$(( $(date +%s) - $(date -r "$STAGED/status.json" +%s) ))\n'
        "\n"
        "# Ownership captured for every path the loops below install, BEFORE any of\n"
        "# it is touched, and restored after. Verified directly rather than assumed:\n"
        "# `cp -a` stamps the SOURCE's owner:group onto the DESTINATION even when the\n"
        "# destination already exists, so the YAML files copied individually below\n"
        "# are exactly as exposed as the directories swapped wholesale. The staged\n"
        "# tree is root-owned throughout -- the pull that built it runs as root\n"
        "# inside the borrowed Home Assistant image -- and CONF_HA_UID documents a\n"
        "# non-root Home Assistant as a supported deployment, with 1000 the wizard's\n"
        "# own default. On that default, before this fix, every fileset promotion\n"
        "# left the ENTIRE custom_components tree unreadable by that uid -- including\n"
        "# this integration itself, so the failover integration stopped loading on\n"
        "# the node it had just promoted. Partially true of .storage alone before\n"
        "# this wave's 0700 staging directories; total, and reaching\n"
        "# custom_components too.\n"
        "#\n"
        "# Reading each path's ACTUAL existing ownership, rather than assuming root\n"
        "# or CONF_HA_UID, is what makes this correct for whichever one a deployment\n"
        "# actually chose. A path with nothing to read -- never installed on this\n"
        "# node before -- inherits $CONFIG's own ownership instead: the config\n"
        "# directory already belongs to whoever is meant to own everything under it,\n"
        "# which beats staying root-owned by construction on a first promotion.\n"
        'CONFIG_OWNER="$(stat -c "%u:%g" "$CONFIG" 2>/dev/null || true)"\n'
        "declare -A OWNER\n"
        "for name in .storage custom_components www blueprints \\\n"
        "            configuration.yaml automations.yaml scripts.yaml scenes.yaml \\\n"
        "            secrets.yaml customize.yaml groups.yaml; do\n"
        '    if [[ -e "$CONFIG/$name" ]]; then\n'
        '        OWNER["$name"]="$(stat -c "%u:%g" "$CONFIG/$name" 2>/dev/null \\\n'
        '            || echo "$CONFIG_OWNER")"\n'
        "    else\n"
        '        OWNER["$name"]="$CONFIG_OWNER"\n'
        "    fi\n"
        "done\n"
        "\n"
        "# 3. Capture this node's OWN cluster_state_sync config entry, BEFORE the swap\n"
        "#    below replaces the file it lives in. `.storage/core.config_entries` holds\n"
        "#    both the mobile_app registrations -- which must be inherited, or the\n"
        "#    companion app stops working, which is what this whole feature is for --\n"
        "#    and this integration's own entry, which must not be. That entry carries\n"
        "#    node_id, and the lease that prevents two leaders renews on *identity*\n"
        "#    (`current == ARGV[1]`), so a node promoted holding its peer's node_id\n"
        "#    renews the peer's lease and both nodes then believe they lead. It also\n"
        "#    carries peer_host -- inherited, a promoted node's peer becomes itself.\n"
        "#    Capturing after the swap would capture the leader's copy: the bug.\n"
        'if ! IDENTITY="$(mktemp "${TMPDIR:-/tmp}/cluster-sync-identity.XXXXXX")"; then\n'
        "    mark identity_restore_failed\n"
        "    exit 0\n"
        "fi\n"
        "# Removed on every exit path: it holds cluster_secret and redis_password in\n"
        "# the clear, in a world-writable directory, on a host that runs other things.\n"
        "trap 'rm -f \"$IDENTITY\"' EXIT\n"
        'IDENTITY_TOOL="$(dirname "$0")/cluster-fileset-identity.py"\n'
        'if ! python3 "$IDENTITY_TOOL" capture \\\n'
        '        --storage "$CONFIG/.storage" --identity "$IDENTITY"; then\n'
        "    # No capture means nothing to put back, so installing the go-bag would\n"
        "    # install the peer's identity. Install nothing: this node promotes on its\n"
        "    # own .storage -- logged out, but identity-safe.\n"
        "    mark identity_restore_failed\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "# 4. Copy, then two renames. The copy is ~28 MB on local disk and nothing is\n"
        "#    reading .storage right now — the container is stopped. The renames are\n"
        "#    what make the visible switch atomic, so a crash mid-swap leaves either the\n"
        "#    old tree or the new one, never a half-written one.\n"
        "swap_dir() {\n"
        '    local src="$1" dst="$2"\n'
        '    [[ -d "$src" ]] || return 0\n'
        '    rm -rf "$dst.new" "$dst.previous"\n'
        '    cp -a "$src" "$dst.new" || return 1\n'
        '    [[ -d "$dst" ]] && mv "$dst" "$dst.previous"\n'
        '    mv "$dst.new" "$dst"\n'
        "}\n"
        "\n"
        'if ! swap_dir "$STAGED/storage/.storage" "$CONFIG/.storage"; then\n'
        "    mark swap_failed\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "# 5. Put this node's own config entry back, into the file the swap just\n"
        "#    installed. Before the bulk copies below, not after: those can fail and\n"
        "#    exit, and an exit taken before the restore would leave the peer's node_id\n"
        "#    installed -- the very thing this step exists to prevent. It is also\n"
        "#    before the device pre-flight in notify_master.sh, so both edits land on\n"
        "#    the file Home Assistant will actually read.\n"
        'python3 "$IDENTITY_TOOL" restore --storage "$CONFIG/.storage" --identity "$IDENTITY"\n'
        "IDENTITY_RC=$?\n"
        "if (( IDENTITY_RC == 2 )); then\n"
        "    # This node has no cluster_state_sync entry of its own, so the peer's were\n"
        "    # removed rather than inherited: an unconfigured node must not acquire an\n"
        "    # identity. Its own reason, not identity_restore_failed: the go-bag WAS\n"
        "    # installed here and this node will simply never sync until someone\n"
        "    # configures it, where identity_restore_failed means the go-bag was not\n"
        "    # installed and the node is running its own previous config. Opposite\n"
        "    # things for the operator to do. The rest of the swap goes ahead; FAILED\n"
        "    # only stops the clean-swap report below from clearing the marker.\n"
        "    mark no_local_identity\n"
        "    FAILED=1\n"
        "elif (( IDENTITY_RC != 0 )); then\n"
        "    # Roll back rather than promote with two possible leaders. D4 promotes\n"
        "    # through a stale or missing go-bag because that costs logins; this costs\n"
        "    # two writers, each pruning the other's blobs, which is corrupting rather\n"
        "    # than degrading. The node promotes on its own .storage instead.\n"
        '    rm -rf "$CONFIG/.storage.rejected"\n'
        '    mv "$CONFIG/.storage" "$CONFIG/.storage.rejected"\n'
        '    if [[ -d "$CONFIG/.storage.previous" ]]; then\n'
        '        mv "$CONFIG/.storage.previous" "$CONFIG/.storage"\n'
        "    else\n"
        "        # Nothing to roll back to. Coming up with no .storage at all means an\n"
        "        # onboarding screen, which is recoverable; coming up as the peer is\n"
        "        # not. Unreachable in practice -- a capture that found entries proves\n"
        "        # .storage existed, so swap_dir made a .previous from it.\n"
        "        logger -t cluster-sync 'no .storage.previous to roll back to'\n"
        "    fi\n"
        "    mark identity_restore_failed\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "# Restore the ownership captured above -- the same OWNER capture the rest\n"
        "# of this tree uses below. Both `cp -a` just above and the identity\n"
        "# restore's own atomic replace (a new inode for core.config_entries) leave\n"
        "# root's ownership on .storage even when nothing else went wrong --\n"
        "# silently, because both report success.\n"
        'if [[ -n "${OWNER[.storage]}" ]] \\\n'
        '        && ! chown -R "${OWNER[.storage]}" "$CONFIG/.storage"; then\n'
        "    mark ownership_restore_failed\n"
        "    FAILED=1\n"
        "fi\n"
        "\n"
        "# Every entry is attempted rather than bailing on the first failure, so one bad\n"
        "# directory does not cost the others a chance to swap in -- but FAILED is what\n"
        "# stops the success path below from running once anything here has gone wrong.\n"
        "# Ownership is restored per entry, same as .storage above and for the same\n"
        "# reason: `cp -a` stamps the staged tree's (root) owner onto each one.\n"
        "for d in custom_components www blueprints; do\n"
        '    if ! swap_dir "$STAGED/storage/$d" "$CONFIG/$d"; then\n'
        "        mark swap_failed\n"
        "        FAILED=1\n"
        '    elif [[ -e "$CONFIG/$d" ]] && [[ -n "${OWNER[$d]}" ]] \\\n'
        '            && ! chown -R "${OWNER[$d]}" "$CONFIG/$d"; then\n'
        "        mark ownership_restore_failed\n"
        "        FAILED=1\n"
        "    fi\n"
        "done\n"
        "for f in configuration.yaml automations.yaml scripts.yaml scenes.yaml \\\n"
        "         secrets.yaml customize.yaml groups.yaml; do\n"
        '    if [[ -f "$STAGED/storage/$f" ]]; then\n'
        '        if ! cp -a "$STAGED/storage/$f" "$CONFIG/$f"; then\n'
        "            mark swap_failed\n"
        "            FAILED=1\n"
        '        elif [[ -n "${OWNER[$f]}" ]] \\\n'
        '                && ! chown -R "${OWNER[$f]}" "$CONFIG/$f"; then\n'
        "            mark ownership_restore_failed\n"
        "            FAILED=1\n"
        "        fi\n"
        "    fi\n"
        "done\n"
        "\n"
        "# A partial install must never look like a clean one. Without this, a single\n"
        "# failed directory or file above still fell through to the success path, which\n"
        "# cleared the marker -- destroying the only evidence anything went wrong -- and\n"
        "# logged a promotion that did not fully happen. `.storage` above already returns\n"
        "# early on its own failure; this is the same guarantee for the loops, which do\n"
        "# not get to return early because they still have other entries to try.\n"
        "if (( FAILED )); then\n"
        "    exit 0\n"
        "fi\n"
        "\n"
        "# 6. Stale still beats nothing, so the swap above happened either way. The\n"
        "#    only question left is whether to raise the alarm.\n"
        "if (( AGE > STALE_AFTER )); then\n"
        '    mark stale "$AGE"\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        'rm -f "$MARKER"\n'
        'logger -t cluster-sync "fileset swapped in (age ${AGE}s)"\n'
        "exit 0\n"
    )


def _fileset_key(cfg: dict[str, Any]) -> str:
    """The derived fileset key, hex-encoded, and nothing else.

    `fileset_pull.py` reads this file with
    `bytes.fromhex(path.read_text().strip())` -- a header or comment here
    would break that parse. What the file is *for* belongs in INSTALL.md
    instead, never inline in the file a machine has to parse.

    This stores the *derived* key (crypto.derive_fileset_key), not the raw
    cluster secret. HKDF derivation is one-way, so the secret itself does not
    become recoverable from this file -- the two uses (this, and ADR-002's
    HMAC) fail independently, which is the whole reason crypto.py derives
    rather than reuses.
    """
    secret = cfg.get(CONF_CLUSTER_SECRET, "")
    return derive_fileset_key(secret).hex() + "\n"


def _fileset_valkey_password(cfg: dict[str, Any]) -> str:
    """The Valkey password, as one shell assignment and nothing else.

    `cluster-fileset-pull.sh` `.`-sources this under `set -euo pipefail` and
    exports the variable, so the file has to be sourceable and must contain
    nothing that could execute. `shlex.quote` is not decoration: a password
    containing `$`, a quote or a backtick, interpolated raw, would be expanded
    or *executed* by the shell that sources it -- as root, from a systemd
    timer. Quoting turns it back into a literal.

    Why a file at all, rather than a `--password` flag on the pull: `ps` shows
    every process's argv to every user on the host. See
    VALKEY_PASSWORD_FILENAME.
    """
    return f"{VALKEY_PASSWORD_ENV}={shlex.quote(_valkey_password(cfg))}\n"


# -- The promoter: Keepalived's replacement ---------------------------------


def _promoter_program() -> str:
    """The host-side promoter, read from the package rather than re-typed.

    Same reasoning as `_fileset_pull_program()`: a hand-copied string here
    would drift from the tested module, and a drifted copy fails silently
    until the one moment it is actually exercised -- deciding which node
    leads.
    """
    return (pathlib.Path(__file__).parent / "scripts" / "cluster_promoter.py").read_text(
        encoding="utf-8"
    )


def _lease_module() -> str:
    """`lease.py`, shipped beside `cluster_promoter.py` in the bundle.

    `cluster_promoter.py` imports it with the same two-arm fallback
    `fileset_pull.py` uses for `crypto.py`: `from ..lease import ...` inside
    the package, `from lease import ...` as a standalone script -- which only
    works because Python puts a script's own directory on `sys.path[0]`, and
    that requires `lease.py` to actually be sitting next to it on the host.
    """
    return (pathlib.Path(__file__).parent / "lease.py").read_text(encoding="utf-8")


def _promoter_ha_url(cfg: dict[str, Any]) -> str:
    """Design D3's probe target.

    Shared between `_promoter_sh` (which emits it as `--ha-url`) and
    `_install_readme` (which documents it), so the generated script and its
    own documentation cannot silently disagree about what this deployment
    actually probes. Falls back to loopback -- matching
    `cluster_promoter.DEFAULT_HA_URL` -- which covers `network_mode: host`
    (READINESS I11), where Home Assistant answers on the host's own
    loopback interface rather than a container address.
    """
    ha_ip = str(cfg.get(CONF_HA_CONTAINER_IP) or "")
    return f"http://{ha_ip}:8123/" if ha_ip else "http://127.0.0.1:8123/"


#: Mirrors `cluster_promoter.DEFAULT_PROBE_GRACE_SECONDS`. Repeated rather
#: than imported for the same reason `_promoter_ha_url`'s loopback fallback
#: is: `cluster_promoter.py` is shipped as raw text (`_promoter_program()`
#: reads the file, never imports the module), not a live dependency of
#: `bundle.py`. Emitted explicitly as `--probe-grace` so the wrapper script
#: does not silently rely on the promoter's own default matching this one --
#: the same reason `--ttl` is always emitted as `LEASE_TTL_SECONDS` rather
#: than left to `cluster_promoter.DEFAULT_TTL_SECONDS`.
PROMOTER_PROBE_GRACE_SECONDS = 300


def _promoter_sh(cfg: dict[str, Any]) -> str:
    """The wrapper systemd invokes every 10 seconds: the promoter itself.

    Runs the host's own `python3` directly -- the same way `ha-device-
    preflight.py` already does in `_notify_master`, and UNLIKE the fileset
    pull's borrowed-container `docker run`. That distinction is load-bearing:
    `cluster_promoter.py` execs `notify_master.sh` / `notify_backup.sh` on a
    transition, and those scripts run `docker start`/`docker stop` and write
    `/run/cluster-sync/vrrp-state` on the HOST. Neither is reachable from
    inside a throwaway container the way `fileset_pull.py`'s target (writing
    into a bind-mounted `/config`) is.

    No VRRP-state gate, deliberately -- unlike every other artefact in this
    bundle. This script is what DECIDES the role, so it has to run
    identically on both nodes, every tick: a gate that only ran it on a
    follower could never demote a live leader, and one that only ran it on a
    leader could never promote a standby.
    """
    redis_host = cfg.get(CONF_REDIS_HOST, "valkey.lan")
    redis_port = cfg.get(CONF_REDIS_PORT, DEFAULT_REDIS_PORT)
    namespace = cfg.get(CONF_CLUSTER_NAMESPACE, "default")
    db = cfg.get(CONF_REDIS_DB, DEFAULT_REDIS_DB)
    username = str(cfg.get(CONF_REDIS_USERNAME) or "")
    use_tls = bool(cfg.get(CONF_REDIS_USE_TLS))
    ca_certs = str(cfg.get(CONF_REDIS_TLS_CA_CERTS) or "") if use_tls else ""
    # Never defaults to a placeholder like "node": a fallback that is merely
    # non-empty (`_keepalived()`'s old `cfg.get(CONF_NODE_ID, "node")`, say)
    # would let two mis-wizarded nodes both present "node" -- the identity
    # collision this guard exists to catch, just moved one step sideways.
    node_id = str(cfg.get(CONF_NODE_ID) or "")
    ha_url = _promoter_ha_url(cfg)

    # host, username, the CA path and the probe URL all reach this shell
    # verbatim -- unlike `namespace` (validated by `validate_namespace` at
    # config-flow time), nothing constrains these to be shell-safe. An
    # unquoted CA path containing a space (`/etc/my ca.pem`) would split
    # into two argv tokens, argparse would exit 2 on the stray one, and
    # there would be no leader election with a journal line as the only
    # symptom.
    username_flag = f"    --username {shlex.quote(username)} \\\n" if username else ""
    tls_flags = "    --tls \\\n" if use_tls else ""
    tls_flags += f"    --tls-ca-file {shlex.quote(ca_certs)} \\\n" if ca_certs else ""

    return (
        f"{SHEBANG}{STRICT}"
        "# Cluster promoter -- takes or renews the cluster lease every tick, and\n"
        "# on a change of leadership, execs notify_master.sh / notify_backup.sh\n"
        "# itself. This IS the leader election: Keepalived is not installed on\n"
        "# either node, and never will be (see scripts/cluster_promoter.py for\n"
        "# the full reasoning). Install and enable this on BOTH hosts.\n"
        "\n"
        f"NODE_ID={shlex.quote(node_id)}\n"
        'if [[ -z "${NODE_ID:-}" ]]; then\n'
        "    logger -t cluster-sync 'promoter has no node id — refusing to touch the lease'\n"
        "    exit 1\n"
        "fi\n"
        "\n"
        "# The Valkey password, best-effort. Unlike the fileset pull, this never\n"
        "# refuses to run over a missing password file: a Valkey with no\n"
        "# `requirepass` and no ACL user needs none, and the promoter must still\n"
        "# take the lease in that deployment. `ps` shows every process's argv to\n"
        "# every user on this host, so the value must never reach a command\n"
        "# line -- see VALKEY_PASSWORD_FILENAME in bundle.py.\n"
        "#\n"
        "# Overridable via CLUSTER_SYNC_PASSWORD_FILE for the same reason\n"
        "# CLUSTER_SYNC_STATE_FILE below is: so a test can exercise this branch\n"
        "# for real. Nothing sets it outside a test harness.\n"
        'PASSWORD_FILE="${CLUSTER_SYNC_PASSWORD_FILE:-'
        f'{INSTALL_DIR}/{VALKEY_PASSWORD_FILENAME}}}"\n'
        'if [[ -f "$PASSWORD_FILE" ]]; then\n'
        "    # shellcheck source=/dev/null\n"
        '    . "$PASSWORD_FILE"\n'
        f"    export {VALKEY_PASSWORD_ENV}\n"
        "fi\n"
        "\n"
        "# Overridable via CLUSTER_SYNC_STATE_FILE and CLUSTER_SYNC_FORCE_FILE for\n"
        "# the same reason the notify scripts' state file is: so a test can point\n"
        "# these at throwaway files and exercise this for real.\n"
        f'STATE_FILE="${{CLUSTER_SYNC_STATE_FILE:-{VRRP_STATE_PATH}}}"\n'
        # /run/cluster-sync, not /etc/cluster-sync (design D2): /run is
        # tmpfs, so a forgotten override self-clears on reboot rather than
        # surviving indefinitely, and this name is deliberately absent from
        # MANAGED_FILENAMES -- regenerating or reinstalling this bundle must
        # never silently remove an operator's own emergency override. The
        # directory already exists at boot: it is the same one
        # cluster-sync-tmpfiles.conf creates for vrrp-state, above.
        f'FORCE_FILE="${{CLUSTER_SYNC_FORCE_FILE:-{VRRP_STATE_DIR}/force-master}}"\n'
        "\n"
        "# Escape hatch for design D3 (see INSTALL.md): set CLUSTER_SYNC_NO_PROBE=1\n"
        "# if this host cannot reach Home Assistant over HTTP, to skip the liveness\n"
        "# probe and renew unconditionally while holding the lease -- the behaviour\n"
        "# every version of this script had before D3. This disables the protection\n"
        "# D3 exists for: a wedged-but-running Home Assistant would then keep the\n"
        "# lease indefinitely, the same defect D3 was written to close.\n"
        'NO_PROBE_FLAG=""\n'
        'if [[ -n "${CLUSTER_SYNC_NO_PROBE:-}" ]]; then\n'
        '    NO_PROBE_FLAG="--no-probe"\n'
        "fi\n"
        "\n"
        "# Overridable via CLUSTER_SYNC_HA_URL, the same pattern as STATE_FILE and\n"
        "# FORCE_FILE above -- an operator whose Home Assistant answers somewhere\n"
        "# other than this generated default (a reverse proxy in front of it, say)\n"
        "# needs a way to point the D3 probe there without hand-editing this file on\n"
        "# every regeneration. Assigned in two steps, not embedded in the\n"
        "# ${VAR:-default} form STATE_FILE uses: that default is always this\n"
        "# bundle's own hardcoded constant, but ha_url can come from operator input\n"
        "# (ha_container_ip) that is not shell-safe, and shlex.quote's single-quote\n"
        "# style is only safe as a standalone assignment, not nested inside the\n"
        "# double-quoted ${...} form.\n"
        'HA_URL="${CLUSTER_SYNC_HA_URL:-}"\n'
        'if [[ -z "$HA_URL" ]]; then\n'
        f"    HA_URL={shlex.quote(ha_url)}\n"
        "fi\n"
        "\n"
        f'python3 "$(dirname "$0")/cluster_promoter.py" \\\n'
        f"    --redis {shlex.quote(redis_host)}:{redis_port} \\\n"
        f"    --namespace {namespace} \\\n"
        '    --node-id "$NODE_ID" \\\n'
        f"    --db {db} \\\n"
        f"{username_flag}"
        f"{tls_flags}"
        f"    --ttl {LEASE_TTL_SECONDS} \\\n"
        '    --state-file "$STATE_FILE" \\\n'
        f"    --install-dir {INSTALL_DIR} \\\n"
        '    --force-file "$FORCE_FILE" \\\n'
        '    --ha-url "$HA_URL" \\\n'
        f"    --probe-grace {PROMOTER_PROBE_GRACE_SECONDS} \\\n"
        "    $NO_PROBE_FLAG\n"
    )


def _promoter_service() -> str:
    return (
        "[Unit]\n"
        "Description=Cluster State Sync — take or renew the cluster lease, and "
        "promote or demote on a change of it\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/etc/cluster-sync/cluster-promoter.sh\n"
        "\n"
        "# A oneshot's own TimeoutStartSec defaults to infinity. Left unset, a tick\n"
        "# that hangs (a stuck notify script, a Valkey call that never returns) never\n"
        "# exits; systemd will not start the NEXT tick of a still-running oneshot; the\n"
        "# lease laps at its TTL while this node sits mid-promotion and can never\n"
        "# demote -- two masters, indefinitely. This is the backstop, not the primary\n"
        "# defence: the promoter bounds itself internally to well under this (a 5s\n"
        "# Valkey connect/socket timeout, a 120s notify-script timeout --\n"
        "# cluster_promoter.py's CONNECT_TIMEOUT_SECONDS and NOTIFY_TIMEOUT_SECONDS).\n"
        "# 150s is comfortably longer than both of those combined, so this only fires\n"
        "# if something outside the promoter's own control -- systemd, the kernel --\n"
        "# is also stuck.\n"
        "TimeoutStartSec=150s\n"
    )


def _promoter_timer() -> str:
    return (
        "[Unit]\n"
        "Description=Run the cluster promoter — this is the leader election; "
        "Keepalived is not installed\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=30s\n"
        "OnUnitActiveSec=10s\n"
        "AccuracySec=1s\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


# -- Warm model: firewall ---------------------------------------------------


def _firewall(cfg: dict[str, Any]) -> dict[str, str]:
    mode = cfg.get(CONF_DOCKER_NETWORK, DOCKER_ALL)
    out: dict[str, str] = {}
    if mode in (DOCKER_HOST, DOCKER_ALL):
        out["leader-host.nft"] = _host_nft(cfg, leader=True)
        out["follower-host.nft"] = _host_nft(cfg, leader=False)
    if mode in (DOCKER_MACVLAN, DOCKER_ALL):
        out["leader-macvlan.nft"] = _macvlan_nft(cfg, leader=True)
        out["follower-macvlan.nft"] = _macvlan_nft(cfg, leader=False)
    if mode in (DOCKER_BRIDGE, DOCKER_ALL):
        out["leader-bridge.sh"] = _bridge_sh(cfg, leader=True)
        out["follower-bridge.sh"] = _bridge_sh(cfg, leader=False)
    return out


def _host_nft(cfg: dict[str, Any], *, leader: bool) -> str:
    uid = cfg.get(CONF_HA_UID, 1000)
    subnets = _subnets(cfg)
    role = "LEADER" if leader else "FOLLOWER"
    body = [
        UNVALIDATED,
        f"# {role} ruleset — Docker network_mode: host.\n"
        "#\n"
        "# In host mode Home Assistant shares the host's network stack, so it\n"
        "# cannot be matched by address. The uid it runs as is the only handle,\n"
        "# which is why this ruleset is uid-scoped and will silently match\n"
        "# nothing if the uid is wrong. Verify with:\n"
        f"#     docker exec {cfg.get(CONF_HA_CONTAINER, 'homeassistant')} id -u\n"
        "\n"
        "# Replace this table rather than merging into it.\n"
        "#\n"
        "# `nft -f` MERGES into an existing table. Without these two lines,\n"
        "# loading the leader ruleset over a live follower ruleset leaves every\n"
        "# follower drop rule in place, and the promoted node stays firewalled\n"
        "# off from the IoT network it was just promoted to talk to. It loads\n"
        "# cleanly and reports success, which is what makes it dangerous.\n"
        "#\n"
        "# The bare declaration first makes the delete safe on a host where the\n"
        "# table does not exist yet; `nft -f` applies the whole file atomically,\n"
        "# so there is no window with no rules loaded.\n"
        "table inet cluster_sync\n"
        "delete table inet cluster_sync\n"
        "\n"
        "table inet cluster_sync {\n"
        "    chain output {\n"
        "        type filter hook output priority 0; policy accept;\n",
    ]
    if leader:
        body.append(
            "        # Leader: no restrictions. Rules are listed for symmetry so\n"
            "        # that swapping rulesets is a single atomic `nft -f`.\n"
        )
    else:
        if subnets:
            body.append(f"        meta skuid {uid} ip daddr {_nft_set(subnets)} counter drop\n")
        if cfg.get(CONF_BLOCK_DISCOVERY, True):
            body.append(
                "        # mDNS and SSDP. This is the HomeKit/Matter guard: two\n"
                "        # nodes announcing on one fabric is an identity collision,\n"
                "        # not merely duplicate traffic.\n"
                f"        meta skuid {uid} udp dport {{ 5353, 1900 }} counter drop\n"
            )
    body.append("    }\n}\n")
    return "".join(body)


def _macvlan_nft(cfg: dict[str, Any], *, leader: bool) -> str:
    ip = cfg.get(CONF_HA_CONTAINER_IP, "192.0.2.10")
    subnets = _subnets(cfg)
    role = "LEADER" if leader else "FOLLOWER"
    body = [
        UNVALIDATED,
        f"# {role} ruleset — Docker macvlan.\n"
        "#\n"
        "# ADR-001 flags this case specifically: macvlan traffic can leave the\n"
        "# host without traversing the `filter` hook at all. A ruleset written\n"
        "# against `hook output` would load without error and drop nothing --\n"
        "# the worst available outcome, because it looks like it worked.\n"
        "#\n"
        "# So this uses the netdev/egress hook, which sits below that path.\n"
        "# Set the device below to the macvlan parent interface, and verify the\n"
        "# counters actually move:\n"
        "#     nft list table netdev cluster_sync\n"
        "\n"
        "# Replace rather than merge — see the host-mode ruleset for why a bare\n"
        "# `table ... { }` would leave the previous role's drop rules live.\n"
        "table netdev cluster_sync\n"
        "delete table netdev cluster_sync\n"
        "\n"
        "table netdev cluster_sync {\n"
        "    chain egress {\n"
        '        type filter hook egress device "eth0" priority 0; policy accept;\n',
    ]
    if leader:
        body.append("        # Leader: unrestricted.\n")
    else:
        if subnets:
            body.append(f"        ip saddr {ip} ip daddr {_nft_set(subnets)} counter drop\n")
        if cfg.get(CONF_BLOCK_DISCOVERY, True):
            body.append(f"        ip saddr {ip} udp dport {{ 5353, 1900 }} counter drop\n")
    body.append("    }\n}\n")
    return "".join(body)


def _bridge_sh(cfg: dict[str, Any], *, leader: bool) -> str:
    ip = cfg.get(CONF_HA_CONTAINER_IP, "172.17.0.2")
    subnets = _subnets(cfg)
    role = "LEADER" if leader else "FOLLOWER"
    lines = [
        SHEBANG,
        STRICT,
        UNVALIDATED.replace("# ", "# ").replace("nft -c -f <this-file>", "bash -n <this-file>"),
        f"# {role} rules — Docker bridge networking.\n"
        "#\n"
        "# Uses DOCKER-USER, the one chain Docker will not rewrite underneath\n"
        "# you. Rules in FORWARD or DOCKER are clobbered whenever Docker\n"
        "# reconfigures, which is a silent failure some hours after it worked.\n"
        "#\n"
        "# Bridge mode breaks mDNS discovery outright, so HomeKit needs an mDNS\n"
        "# reflector on the host regardless of the ruleset below.\n"
        "\n"
        # Only the follower ruleset references HA_IP. Emitting it on the
        # leader left a variable assigned and never used — shellcheck flags
        # it, and a reader has to stop and work out whether a rule is
        # missing.
        + (f"HA_IP={ip}\n\n" if not leader else "")
        + "# Idempotent: clear our own rules before re-adding.\n"
        "iptables -S DOCKER-USER | grep -- '--comment cluster-sync' | \\\n"
        "    sed 's/^-A/-D/' | while read -r rule; do\n"
        "        # shellcheck disable=SC2086\n"
        "        iptables $rule || true\n"
        "    done\n"
        "\n",
    ]
    if leader:
        lines.append("# Leader: no restrictions re-added.\n")
    else:
        for subnet in subnets:
            lines.append(
                f'iptables -I DOCKER-USER -s "$HA_IP" -d {subnet} '
                "-m comment --comment cluster-sync -j DROP\n"
            )
        if cfg.get(CONF_BLOCK_DISCOVERY, True):
            for port in (5353, 1900):
                lines.append(
                    f'iptables -I DOCKER-USER -s "$HA_IP" -p udp --dport {port} '
                    "-m comment --comment cluster-sync -j DROP\n"
                )
    return "".join(lines)


def _safety_revert() -> str:
    return (
        f"{SHEBANG}{STRICT}"
        "# Ruleset safety net.\n"
        "#\n"
        "# Loading a bad ruleset on a remote host can cut off your own SSH\n"
        "# session, and these hosts run other services. This snapshots the\n"
        "# current ruleset and schedules a restore, which you cancel only once\n"
        "# you have confirmed you are still alive on the other side.\n"
        "#\n"
        "#   ./nft-safety-revert.sh arm 120     # snapshot + revert in 120s\n"
        "#   nft -f follower-host.nft\n"
        "#   ./nft-safety-revert.sh cancel      # ONLY if you still have a shell\n"
        "\n"
        "SNAPSHOT=/run/cluster-sync/nft-snapshot.rules\n"
        "MARKER=/run/cluster-sync/revert.pid\n"
        "\n"
        "mkdir -p /run/cluster-sync\n"
        "\n"
        'case "${1:-}" in\n'
        "  arm)\n"
        '    DELAY="${2:-120}"\n'
        '    nft list ruleset > "$SNAPSHOT"\n'
        '    ( sleep "$DELAY"\n'
        "      logger -t cluster-sync 'Safety revert firing — restoring ruleset'\n"
        "      nft flush ruleset\n"
        '      nft -f "$SNAPSHOT"\n'
        "    ) &\n"
        '    echo $! > "$MARKER"\n'
        '    echo "Armed: ruleset reverts in ${DELAY}s unless cancelled."\n'
        "    ;;\n"
        "  cancel)\n"
        '    if [[ -f "$MARKER" ]]; then\n'
        '        kill "$(cat "$MARKER")" 2>/dev/null || true\n'
        '        rm -f "$MARKER"\n'
        "        echo 'Cancelled. New ruleset is now permanent.'\n"
        "    else\n"
        "        echo 'Nothing armed.'\n"
        "    fi\n"
        "    ;;\n"
        "  *)\n"
        "    echo 'usage: nft-safety-revert.sh {arm [seconds]|cancel}' >&2\n"
        "    exit 2\n"
        "    ;;\n"
        "esac\n"
    )


def _install_readme(cfg: dict[str, Any], warm: bool) -> str:
    model = "warm" if warm else "cold"
    namespace = cfg.get(CONF_CLUSTER_NAMESPACE, "default")
    node = cfg.get(CONF_NODE_ID, "node")
    ha_url = _promoter_ha_url(cfg)
    lines = [
        f"# Cluster State Sync — host bundle ({model} standby)\n\n",
        f"Generated for node `{node}`, cluster namespace `{namespace}`.\n\n",
        "> **Generated blind.** None of this has been run against a real host.\n"
        "> The firewall rules in particular were written from your wizard answers\n"
        "> without access to the machine, and the correct filter point depends on\n"
        "> how Home Assistant is networked. Read them before loading them.\n\n",
        "## Install\n\n",
        "On **both** hosts:\n\n",
        "```bash\n"
        "sudo mkdir -p /etc/cluster-sync\n"
        "sudo cp * /etc/cluster-sync/\n"
        "sudo chmod +x /etc/cluster-sync/*.sh\n"
        "```\n\n",
        "Keepalived is not installed on either node, and this bundle no longer\n"
        "generates a config for it — the cluster promoter below is what decides\n"
        'leadership and calls the notify scripts now (see "Cluster promoter" if\n'
        "you have fileset replication enabled). If an earlier version of this\n"
        "bundle installed `keepalived-cluster.conf` here, remove it and make\n"
        "sure nothing still runs the daemon it was written for:\n\n"
        "```bash\n"
        "sudo rm -f /etc/cluster-sync/keepalived-cluster.conf\n"
        "sudo systemctl disable --now keepalived 2>/dev/null || true\n"
        "```\n\n",
        "Install the VRRP state default, on **both** hosts:\n\n",
        "```bash\n"
        "sudo cp cluster-sync-tmpfiles.conf /etc/tmpfiles.d/cluster-sync.conf\n"
        "sudo systemd-tmpfiles --create /etc/tmpfiles.d/cluster-sync.conf\n"
        "```\n\n",
        f"This is not optional. `{VRRP_STATE_PATH}` is how every other artefact\n"
        "here knows which node it is on: the notify scripts write it on each VRRP\n"
        "transition, and — if you enable fileset replication below — the pull\n"
        "timer reads it. `/run` is a tmpfs, so between a reboot and the first\n"
        "transition the file would not exist — and the pull fails closed on that,\n"
        "which means a rebooted follower would never pull. The default is\n"
        "`BACKUP`, the safe direction; the notify scripts overwrite it within\n"
        "moments of whatever calls them (the cluster promoter, below) deciding\n"
        "otherwise.\n\n",
        "Check it after the first failover — if this file is wrong, everything\n"
        "downstream of it is quietly wrong too:\n\n",
        f"```bash\ncat {VRRP_STATE_PATH}\n```\n\n",
    ]
    lines += [
        "### `cluster-config-sync` is no longer generated\n\n",
        "ADR-001 tier 1, the `.storage`-and-registry rsync, is gone from this\n"
        "bundle in every configuration now — not only alongside fileset\n"
        "replication. It never ran on any real install: nothing wrote the VRRP\n"
        "state file it gated on until this integration's notify scripts were\n"
        "fixed to write it, and the moment they were, the rsync would have come\n"
        "alive for the first time straight into a two-leader bug — the follower's\n"
        "`core.config_entries` would become the leader's copy, so the promotion\n"
        "swap would capture the *leader's* `cluster_state_sync` entry as this\n"
        "node's own and graft it back, and the promoted node would come up\n"
        "holding the peer's `node_id`, renew the peer's lease, and both nodes\n"
        "would believe they lead. There is no installed base this removal\n"
        "breaks.\n\n",
        "If an earlier bundle installed that timer, turn it off on **both**\nhosts:\n\n",
        "```bash\n"
        "sudo systemctl disable --now cluster-config-sync.timer\n"
        "sudo rm -f /etc/systemd/system/cluster-config-sync.{service,timer}\n"
        "```\n\n",
    ]
    if cfg.get(CONF_FILESET_ENABLED):
        lines += [
            "Fileset replication (below) covers the same allow-list the rsync did\n"
            "— `.storage`, `custom_components`, `www`, `blueprints` and the\n"
            "top-level YAML — properly: encrypted, identity-preserving, and with no\n"
            "SSH trust between hosts. Nothing is lost by the rsync's removal.\n\n",
        ]
    else:
        lines += [
            "**This deployment has fileset replication off, so nothing in this\n"
            "bundle replicates `.storage` at all.** A promoted standby comes up on\n"
            "whatever `.storage` it already had — every session logged out, no\n"
            "`mobile_app` registration carried over, no integration credential\n"
            "inherited from the peer. Set `fileset_enabled` in the integration and\n"
            "regenerate this bundle if a promotion needs to carry that across;\n"
            "there is no rsync fallback to fall back to any more.\n\n",
            "**The cluster promoter (below) is also gated on that same setting, so\n"
            "this bundle emits nothing that calls the notify scripts above — the\n"
            "integration's own in-HA leadership signal still runs regardless.**\n"
            "What this deployment loses is host-side promotion while Home Assistant\n"
            "is stopped: with no go-bag there is nothing for a cold standby to\n"
            "promote *to*, so the coupling is deliberate, not an oversight. Enable\n"
            "`fileset_enabled` and regenerate this bundle to get\n"
            "`cluster-promoter.sh` once a promotion has a go-bag to promote onto.\n\n",
        ]
    if cfg.get(CONF_FILESET_ENABLED):
        lines += [
            "## Fileset replication (enabled)\n\n",
            "Three of the files in this bundle are the pull **program**, not shell:\n"
            f"`fileset_pull.py`, `crypto.py` and `resp.py`. The blanket `cp` above\n"
            f"puts all three in `{INSTALL_DIR}/`, which is where\n"
            "`cluster-fileset-pull.sh` expects them — it mounts them into the Home\n"
            f"Assistant image and runs `{PULL_MOUNTPOINT}/fileset_pull.py` from\n"
            "there. Check all three arrived:\n\n",
            f"```bash\nls -l {INSTALL_DIR}/fileset_pull.py {INSTALL_DIR}/crypto.py "
            f"{INSTALL_DIR}/resp.py\n```\n\n",
            "They must stay together and keep their names. `fileset_pull.py` imports\n"
            "`crypto` and `resp` from its own directory, so a pull missing either\n"
            "one fails on every run with an `ImportError` — once a minute, into the\n"
            "journal, on the node standing by to take over.\n\n",
            "`cluster-fileset.key` is the derived fileset key. It decrypts every\n"
            "credential this integration mirrors from `.storage`, so it is written\n"
            "mode `0600` in this bundle — confirm that survived the blanket `cp`\n"
            "above before relying on it, then enable the pull timer on the\n"
            "**FOLLOWER only.** It is not a guaranteed no-op on the leader: the\n"
            "tmpfiles default above makes a rebooting node read `BACKUP` until the\n"
            "cluster promoter's first tick, and this timer's own `OnBootSec=1min`\n"
            "can fire inside that window. The promoter's own `OnBootSec=30s` means\n"
            'it usually wins that race, but "usually" is not a guarantee — which\n'
            "is exactly why there is no reason to also run this timer on the node\n"
            "you intend as leader:\n\n",
            "```bash\n"
            "sudo chmod 600 /etc/cluster-sync/cluster-fileset.key\n"
            "sudo cp cluster-fileset-pull.{service,timer} /etc/systemd/system/\n"
            "sudo systemctl enable --now cluster-fileset-pull.timer\n"
            "```\n\n",
            "## Cluster promoter — leadership election\n\n",
            "Keepalived is not installed on either node, and never will be: this\n"
            "integration replaces it with `cluster-promoter.sh`, which takes or\n"
            "renews the cluster lease every 10 seconds and execs `notify_master.sh`\n"
            "/ `notify_backup.sh` itself, only on a change of leadership.\n\n",
            "`cluster_promoter.py` and `lease.py` are, like the pull's own\n"
            "siblings above, **program** files rather than shell, and must sit\n"
            "**beside** `cluster-promoter.sh` in the same directory the blanket\n"
            "`cp` above already put them in. `cluster_promoter.py` imports `lease`\n"
            "and `resp` from its own directory the same way `fileset_pull.py`\n"
            "imports `crypto` and `resp` — **never `fileset_pull.py` itself**,\n"
            "deliberately: that module also imports `crypto.py`, which needs the\n"
            "third-party `cryptography` package, and the promoter needs none of\n"
            "it. Installing any one of `cluster_promoter.py`, `lease.py` or\n"
            "`resp.py` without the other two fails on every tick with an\n"
            "`ImportError` — on the node standing by to take over.\n\n",
            "Install and enable it on **both** hosts, unlike the pull timer above:\n"
            "the promoter is what decides which node leads, so a node it never ran\n"
            "on could never be promoted, and one it never ran on could never be\n"
            "demoted.\n\n",
            "```bash\n"
            "sudo cp cluster-promoter.{service,timer} /etc/systemd/system/\n"
            "sudo systemctl enable --now cluster-promoter.timer\n"
            "```\n\n",
            "It shares this deployment's Valkey connection settings (and the TLS\n"
            "CA-path caveat below, if you use one) with the pull, and reads the\n"
            "same password file. It refuses to run rather than touch the lease\n"
            "under an empty node id — check `journalctl -u cluster-promoter.service`\n"
            "if a promotion never happens.\n\n",
            "**The leader only renews the lease while Home Assistant answers HTTP\n"
            '(design D3).** Before this, the lease meant "this host is powered on" —\n'
            "a dead Home Assistant on a live host kept renewing forever, and a\n"
            "wedged, OOM-killed or otherwise unresponsive Home Assistant never\n"
            f"failed over. Every tick this node believes it is MASTER, it probes\n"
            f"`{ha_url}` (`GET /api/`, no credentials — a 401 is proof of life) before\n"
            "renewing; a silent probe releases the lease immediately, rather than\n"
            "merely letting it lapse, so the standby can promote on its very next\n"
            "tick instead of waiting out the remaining TTL. A freshly-promoted node\n"
            "is exempt for the first few minutes (`--probe-grace`, default 300s):\n"
            "`docker start` returns long before Home Assistant serves HTTP, and\n"
            "without this a cold boot would fail its own probe, release the lease\n"
            "it just took, and flap. **The probe only ever gates renewal, never\n"
            "taking** — a standby's Home Assistant is deliberately stopped, and\n"
            "taking the lease is what starts it.\n\n",
            "**A release is followed by a hold-down (design M3), not an immediate\n"
            "re-take.** Releasing writes BACKUP; without a hold-down this node's own\n"
            "very next tick would see the lease free, take it straight back, promote,\n"
            "earn a fresh boot grace, fail the probe again once that expires, and\n"
            "release again — a roughly five-minute cycle for as long as the peer\n"
            "stays down, and worse than a plain restart loop because **each\n"
            "promotion also re-runs the fileset swap**. `--release-holddown`\n"
            "(default 900s) refuses to TAKE a free lease for that long after this\n"
            "node's own release — never a renewal, and never a lease that is free\n"
            "for any other reason (first boot, the peer actually crashed), so this\n"
            "is not a general brake on promotion.\n\n",
            "If this host cannot reach Home Assistant over HTTP at all — a firewall\n"
            "between the promoter and the container, say — set\n"
            "`CLUSTER_SYNC_NO_PROBE=1` in the promoter's environment (e.g. in\n"
            "`/etc/cluster-sync/cluster-promoter.sh` itself, or a systemd drop-in)\n"
            "to renew unconditionally, the behaviour every version of this bundle\n"
            "had before D3. **This disables the protection D3 exists for** — a\n"
            "wedged-but-running Home Assistant would once again keep the lease\n"
            "indefinitely, so use it only when the probe itself cannot work, not\n"
            "as a general troubleshooting toggle. If instead Home Assistant simply\n"
            "answers somewhere other than the address above — behind a reverse\n"
            "proxy, say — set `CLUSTER_SYNC_HA_URL` instead, the same way\n"
            "`CLUSTER_SYNC_STATE_FILE` and `CLUSTER_SYNC_FORCE_FILE` override their\n"
            "own defaults; this keeps the probe itself, and its protection, intact.\n\n",
            "**Known failure mode: an unwritable `/run` causes a 10-second restart\n"
            "loop on the leader**, not merely a noisy log. `notify_master.sh`'s own\n"
            f"write of `{VRRP_STATE_PATH}` is deliberately non-fatal — if `/run` is\n"
            "full or remounted read-only, that write silently fails and the script\n"
            "still exits 0. The promoter then reads the same stale `previous` state\n"
            "on the next tick, decides MASTER again, and re-execs `notify_master.sh`\n"
            "again — stopping, swapping and starting Home Assistant every ten\n"
            "seconds, indefinitely, on the node that already IS the leader. Loud\n"
            "(the journal fills with promotions that never stop happening) but not\n"
            "self-healing: there is no remedy but fixing `/run` itself.\n\n",
            "### `force-master` — the manual override (design D2)\n\n",
            "Every promotion above goes through the lease: this node only becomes\n"
            "MASTER when Valkey confirms it holds the lease, and refuses otherwise.\n"
            "That fail-closed default is correct on its own — but fail-closed with\n"
            "Valkey unreachable **and** the peer genuinely dead leaves no path to\n"
            "recovery at all, which is worse than the split-brain the lease exists\n"
            "to prevent. `force-master` is that path, and it exists **only** for\n"
            "that situation — use it only once you have confirmed the peer is\n"
            "genuinely dead:\n\n",
            f"```bash\nsudo touch {VRRP_STATE_DIR}/force-master\n```\n\n",
            "On the next tick this node promotes to MASTER unconditionally — it does\n"
            "not consult the lease at all — and best-effort claims the lease under\n"
            "its own identity, so a healthy or later-recovered peer's own renewal is\n"
            "refused and it demotes, converging on one master. **If the peer is\n"
            "alive, or comes back while this file is still present, both nodes end\n"
            "up believing they lead** — the exact two-leader state the lease exists\n"
            "to prevent, and the one this override deliberately bypasses.\n\n",
            "**Remove it as soon as the incident is over:**\n\n",
            f"```bash\nsudo rm {VRRP_STATE_DIR}/force-master\n```\n\n",
            "Left in place, this node claims the lease under force every ten\n"
            "seconds, forever — indistinguishable from a healthy leader from the\n"
            "outside, and permanently refusing the peer's legitimate renewal. It\n"
            f"defaults to `{VRRP_STATE_DIR}/`, on tmpfs, deliberately: a forgotten\n"
            "override then self-clears on the node's next reboot rather than\n"
            "surviving indefinitely, and the filename is deliberately absent from\n"
            "this bundle's managed files, so regenerating or reinstalling it can\n"
            f"never silently remove an operator's own override. A marker at\n"
            f"`{VRRP_STATE_DIR}/force-master.used` records who first used it and\n"
            "when, so a stale override is something a routine check can catch\n"
            "rather than only the next incident.\n\n",
        ]
    if cfg.get(CONF_FILESET_ENABLED) and _valkey_password(cfg):
        lines += [
            f"`{VALKEY_PASSWORD_FILENAME}` is your Valkey password, and it is written\n"
            "mode `0600` for the same reason the key file is — confirm that survived\n"
            "the blanket `cp` above:\n\n",
            f"```bash\nsudo chmod 600 /etc/cluster-sync/{VALKEY_PASSWORD_FILENAME}\n```\n\n",
            "It is a file rather than a `--password` flag on the pull because `ps`\n"
            "shows every process's command line to every user on the host, and the\n"
            "pull runs from a timer on a machine that runs other things.\n"
            f"`cluster-fileset-pull.sh` sources it, exports `{VALKEY_PASSWORD_ENV}`,\n"
            "and hands it to `docker run` by name — so the password reaches the\n"
            "container without ever appearing on a command line. If you rotate the\n"
            "Valkey password, change it here **and** in the integration, or the\n"
            "follower stops pulling while the leader carries on publishing: a\n"
            "go-bag that silently stops ageing forward, which the swap only reports\n"
            "as staleness at the moment you promote.\n\n",
        ]
    if cfg.get(CONF_FILESET_ENABLED) and cfg.get(CONF_REDIS_USE_TLS):
        ca_certs = str(cfg.get(CONF_REDIS_TLS_CA_CERTS) or "")
        lines += [
            "The pull connects over TLS with verification on, and there is no\n"
            "insecure toggle: with TLS enabled but verification off, anyone on the\n"
            "path presents any certificate and the connection is encrypted to\n"
            "*them*.\n\n",
            (
                f"**Check this path.** `{ca_certs}` is the CA path you gave the\n"
                "integration, which is a path **inside the Home Assistant\n"
                f"container**. `cluster-fileset-pull.sh` mounts it from the *host* at\n"
                f'the same path (`-v "{ca_certs}:/ca:ro"`), and this bundle had no\n'
                "way to check whether the file is really there. If it is not, put a\n"
                "copy of the CA there or edit that one line — `docker run` would\n"
                "otherwise create a directory at that path and the pull would fail\n"
                "every minute with an unhelpful TLS error.\n\n"
            )
            if ca_certs
            else (
                "No CA file was configured, so the pull verifies against the\n"
                "container's system trust store. A step-ca-issued certificate is not\n"
                "in it — set `redis_tls_ca_certs` in the integration and regenerate\n"
                "this bundle if the pull reports a certificate-verification failure.\n\n"
            ),
        ]
    if cfg.get(CONF_FILESET_ENABLED):
        lines += [
            "`cluster-fileset-swap.sh` installs the staged copy at promotion, before\n"
            "Home Assistant starts. `notify_master.sh`, as generated by this bundle,\n"
            "already calls it automatically, ahead of the device pre-flight, on both\n"
            "the cold and warm scripts — **do not add that call yourself.** `swap_dir`\n"
            "keeps exactly one generation of rollback, in `.storage.previous`; running\n"
            "the swap a second time (this bundle's own call, plus one added by hand)\n"
            "overwrites that rollback with the copy the first run just installed,\n"
            "destroying the node's original `.storage` at exactly the moment someone\n"
            "might need to restore it.\n\n",
            "`cluster-fileset-identity.py` must sit **beside** the swap script — the\n"
            'swap finds it with `$(dirname "$0")` and runs it twice, once before the\n'
            "swap and once after. It preserves this node's own `cluster_state_sync`\n"
            "config entry across the swap while everything else in\n"
            "`.storage/core.config_entries` (your `mobile_app` registrations included)\n"
            "is inherited from the peer. Without it the promoted node comes up holding\n"
            "the peer's `node_id`, renews the peer's lease, and both nodes believe they\n"
            "are leader. Do not call it yourself, and do not move it.\n\n",
            "One consequence worth knowing before you need it: a cluster-wide setting\n"
            "you change on the leader does **not** reach the standby's config entry\n"
            "through the go-bag. Change it on both nodes, or reconfigure the standby\n"
            "after a promotion. The whole entry is preserved rather than a list of\n"
            "per-node fields, so a setting added to this integration later cannot\n"
            "silently start crossing between nodes.\n\n",
            "If the restore fails, the swap rolls `.storage` back (the rejected copy is\n"
            "left in `.storage.rejected` for diagnosis) and writes the degraded marker\n"
            "with reason `identity_restore_failed`. The node still promotes, on its own\n"
            "config: logged out, but never a second leader. If instead this node had no\n"
            "`cluster_state_sync` entry at all, the go-bag *is* installed but the peer's\n"
            "entry is deleted rather than inherited, and the reason is\n"
            "`no_local_identity` — the node is up and logged in, and will not sync until\n"
            "you configure the integration on it. The two want opposite responses, which\n"
            "is why they are two reasons.\n\n",
        ]
    if warm:
        lines += [
            "## Firewall (warm only)\n\n",
            "Pick the variant matching your Docker network mode. If you are not\n"
            "sure, all three were generated — check with:\n\n",
            "```bash\n"
            "docker inspect -f '{{.HostConfig.NetworkMode}}' "
            f"{cfg.get(CONF_HA_CONTAINER, 'homeassistant')}\n"
            "```\n\n",
            "Load them **with the safety net**, never by hand:\n\n",
            "```bash\n"
            "./nft-safety-revert.sh arm 120\n"
            "nft -f follower-host.nft      # or the macvlan/bridge variant\n"
            "# confirm you still have a shell, THEN:\n"
            "./nft-safety-revert.sh cancel\n"
            "```\n\n",
            "Verify the counters actually move. A ruleset that loads cleanly and\n"
            "matches nothing is the failure mode to watch for — it looks like it\n"
            "worked:\n\n",
            "```bash\nnft list table inet cluster_sync\n```\n\n",
        ]
    else:
        lines += [
            "## No firewall\n\n",
            "The cold model does not need one. The standby's Home Assistant is not\n"
            "running, so there are no side effects to suppress. That is the whole\n"
            "reason ADR-001 recommends it as the default.\n\n",
            "## Measure your RTO\n\n",
            "Cold-boot time is the acceptance test that decides whether cold is\n"
            "viable for you, against the 2.5-minute failover budget:\n\n",
            "```bash\n"
            f"time (docker start {cfg.get(CONF_HA_CONTAINER, 'homeassistant')} && \\\n"
            "  until curl -sf localhost:8123 >/dev/null; do sleep 1; done)\n"
            "```\n\n",
            "If that exceeds your budget, switch the wizard to the warm model.\n\n",
        ]
    lines += [
        "## Still yours to do\n\n",
        "- Provision the dedicated Valkey ACL user (see the integration README).\n",
        "- Point both nodes' recorder at shared Postgres — ADR-001 tier 3.\n",
        "- Confirm HomeKit/Matter load on one node only. This is an identity\n"
        "  problem, not just a traffic one; blocking mDNS is necessary but not\n"
        "  sufficient if both nodes have the integration configured.\n",
    ]
    return "".join(lines)


# Every filename this module can ever emit, plus four it deliberately never
# does any more. Used to prune stale artifacts on regeneration *without*
# touching anything the operator put in the directory themselves —
# regenerating a bundle is not licence to delete someone's notes.
#
# `cluster-config-sync.{sh,service,timer}` (ADR-001 tier 1) stay listed even
# though `build_bundle` never writes them: an install from before this change
# — or from any bundle, fileset on or off, since the rsync used to be
# generated in both cases — can still have them on disk, and dropping the
# names here would strand that copy instead of pruning it, exactly what this
# set exists to prevent (ADR-005).
#
# `keepalived-cluster.conf` stays listed for the same reason, as of the
# promoter's introduction: `build_bundle` no longer emits it (the promoter
# replaces Keepalived, and shipping both invites installing them side by side
# -- two leadership mechanisms racing, the exact outcome this design argues
# against), but an install with an older bundle on disk still has the file,
# and dropping the name here would strand it instead of pruning it.
MANAGED_FILENAMES: frozenset[str] = frozenset(
    {
        "ha-device-preflight.py",
        "notify_master.sh",
        "notify_backup.sh",
        "notify_fault.sh",
        "keepalived-cluster.conf",
        "cluster-config-sync.sh",
        "cluster-config-sync.service",
        "cluster-config-sync.timer",
        "cluster-sync-tmpfiles.conf",
        "nft-safety-revert.sh",
        "apply-leader.sh",
        "apply-follower.sh",
        "leader-host.nft",
        "follower-host.nft",
        "leader-macvlan.nft",
        "follower-macvlan.nft",
        "leader-bridge.sh",
        "follower-bridge.sh",
        "fileset_pull.py",
        "crypto.py",
        "resp.py",
        "cluster-fileset-pull.sh",
        "cluster-fileset-pull.service",
        "cluster-fileset-pull.timer",
        "cluster-fileset-swap.sh",
        "cluster-fileset-identity.py",
        "cluster-fileset.key",
        VALKEY_PASSWORD_FILENAME,
        "cluster_promoter.py",
        "lease.py",
        "cluster-promoter.sh",
        "cluster-promoter.service",
        "cluster-promoter.timer",
        "INSTALL.md",
    }
)


def write_bundle(directory: str, cfg: dict[str, Any]) -> list[str]:
    """Render the bundle and write it to `directory`. Returns the filenames.

    Blocking filesystem work — call it from an executor, never on the event
    loop.

    Scripts are written executable because Keepalived `exec`s them directly. A
    non-executable notify script fails at the exact moment it is needed, during
    a promotion, and the operator finds out from a failover that did not happen.
    """
    import os
    import stat

    target = pathlib.Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for name, body in sorted(build_bundle(cfg).items()):
        path = target / name
        path.write_text(body, encoding="utf-8")
        if name.endswith(".sh"):
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if name in SECRET_FILENAMES:
            path.chmod(0o600)
        written.append(name)

    # Prune artifacts from a previous, different configuration. Switching warm
    # -> cold otherwise leaves follower-host.nft on disk while INSTALL.md says
    # the cold model needs no firewall, and a stale ruleset that still loads is
    # exactly the sort of thing that gets loaded by mistake.
    for stale in MANAGED_FILENAMES - set(written):
        (target / stale).unlink(missing_ok=True)

    # The bundle carries credentials in exactly two artefacts, both written
    # 0600 above: cluster-fileset.key, because the host-side pull cannot
    # decrypt without it, and cluster-fileset-valkey.env, because it cannot
    # authenticate without that. Everything else describes topology only.
    # Keep the directory owner-only regardless.
    os.chmod(target, 0o750)
    return written
