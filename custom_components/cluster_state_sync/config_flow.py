"""Config flow for Cluster State Sync."""

from __future__ import annotations

from collections.abc import Sequence
import fnmatch
import logging
import pathlib
import secrets
import shlex
from typing import Any, Final

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import instance_id
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .backend import RedisBackend, default_node_id
from .bundle import write_bundle
from .const import (
    BUNDLE_DIR_NAME,
    CONF_ACCEPT_RISK,
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
    CONF_FILESET_HOT_INTERVAL,
    CONF_FILESET_MAX_BYTES,
    CONF_FILESET_STALE_AFTER,
    CONF_GATE_AUTOMATIONS,
    CONF_GATE_RECORDER,
    CONF_HA_CONFIG_PATH,
    CONF_HA_CONTAINER,
    CONF_HA_CONTAINER_IP,
    CONF_HA_START_MODE,
    CONF_HA_UID,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_IOT_SUBNETS,
    CONF_LEADERSHIP_ENTITY,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_RADIO_WATCH,
    CONF_REDIS_DB,
    CONF_REDIS_HOST,
    CONF_REDIS_PASSWORD,
    CONF_REDIS_PORT,
    CONF_REDIS_SENTINEL_HOSTS,
    CONF_REDIS_SENTINEL_SERVICE,
    CONF_REDIS_TLS_CA_CERTS,
    CONF_REDIS_USE_SENTINEL,
    CONF_REDIS_USE_TLS,
    CONF_REDIS_USERNAME,
    CONF_RESTORE_MAX_AGE,
    CONF_SETTLE_DELAY,
    CONF_SNAPSHOT_INTERVAL,
    CONF_TOPOLOGY_MODEL,
    DEFAULT_CLUSTER_NAMESPACE,
    DEFAULT_DOCKER_NETWORK,
    DEFAULT_FILESET_ENABLED,
    DEFAULT_FILESET_EXCLUSIONS,
    DEFAULT_FILESET_EXTRA_CUSTOM,
    DEFAULT_FILESET_EXTRA_PATHS,
    DEFAULT_FILESET_HOT_INTERVAL,
    DEFAULT_FILESET_MAX_BYTES,
    DEFAULT_FILESET_STALE_AFTER,
    DEFAULT_HA_CONFIG_PATH,
    DEFAULT_HA_START_MODE,
    DEFAULT_INCLUDE_DOMAINS,
    DEFAULT_LEADERSHIP_SOURCE,
    DEFAULT_REDIS_DB,
    DEFAULT_REDIS_PORT,
    DEFAULT_RESTORE_MAX_AGE,
    DEFAULT_SETTLE_DELAY,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_TOPOLOGY_MODEL,
    DOCKER_NETWORK_MODES,
    DOMAIN,
    FILESET_EXTRA_CANDIDATES,
    HA_START_MODES,
    LEADERSHIP_SOURCES,
    TOPOLOGY_MODELS,
    TOPOLOGY_WARM,
    validate_namespace,
)
from .fileset import REPLICATED_DIRS, REPLICATED_FILES, is_excluded
from .includes import scan as scan_includes
from .util import parse_sentinel_hosts

_LOGGER = logging.getLogger(__name__)

# AR-0006/AR-0007: these fields are rendered as password inputs so they are not
# shouldered off the screen during setup. Note what that does and does not buy
# you — HA stores config entries as plaintext JSON under `.storage/`, so masking
# is a shoulder-surfing control, not encryption at rest. The README says so
# explicitly rather than letting the dots imply a protection that isn't there.
_SECRET_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _number(*, minimum: int, maximum: int | None = None, slider: bool = False) -> vol.All:
    """An integer field whose current value is always on screen.

    A bare `vol.All(int, vol.Range(min=…, max=…))` serialises to a plain
    bounded integer, which the frontend renders as a **naked slider**: the
    number it is set to is invisible until you grab the handle. Every field on
    this form that has to match the other node -- the database above all -- was
    then unreadable at a glance, so checking whether two nodes agree meant
    dragging the handle and undoing it. That is how a mismatch survives being
    looked at.

    A `NumberSelector` keeps the value in a box beside the control. `slider`
    picks the shape: on for a short enumerated range like the database (0-15),
    off for anything wide enough that a slider is a worse control than typing
    -- a port, a byte count, a timeout in seconds.

    The `vol.Coerce(int)` is not decoration. `NumberSelector` validates to
    **float**, so without it the database lands in the config entry as `2.0`
    and every downstream comparison and Redis call inherits a float where an
    int is meant.

    `max` is *omitted* rather than set to `None` for the open-ended fields.
    `NumberSelectorConfig` validates its own keys and rejects a null maximum
    with `expected float for dictionary value @ data['max']`, which surfaces
    as "unknown error occurred" on the step -- the same class of failure the
    exclusions field already cost us once.
    """
    config: dict[str, Any] = {
        "min": minimum,
        "step": 1,
        "mode": NumberSelectorMode.SLIDER if slider else NumberSelectorMode.BOX,
    }
    if maximum is not None:
        config["max"] = maximum
    return vol.All(NumberSelector(NumberSelectorConfig(config)), vol.Coerce(int))


