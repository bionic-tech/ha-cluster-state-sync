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

from dataclasses import dataclass
import pathlib
import shlex
from typing import Any

from .const import (
    BUNDLE_DIR_NAME,
    CONF_BLOCK_DISCOVERY,
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_COMPOSE_ENV_FILE,
    CONF_COMPOSE_FILE,
    CONF_COMPOSE_PROFILE,
    CONF_COMPOSE_SERVICE,
    CONF_DOCKER_NETWORK,
    CONF_FILESET_ENABLED,
    CONF_FILESET_EXCLUSIONS,
    CONF_FILESET_EXTRA_CUSTOM,
    CONF_FILESET_EXTRA_PATHS,
    CONF_FILESET_STALE_AFTER,
    CONF_HA_CONFIG_PATH,
    CONF_HA_CONTAINER,
    CONF_HA_CONTAINER_IP,
    CONF_HA_START_MODE,
    CONF_HA_UID,
    CONF_IOT_SUBNETS,
    CONF_LEADERSHIP_ENTITY,
    CONF_NODE_ID,
    CONF_PEER_HOST,
    CONF_RADIO_WATCH,
    CONF_REDIS_DB,
    CONF_REDIS_HOST,
    CONF_REDIS_PASSWORD,
    CONF_REDIS_PORT,
    CONF_REDIS_SENTINEL_HOSTS,
    CONF_REDIS_TLS_CA_CERTS,
    CONF_REDIS_USE_TLS,
    CONF_REDIS_USERNAME,
    CONF_SETTLE_DELAY,
    CONF_STATISTICS_ENABLED,
    CONF_STATISTICS_INTERVAL_MINUTES,
    CONF_TOPOLOGY_MODEL,
    DEFAULT_FILESET_STALE_AFTER,
    DEFAULT_HA_CONFIG_PATH,
    DEFAULT_REDIS_DB,
    DEFAULT_REDIS_PORT,
    DEFAULT_SETTLE_DELAY,
    DEFAULT_STATISTICS_ENABLED,
    DEFAULT_STATISTICS_INTERVAL_MINUTES,
    DEGRADED_MARKER_NAME,
    DOCKER_ALL,
    DOCKER_BRIDGE,
    DOCKER_HOST,
    DOCKER_MACVLAN,
    HA_START_COMPOSE,
    LEASE_TTL_SECONDS,
    MAX_STATISTICS_INTERVAL_MINUTES,
    MIN_STATISTICS_INTERVAL_MINUTES,
    STAGED_DIR_NAME,
    STATISTICS_DB_NAME,
    TOPOLOGY_WARM,
)
from .crypto import derive_fileset_key
from .handover import HANDOVER_FILENAME
from .hold import HOLD_FILENAME

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

#: What Home Assistant calls its own config directory, from inside the
#: container. Every path the wizard collects is expressed against this, because
#: it is the only filesystem the integration can check an answer against.
CONTAINER_CONFIG_DIR = "/config"


def _host_path(cfg: dict[str, Any], container_path: str) -> str:
    """Translate a path Home Assistant sees into the one the host sees.

    The TLS CA the operator enters is a *container* path -- `/config/ca.crt` is
    what the integration opens and what its own file picker shows -- and the
    integration is right to store it that way, because `backend.py` runs inside
    the container and needs exactly that.

    Two generated artefacts do not. `cluster-promoter.sh` runs `python3` on the
    **host**, and `cluster-fileset-pull.sh` bind-mounts the CA with
    `docker run -v`, whose source path is also resolved on the host. Neither
    host has a `/config` at all.

    Left untranslated both fail, and both fail quietly in this project's
    signature way -- a timer that runs, fails, and logs:

    * The promoter's `load_verify_locations` raises `FileNotFoundError`, which
      `run()` catches as an `OSError`, so every tick exits 1 and the lease is
      never taken. Failover never happens.
    * Docker, handed a `-v` source that does not exist, **creates an empty
      directory** there and mounts it, so the pull verifies against a directory
      instead of a certificate.

    Anything not under the container config directory is returned unchanged:
    it is already a host path, or a system trust-store path like
    `/etc/ssl/certs/ca-certificates.crt` that means the same thing on both
    sides. So is anything at all when `ha_config_path` is unset, since there is
    then nothing to translate against and a wrong guess is worse than the
    original.
    """
    host_root = str(cfg.get(CONF_HA_CONFIG_PATH) or "").rstrip("/")
    prefix = CONTAINER_CONFIG_DIR + "/"
    if not host_root or not container_path.startswith(prefix):
        return container_path
    return f"{host_root}/{container_path[len(prefix) :]}"


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


# --- AR-0043: nothing operator-supplied reaches root-run shell unchecked ---
#
# Every artefact this module emits is executed by systemd **as root, every ten
# seconds, on both hosts**. Config values are interpolated into that shell, and
# until 2026-09-08 none of them was quoted or validated. Verified by execution,
# not by reading:
#
#     CONTAINER="homeassistant"; touch /tmp/PWNED; #"    -> ran
#     --redis valkey.lan$(touch /tmp/PWNED):6379         -> ran
#
# The boundary that crosses is **Home Assistant admin -> root on the host**, in
# a project whose whole premise is that the host does not need touching. There
# is a human `sudo cp` between the wizard and `/etc/cluster-sync/`, which is why
# this was P1 and not P0 -- but it is a blind `cp *` nobody inspects.
#
# 🚨 **A chokepoint, not a hunt.** The obvious fix is to wrap each of the ~40
# interpolation sites in `shlex.quote`, and the obvious fix is wrong: the next
# person to add a site will forget, and the failure is silent and root. So
# every value is validated **once, here, before any artefact is built**, and a
# value that could change the meaning of a shell word never reaches a template
# at all. Quoting at the sites is defence in depth on top of this, not instead.

#: Characters that can end a word, start a command, or expand to one. A value
#: containing any of these is rejected outright rather than escaped: none of
#: the fields below has a legitimate use for them, so an appearance is either a
#: mistake or an attack, and both deserve to stop the build.
_SHELL_METACHARACTERS = frozenset("\"'`$\\;&|<>()[]{}!*?~ \n\r\t")

#: 🚨 AR-0056. The first version of this guard validated a hand-written list of
#: thirteen fields, and its own docstring said the reason a chokepoint beats a
#: hunt is that "the next person to add a site will forget". Curating the FIELD
#: list is that same mistake one level up, and it was made immediately: a
#: review five days later found `redis_port`, `redis_db`, `fileset_stale_after`,
#: `iot_subnets` and `ha_uid` all reaching root-run shell unchecked, because
#: they look like numbers and nobody had typed them into the list.
#:
#:     --redis v:6379; touch /tmp/PWNED     <- generated, and it executed
#:
#: So the rule is inverted. **Everything in the config is validated unless it
#: is named here, with a reason.** A field added tomorrow is checked by
#: default; forgetting to think about it now fails closed instead of open.
SHELL_SAFE_EXCEPTIONS: dict[str, str] = {
    CONF_REDIS_PASSWORD: (
        "never interpolated into a script. Its single use is "
        "`_fileset_valkey_password`, which wraps it in shlex.quote for a file "
        "the pull sources as root -- verified by executing a payload against "
        "the generated env file. Passwords legitimately contain $ and quotes, "
        "so validating this one would reject valid configurations."
    ),
    CONF_CLUSTER_SECRET: (
        "never reaches shell at all. It derives the fileset key, and only the "
        "hex of that key is written. Asserted by a test that builds with a "
        "marker secret and greps every emitted artefact for it."
    ),
    CONF_FILESET_EXCLUSIONS: "glob patterns (`*.bak-*`); never read by bundle.py",
    CONF_FILESET_EXTRA_PATHS: "operator path list; never read by bundle.py",
    CONF_FILESET_EXTRA_CUSTOM: "operator path list; never read by bundle.py",
    CONF_LEADERSHIP_ENTITY: "an entity_id used inside Home Assistant; never read by bundle.py",
    CONF_REDIS_SENTINEL_HOSTS: "consumed by the integration's client, never by a script",
    CONF_RADIO_WATCH: "entity globs; never read by bundle.py",
}


#: Fields whose shape is known, and therefore checkable more strictly than
#: "contains no metacharacter". Each is an ALLOWLIST of permitted characters,
#: which is tighter than the generic rule rather than an exemption from it.
#:
#: `iot_subnets` needs this because it is a comma-separated list and a space
#: after the comma is ordinary -- rejecting the space would refuse a valid
#: firewall configuration, and exempting the field would leave nftables input
#: unchecked. Neither is acceptable, so its actual shape is validated.
_SUBNET_CHARS = frozenset("0123456789abcdefABCDEF.:/")