# ADR-001 wizard step 3: how this node learns it is leader. Every gating layer
# in the ADR keys off this one signal; the integration enforces the layer it
# can reach from inside an unprivileged container — whether it writes at all.
_LEADERSHIP_FIELD = SelectSelector(
    SelectSelectorConfig(
        options=LEADERSHIP_SOURCES,
        mode=SelectSelectorMode.DROPDOWN,
        translation_key="leadership_source",
    )
)


async def _default_node_id(hass: HomeAssistant) -> str:
    """Suggest a node ID that is actually unique to this install (AR-0025).

    The v0.1 default was the bare container hostname, and the config entry's
    unique_id is `namespace@node_id`. Two nodes built from the same compose
    file routinely have the same hostname, which meant both halves of the
    cluster claimed one identity: the peer-trust check in the restore path
    ("is this entry from the other node?") silently answered wrong, and each
    node skipped the other's entries as its own.

    Suffixing with Home Assistant's own install UUID makes the default unique
    per instance while staying readable. The operator can still override it.
    """
    uuid = await instance_id.async_get(hass)
    return f"{default_node_id()}-{uuid[:6]}"


def _generate_cluster_secret() -> str:
    """Mint a cluster secret (AR-0005).

    Pre-filled on first setup so the operator's path of least resistance is a
    strong random key they copy to the peer, rather than a memorable one they
    invent. `token_urlsafe(32)` is 256 bits.
    """
    return secrets.token_urlsafe(32)


def _bundle_next_steps(cfg: dict[str, Any], files: list[str] | None) -> str:
    """What to do with the files, described from what was actually generated.

    This paragraph used to be one static sentence telling every operator to
    load the firewall rules with `nft-safety-revert.sh` armed. Cold bundles
    contain no firewall rules and no such script, so most installs were being
    pointed at a file that was not there -- and the one instruction that
    mattered to them, that `install.sh` exists, was not mentioned at all.
    """
    names = set(files or [])
    lines: list[str] = []
    if "install.sh" in names:
        lines.append(
            "Copy this whole directory to each host and run `sudo ./install.sh "
            "--dry-run` there, then without `--dry-run`. **Standby first, then "
            "the primary** — a primary armed alone will stop its own container "
            "on the next restart with nothing to fail over to."
        )
    else:
        lines.append("Copy these to `/etc/cluster-sync/` on **both** hosts.")
    lines.append("Read `INSTALL.md` before running anything.")
    if "nft-safety-revert.sh" in names:
        lines.append(
            "This bundle contains firewall rules, generated without access to "
            "your machines and never tested. Load them with "
            "`nft-safety-revert.sh` armed."
        )
    return "\n\n".join(lines)


def _transfer_commands(cfg: dict[str, Any], bundle_dir: str, peer: dict[str, Any]) -> str:
    """How to get THIS node's bundle out of the container and onto its host.

    Not "copy it to the other node". Nine of the bundle's files carry this
    node's identity or this host's paths -- `cluster-promoter.sh` embeds the
    node id -- so installing this bundle on the peer would make the peer
    present *this* node's id to the lease. The lease renews on identity, so
    both would renew the same one and both would believe they lead. That is
    AR-0025's identity collision, reached by `scp`. The peer runs its own
    wizard.

    What is genuinely awkward, and what this solves, is the step nobody
    documents: the bundle is written inside Home Assistant's container, owned
    by root, and it has to reach `/etc/cluster-sync` on the host -- which may
    not be the machine you are sitting at.

    The operator's answers are used to render text and are **never stored**.
    Home Assistant writes config entries as unencrypted JSON, so a remembered
    hostname and key path would be two more facts sitting in `.storage` for
    anyone who can read `/config`.
    """
    container = shlex.quote(str(cfg.get(CONF_HA_CONTAINER) or "homeassistant"))
    remote = str(peer.get("host") or "").strip()
    user = str(peer.get("user") or "").strip()
    key = str(peer.get("key_path") or "").strip()

    local = (
        f"docker exec {container} tar -C {shlex.quote(bundle_dir)} -cf - . \\\n"
        "  | sudo tar -C /etc/cluster-sync -xf -"
    )
    if not remote:
        return (
            "**On this host**, as a user in the `docker` group:\n\n"
            "```bash\n"
            "sudo mkdir -p /etc/cluster-sync\n"
            f"{local}\n"
            "sudo /etc/cluster-sync/install.sh --dry-run\n"
            "sudo /etc/cluster-sync/install.sh\n"
            "```\n\n"
            "The `tar` pipe rather than `docker cp`: the bundle contains two "
            "files with `0600` permissions, and `docker cp` does not preserve "
            "them."
        )

    target = f"{user}@{remote}" if user else remote
    ssh_opts = f" -i {shlex.quote(key)}" if key else ""
    hint = (
        ""
        if key
        else (
            "\n\nNo key path given, so this uses your default. If you do not "
            "know where your key is:\n\n"
            "```bash\nfind ~/.ssh -name 'id_*' ! -name '*.pub'\n```\n\n"
            "Re-run this step with the answer to have it filled in."
        )
    )
    return (
        f"**From a terminal on the machine you are sitting at**, to `{target}`:\n\n"
        "```bash\n"
        f"ssh{ssh_opts} {shlex.quote(target)} 'sudo mkdir -p /etc/cluster-sync'\n"
        f'ssh{ssh_opts} {shlex.quote(target)} "docker exec {container} tar -C '
        f'{shlex.quote(bundle_dir)} -cf - ." \\\n'
        f"  | ssh{ssh_opts} {shlex.quote(target)} 'sudo tar -C /etc/cluster-sync -xf -'\n"
        "```\n\n"
        "Then, on that host:\n\n"
        "```bash\n"
        "sudo /etc/cluster-sync/install.sh --dry-run\n"
        "sudo /etc/cluster-sync/install.sh\n"
        "```"
        f"{hint}\n\n"
        "🚨 This moves **this node's** bundle to **this node's** host. It is "
        "not a way to set up the other node -- that one runs its own wizard."
    )