def _check_subnets(field: str, value: str) -> None:
    """Every comma-separated element must look like an address or a CIDR."""
    for element in (e.strip() for e in value.split(",")):
        if not element:
            continue
        if not set(element) <= _SUBNET_CHARS:
            raise UnsafeBundleValue(
                f"{field} element {element!r} is not an address or CIDR range. This "
                "value is written into nftables rules loaded as root, so only "
                "address characters are accepted."
            )


SHELL_SAFE_VALIDATORS: dict[str, Any] = {}


class UnsafeBundleValue(ValueError):
    """A config value could change the meaning of the shell it lands in."""


def validate_shell_safe(cfg: dict[str, Any]) -> None:
    """Reject any config value that shell would not treat as a single word.

    Default-deny (AR-0056): **every** value is checked unless its key appears
    in `SHELL_SAFE_EXCEPTIONS`. Booleans and real numbers are skipped because
    they render as literals and cannot carry a metacharacter -- but a *string*
    in a numeric field is still a string, which is exactly how `redis_port`
    became an injection vector.

    Raises `UnsafeBundleValue`. The config flow calls this too, so the operator
    meets a form error rather than a traceback -- but this runs on every build
    regardless of how the config got here, including a hand-edited
    `core.config_entries` replicated from a peer.
    """

    def check(field: str, value: Any) -> None:
        if value is None or isinstance(value, bool | int | float):
            # Rendered as a literal; cannot carry a metacharacter. A string
            # that merely looks numeric is NOT this case and falls through.
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                check(field, item)
            return
        text = str(value)
        bad = sorted(set(text) & _SHELL_METACHARACTERS)
        if bad:
            raise UnsafeBundleValue(
                f"{field} contains {''.join(bad)!r}, which cannot appear in a value "
                "that is written into scripts this host runs as root. Remove it."
            )

    for field, value in cfg.items():
        if field in SHELL_SAFE_EXCEPTIONS:
            continue
        shaped = SHELL_SAFE_VALIDATORS.get(field)
        if shaped is not None:
            if value is not None:
                shaped(field, str(value))
            continue
        check(field, value)


SHELL_SAFE_VALIDATORS[CONF_IOT_SUBNETS] = _check_subnets


def sh(value: Any) -> str:
    """Quote a value for the shell. Belt to `validate_shell_safe`'s braces.

    Normal values -- container names, absolute paths, hostnames -- contain no
    metacharacters, so `shlex.quote` returns them unchanged and the generated
    artefacts are byte-identical to before. It earns its place the day someone
    adds a field to a template and forgets to add it to SHELL_EXPOSED_FIELDS.
    """
    return shlex.quote(str(value))


def build_bundle(cfg: dict[str, Any]) -> dict[str, str]:
    """Render every artifact for this deployment as {filename: content}."""
    # AR-0043. Before anything is rendered: a value that could change the
    # meaning of a shell word must never reach a template, and failing here
    # is how that stays true no matter which site forgets to quote.
    validate_shell_safe(cfg)
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
        # Statistics replication rides on the fileset's opt-in because it
        # reuses its key, its Valkey credentials and its follower gate --
        # but it is separately switchable, because an estate on a shared
        # Postgres recorder needs the go-bag and not this.
        if cfg.get(CONF_STATISTICS_ENABLED, DEFAULT_STATISTICS_ENABLED):
            bundle["statistics_pull.py"] = _statistics_pull_program()
            bundle["statistics_sync.py"] = _statistics_sync_module()
            bundle["cluster-statistics-pull.sh"] = _statistics_pull(cfg)
            bundle["cluster-statistics-pull.service"] = _statistics_pull_service()
            bundle["cluster-statistics-pull.timer"] = _statistics_pull_timer(cfg)
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
    # Emitted in every configuration: the hold is about leadership, which
    # exists whether or not the fileset does.
    bundle["cluster-hold.sh"] = _hold_script(cfg)
    bundle["INSTALL.md"] = _install_readme(cfg, warm)
    bundle["cluster-dashboard.yaml"] = _dashboard_yaml()
    # Last, and computed from the filenames above: it copies what is actually
    # here and enables only the units this configuration produced. Generating
    # it from `cfg` alone would let it drift from the bundle it installs.
    bundle["install.sh"] = _install_script(cfg, sorted(bundle))
    return bundle


#: Seconds `docker stop` waits for Home Assistant to exit before Docker sends
#: SIGKILL. Docker's default is 10, and 10 is not enough: Home Assistant writes
#: `.storage` during shutdown (AR-0034), so a SIGKILLed instance loses whatever
#: the registries held in memory.
#:
#: Measured on node-b, 2026-09-04:
#:
#:     18:45:14  cluster-sync: Demoting to BACKUP - stopping home-assistant-2
#:     18:45:24  dockerd: Container failed to exit within 10s of signal 15
#:               - using the force
#:
#: Exit 137, every demotion, on a node that had hung `speedtest` threads. That
#: was read as "the standby keeps crashing" for most of a day; it was our own
#: demotion killing it, and taking the shutdown write with it.
#:
#: Costs nothing when Home Assistant exits promptly -- `docker stop` returns as
#: soon as the container is down. It only spends the time it needs to, and only
#: on the node that is already handing over, so it never delays the peer's
#: promotion.
DOCKER_STOP_TIMEOUT_SECONDS = 120

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


#: How long a single hardware-custody hook may run before it is killed. Bounded
#: well inside the failover budget: `pre-start.d/` delays the container start
#: directly, so a hung hook is an outage.
HOOK_TIMEOUT_SECONDS = 60


def _hooks(kind: str) -> str:
    """Run every executable in `<kind>.d/`, in sort order, never fatally.

    Hardware custody is not this integration's business. Some installations
    pass USB through directly and need nothing; others front their radios with
    USB-over-IP, a serial concentrator, or a zigbee2mqtt on another host. What
    they share is *when* the hardware has to move, not *how*. So the promoter
    provides the moment and the operator provides the script.

    Nothing is installed here by default: an empty (or absent) directory is the
    common case and costs one `[[ -d ]]` test.

    **`pre-start.d/` runs before the device pre-flight, not merely before
    `docker start`.** Both orderings matter and for different reasons. Before
    `docker start`, because a container's `/dev` is a snapshot taken at start
    and a device attached afterwards never appears inside it (GOTCHAS 17).
    Before the pre-flight, because the pre-flight disables config entries whose
    hardware is absent -- so claiming first is what stops a promoted node
    disabling the very radios it just acquired.

    **A failed hook never blocks promotion.** Same rule as D4's stale go-bag:
    network-attached devices work regardless of what happened to a radio, and
    refusing to promote over an unattached one turns a partial outage into a
    total one. It is logged and the node carries on.
    """
    return (
        f'HOOKS="$(dirname "$0")/{kind}.d"\n'
        f'if [[ -d "$HOOKS" ]]; then\n'
        f'    for hook in "$HOOKS"/*; do\n'
        f'        [[ -f "$hook" && -x "$hook" ]] || continue\n'
        f'        name=$(basename "$hook")\n'
        f'        if timeout {HOOK_TIMEOUT_SECONDS} "$hook"; then\n'
        f'            logger -t cluster-sync "{kind} hook $name ok"\n'
        f"        else\n"
        # `rc=$?` FIRST in this branch, before anything else runs. A
        # `$(basename ...)` here would reset $? to its own success and every
        # failure would be logged as rc 0 -- the message would exist and lie,
        # which is worse than no message.
        f"            rc=$?\n"
        f'            logger -t cluster-sync "{kind} hook $name FAILED'
        f' (rc $rc) -- continuing, this node is DEGRADED"\n'
        f"        fi\n"
        f"    done\n"
        f"fi\n"
        f"\n"
    )


#: How long the promoter waits for Home Assistant to answer before running the
#: `post-start.d/` hooks anyway.
#:
#: Generous, because that is the honest number: 46 seconds measured on the
#: reference leader between our own setup finishing and the instance being
#: ready. A cap well above it, so an ordinary boot is never reported degraded.
HA_READY_TIMEOUT_SECONDS = 180