#: Never worth offering as an extra replicated path. Not a security boundary --
#: an operator can still type any of these -- just noise removal, measured
#: against node-a's real config directory.
_NEVER_OFFER: Final = (
    "*.log",
    "*.log.*",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.gz",
    "*.zip",
    "*.tar",
    "*.tar.*",
)

#: Directories that are large, regenerable, or ours.
_NEVER_OFFER_EXACT: Final = frozenset(
    {
        "backups",
        "deps",
        "tts",
        "www",
        "blueprints",
        # Our own output. Replicating the bundle would ship one node's
        # generated host config -- including its node id -- to the other,
        # which is the identity collision GOTCHAS 3 describes.
        "cluster_state_sync_bundle",
        "old_._storage",
    }
)


def _extra_path_candidates(
    config_dir: str, exclusions: Sequence[str] = DEFAULT_FILESET_EXCLUSIONS
) -> list[str]:
    """Things worth offering as extra replicated paths, found on disk.

    Deliberately *offered*, never pre-selected. The go-bag's original failure
    was a fixed list that matched nobody's configuration; guessing again on the
    operator's behalf would repeat it in a smaller way.

    Two sources. `FILESET_EXTRA_CANDIDATES` is the set integrations are known
    to read by path -- `python_scripts/`, `zigbee.db` and so on -- which is
    precisely what the include scan cannot see, because nothing in the YAML
    mentions them. Then every other top-level entry in the config directory,
    so an operator who keeps something unusual can tick it rather than having
    to know to type it.

    Anything the include scan already covers is left out: it will be replicated
    regardless, and offering it would imply it needed ticking.
    """
    root = pathlib.Path(config_dir)
    try:
        referenced = scan_includes(config_dir).paths
    except Exception:  # noqa: BLE001 - a broken config must not break the form
        referenced = set()

    covered = {p.split("/")[0] for p in referenced} | set(REPLICATED_DIRS) | set(REPLICATED_FILES)
    offered: list[str] = []

    for name in FILESET_EXTRA_CANDIDATES:
        if name not in covered and (root / name).exists():
            offered.append(name)

    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in covered or name in offered:
            continue
        # Filter hard, or the list is worse than useless. Run against
        # node-a unfiltered this offered forty entries, of which about
        # twenty-five were `*.bak-*` copies, log fragments and SQLite journals.
        # A suggestion list nobody can read is one nobody uses.
        #
        # The operator's own exclusion patterns come first -- if they have said
        # they do not want `*.bak-*` replicated, offering it here contradicts
        # them.
        if is_excluded(name, exclusions):
            continue
        if any(fnmatch.fnmatch(name, pat) for pat in _NEVER_OFFER):
            continue
        if name in _NEVER_OFFER_EXACT:
            continue
        offered.append(name)

    return offered


def _direct_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_REDIS_HOST, default=d.get(CONF_REDIS_HOST, "valkey.lan")): str,
            vol.Required(
                CONF_REDIS_PORT, default=d.get(CONF_REDIS_PORT, DEFAULT_REDIS_PORT)
            ): _number(minimum=1, maximum=65535),
            vol.Optional(CONF_REDIS_USERNAME, default=d.get(CONF_REDIS_USERNAME, "")): str,
            vol.Optional(
                CONF_REDIS_PASSWORD, default=d.get(CONF_REDIS_PASSWORD, "")
            ): _SECRET_FIELD,
            vol.Required(CONF_REDIS_DB, default=d.get(CONF_REDIS_DB, DEFAULT_REDIS_DB)): _number(
                minimum=0, maximum=15, slider=True
            ),
            vol.Required(
                CONF_CLUSTER_NAMESPACE,
                default=d.get(CONF_CLUSTER_NAMESPACE, DEFAULT_CLUSTER_NAMESPACE),
            ): str,  # validated in _handle_step, NOT here -- see below
            vol.Required(CONF_NODE_ID, default=d.get(CONF_NODE_ID, default_node_id())): str,
            vol.Required(
                CONF_CLUSTER_SECRET,
                default=d.get(CONF_CLUSTER_SECRET) or _generate_cluster_secret(),
            ): _SECRET_FIELD,
            vol.Required(CONF_REDIS_USE_TLS, default=d.get(CONF_REDIS_USE_TLS, False)): bool,
            vol.Optional(CONF_REDIS_TLS_CA_CERTS, default=d.get(CONF_REDIS_TLS_CA_CERTS, "")): str,
            vol.Required(
                CONF_SNAPSHOT_INTERVAL,
                default=d.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
            ): _number(minimum=1, maximum=300),
            vol.Required(
                CONF_RESTORE_MAX_AGE,
                default=d.get(CONF_RESTORE_MAX_AGE, DEFAULT_RESTORE_MAX_AGE),
            ): _number(minimum=60, maximum=86400),
        }
    )