def _wait_for_ha(cfg: dict[str, Any]) -> str:
    """Poll Home Assistant until it answers, then let the hooks run.

    🚨 AR-0064. `pre-start.d/` is before `docker start` and `post-stop.d/` is
    after the container stops, so this project's own ingress guidance -- claim
    a floating address only **after** Home Assistant answers -- described a
    moment that did not exist. It was found by trying to write the example we
    recommend, not by reasoning about it.

    Running the hooks straight after `docker start` would not have fixed it.
    The container is up in a second and the instance takes the best part of a
    minute; an address claimed in between accepts connections and returns 502,
    which is worse than no address at all because a health check sees a live
    host and stops looking.

    So the wait is the feature. `curl` is used rather than anything installed,
    and any HTTP response at all counts -- including 401, which is what a
    correctly configured instance returns to an unauthenticated request. We are
    asking "is something serving?", not "may I in?".

    `CLUSTER_SYNC_HA_READY_TIMEOUT` overrides the cap, which a drill or a test
    needs and an operator with an unusually slow instance may too.

    **The hooks run either way**, with `CLUSTER_SYNC_HA_READY` set to 1 or 0.
    Refusing to run them on timeout would mean an unreachable node stays
    unreachable, and running them blindly would claim an address for a black
    hole -- so the promoter reports what it observed and the hook decides,
    which is the same division of labour `_hooks` already describes: the
    promoter provides the moment, the operator provides the script.
    """
    # The same URL design D3's probe uses, via the same helper, so the promoter
    # and its readiness wait cannot disagree about where Home Assistant is --
    # including the loopback fallback for `network_mode: host`.
    url = _promoter_ha_url(cfg)
    return (
        f"# AR-0064: give the post-start hooks the moment they were promised --\n"
        f"# Home Assistant ANSWERING, not merely the container existing.\n"
        f"CLUSTER_SYNC_HA_READY=0\n"
        f'for _ in $(seq 1 "${{CLUSTER_SYNC_HA_READY_TIMEOUT:-{HA_READY_TIMEOUT_SECONDS}}}"); do\n'
        f'    if curl -s -o /dev/null -m 2 "{url}" ; then\n'
        f"        CLUSTER_SYNC_HA_READY=1\n"
        f"        break\n"
        f"    fi\n"
        f"    sleep 1\n"
        f"done\n"
        f'if [[ "$CLUSTER_SYNC_HA_READY" == "1" ]]; then\n'
        f"    logger -t cluster-sync 'Home Assistant is answering; running post-start hooks'\n"
        f"else\n"
        f"    logger -t cluster-sync 'Home Assistant did NOT answer within"
        f" {HA_READY_TIMEOUT_SECONDS}s -- running post-start hooks anyway with"
        f" CLUSTER_SYNC_HA_READY=0. A hook that claims an address should check it.'\n"
        f"fi\n"
        f"export CLUSTER_SYNC_HA_READY\n"
        f"\n"
    )


def _compose_prefix(cfg: dict[str, Any]) -> str:
    """`docker compose` with this installation's file, profile and env.

    Env file first, and sourced rather than passed as `--env-file`: compose
    interpolates the WHOLE project file before it filters by profile or
    service, so one unset variable in a service you are not touching aborts the
    command. Measured on this fleet -- `--profile automation` still failed on a
    pgbouncer password three services away. A promoter started by systemd has
    none of the operator's shell environment, so without this compose mode
    fails at precisely the moment it is needed.
    """
    env = cfg.get(CONF_COMPOSE_ENV_FILE)
    file = cfg.get(CONF_COMPOSE_FILE) or ""
    profile = cfg.get(CONF_COMPOSE_PROFILE)
    parts = []
    if env:
        # `set -a` so the sourced values are exported to the compose child.
        parts.append(f"set -a; . {shlex.quote(str(env))}; set +a; ")
    parts.append("docker compose")
    if file:
        parts.append(f" -f {shlex.quote(str(file))}")
    if profile:
        parts.append(f" --profile {shlex.quote(str(profile))}")
    return "".join(parts)


def _start_ha(cfg: dict[str, Any]) -> str:
    """The command that brings Home Assistant up on this node.

    Compose uses `up -d`, not `start`: on a node that has never led, the
    container may not exist at all, and `start` cannot create one. `up -d` also
    applies configuration the operator has since changed -- which is how the
    `/dev/serial` mount a promoted node needs actually arrives.
    """
    container = cfg[CONF_HA_CONTAINER]
    if cfg.get(CONF_HA_START_MODE) != HA_START_COMPOSE:
        return f"docker start {shlex.quote(container)}"
    service = cfg.get(CONF_COMPOSE_SERVICE) or container
    return f"{_compose_prefix(cfg)} up -d --no-deps {shlex.quote(str(service))}"


def _stop_ha(cfg: dict[str, Any]) -> str:
    """The command that takes Home Assistant down on this node.

    Compose uses `stop`, never `down`: `down` removes containers, networks and
    -- depending on flags and someone's muscle memory -- volumes. Demoting a
    node must leave it able to be promoted again in ten seconds, not rebuilt.

    `--no-deps` throughout, because nothing else in an estate should move
    because one Home Assistant changed node.
    """
    container = cfg[CONF_HA_CONTAINER]
    if cfg.get(CONF_HA_START_MODE) != HA_START_COMPOSE:
        return f"docker stop -t {DOCKER_STOP_TIMEOUT_SECONDS} {shlex.quote(container)}"
    service = cfg.get(CONF_COMPOSE_SERVICE) or container
    return (
        f"{_compose_prefix(cfg)} stop -t {DOCKER_STOP_TIMEOUT_SECONDS} {shlex.quote(str(service))}"
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
            "# Hardware custody, BEFORE the pre-flight below -- claiming first\n"
            "# is what stops the pre-flight disabling the radios this node has\n"
            "# just acquired.\n"
            f"{_hooks('pre-start')}"
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
            f"{_start_ha(cfg)}\n"
            "\n"
            f"{_wait_for_ha(cfg)}"
            f"{_hooks('post-start')}"
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
            f"{_stop_ha(cfg)} || \\\n"
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
            f"{_start_ha(cfg)} || \\\n"
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
            f"{_stop_ha(cfg)}\n"
            "\n"
            "# Hardware custody, AFTER the container has stopped: release what\n"
            "# this node holds so the new leader can take it. This is the easy\n"
            "# half. The case failover exists for -- a node that is simply gone\n"
            "# -- runs no hooks at all, and recovery then depends entirely on\n"
            "# the provider reaping a dead client's claim.\n"
            f"{_hooks('post-stop')}"
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


@dataclass(frozen=True)
class _PullContext:
    """The shell fragments both container-borrowing pulls need.

    Two programs run inside the borrowed Home Assistant image on a follower --
    the fileset pull and the statistics pull -- and both need the same Valkey
    address, the same credential handling, the same TLS flags and the same
    refusal to run anywhere but a confirmed follower. Computed once so the two
    cannot drift: a TLS flag that reached one script and not the other would
    show up as a statistics stream that silently stopped, on the node whose
    Home Assistant is off and therefore has nobody watching.
    """

    container: str
    config: str
    redis_host: str
    redis_port: int
    namespace: str
    db: int
    password_block: str
    password_flag: str
    ca_mount: str
    username_flag: str
    tls_flags: str


def _pull_context(cfg: dict[str, Any]) -> _PullContext:
    """Everything `_fileset_pull` and `_statistics_pull` share."""

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
    # A HOST path: this is `docker run -v`, resolved by the daemon on this
    # machine, not inside Home Assistant. See _host_path.
    ca_mount = f'    -v "{_host_path(cfg, ca_certs)}:/ca:ro" \\\n' if ca_certs else ""
    username_flag = f"        --username {username} \\\n" if username else ""
    tls_flags = "        --tls \\\n" if use_tls else ""
    tls_flags += "        --tls-ca-file /ca \\\n" if ca_certs else ""

    return _PullContext(
        container=container,
        config=_config_path(cfg),
        redis_host=redis_host,
        redis_port=redis_port,
        namespace=namespace,
        db=db,
        password_block=password_block,
        password_flag=password_flag,
        ca_mount=ca_mount,
        username_flag=username_flag,
        tls_flags=tls_flags,
    )


def _fileset_pull(cfg: dict[str, Any]) -> str:
    ctx = _pull_context(cfg)
    container = ctx.container
    redis_host, redis_port = ctx.redis_host, ctx.redis_port
    namespace, db = ctx.namespace, ctx.db
    password_block, password_flag = ctx.password_block, ctx.password_flag
    ca_mount, username_flag, tls_flags = ctx.ca_mount, ctx.username_flag, ctx.tls_flags

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
        "\n"
    )


def _statistics_pull_program() -> str:
    """`statistics_pull.py`, shipped as its own file for the same reason
    `fileset_pull.py` is: a hand-copied string here would be a second copy
    nothing tests, and the one that ships is the one that must be right."""
    return (pathlib.Path(__file__).parent / "scripts" / "statistics_pull.py").read_text(
        encoding="utf-8"
    )


def _statistics_sync_module() -> str:
    """`statistics_sync.py`, shipped beside the puller.

    It is stdlib-only by design and stays that way precisely so it can be
    mounted into a throwaway container that has never run `pip`. The puller
    imports it with the same two-arm fallback everything else in the bundle
    uses, which resolves only because Python puts a script's own directory on
    `sys.path[0]` -- so omitting this mount fails the pull on every run with an
    ImportError, on the node standing by to take over.
    """
    return (pathlib.Path(__file__).parent / "statistics_sync.py").read_text(encoding="utf-8")