def _sentinel_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_REDIS_SENTINEL_HOSTS,
                default=d.get(
                    CONF_REDIS_SENTINEL_HOSTS,
                    "valkey-1.lan:26379,valkey-2.lan:26379,valkey-3.lan:26379",
                ),
            ): str,
            vol.Required(
                CONF_REDIS_SENTINEL_SERVICE,
                default=d.get(CONF_REDIS_SENTINEL_SERVICE, "valkey-primary"),
            ): str,
            vol.Optional(CONF_REDIS_USERNAME, default=d.get(CONF_REDIS_USERNAME, "")): str,
            vol.Optional(
                CONF_REDIS_PASSWORD, default=d.get(CONF_REDIS_PASSWORD, "")
            ): _SECRET_FIELD,
            vol.Required(CONF_REDIS_DB, default=d.get(CONF_REDIS_DB, DEFAULT_REDIS_DB)): _number(
                minimum=0, maximum=15, slider=True
            ),
            vol.Required(
                CONF_CLUSTER_NAMESPACE,
                default=d.get(CONF_CLUSTER_NAMESPACE, DEFAULT_CLUSTER_NAMESPACE),
            ): str,  # validated in _handle_step, NOT here -- see below
            vol.Required(CONF_NODE_ID, default=d.get(CONF_NODE_ID, default_node_id())): str,
            vol.Required(
                CONF_CLUSTER_SECRET,
                default=d.get(CONF_CLUSTER_SECRET) or _generate_cluster_secret(),
            ): _SECRET_FIELD,
            vol.Required(CONF_REDIS_USE_TLS, default=d.get(CONF_REDIS_USE_TLS, False)): bool,
            vol.Optional(CONF_REDIS_TLS_CA_CERTS, default=d.get(CONF_REDIS_TLS_CA_CERTS, "")): str,
            vol.Required(
                CONF_SNAPSHOT_INTERVAL,
                default=d.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
            ): _number(minimum=1, maximum=300),
            vol.Required(
                CONF_RESTORE_MAX_AGE,
                default=d.get(CONF_RESTORE_MAX_AGE, DEFAULT_RESTORE_MAX_AGE),
            ): _number(minimum=60, maximum=86400),
        }
    )