def _statistics_pull(cfg: dict[str, Any]) -> str:
    """The follower's statistics apply. Its own script, its own timer.

    Deliberately NOT appended to `cluster-fileset-pull.sh`, for two reasons.
    The fileset pull runs once a minute because a go-bag that is a minute stale
    is a promotion that loses a minute; the statistics window is republished
    every half hour and applying it means an `INSERT OR IGNORE` of ~200,000
    rows, which on the flash storage `storage.py` exists to worry about is
    thirty times the write wear for nothing. And the two must fail
    independently: a fileset pull that cannot reach Valkey exits non-zero, and
    sharing a script would silently stop history replication as a side effect
    of an unrelated fault.
    """
    ctx = _pull_context(cfg)
    return (
        f"{SHEBANG}{STRICT}"
        "# Long-term statistics replication — pull. Follower only.\n"
        "#\n"
        "# Runs on the HOST inside the Home Assistant image, exactly like the fileset\n"
        "# pull and for the same reason: in the cold model this node's Home Assistant\n"
        "# is stopped, so there is no integration here to receive anything.\n"
        "#\n"
        "# It writes .cluster_sync_statistics.db, NEVER home-assistant_v2.db. On a warm\n"
        "# standby the real recorder is open by a live Home Assistant, and this program\n"
        "# runs outside it with no way to know. cluster-fileset-swap.sh installs the\n"
        "# accumulated store under the real name at promotion, when the container is\n"
        "# provably stopped.\n"
        "\n"
        'STATE_FILE="${CLUSTER_SYNC_STATE_FILE:-/run/cluster-sync/vrrp-state}"\n'
        f"CONFIG={ctx.config}\n"
        f'CONTAINER="{ctx.container}"\n'
        "\n"
        "# Follower only, failing closed on an unknown state — the same positive\n"
        "# match on BACKUP or FAULT the fileset pull uses. A leader applying its\n"
        "# peer's window would write the peer's history into its own live estate.\n"
        'STATE="$(cat "$STATE_FILE" 2>/dev/null || true)"\n'
        'if [[ "$STATE" != "BACKUP" ]] && [[ "$STATE" != "FAULT" ]]; then\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        f"{ctx.password_block}"
        "IMAGE=$(docker inspect -f '{{.Config.Image}}' \"$CONTAINER\")\n"
        "\n"
        "docker run --rm \\\n"
        "    --entrypoint python3 \\\n"
        '    -v "$CONFIG:/config" \\\n'
        f"    -v {INSTALL_DIR}/cluster-fileset.key:/key:ro \\\n"
        f"    -v {INSTALL_DIR}/statistics_pull.py:{PULL_MOUNTPOINT}/statistics_pull.py:ro \\\n"
        f"    -v {INSTALL_DIR}/statistics_sync.py:{PULL_MOUNTPOINT}/statistics_sync.py:ro \\\n"
        f"    -v {INSTALL_DIR}/crypto.py:{PULL_MOUNTPOINT}/crypto.py:ro \\\n"
        f"    -v {INSTALL_DIR}/resp.py:{PULL_MOUNTPOINT}/resp.py:ro \\\n"
        f"{ctx.ca_mount}"
        f"{ctx.password_flag}"
        "    --network host \\\n"
        '    "$IMAGE" \\\n'
        f"    {PULL_MOUNTPOINT}/statistics_pull.py \\\n"
        f"        --redis {ctx.redis_host}:{ctx.redis_port} \\\n"
        f"        --namespace {ctx.namespace} \\\n"
        "        --key-file /key \\\n"
        "        --config /config \\\n"
        f"        --db {ctx.db} \\\n"
        f"{ctx.username_flag}"
        f"{ctx.tls_flags}"
        "    || { logger -t cluster-sync 'statistics pull failed; history not advanced'; "
        "exit 1; }\n"
        "\n"
    )


def _statistics_pull_service() -> str:
    return (
        "[Unit]\n"
        "Description=Cluster State Sync — apply the leader's long-term statistics\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/etc/cluster-sync/cluster-statistics-pull.sh\n"
    )


def _statistics_pull_timer(cfg: dict[str, Any]) -> str:
    """Matched to the publisher's cadence: a faster timer re-applies a window
    that has not changed, and a slower one throws away history the leader has
    already published and may not still hold when this node next looks."""
    minutes = int(cfg.get(CONF_STATISTICS_INTERVAL_MINUTES) or DEFAULT_STATISTICS_INTERVAL_MINUTES)
    minutes = max(MIN_STATISTICS_INTERVAL_MINUTES, min(MAX_STATISTICS_INTERVAL_MINUTES, minutes))
    return (
        "[Unit]\n"
        "Description=Run the cluster statistics pull, so a follower's history keeps "
        "pace with the leader's\n"
        "\n"
        "[Timer]\n"
        # Not OnBootSec=0: a node that has just booted is competing with Home
        # Assistant's own start-up for the same disk, and half an hour of
        # statistics is not worth winning that race.
        "OnBootSec=5min\n"
        f"OnUnitActiveSec={minutes}min\n"
        "AccuracySec=1min\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
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
        f"CONTAINER={shlex.quote(str(cfg.get(CONF_HA_CONTAINER) or 'homeassistant'))}\n"
        "FAILED=0\n"
        "MARKED=0\n"
        "\n"
        "# The cold model's one unchecked assumption, until it bit: this runs at\n"
        "# promotion, before `docker start`, and every path below rewrites\n"
        "# `.storage` on the premise that nothing is holding it open. A standby\n"
        "# whose Home Assistant is still running -- left up after setup, say, or\n"
        "# started by hand to look at something -- holds those files in memory and\n"
        "# rewrites them on ITS shutdown (AR-0034), so the swap lands and is then\n"
        "# silently undone. The promoted node comes up on its own old identity with\n"
        "# a clean log and a successful promotion behind it.\n"
        "#\n"
        "# Refusing is the safe direction: a promotion that keeps this node's own\n"
        "# `.storage` is a degraded promotion, which D4 already accommodates and\n"
        "# marks. Swapping under a live process is data loss.\n"
        "RUNNING=\"$(docker inspect -f '{{.State.Running}}' "
        '"$CONTAINER" 2>/dev/null || true)"\n'
        'if [ "$RUNNING" = "true" ]; then\n'
        '    logger -t cluster-sync "refusing to swap: $CONTAINER is still running"\n'
        '    mark "container_running"\n'
        "    exit 0\n"
        "fi\n"
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
        "# Every top-level entry the leader actually published, not a fixed\n"
        "# list. The publisher follows `configuration.yaml`'s includes, so what\n"
        "# arrives varies per deployment -- `configs/`, `themes/`, `packages/`,\n"
        "# whatever the operator ticked. A hardcoded list here staged those\n"
        "# files faithfully and then installed none of them: the standby\n"
        "# promoted into recovery mode with the missing file sitting in\n"
        "# .cluster_sync_staged the whole time.\n"
        'for name in $(cd "$STAGED/storage" 2>/dev/null && ls -A); do\n'
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
        "# Directories, again from what was staged. `.storage` is handled\n"
        "# separately above because it carries the identity graft.\n"
        'for d in $(cd "$STAGED/storage" 2>/dev/null && ls -A); do\n'
        '    [[ -d "$STAGED/storage/$d" ]] || continue\n'
        '    [[ "$d" == ".storage" ]] && continue\n'
        '    if ! swap_dir "$STAGED/storage/$d" "$CONFIG/$d"; then\n'
        "        mark swap_failed\n"
        "        FAILED=1\n"
        '    elif [[ -e "$CONFIG/$d" ]] && [[ -n "${OWNER[$d]}" ]] \\\n'
        '            && ! chown -R "${OWNER[$d]}" "$CONFIG/$d"; then\n'
        "        mark ownership_restore_failed\n"
        "        FAILED=1\n"
        "    fi\n"
        "done\n"
        "# Top-level files, from what was staged rather than a fixed list.\n"
        'for f in $(cd "$STAGED/storage" 2>/dev/null && ls -A); do\n'
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
        "# 5b. Recorder history hand-over (ADR-010).\n"
        "#\n"
        "#     Here because Home Assistant is STOPPED at this point, and that is the\n"
        "#     only moment this is possible at all: `recorder.disable` merely makes\n"
        "#     the recorder drop events, it does not close the database, and there is\n"
        "#     no reload service. Swapping it under a running instance cannot be done.\n"
        "#\n"
        "#     Before the staleness check on purpose: a stale go-bag still beats none,\n"
        "#     and so does old history. Never fatal -- a house with no graphs beats no\n"
        "#     house (D4).\n"
        "#\n"
        "#     🚨 It LOGS, it never calls mark(). The degraded marker is first-reason-\n"
        '#     wins and means "your identity may be wrong" -- a signal an operator acts\n'
        "#     on. Letting a missing history file claim it would pre-empt `stale`, which\n"
        "#     is strictly more important, and dilute the one marker that matters at\n"
        "#     3am. Recorder health has its own diagnostic sensor.\n"
        f'STATS_STORE="$CONFIG/{STATISTICS_DB_NAME}"\n'
        f'RECORDER="$CONFIG/{RECORDER_DB_NAME}"\n'
        "#     Two possible sources, in this order:\n"
        "#\n"
        "#       1. The replicated statistics store, kept current by\n"
        "#          cluster-statistics-pull.timer. A full recorder schema holding\n"
        "#          years of long-term statistics and none of the ten-day churn --\n"
        "#          the Energy dashboard and every long climate graph survive, the\n"
        "#          recent logbook does not. This is the one that is actually\n"
        "#          replicated, so it is preferred.\n"
        "#       2. A whole-database copy an operator placed here by hand. Nothing\n"
        "#          ships one automatically: at 1.59 GB it cannot travel through\n"
        "#          Valkey, and node-to-node rsync was removed rather than make this\n"
        "#          the one feature that needs the standby to reach the leader over\n"
        "#          SSH. Honoured if present because someone went to the trouble.\n"
        "#\n"
        "#     MOVE, for the store only: once it is installed as this node's\n"
        "#     recorder, leaving a copy behind would freeze at today's date and be\n"
        "#     re-installed, stale, at some later promotion. Consumed instead, and\n"
        "#     statistics_pull.py rebuilds it from the live recorder when this node\n"
        "#     goes back to standby -- so a failover and a failback need no manual\n"
        "#     re-seed.\n"
        'if [[ -f "$STATS_STORE" ]]; then\n'
        '    SNAPSHOT="$STATS_STORE"\n'
        "    INSTALL=(mv -f)\n"
        "else\n"
        f'    SNAPSHOT="$CONFIG/{RECORDER_SNAPSHOT_NAME}"\n'
        "    # Copy rather than move: an operator's hand-placed database is still\n"
        "    # there for the next attempt if the promotion fails after this point.\n"
        "    INSTALL=(cp -a)\n"
        "fi\n"
        'if [[ -f "$SNAPSHOT" ]]; then\n'
        '    if [[ -f "$RECORDER" && "$RECORDER" -nt "$SNAPSHOT" ]]; then\n'
        "        # The local database is NEWER than the copy we were sent, so\n"
        "        # installing it would discard more history than it restores.\n"
        "        logger -t cluster-sync \\\n"
        "            'recorder: local history newer than the peer snapshot; keeping local'\n"
        "    else\n"
        "        # 🚨 The old -wal and -shm MUST go. SQLite would otherwise try to\n"
        "        #    recover the PREVIOUS database's write-ahead log against the new\n"
        "        #    file, which is a corrupt database rather than a failed swap.\n"
        '        rm -f "$RECORDER-wal" "$RECORDER-shm"\n'
        '        if [[ -f "$RECORDER" ]]; then\n'
        '            mv -f "$RECORDER" "$RECORDER.superseded" || true\n'
        "        fi\n"
        '        if "${INSTALL[@]}" "$SNAPSHOT" "$RECORDER"; then\n'
        '            logger -t cluster-sync "recorder history installed from $SNAPSHOT"\n'
        "        else\n"
        "            logger -t cluster-sync 'recorder: installing peer history FAILED'\n"
        "        fi\n"
        "    fi\n"
        "else\n"
        "    # Nothing ever arrived. Promote regardless; the graphs will have a\n"
        "    # hole, and a house with no graphs still beats no house (D4).\n"
        "    logger -t cluster-sync 'recorder: no replicated history present; graphs will "
        "have a gap'\n"
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
PROMOTER_PROBE_GRACE_SECONDS = 600

#: Mirrors `cluster_promoter.DEFAULT_BASE_GRACE_SECONDS`, repeated here for the
#: same reason as the ceiling above: the generated wrapper states its own
#: timings rather than inheriting whatever default the installed script happens
#: to carry.
#:
#: The base is what a WEDGED Home Assistant costs before its peer may promote.
#: The ceiling above is reached only while the container is demonstrably
#: restarting, and extension stops there regardless -- so a crash loop still
#: demotes (ADR-001's budget, ADR-009's radio latency).
PROMOTER_BASE_GRACE_SECONDS = 120

#: Mirrors `recorder_snapshot.SNAPSHOT_NAME` and Home Assistant's own database
#: name. Repeated rather than imported for the same reason every constant in
#: the generated shell is: it runs on a host with no Python package present.
RECORDER_SNAPSHOT_NAME = ".cluster_sync_recorder.db"
RECORDER_DB_NAME = "home-assistant_v2.db"

# --- Why the recorder snapshot is not shipped between nodes ------------------
#
# (removed 2026-09-08) Shipping it by rsync-over-SSH would have been the FIRST
# direct node-to-node dependency this project has. Everything else reaches
# Valkey and nothing else -- a node has never needed to talk to its peer, which
# is why the failure modes are as simple as they are. It was also not native to
# Home Assistant, which this integration is required to be: rsync-over-SSH
# works on Docker, might work on a Supervised install, and cannot work on Home
# Assistant OS at all.
#
# What replaced it: only long-term `statistics` cross, as ROWS through the same
# Valkey and the same sealed envelope as everything else (ADR-010). That is half
# a megabyte a day rather than 1.59 GB, it needs no new transport, no new port
# and no new trust, and it works identically on every install shape. The price
# is honest and stated: raw `states` -- the recent logbook and history graphs --
# does not cross. Long-term statistics do, and those are the years of energy and
# climate data people actually grieve losing.
#
# `RECORDER_SNAPSHOT_LOCK_NAME` lived here and was deleted 2026-09-09: it was
# the rsync transport's write-lock marker and nothing has referenced it since
# the transport went. The reasoning above is kept because it is the live record
# of a decision this project is asked to revisit roughly once a week.

#: Mirrors `cluster_promoter.DEFAULT_RELEASE_HOLDDOWN_SECONDS`, for the same
#: reason and with the same caveat as the grace above. Used only by
#: `install.sh`, to say how long a failed D3 probe keeps Home Assistant stopped
#: -- which is the number that makes the probe pre-flight worth doing rather
#: than a nicety. A test asserts the two stay in step.
PROMOTER_RELEASE_HOLDDOWN_SECONDS = 900


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
    # The maintenance hold lives in Home Assistant's config directory so
    # the integration and this host-side script read the same file --
    # see hold.py. This is the host's view of it.
    hold_file = _host_path(cfg, f"{CONTAINER_CONFIG_DIR}/{HOLD_FILENAME}")
    handover_file = _host_path(cfg, f"{CONTAINER_CONFIG_DIR}/{HANDOVER_FILENAME}")
    # Why a demotion is being deferred, for the operator surface. Under /run
    # like the other volatile promoter state: an explanation from a previous
    # boot would be a claim nobody checked (ADR-008 §3).
    grace_reason_file = "/run/cluster-sync/grace-reason"
    # The container the adaptive grace inspects. Empty disables it and falls
    # back to the flat ceiling, which is right wherever docker cannot answer.
    container = str(cfg.get(CONF_HA_CONTAINER) or "")

    # host, username, the CA path and the probe URL all reach this shell
    # verbatim -- unlike `namespace` (validated by `validate_namespace` at
    # config-flow time), nothing constrains these to be shell-safe. An
    # unquoted CA path containing a space (`/etc/my ca.pem`) would split
    # into two argv tokens, argparse would exit 2 on the stray one, and
    # there would be no leader election with a journal line as the only
    # symptom.
    username_flag = f"    --username {shlex.quote(username)} \\\n" if username else ""
    tls_flags = "    --tls \\\n" if use_tls else ""
    # A HOST path: cluster-promoter.sh runs python3 on this machine, which
    # has no /config. See _host_path.
    tls_flags += (
        f"    --tls-ca-file {shlex.quote(_host_path(cfg, ca_certs))} \\\n" if ca_certs else ""
    )

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
        f"    --hold-file {shlex.quote(hold_file)} \\\n"
        f"    --handover-file {shlex.quote(handover_file)} \\\n"
        f"    --probe-grace {PROMOTER_PROBE_GRACE_SECONDS} \\\n"
        f"    --base-grace {PROMOTER_BASE_GRACE_SECONDS} \\\n"
        f"    --ha-container {shlex.quote(container)} \\\n"
        f"    --grace-reason-file {shlex.quote(grace_reason_file)} \\\n"
        "    $NO_PROBE_FLAG \\\n"
        # Forwarded so `install.sh` can run this same wrapper with `--adopt`
        # and inherit every connection flag above, rather than restating the
        # host, port, namespace, TLS and credentials a second time where they
        # could drift out of step with these.
        '    "$@"\n'
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
    # Named in the recovery instructions, so someone whose session died
    # can fetch the bundle again without knowing anything by heart.
    container = str(cfg.get(CONF_HA_CONTAINER) or "homeassistant")
    lines = [
        f"# Cluster State Sync — host bundle ({model} standby)\n\n",
        f"Generated for node `{node}`, cluster namespace `{namespace}`.\n\n",
        "> **Generated blind.** None of this has been run against a real host.\n"
        "> The firewall rules in particular were written from your wizard answers\n"
        "> without access to the machine, and the correct filter point depends on\n"
        "> how Home Assistant is networked. Read them before loading them.\n\n",
        "## If you lose your session part-way through\n\n",
        "**Nothing here depends on your clipboard, and nothing is left half\n"
        "applied.** If SSH drops, a laptop sleeps, or the command you copied is\n"
        "gone, you have lost nothing but the typing.\n\n",
        "**The bundle lives inside Home Assistant** and is rewritten every time\n"
        f"the wizard runs, at `{BUNDLE_DIR_NAME}/` in the config directory. It is\n"
        "not a one-time paste; fetch it again as often as you like:\n\n",
        "```bash\n"
        f"docker exec {container} tar -C /config/{BUNDLE_DIR_NAME} -cf - . \\\n"
        "  | sudo tar -C /etc/cluster-sync -xf -\n"
        "```\n\n",
        "**Lost this file?** It is in there too:\n\n",
        f"```bash\ndocker exec {container} cat /config/{BUNDLE_DIR_NAME}/INSTALL.md\n```\n\n",
        "**`install.sh` is safe to re-run.** It re-copies the files and leaves\n"
        "already-enabled timers alone, so finishing an interrupted install is\n"
        "just running it again -- with `--dry-run` first if you want to see what\n"
        "it would do:\n\n",
        "```bash\n"
        "cd /etc/cluster-sync && sudo ./install.sh --dry-run   # look\n"
        "cd /etc/cluster-sync && sudo ./install.sh             # then do\n"
        "```\n\n",
        "**Nothing is armed until the timers are enabled**, which is the last\n"
        "thing the script does. An interrupted install leaves files in\n"
        "`/etc/cluster-sync` and no change in behaviour at all.\n\n",
        "**On the wrong machine?** The script checks before it touches anything:\n"
        "it verifies the container and config path exist, and refuses outright if\n"
        "this host answers to the peer's name.\n\n",
        "## Install\n\n",
        "> 🚨 **This bundle belongs to ONE node. Do not copy it to the other.**\n"
        f"> It was generated for `{node}`, and nine of its files carry that\n"
        "> identity or this host's own paths -- `cluster-promoter.sh` embeds\n"
        "> the node id, the notify scripts and the swap embed this host's\n"
        "> config directory and container name, and the two hosts differ in\n"
        "> both.\n"
        ">\n"
        "> Installed on the peer, this bundle makes it present **this** node's\n"
        "> id to the lease. The lease renews on identity, so both nodes would\n"
        "> renew the same one and both would believe they lead -- the identity\n"
        "> collision AR-0025 exists to prevent, arrived at by `scp`.\n"
        ">\n"
        "> **The peer runs its own wizard and generates its own bundle.** The\n"
        "> shared values (namespace, database, cluster secret, TLS) are what\n"
        "> you carry across; the files are not.\n\n",
        "### The short way\n\n",
        "This bundle ships its own installer. On **this** host, as root, from\n"
        "the directory holding these files:\n\n",
        "```bash\n"
        "./install.sh --dry-run   # prints every command, changes nothing\n"
        "sudo ./install.sh        # then do it\n"
        "```\n\n",
        "It does everything the rest of this section describes, in the right\n"
        "order, and pre-flights first: the container exists, the config directory\n"
        "really holds `.storage`, the TLS CA is readable **from the host**, and\n"
        f"Home Assistant answers `{ha_url}`. Each of those has been a silent\n"
        "failure in this project at least once.\n\n",
        "**Order matters between the hosts.** Generate and install each node's\n"
        "own bundle, **standby first**, then the primary.\n\n",
        "The order matters because\n"
        "then the primary. A primary whose promoter is armed while the standby\n"
        "has none will, on any Home Assistant restart that outlasts one tick,\n"
        "release the lease and stop the container — with nothing to fail over\n"
        f"to, and a {int(PROMOTER_RELEASE_HOLDDOWN_SECONDS)}s hold-down before it\n"
        "starts it again.\n\n",
        "Read it before you run it. It prints what it is about to do, and the\n"
        "commands below are the same steps written out — they are the audit\n"
        "trail for what the script does, not a second, different method.\n\n",
        "### The long way, and what the script is doing\n\n",
        "On **this** host (and, from its own bundle, on the peer):\n\n",
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
        "Install the VRRP state default. Each node needs this, from its own\nbundle:\n\n",
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


def _hold_script(cfg: dict[str, Any]) -> str:
    """`cluster-hold.sh on|off|status` -- set, clear and show the hold.

    A bare `touch` would do, and the docs say so, but the path is long enough
    to fat-finger and the thing it controls is whether the cluster is allowed
    to fail over. A wrapper that prints what it did, and refuses to be vague
    about it, is worth the twenty lines.

    `on` takes an optional reason, which lands in the file and comes back out
    in the sensor's attributes and every promoter tick. A hold found days later
    with no explanation is a hold nobody dares remove.
    """
    hold_file = shlex.quote(_host_path(cfg, f"{CONTAINER_CONFIG_DIR}/{HOLD_FILENAME}"))
    return f"""{SHEBANG}{STRICT}
# Cluster State Sync -- suspend or resume failover on THIS node.
#
#   ./cluster-hold.sh on  "upgrading the integration"   # suspend
#   ./cluster-hold.sh off                               # resume
#   ./cluster-hold.sh status
#
# While the hold is on, this node renews a lease it already holds but never
# takes a free one, never promotes, and never demotes -- so a planned restart
# does not become a failover. `force-master` still overrides it.
#
# 🚨 Set it on BOTH nodes before planned work, and remember to clear it. A
# forgotten hold is a cluster that has silently stopped failing over.

HOLD={hold_file}

case "${{1:-status}}" in
    on)
        printf '%s\\n' "${{2:-set $(date -Is) by $(id -un)}}" > "$HOLD"
        echo "hold ON  -> $HOLD"
        echo "  failover is now SUSPENDED on this node. Clear it with: $0 off"
        ;;
    off)
        if [[ -f "$HOLD" ]]; then
            rm -f "$HOLD"
            echo "hold OFF -> removed $HOLD"
            echo "  failover is live again on this node."
        else
            echo "hold OFF -> $HOLD was not set; nothing to do"
        fi
        ;;
    status)
        if [[ -f "$HOLD" ]]; then
            echo "hold ON  -> $HOLD"
            echo "  reason: $(cat "$HOLD" 2>/dev/null || echo '(unreadable)')"
            exit 1
        fi
        echo "hold off -> $HOLD absent; failover is live"
        ;;
    *)
        echo "usage: $0 [on [reason] | off | status]" >&2
        exit 2
        ;;
esac
"""


def _install_script(cfg: dict[str, Any], filenames: list[str]) -> str:
    """`install.sh` — put this bundle on this host and start it.

    Generated rather than documented because INSTALL.md's sixteen `sudo`
    commands are sixteen chances to paste one wrong, and because two of the
    steps are not obvious enough to be left to a reader:

    **The order.** `--adopt` must run before the promoter timer is enabled.
    `decide()` acts on a *change* of the lease outcome, and a fresh install has
    no `vrrp-state`, so the first tick reads `previous == ""`, calls the status
    quo a transition into it, and runs the matching notify script. On the node
    that is already leader that is `notify_master.sh`: a fileset swap and a
    device pre-flight rewriting `.storage` underneath a Home Assistant that
    never stopped. Installing would break the thing being installed.

    **The pre-flight.** Both checks are incidents, not hypotheticals. A config
    path that does not hold `.storage` is the `…/homeassistant` vs
    `…/homeassistant/config` slip; a CA the host cannot open is a container
    path that reached a host-side script. Each cost a working failover and each
    was silent -- a timer that runs, fails, and logs.

    It prints every command before running it and takes `--dry-run`, because
    nobody should run a stranger's install script without seeing what it does,
    and because ADR-005's whole argument is that the operator reads the rules
    before they run. That applies to us too.
    """
    config_path = shlex.quote(str(cfg.get(CONF_HA_CONFIG_PATH) or DEFAULT_HA_CONFIG_PATH))
    container = shlex.quote(str(cfg.get(CONF_HA_CONTAINER) or "homeassistant"))
    # AR-0058: used to prove this is not the OTHER node.
    peer_host = str(cfg.get(CONF_PEER_HOST) or "")
    use_tls = bool(cfg.get(CONF_REDIS_USE_TLS))
    ca_certs = str(cfg.get(CONF_REDIS_TLS_CA_CERTS) or "") if use_tls else ""
    host_ca = shlex.quote(_host_path(cfg, ca_certs)) if ca_certs else ""

    # `install.sh` copies its siblings, never itself: it is already here, and
    # copying it into INSTALL_DIR would leave a stale installer beside the
    # bundle it installed.
    payload = [n for n in filenames if n != "install.sh"]
    file_lines = "\n".join(f"    {shlex.quote(n)}" for n in payload)
    file_count = len(payload)
    probe_url = shlex.quote(_promoter_ha_url(cfg))
    holddown = int(PROMOTER_RELEASE_HOLDDOWN_SECONDS)
    units = [n for n in filenames if n.endswith((".service", ".timer"))]
    timers = sorted(n for n in units if n.endswith(".timer"))
    secrets = sorted(n for n in filenames if n in SECRET_FILENAMES)
    has_promoter = "cluster-promoter.timer" in timers

    ca_check = (
        f"""
if [[ ! -r {host_ca} ]]; then
    fail "the TLS CA {host_ca} is not readable from this host.
    That path is what the generated scripts pass to Valkey. If it looks like a
    path inside Home Assistant (/config/...), the bundle was generated before
    the host translation existed -- regenerate it from the wizard."
fi
ok "TLS CA readable: {host_ca}"
"""
        if host_ca
        else '\nnote "no TLS CA configured; nothing to check"\n'
    )

    if has_promoter:
        adopt_block = f"""
say "3. Recording the role this node already has"
note "Arming the timer must not itself be a promotion. The promoter acts on a
      CHANGE of the lease, and with no state file its first tick would call the
      status quo a change -- running notify_master.sh on the node that is
      already leader, which swaps the fileset and rewrites .storage underneath
      a Home Assistant that never stopped. This records the current answer
      first, so the first real tick sees nothing to do."
run {INSTALL_DIR}/cluster-promoter.sh --adopt
if [[ $DRY_RUN -eq 0 ]]; then
    ok "state file now reads: $(cat {VRRP_STATE_PATH} 2>/dev/null || echo '<unwritten>')"
fi
"""
    else:
        adopt_block = """
say "3. Recording the role this node already has"
note "Skipped: this bundle has no promoter, because fileset replication is off.
      Nothing here runs on a timer, so there is no first tick to protect."
"""

    unit_copies = "".join(f'run install -m 0644 "$SRC/{u}" /etc/systemd/system/\n' for u in units)
    timer_starts = "".join(f"run systemctl enable --now {t}\n" for t in timers)
    secret_chmods = "".join(f"run chmod 0600 {INSTALL_DIR}/{s}\n" for s in secrets)
    status_lines = "".join(
        f'    systemctl --no-pager --lines=0 status {t} 2>&1 | sed "s/^/    /" || true\n'
        for t in timers
    )

    return f"""{SHEBANG}{STRICT}
# ============================================================================
# Cluster State Sync -- install this bundle on THIS host.
#
# READ IT BEFORE YOU RUN IT. That is not a formality. This is the step that
# arms failover, and it is exactly why this integration generates host config
# instead of applying it: so the rules get read before they run. Every command
# below is printed as it executes.
#
#   ./install.sh --dry-run     print everything, change nothing
#   sudo ./install.sh          install and start
#
# Safe to re-run after regenerating the bundle: it re-copies the files and
# leaves already-enabled timers alone.
# ============================================================================

DRY_RUN=0
case "${{1:-}}" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '3,20p' "$0"; exit 0 ;;
    "") ;;
    *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

SRC="$(cd "$(dirname "$0")" && pwd)"

say()  {{ printf '\\n=== %s\\n' "$*"; }}
note() {{ printf '    %s\\n' "$*"; }}
ok()   {{ printf '    ok: %s\\n' "$*"; }}
fail() {{ printf '\\n!!! %s\\n\\n' "$*" >&2; exit 1; }}
run()  {{
    printf '  + %s\\n' "$*"
    if [[ $DRY_RUN -eq 0 ]]; then "$@"; fi
}}

if [[ $DRY_RUN -eq 1 ]]; then
    note "DRY RUN -- nothing below will be executed."
fi

# ---------------------------------------------------------------------------
say "1. Pre-flight"
# ---------------------------------------------------------------------------
if [[ $DRY_RUN -eq 0 && ${{EUID:-$(id -u)}} -ne 0 ]]; then
    fail "must run as root: sudo $0"
fi

for cmd in docker systemctl python3; do
    command -v "$cmd" >/dev/null 2>&1 || fail "$cmd is not on PATH"
done
ok "docker, systemctl and python3 present"

# 🚨 Are we on the RIGHT machine? (AR-0058)
#
# This bundle is built for ONE node: the container name, the config path, the
# node id and the peer are all specific to it. Installing the other node's
# bundle is silently wrong -- the promoter inspects a container that is not
# there, the pull writes to a path nothing reads.
#
# The container and path checks above catch it ONLY when the two hosts happen
# to differ. Anyone who followed a standard guide has `homeassistant` on both
# and the same config path, and then nothing above notices.
#
# So this asks the reliable question instead. We may not know our own
# hostname -- the integration sees the CONTAINER's -- but we know the peer's,
# because the operator typed it. If this machine answers to the peer's name,
# this is the wrong bundle, and no amount of the rest of the script is going
# to be right.
PEER_NAME="{peer_host}"
if [[ -n "$PEER_NAME" ]]; then
    THIS_HOST="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
    PEER_SHORT="${{PEER_NAME%%.*}}"
    if [[ -n "$THIS_HOST" && "${{THIS_HOST,,}}" == "${{PEER_SHORT,,}}" ]]; then
        fail "this bundle is for the PEER of '$PEER_NAME', and this machine
    calls itself '$THIS_HOST'. You are installing the wrong node's bundle.

    Each node has its own: regenerate on the other machine's Home Assistant,
    or copy that node's bundle here. Nothing has been changed."
    fi
    ok "not the peer ('$PEER_NAME'), so this is the intended machine"
fi

if ! docker inspect {container} >/dev/null 2>&1; then
    fail "no container named {container} on this host.
    That name came from the wizard. The two nodes of a pair routinely differ
    here -- check with: docker ps -a --format '{{{{.Names}}}}'"
fi
ok "container {container} exists"

# The config path is a HOST path, and the marker for the right one is
# .storage. Two hosts built from different compose files routinely differ by a
# trailing /config component, and a wrong answer here is silent: the swap
# writes a promoted .storage into a directory nothing reads.
if [[ ! -d {config_path} ]]; then
    fail "{config_path} does not exist on this host."
fi
if [[ ! -d {config_path}/.storage ]]; then
    fail "{config_path} exists but holds no .storage, so it is not Home
    Assistant's config directory. Check whether you want its parent or a
    /config level below it:
        find /mnt /srv /opt /var/lib -maxdepth 5 -type d -name .storage 2>/dev/null"
fi
ok "config directory holds .storage: {config_path}"
{ca_check}

# Design D3: the promoter probes this URL before renewing a lease it already
# holds, and a node that fails the probe RELEASES and demotes -- which runs
# notify_backup.sh, which stops Home Assistant. If the probe cannot reach Home
# Assistant from this host, arming the timer schedules an outage: the release
# hold-down is {holddown}s, so the container stays stopped that long before the
# promoter takes the lease back. Any HTTP answer counts as alive, 401 included
# -- only a refused connection, a DNS failure or a timeout reads as dead.
if command -v curl >/dev/null 2>&1; then
    if ! curl -s -o /dev/null --max-time 5 {probe_url}; then
        fail "the D3 probe target {probe_url} does not answer from this host.
    The promoter would read that as a dead Home Assistant, release the lease and
    run notify_backup.sh -- which stops the container -- for {holddown}s at a
    time. Check the container's network mode: with a published port rather than
    network_mode host, loopback on the host is not where it answers, and the
    wizard's Container IP field is how you tell the promoter where it is."
    fi
    ok "D3 probe target answers: {probe_url}"
else
    note "curl absent -- cannot verify the D3 probe target {probe_url}."
    note "Check it by hand before trusting failover; a probe that cannot reach"
    note "Home Assistant stops the container instead of protecting it."
fi

# ---------------------------------------------------------------------------
say "2. Copying the bundle into {INSTALL_DIR}"
# ---------------------------------------------------------------------------
run mkdir -p {INSTALL_DIR}
run chmod 0755 {INSTALL_DIR}

# Named explicitly rather than copied with a wildcard. `cp "$SRC"/*` copies
# whatever else is in the directory this script happens to be sitting in, and
# the first dry run of this installer -- from /tmp -- proposed copying all of
# /tmp into {INSTALL_DIR}. These are the files this bundle actually generated.
BUNDLE_FILES=(
{file_lines}
)
for f in "${{BUNDLE_FILES[@]}}"; do
    if [[ ! -f "$SRC/$f" ]]; then
        fail "$SRC/$f is missing. Copy the whole bundle directory across, not
    just this script."
    fi
done
ok "all {file_count} bundle files present in $SRC"
for f in "${{BUNDLE_FILES[@]}}"; do
    run cp -p "$SRC/$f" {INSTALL_DIR}/
done
run find {INSTALL_DIR} -maxdepth 1 -name '*.sh' -exec chmod 0755 {{}} +
{secret_chmods}
run install -m 0644 "$SRC/cluster-sync-tmpfiles.conf" /etc/tmpfiles.d/cluster-sync.conf
run systemd-tmpfiles --create /etc/tmpfiles.d/cluster-sync.conf
{adopt_block}
# ---------------------------------------------------------------------------
say "4. Installing the systemd units"
# ---------------------------------------------------------------------------
{unit_copies}run systemctl daemon-reload
{timer_starts}
# ---------------------------------------------------------------------------
say "5. Where that leaves you"
# ---------------------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
    note "Dry run finished. Nothing was changed."
else
{status_lines}    note "Watch the next few ticks with:"
    note "  journalctl -fu cluster-promoter.service"
fi
"""


MANAGED_FILENAMES: frozenset[str] = frozenset(
    {
        "install.sh",
        "cluster-hold.sh",
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
        # Emitted only when statistics replication is on, and listed here so
        # that turning it back OFF prunes the installed copies rather than
        # leaving a timer running against a key nobody publishes (ADR-005).
        "statistics_pull.py",
        "statistics_sync.py",
        "cluster-statistics-pull.sh",
        "cluster-statistics-pull.service",
        "cluster-statistics-pull.timer",
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
        "cluster-dashboard.yaml",
    }
)


def _dashboard_yaml() -> str:
    """A Lovelace dashboard for the cluster, as text for the operator to paste.

    Generated and not applied, exactly like everything else in this bundle
    (ADR-005). That is not a shortcut: Home Assistant exposes no public API for
    one integration to create a dashboard for another. `DashboardsCollection`
    lives inside `lovelace.async_setup` and is never stored on `hass.data`, so
    creating one would mean writing to Home Assistant's own storage behind its
    back and then re-implementing its panel-registration listener. This project
    works through public APIs only, precisely so a minor Home Assistant bump
    cannot break it.

    🚨 **No entity ids are hardcoded.** Every card finds its entities by the
    suffix of their id, because the prefix is the device name and that is not
    stable: after a promotion the standby inherits the leader's entity registry,
    so a node called `node2` serves entities named `..._node1_...` forever.
    Hardcoding ids would produce a dashboard that works on the node it was
    generated from and breaks on the one you actually need it on.
    """
    return (
        "# Cluster State Sync — overview\n"
        "#\n"
        "# Settings -> Dashboards -> Add dashboard -> New dashboard from scratch,\n"
        "# then open its three-dot menu -> Raw configuration editor and paste all\n"
        "# of this over what is there.\n"
        "#\n"
        "# Tick 'Admin only' in the dashboard's settings. Home Assistant enforces\n"
        "# that itself; nothing here can.\n"
        "#\n"
        "# Open it on BOTH nodes and compare. That is the point of it: these\n"
        "# numbers are read from the shared store, so they must agree. Two nodes\n"
        "# both showing 'this node leads' is a split brain.\n"
        "views:\n"
        "  - title: Cluster\n"
        "    path: cluster\n"
        "    icon: mdi:server-network\n"
        "    cards:\n"
        "      - type: markdown\n"
        "        title: Who leads\n"
        "        content: |-\n"
        "          {% set _leader = states.sensor "
        "| selectattr('entity_id','search','_cluster_leader') | list %}\n"
        "          {% set leader = _leader[0] if _leader else none %}\n"
        "          {% set _mine = states.binary_sensor "
        "| selectattr('entity_id','search','_is_leader') | list %}\n"
        "          {% set mine = _mine[0] if _mine else none %}\n"
        "          {% if leader is none %}\n"
        "          Cluster State Sync is not set up on this node.\n"
        "          {% else %}\n"
        "          **Lease holder:** {{ leader.state }}\n"
        "\n"
        "          **This node:** "
        "{% if mine is none %}unknown"
        "{% elif mine.state == 'on' %}**leading**"
        "{% elif mine.state == 'off' %}standing by"
        "{% else %}unknown — cannot reach the store{% endif %}\n"
        "\n"
        "          **Snapshot written by:** "
        "{{ leader.attributes.get('snapshot_source', 'unknown') }}\n"
        "\n"
        "          {% if leader.attributes.get('snapshot_source') "
        "and leader.state not in ['unknown','unavailable'] "
        "and leader.attributes.get('snapshot_source') != leader.state %}\n"
        "          > The leader is not the node that last wrote. Normal for a few\n"
        "          > seconds after a promotion; a lasting difference means the\n"
        "          > leader is not flushing.\n"
        "          {% endif %}\n"
        "          {% endif %}\n"
        "      - type: markdown\n"
        "        title: Freshness\n"
        "        content: |-\n"
        "          {% set _shared = states.sensor "
        "| selectattr('entity_id','search','_shared_snapshot_age') | list %}\n"
        "          {% set shared = _shared[0] if _shared else none %}\n"
        "          {% set _local = states.sensor "
        "| selectattr('entity_id','search','_last_snapshot_age') | list %}\n"
        "          {% set local = _local[0] if _local else none %}\n"
        "          {% set _backend = states.binary_sensor "
        "| selectattr('entity_id','search','_backend') | list %}\n"
        "          {% set backend = _backend[0] if _backend else none %}\n"
        "          **Backend:** "
        "{% if backend is none %}unknown"
        "{% elif backend.state == 'on' %}connected"
        "{% else %}**UNREACHABLE**{% endif %}\n"
        "\n"
        "          **Shared snapshot:** "
        "{% if shared is none or shared.state in ['unknown','unavailable'] %}"
        "nothing written yet"
        "{% else %}{{ shared.state | float | round(0) }}s old{% endif %}\n"
        "\n"
        "          **This node last flushed:** "
        "{% if local is none or local.state in ['unknown','unavailable'] %}"
        "never — normal on a standby"
        "{% else %}{{ local.state | float | round(0) }}s ago{% endif %}\n"
        "\n"
        "          > Shared is the number that matters before promoting: it is how\n"
        "          > much state you would lose. The local one is meaningless on a\n"
        "          > standby, which by design never flushes.\n"
        "      - type: markdown\n"
        "        title: Go-bag\n"
        "        content: |-\n"
        "          {% set _age = states.sensor "
        "| selectattr('entity_id','search','_fileset_age') | list %}\n"
        "          {% set age = _age[0] if _age else none %}\n"
        "          {% set _bad = states.binary_sensor "
        "| selectattr('entity_id','search','_fileset_degraded') | list %}\n"
        "          {% set bad = _bad[0] if _bad else none %}\n"
        "          {% if bad is not none and bad.state == 'on' %}\n"
        "          ## ⚠ Degraded\n"
        "          The last promotion found the go-bag stale or missing. A standby\n"
        "          promoted now would come up without the leader's identity, and\n"
        "          everyone would be asked to log in again.\n"
        "          {% else %}\n"
        "          Healthy.\n"
        "          {% endif %}\n"
        "\n"
        "          **Staged go-bag:** "
        "{% if age is none or age.state in ['unknown','unavailable'] %}"
        "none staged{% else %}{{ age.state | float | round(0) }}s old{% endif %}\n"
        "      - type: horizontal-stack\n"
        "        cards:\n"
        "          - type: button\n"
        "            name: Flush now\n"
        "            icon: mdi:content-save-move\n"
        "            tap_action:\n"
        "              action: perform-action\n"
        "              perform_action: cluster_state_sync.flush_snapshot\n"
        "          - type: button\n"
        "            name: Clear degraded\n"
        "            icon: mdi:check-decagram\n"
        "            tap_action:\n"
        "              action: perform-action\n"
        "              perform_action: cluster_state_sync.clear_degraded\n"
        "      - type: markdown\n"
        "        content: |-\n"
        "          *Flush refuses on a node that does not hold leadership — a\n"
        "          follower writing would overwrite the leader's snapshot.*\n"
        "\n"
        "          *There is deliberately no force-promote button. That bypasses\n"
        "          the split-brain guard and stays a file you must be on the host\n"
        "          to create: `touch /run/cluster-sync/force-master`.*\n"
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