def _container_schema(d: dict[str, Any]) -> vol.Schema:
    """How this node starts and stops Home Assistant.

    `docker start` is the default and right for most installs. Compose mode is
    for the ones where nobody starts the container by hand -- it is one service
    in a project, possibly behind a profile -- and where bringing up the whole
    estate to promote one node is not acceptable.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_HA_START_MODE,
                default=d.get(CONF_HA_START_MODE, DEFAULT_HA_START_MODE),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=HA_START_MODES,
                    mode=SelectSelectorMode.LIST,
                    translation_key="ha_start_mode",
                )
            ),
            vol.Optional(CONF_COMPOSE_FILE, default=d.get(CONF_COMPOSE_FILE, "")): str,
            vol.Optional(CONF_COMPOSE_SERVICE, default=d.get(CONF_COMPOSE_SERVICE, "")): str,
            vol.Optional(CONF_COMPOSE_PROFILE, default=d.get(CONF_COMPOSE_PROFILE, "")): str,
            vol.Optional(CONF_COMPOSE_ENV_FILE, default=d.get(CONF_COMPOSE_ENV_FILE, "")): str,
            # Radio liveness. Globs rather than a list, because what proves a
            # radio is receiving is installation-specific -- on the fleet this
            # was built for it is `sensor.*_rssi_numeric`, which updates on
            # every packet received.
            vol.Optional(CONF_RADIO_WATCH, default=d.get(CONF_RADIO_WATCH) or []): SelectSelector(
                SelectSelectorConfig(options=[], multiple=True, custom_value=True)
            ),
        }
    )


def _domain_options(hass: HomeAssistant | None) -> list[str]:
    """Domains to offer, drawn from what this instance actually has.

    A fixed list would offer domains the operator does not run and hide the
    ones they do. The defaults are always included even if nothing of that
    domain exists yet, so a default never silently disappears from the form.
    """
    found: set[str] = set(DEFAULT_INCLUDE_DOMAINS)
    if hass is not None:
        found.update(state.domain for state in hass.states.async_all())
    return sorted(found)


def _domains_schema(d: dict[str, Any], hass: HomeAssistant | None = None) -> vol.Schema:
    """Which entity domains cross to the standby.

    This has been readable by the integration since the beginning
    (`CONF_INCLUDE_DOMAINS`) and settable only by hand-editing the config
    entry -- so in practice nobody changed it. The defaults are deliberately
    conservative: stateful things whose value a promoted node needs, and
    nothing whose restore could actuate hardware.

    `light` and `switch` are offered but NOT default, and the reason is worth
    stating plainly: restoring them writes a state change, a state change is
    what automations trigger on, and an automation firing on a restored value
    turns real things on in someone's house. If you add them, check what
    triggers on them first.
    """
    return vol.Schema(
        {
            vol.Optional(
                CONF_INCLUDE_DOMAINS,
                default=d.get(CONF_INCLUDE_DOMAINS) or list(DEFAULT_INCLUDE_DOMAINS),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=_domain_options(hass),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            ),
            # Escape hatch for one-off entities in a domain you do not want
            # wholesale. Free text, because the entity may not exist yet on
            # the node running the wizard.
            vol.Optional(
                CONF_INCLUDE_ENTITIES,
                default=d.get(CONF_INCLUDE_ENTITIES) or [],
            ): SelectSelector(SelectSelectorConfig(options=[], multiple=True, custom_value=True)),
        }
    )


def _topology_schema(d: dict[str, Any]) -> vol.Schema:
    """ADR-001 wizard step 2 + 3: standby model and leadership signal."""
    return vol.Schema(
        {
            vol.Required(
                CONF_TOPOLOGY_MODEL,
                default=d.get(CONF_TOPOLOGY_MODEL, DEFAULT_TOPOLOGY_MODEL),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=TOPOLOGY_MODELS,
                    mode=SelectSelectorMode.LIST,
                    translation_key="topology_model",
                )
            ),
            vol.Required(
                CONF_LEADERSHIP_SOURCE,
                default=d.get(CONF_LEADERSHIP_SOURCE, DEFAULT_LEADERSHIP_SOURCE),
            ): _LEADERSHIP_FIELD,
            vol.Optional(CONF_LEADERSHIP_ENTITY, default=d.get(CONF_LEADERSHIP_ENTITY, "")): str,
            # ADR-001 layers 3 and 4. Off by default on purpose: the leadership
            # signal fails closed, so a backend blip that merely delays a flush
            # would otherwise also stop the recorder and every automation.
            vol.Optional(CONF_GATE_RECORDER, default=d.get(CONF_GATE_RECORDER, False)): bool,
            vol.Optional(CONF_GATE_AUTOMATIONS, default=d.get(CONF_GATE_AUTOMATIONS, False)): bool,
            # AR-0042: the host path, which the container cannot discover for
            # itself — `/config` in here is a bind mount and says nothing about
            # where it came from. Asked rather than assumed, because the
            # default was wrong on the very deployment this was built for.
            vol.Required(
                CONF_HA_CONFIG_PATH,
                default=d.get(CONF_HA_CONFIG_PATH, DEFAULT_HA_CONFIG_PATH),
            ): str,
            vol.Required(
                CONF_HA_CONTAINER,
                default=d.get(CONF_HA_CONTAINER, "homeassistant"),
            ): str,
        }
    )


def _warm_schema(d: dict[str, Any]) -> vol.Schema:
    """ADR-001 wizard step 4 — warm-only, hidden entirely when Cold is chosen."""
    return vol.Schema(
        {
            vol.Required(CONF_IOT_SUBNETS, default=d.get(CONF_IOT_SUBNETS, "")): str,
            vol.Required(CONF_BLOCK_DISCOVERY, default=d.get(CONF_BLOCK_DISCOVERY, True)): bool,
            vol.Required(
                CONF_DOCKER_NETWORK,
                default=d.get(CONF_DOCKER_NETWORK, DEFAULT_DOCKER_NETWORK),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=DOCKER_NETWORK_MODES,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="docker_network",
                )
            ),
            vol.Required(CONF_HA_UID, default=d.get(CONF_HA_UID, 1000)): _number(
                minimum=0, maximum=65535
            ),
            vol.Optional(CONF_HA_CONTAINER_IP, default=d.get(CONF_HA_CONTAINER_IP, "")): str,
            vol.Required(
                CONF_SETTLE_DELAY,
                default=d.get(CONF_SETTLE_DELAY, DEFAULT_SETTLE_DELAY),
            ): _number(minimum=0, maximum=300),
        }
    )


def _fileset_schema(d: dict[str, Any], candidates: list[str] | None = None) -> vol.Schema:
    candidates = candidates or []
    """Replicate `.storage` to the standby (design 2026-08-29).

    Off by default. Enabling it means the integration reads every file in the
    config directory — including the credentials — seals them and publishes
    them to Valkey. That is the right trade for a failover that keeps the
    companion app working, and it is not one to make on someone's behalf.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_FILESET_ENABLED,
                default=d.get(CONF_FILESET_ENABLED, DEFAULT_FILESET_ENABLED),
            ): bool,
            vol.Required(
                CONF_FILESET_HOT_INTERVAL,
                default=d.get(CONF_FILESET_HOT_INTERVAL, DEFAULT_FILESET_HOT_INTERVAL),
            ): _number(minimum=10, maximum=3600),
            vol.Required(
                CONF_FILESET_EXCLUSIONS,
                default=list(d.get(CONF_FILESET_EXCLUSIONS, DEFAULT_FILESET_EXCLUSIONS)),
                # A SelectSelector, NOT `vol.All(cv.ensure_list, [str])`.
                #
                # Home Assistant serialises a step's schema to JSON for the
                # frontend and cannot convert a bare callable, so `cv.ensure_list`
                # made this step raise `ValueError: Unable to convert schema`
                # and the operator saw "unknown error occurred". A selector
                # serialises properly, still yields a list, and gives the
                # operator editable chips with the measured defaults offered.
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(DEFAULT_FILESET_EXCLUSIONS),
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(
                CONF_FILESET_EXTRA_PATHS,
                default=list(d.get(CONF_FILESET_EXTRA_PATHS, DEFAULT_FILESET_EXTRA_PATHS)),
                # Same selector as the exclusions above, for the same reason:
                # a bare callable cannot be serialised to the frontend, and a
                # multi-select with `custom_value` lets an operator tick what
                # was found or type a path nobody thought to offer.
                # No `custom_value` here, deliberately. With it, Home Assistant
                # renders a multi-select as a type-to-add chip box rather than
                # a tick list -- which defeats the point of having gone and
                # found the candidates. Free text lives in its own field below.
            ): SelectSelector(
                SelectSelectorConfig(
                    options=candidates,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(
                CONF_FILESET_EXTRA_CUSTOM,
                default=list(d.get(CONF_FILESET_EXTRA_CUSTOM, DEFAULT_FILESET_EXTRA_CUSTOM)),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(d.get(CONF_FILESET_EXTRA_CUSTOM, ())),
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_FILESET_MAX_BYTES,
                default=d.get(CONF_FILESET_MAX_BYTES, DEFAULT_FILESET_MAX_BYTES),
            ): _number(minimum=1024 * 1024),
            vol.Required(
                CONF_FILESET_STALE_AFTER,
                default=d.get(CONF_FILESET_STALE_AFTER, DEFAULT_FILESET_STALE_AFTER),
            ): _number(minimum=60),
        }
    )


class ClusterStateSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial UI setup."""

    VERSION = 1

    def __init__(self) -> None:
        # Accumulates across steps; each step folds its answers in.
        self._data: dict[str, Any] = {}
        self._bundle_files: list[str] | None = None
        # Set only by `async_step_reconfigure`. The steps in between are
        # identical either way; only the last one differs, and it has to
        # know whether it is creating an entry or correcting one.
        self._reconfigure = False
        # Transient, and deliberately never merged into `_data`: these are
        # SSH details, and `_data` becomes an unencrypted config entry.
        self._peer: dict[str, Any] = {}

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Re-walk the wizard against an existing entry.

        Without this, correcting `ha_config_path`, `ha_container`,
        `topology_model` or `fileset_enabled` meant deleting the
        entry and starting over -- and starting over means re-entering the
        cluster secret, whose failure mode when mistyped is silence: every
        entry fails verification, the restore skips all of them, and both nodes
        report themselves healthy.

        So the cost of fixing a one-character mistake in a path was risking the
        one value that must never be retyped. Two of this fleet's four
        host-specific fields were wrong at first attempt, which is the measured
        version of that argument.

        Skips the acknowledgement step: this entry already exists, so the
        no-warranty gate was passed when it was created. Everything else is the
        same flow, pre-filled from the entry.
        """
        entry = self._get_reconfigure_entry()
        self._data = {**entry.data, **entry.options}
        self._reconfigure = True
        return await self.async_step_choose()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """First step: say plainly what this is, and make the operator agree.

        A warning in a README is read by people who were already careful. This
        one is unavoidable, and it asks for an explicit yes -- which is the
        difference between having published a disclaimer and having actually
        told this particular person.

        It leads with the physical consequence rather than with data loss,
        because that is the honest headline: `climate` and `water_heater` are in
        DEFAULT_INCLUDE_DOMAINS, so a wrong restore changes heating or hot water
        and not merely a number on a dashboard. `alarm_control_panel` is in
        SENSITIVE_DOMAINS and is opt-in — the warning says so, because claiming
        it is on by default would be false, and a warning that overstates is as
        corrosive as one that understates.
        """
        if user_input is not None and user_input.get(CONF_ACCEPT_RISK):
            return await self.async_step_choose()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ACCEPT_RISK, default=False): bool}),
            errors={"base": "must_accept"} if user_input is not None else None,
        )

    async def async_step_choose(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Then: direct or sentinel."""
        return self.async_show_menu(
            step_id="choose",
            menu_options=["direct", "sentinel"],
        )

    async def async_step_direct(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._handle_step(user_input, use_sentinel=False)

    async def async_step_sentinel(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._handle_step(user_input, use_sentinel=True)

    async def _handle_step(
        self, user_input: dict[str, Any] | None, *, use_sentinel: bool
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # AR-0010's namespace check lives HERE and not in the schema.
            #
            # `vol.All(str, validate_namespace)` looked tidier and was
            # unusable: Home Assistant serialises a step's schema to JSON for
            # the frontend with `voluptuous_serialize`, which cannot convert an
            # arbitrary callable. Rendering the form raised
            # `ValueError: Unable to convert schema: <function
            # validate_namespace>`, the request 500'd, and the operator got an
            # error dialog with no text in it. The form had never once
            # displayed in a real Home Assistant.
            #
            # Validating here also gives the operator the friendly message that
            # was already sitting unused in strings.json, instead of an
            # exception.
            try:
                validate_namespace(user_input.get(CONF_CLUSTER_NAMESPACE, ""))
            except vol.Invalid:
                errors["base"] = "invalid_namespace"
                defaults = {
                    CONF_NODE_ID: await _default_node_id(self.hass),
                    **self._data,
                    **user_input,
                }
                return self.async_show_form(
                    step_id="sentinel" if use_sentinel else "direct",
                    data_schema=(
                        _sentinel_schema(defaults) if use_sentinel else _direct_schema(defaults)
                    ),
                    errors=errors,
                )
            data = {**user_input, CONF_REDIS_USE_SENTINEL: use_sentinel}
            # Test the connection before going further — no point walking the
            # operator through four more steps to fail on a typo in step one.
            try:
                await _test_backend(data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Backend connectivity test failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                # Merge, never replace: a reconfigure carries values this
                # form does not collect -- ha_config_path, ha_container,
                # topology_model -- and the later steps pre-fill from them.
                self._data = {**self._data, **data}
                return await self.async_step_topology()

        # `self._data` last, so a reconfigure shows the entry's own values
        # rather than a freshly-minted node id and a brand-new cluster
        # secret -- which, submitted unnoticed, would silently orphan
        # this node from the cluster it is already in. Empty on a first
        # run, where the generated defaults are exactly right.
        defaults = {CONF_NODE_ID: await _default_node_id(self.hass), **self._data}
        schema = _sentinel_schema(defaults) if use_sentinel else _direct_schema(defaults)
        return self.async_show_form(
            step_id="sentinel" if use_sentinel else "direct",
            data_schema=schema,
            errors=errors,
        )

    # -- ADR-001 wizard step 2: topology model -----------------------------

    async def async_step_topology(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose Cold or Warm, and how this node learns it is leader.

        ADR-001 keeps these adjacent on purpose: the leadership signal only has
        teeth in the warm model, and the sensible default for each differs.
        """
        if user_input is not None:
            self._data.update(user_input)
            if user_input.get(CONF_TOPOLOGY_MODEL) == TOPOLOGY_WARM:
                return await self.async_step_warm()
            return await self.async_step_domains()

        return self.async_show_form(step_id="topology", data_schema=_topology_schema(self._data))

    # -- ADR-001 wizard step 4: warm-only --------------------------------

    async def async_step_warm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Firewall inputs. Only reached when Warm was chosen."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_domains()

        return self.async_show_form(step_id="warm", data_schema=_warm_schema(self._data))

    # -- wizard step 4a: which entity domains cross ------------------------

    async def async_step_domains(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose what actually fails over.

        Reachable from both routes, exactly like the fileset step after it:
        cold arrives from `async_step_topology`, warm from `async_step_warm`.

        Empty selections are stored as empty and `async_setup_entry` falls back
        to `DEFAULT_INCLUDE_DOMAINS` -- so clearing the field cannot leave a
        node mirroring nothing without saying so.
        """
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_container()

        return self.async_show_form(
            step_id="domains",
            data_schema=_domains_schema(self._data, self.hass),
        )

    # -- wizard step 4b: how Home Assistant is started -------------------

    async def async_step_container(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Whether the promoter uses `docker start` or `docker compose`."""
        if user_input is not None:
            self._data.update({k: v for k, v in user_input.items() if v != ""})
            return await self.async_step_fileset()

        return self.async_show_form(step_id="container", data_schema=_container_schema(self._data))

    # -- wizard step 4c: fileset replication (design 2026-08-29) -----------

    async def async_step_fileset(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Whether to replicate the config fileset, and how.

        Reachable from both routes into it: cold goes straight here from
        `async_step_topology`; warm goes through `async_step_warm` first.
        Both continue on to `async_step_bundle`.

        `crypto.derive_fileset_key` does not validate its input — an empty
        secret still derives a deterministic 32-byte AES key, which anyone can
        reproduce. `async_setup_entry` already refuses to start the publisher
        without a secret, but that only surfaces as a log line after the
        operator believes setup is finished. Refusing here instead — while
        they are still in the wizard, one step from the field that mints the
        secret — is cheaper than a repair issue after the fact.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_FILESET_ENABLED) and not self._data.get(CONF_CLUSTER_SECRET):
                errors["base"] = "fileset_needs_secret"
            else:
                self._data.update(user_input)
                return await self.async_step_bundle()

        return self.async_show_form(
            step_id="fileset",
            data_schema=_fileset_schema(
                self._data,
                await self.hass.async_add_executor_job(
                    _extra_path_candidates,
                    self.hass.config.path(),
                    self._data.get(CONF_FILESET_EXCLUSIONS, DEFAULT_FILESET_EXCLUSIONS),
                ),
            ),
            errors=errors,
        )

    # -- ADR-001 wizard step 5: generate the bundle ------------------------

    async def async_step_bundle(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Write the host bundle and show the operator where it landed.

        The files are written before the menu rather than after, so this can
        list what was actually produced instead of promising what it intends
        to produce.
        """
        if self._bundle_files is None:
            self._bundle_files = await self.hass.async_add_executor_job(
                write_bundle, self.hass.config.path(BUNDLE_DIR_NAME), self._data
            )

        return self.async_show_menu(
            step_id="bundle",
            menu_options=["transfer", "finish"],
            description_placeholders={
                "bundle_path": self.hass.config.path(BUNDLE_DIR_NAME),
                "file_list": "\n".join(f"- {name}" for name in self._bundle_files),
                "model": self._data.get(CONF_TOPOLOGY_MODEL, DEFAULT_TOPOLOGY_MODEL),
                "next_steps": _bundle_next_steps(self._data, self._bundle_files),
            },
        )

    async def async_step_transfer(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask where this host is, so the copy commands can be written out.

        Three questions, and none of their answers is stored. Home Assistant
        writes config entries as unencrypted JSON, so a remembered hostname and
        key path would be two more facts sitting in `.storage` for anyone who
        can read `/config`. They are used to render text and discarded with the
        flow.
        """
        if user_input is not None:
            self._peer = dict(user_input)
            return await self.async_step_transfer_commands()

        return self.async_show_form(
            step_id="transfer",
            data_schema=vol.Schema(
                {
                    vol.Optional("host", default=""): str,
                    vol.Optional("user", default=""): str,
                    vol.Optional("key_path", default=""): str,
                }
            ),
        )

    async def async_step_transfer_commands(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the commands, then return to the menu.

        A form rather than a menu so the operator can read it, copy from it,
        and come back -- the whole point is that they see what they are about
        to run before running it, which is ADR-005's argument applied to our
        own install step.
        """
        if user_input is not None:
            return await self.async_step_bundle()

        return self.async_show_form(
            step_id="transfer_commands",
            data_schema=vol.Schema({}),
            description_placeholders={
                "commands": _transfer_commands(
                    self._data, self.hass.config.path(BUNDLE_DIR_NAME), self._peer
                )
            },
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Create the entry, or update it when reconfiguring."""
        await self.async_set_unique_id(
            f"{self._data[CONF_CLUSTER_NAMESPACE]}@{self._data[CONF_NODE_ID]}"
        )
        if self._reconfigure:
            # `_abort_if_unique_id_configured` would fire against this entry's
            # own id. `async_update_reload_and_abort` checks the id belongs to
            # the entry being reconfigured, which is the check that actually
            # matters: it still refuses a namespace/node_id pair that would
            # collide with the *other* node.
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data=self._data,
                title=f"Cluster Sync ({self._data[CONF_NODE_ID]})",
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Cluster Sync ({self._data[CONF_NODE_ID]})",
            data=self._data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return ClusterStateSyncOptionsFlow(entry)


class ClusterStateSyncOptionsFlow(OptionsFlow):
    """Allow editing tunables without removing the entry."""

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        merged = {**self.entry.data, **self.entry.options}
        use_sentinel = merged.get(CONF_REDIS_USE_SENTINEL, False)
        schema = _sentinel_schema(merged) if use_sentinel else _direct_schema(merged)
        return self.async_show_form(step_id="init", data_schema=schema)


async def _test_backend(data: dict[str, Any]) -> None:
    """Open and close a connection to validate the user's input."""
    backend = RedisBackend(
        namespace=data[CONF_CLUSTER_NAMESPACE],
        host=data.get(CONF_REDIS_HOST),
        port=data.get(CONF_REDIS_PORT, DEFAULT_REDIS_PORT),
        username=data.get(CONF_REDIS_USERNAME) or None,
        password=data.get(CONF_REDIS_PASSWORD) or None,
        db=data.get(CONF_REDIS_DB, DEFAULT_REDIS_DB),
        use_sentinel=data.get(CONF_REDIS_USE_SENTINEL, False),
        sentinel_hosts=parse_sentinel_hosts(data.get(CONF_REDIS_SENTINEL_HOSTS, "")),
        sentinel_service=data.get(CONF_REDIS_SENTINEL_SERVICE),
        use_tls=data.get(CONF_REDIS_USE_TLS, False),
        tls_ca_certs=data.get(CONF_REDIS_TLS_CA_CERTS) or None,
        secret=data.get(CONF_CLUSTER_SECRET) or None,
    )
    try:
        await backend.connect()
    finally:
        await backend.close()
