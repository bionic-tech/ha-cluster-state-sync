"""Constants for the Cluster State Sync integration."""

from __future__ import annotations

import re
from typing import Final

import voluptuous as vol

DOMAIN: Final = "cluster_state_sync"

# Config flow keys
CONF_REDIS_HOST: Final = "redis_host"
CONF_REDIS_PORT: Final = "redis_port"
# ADR-001 layers 3 and 4. Both default OFF: the leadership signal fails closed,
# and a backend blip that merely delays a flush must not also disable every
# automation in the house. Opting in says you have a warm standby or a shared
# recorder and have accepted that trade.
CONF_GATE_RECORDER: Final = "gate_recorder"
CONF_GATE_AUTOMATIONS: Final = "gate_automations"
CONF_REDIS_USERNAME: Final = "redis_username"
CONF_REDIS_PASSWORD: Final = "redis_password"
CONF_REDIS_DB: Final = "redis_db"
CONF_REDIS_USE_SENTINEL: Final = "redis_use_sentinel"
CONF_REDIS_SENTINEL_HOSTS: Final = "redis_sentinel_hosts"
CONF_REDIS_SENTINEL_SERVICE: Final = "redis_sentinel_service"
CONF_CLUSTER_NAMESPACE: Final = "cluster_namespace"
CONF_CLUSTER_SECRET: Final = "cluster_secret"
CONF_REDIS_USE_TLS: Final = "redis_use_tls"
CONF_REDIS_TLS_CA_CERTS: Final = "redis_tls_ca_certs"
CONF_NODE_ID: Final = "node_id"
CONF_ACCEPT_RISK: Final = "accept_risk"
CONF_LEADERSHIP_SOURCE: Final = "leadership_source"
CONF_TOPOLOGY_MODEL: Final = "topology_model"
#: Removed from the wizard 2026-09-04: collected, stored, and read by nothing.
#: The constant stays so existing entries that carry the key can still be read
#: without a KeyError anywhere that enumerates config, and so a future field
#: does not silently reuse the name for a different meaning.
CONF_PEER_HOST: Final = "peer_host"
CONF_HA_CONTAINER: Final = "ha_container"
# AR-0042: where Home Assistant's config directory lives ON THE HOST, which is
# not knowable from inside the container — `/config` in here is a bind mount and
# says nothing about the path outside. Every generated artefact needs it: the
# pre-flight reads `.storage`, and fileset replication (when enabled) pulls and
# swaps it.
CONF_HA_CONFIG_PATH: Final = "ha_config_path"
CONF_IOT_SUBNETS: Final = "iot_subnets"
CONF_BLOCK_DISCOVERY: Final = "block_discovery"
CONF_DOCKER_NETWORK: Final = "docker_network"
CONF_HA_UID: Final = "ha_uid"
CONF_HA_CONTAINER_IP: Final = "ha_container_ip"

# -- how Home Assistant is started and stopped -----------------------------
#
# `docker start` is the default because it is the fastest and least surprising
# thing that can work: it acts on one existing container, needs no project
# file, and cannot be broken by an unrelated service elsewhere in an estate.
#
# Compose mode exists because plenty of installations do not have a container
# anyone starts by hand -- it is one service in a project, possibly behind a
# profile, and `docker start` on a container the project has since recreated
# addresses the wrong thing. Bringing up a whole estate to promote one node is
# not acceptable either, which is why the service name (and profile) are part
# of the configuration rather than assumed.
CONF_HA_START_MODE: Final = "ha_start_mode"
HA_START_DOCKER: Final = "docker"
HA_START_COMPOSE: Final = "compose"
HA_START_MODES: Final = [HA_START_DOCKER, HA_START_COMPOSE]
DEFAULT_HA_START_MODE: Final = HA_START_DOCKER

CONF_COMPOSE_FILE: Final = "compose_file"
CONF_COMPOSE_SERVICE: Final = "compose_service"
CONF_COMPOSE_PROFILE: Final = "compose_profile"
#: Sourced before every compose call. Compose interpolates the WHOLE project
#: file before it filters by profile or service, so one unset variable in an
#: unrelated service aborts the command -- measured on this fleet, where
#: `--profile automation` still failed on a pgbouncer password. A promoter
#: running from systemd has none of the operator's shell environment, so
#: without this compose mode fails at exactly the moment it is needed.
CONF_COMPOSE_ENV_FILE: Final = "compose_env_file"


# -- radio liveness (GOTCHAS §18) ------------------------------------------
#
# The D3 promotion probe asks Home Assistant whether it is alive, and Home
# Assistant can be perfectly alive while every radio behind it is dead. On this
# fleet a Zigbee daemon livelocked -- 78% CPU, no output for 32 minutes, its
# healthcheck reporting `healthy` -- and the whole house lost Zigbee with
# nothing anywhere marking the cluster degraded.
#
# This does NOT gate promotion, deliberately: promoting because a radio died
# would move a house onto a node whose radios may be no better. It makes the
# silence VISIBLE, which is the part that was missing.
#
# Entity globs rather than a fixed list: what proves a radio is receiving is
# installation-specific. On this fleet it is `sensor.*_rssi_numeric`, which
# updates on every packet received; elsewhere it is a link-quality sensor, a
# last-seen timestamp, or a coordinator's own diagnostic.
CONF_RADIO_WATCH: Final = "radio_watch"
CONF_SETTLE_DELAY: Final = "settle_delay"
CONF_LEADERSHIP_ENTITY: Final = "leadership_entity"
CONF_SNAPSHOT_INTERVAL: Final = "snapshot_interval"
CONF_RESTORE_MAX_AGE: Final = "restore_max_age"
CONF_INCLUDE_DOMAINS: Final = "include_domains"
CONF_INCLUDE_ENTITIES: Final = "include_entities"
CONF_EXCLUDE_ENTITIES: Final = "exclude_entities"

# Leadership sources (AR-0017 / ADR-001 wizard step 3). Every gating layer in
# ADR-001 keys off one signal; this is how the integration learns it.
LEADERSHIP_ALWAYS: Final = "always"
LEADERSHIP_ENTITY: Final = "entity"
LEADERSHIP_LEASE: Final = "lease"
LEADERSHIP_SOURCES: Final = [LEADERSHIP_ALWAYS, LEADERSHIP_ENTITY, LEADERSHIP_LEASE]

# `always` keeps existing single-node installs working untouched. Silently
# stopping them from writing would be a worse regression than the dual-writer
# race the other modes guard against.
DEFAULT_LEADERSHIP_SOURCE: Final = LEADERSHIP_ALWAYS

# How long a Valkey leadership lease survives without renewal. Must comfortably
# exceed the snapshot interval (the renewal cadence) or a healthy leader would
# drop its own lease between flushes; short enough that a dead leader's lease
# expires well inside the 2.5-minute failover budget.
LEASE_TTL_SECONDS: Final = 30

# Topology models (ADR-001 wizard step 2).
TOPOLOGY_COLD: Final = "cold"
TOPOLOGY_WARM: Final = "warm"
TOPOLOGY_MODELS: Final = [TOPOLOGY_COLD, TOPOLOGY_WARM]

# Cold is ADR-001's recommended default: the standby's Home Assistant is not
# running, so side-effect suppression is achieved by *not running* rather than
# by getting four gating layers right. It costs cold-boot RTO, which is the
# gating acceptance test against the 2.5-minute budget.
DEFAULT_TOPOLOGY_MODEL: Final = TOPOLOGY_COLD

# Docker network modes (ADR-001 wizard step 4). Determines where the warm
# model's firewall can filter Home Assistant's traffic.
DOCKER_HOST: Final = "host"
DOCKER_MACVLAN: Final = "macvlan"
DOCKER_BRIDGE: Final = "bridge"
DOCKER_ALL: Final = "all"
DOCKER_NETWORK_MODES: Final = [DOCKER_HOST, DOCKER_MACVLAN, DOCKER_BRIDGE, DOCKER_ALL]
DEFAULT_DOCKER_NETWORK: Final = DOCKER_ALL

# Seconds a newly-promoted warm node waits before enabling automations, so the
# restore has landed and integrations have reconnected before anything acts on
# the state. ADR-001 suggests 15.
DEFAULT_SETTLE_DELAY: Final = 15
# The layout the linuxserver/homeassistant compose examples produce. A default
# rather than a guess: it is right often enough to be useful and wrong loudly
# rather than quietly, because a path that does not exist fails on first run.
DEFAULT_HA_CONFIG_PATH: Final = "/opt/homeassistant/config"

# Where the generated host-side bundle is written, under the HA config dir.
BUNDLE_DIR_NAME: Final = "cluster_state_sync_bundle"

# Snapshot wire-format version (AR-0026).
#
# Bump when the shape of a stored entry changes in a way a reader must know
# about. Entries written before versioning are treated as version 1. Landing
# this alongside the AR-0001 rewrite means the AR-0005 signature field can be a
# version bump the reader already understands, rather than a second migration.
SCHEMA_VERSION: Final = 1

# Seconds a shutdown flush may take before we give up and let HA finish dying.
# The standby's snapshot being one interval stale beats blocking teardown.
FINAL_FLUSH_TIMEOUT: Final = 2.0

# Restore bounds (AR-0009).
#
# Both sit far above any legitimate full-state snapshot and exist only to stop
# an oversized or hostile hash from blocking the event loop during the failover
# window — the one moment the loop must stay responsive. Review conflict C1
# resolved in favour of "generous but finite": a real deployment tracking the
# default domains lands in the hundreds, not the thousands.
MAX_RESTORE_ENTRIES: Final = 5000

# Per-entry attribute budget, measured on the serialised attributes. Real
# attribute payloads are tens to hundreds of bytes; a few KB is already unusual.
MAX_ATTRIBUTE_BYTES: Final = 16384

# How far ahead of us a peer's timestamp may legitimately be (AR-0035).
#
# Two nodes sharing a cluster should be NTP-synced to well under a second; a
# minute is generous headroom for honest skew. Anything further ahead is not
# clock drift, so it is refused rather than clamped — clamping a year-3000
# stamp to "now" still leaves it newer than every existing local state, which
# is exactly the guard the finding was about.
CLOCK_SKEW_TOLERANCE: Final = 60

# Fraction of the max-age window past which a restored snapshot is called stale
# (AR-0020). Inside the window is legal, not necessarily healthy: a snapshot at
# 90% of the limit means the peer stopped flushing long before it stopped
# serving, which is worth a warning even though the restore proceeds.
STALE_SNAPSHOT_FRACTION: Final = 0.9

# Defaults — tuned for a 2.5-minute failover budget
DEFAULT_REDIS_PORT: Final = 6379
DEFAULT_SENTINEL_PORT: Final = 26379
DEFAULT_REDIS_DB: Final = 2
DEFAULT_CLUSTER_NAMESPACE: Final = "default"
DEFAULT_SNAPSHOT_INTERVAL: Final = 5  # seconds between flushes
DEFAULT_RESTORE_MAX_AGE: Final = 1800  # don't restore states older than 30m

# Domains that describe where people are or whether the house is defended
# (AR-0004). Mirroring these puts a live copy of the household's movements and
# alarm state on the shared backend, so they are opt-in rather than default:
# a reader of the shared hash learns when the house is empty and unarmed.
#
# Turning them on is a legitimate choice — they are exactly the states you most
# want a promoted standby to know — but it should be a decision, and it should
# be paired with TLS and a dedicated ACL user.
SENSITIVE_DOMAINS: Final = frozenset(
    {
        "person",
        "device_tracker",
        "alarm_control_panel",
    }
)

# Default entity-domain allowlist — survives failover well, low double-trigger risk.
# Stateful sensors, modes, vacation/away flags — not transient things like cameras.
DEFAULT_INCLUDE_DOMAINS: Final = [
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "counter",
    "timer",
    "vacuum",
    "climate",
    "humidifier",
    "water_heater",
]

# AR-0010: the namespace is interpolated straight into the Redis key, so it is
# constrained to a charset that cannot forge a key boundary. A colon is the
# specific danger — `ha:cluster_state_sync:<ns>:states` with a colon in <ns>
# addresses a key outside its own prefix.
#
# The namespace is an organisational label, not a security boundary: anything
# holding the credential can read any namespace. Isolation between tenants is
# the ACL user's job (AR-0008), not this string's.
NAMESPACE_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def validate_namespace(value: str) -> str:
    """Voluptuous-compatible validator for the cluster namespace."""
    if not isinstance(value, str) or not NAMESPACE_PATTERN.match(value):
        raise vol.Invalid(
            "Namespace must be 1-64 characters of lowercase letters, digits, "
            "'_' or '-', and may not start or end with a separator."
        )
    return value


# Redis key layout — single hash per namespace for atomic restore
def states_key(namespace: str) -> str:
    """Return the Redis hash key holding all entity states."""
    return f"ha:cluster_state_sync:{namespace}:states"


def meta_key(namespace: str) -> str:
    """Return the Redis key holding snapshot metadata."""
    return f"ha:cluster_state_sync:{namespace}:meta"


def leader_key(namespace: str) -> str:
    """Return the Redis key holding the active-node lease.

    Reserved for the AR-0017 leader lease, which ADR-001's warm-standby model
    depends on. Kept deliberately: unlike NOISY_EVENTS_TO_IGNORE (deleted in
    Phase 5 as genuinely dead), this one has a named consumer in an accepted
    ADR, and the key layout is part of the on-the-wire contract between nodes.
    """
    return f"ha:cluster_state_sync:{namespace}:leader"


def node_key(namespace: str, node_id: str) -> str:
    """This node's entry in the cluster registry.

    Nothing anywhere enumerated cluster members before this. Leadership was
    always answerable -- one key, one holder -- but "who else is in this
    cluster" had no answer at all, so a node that should not be here (a
    restored backup on a test box pointed at `prod`, most plausibly) joined
    silently and began pulling everyone's `.storage`. A registry does not
    prevent that; it makes it visible, which is the part that was missing.
    """
    return f"ha:cluster_state_sync:{namespace}:nodes:{node_id}"


def node_key_pattern(namespace: str) -> str:
    """Every member's key, for the scan that counts them."""
    return f"ha:cluster_state_sync:{namespace}:nodes:*"


#: How long a member's registry entry survives without being refreshed.
#:
#: Comfortably more than two `HEALTH_POLL_INTERVAL`s, so a node that misses a
#: single poll -- a slow backup, a GC pause -- does not flicker out of the
#: member list and back. Short enough that a node genuinely gone stops being
#: counted within a few minutes rather than lingering as a phantom member and
#: making a two-node cluster read as three.
NODE_REGISTRY_TTL: Final = 180

#: Clock skew above which the restore starts silently refusing entries.
#:
#: Not a threshold anyone chose: it is `CLOCK_SKEW_TOLERANCE` above, the point
#: at which `_restore_from_snapshot` rejects a peer's entries outright as
#: `skipped_future`. Repeated here as a name so the sensor and the guard cannot
#: drift apart -- a warning at a number the code no longer uses would be worse
#: than none.
CLOCK_SKEW_CRITICAL_SECONDS: Final = CLOCK_SKEW_TOLERANCE

#: Where the sensor starts complaining. Deliberately far below the cliff: 60s
#: is where the restore is already fully broken, not where it starts to be, and
#: NTP-disciplined hosts sit in single-digit milliseconds -- so anything near
#: this is already a fault, not a fluctuation.
CLOCK_SKEW_WARN_SECONDS: Final = 10


# -- Fileset replication (design 2026-08-29) --------------------------------

CONF_FILESET_ENABLED: Final = "fileset_enabled"
CONF_FILESET_HOT_INTERVAL: Final = "fileset_hot_interval"
CONF_FILESET_EXCLUSIONS: Final = "fileset_exclusions"

#: Extra paths the operator adds by hand, relative to the config directory.
#:
#: The include scan finds everything `configuration.yaml` *references*. It
#: cannot see what an integration opens by path at runtime -- `python_scripts/`,
#: `custom_templates/`, ZHA's `zigbee.db`, `known_devices.yaml` -- because
#: nothing in the YAML mentions them. This is where those go.
CONF_FILESET_EXTRA_PATHS: Final = "fileset_extra_paths"

#: Free-text paths, for anything the picker did not offer -- a nested directory
#: like `configs/private`, most likely. Separate from the tick list above
#: because a single field cannot be both: Home Assistant renders a multi-select
#: with `custom_value` as a type-to-add chip box, losing the checkboxes that
#: make a list of found candidates usable.
CONF_FILESET_EXTRA_CUSTOM: Final = "fileset_extra_custom"
DEFAULT_FILESET_EXTRA_CUSTOM: Final[tuple[str, ...]] = ()

#: Empty. Guessing on the operator's behalf is how the go-bag ended up carrying
#: a fixed list that did not match anyone's configuration; the wizard offers
#: candidates instead, and the operator chooses.
DEFAULT_FILESET_EXTRA_PATHS: Final[tuple[str, ...]] = ()

#: Offered in the wizard when present in the config directory. Not replicated
#: unless ticked -- a suggestion, not a default. Drawn from what integrations
#: are known to read by path, which is exactly the set a parser cannot find.
FILESET_EXTRA_CANDIDATES: Final[tuple[str, ...]] = (
    "python_scripts",
    "custom_templates",
    "zigbee.db",
    "known_devices.yaml",
    "ip_bans.yaml",
    "ui-lovelace.yaml",
    "shell_scripts",
    "templates",
)
CONF_FILESET_MAX_BYTES: Final = "fileset_max_bytes"
CONF_FILESET_STALE_AFTER: Final = "fileset_stale_after"

# Off by default. This is a new capability in an alpha integration, and it
# reads every credential in the config directory — the operator opts in.
DEFAULT_FILESET_ENABLED: Final = False
DEFAULT_FILESET_HOT_INTERVAL: Final = 60
# Measured on node-a 2026-08-29. `*.bak-*` is 81 MB of nine hand-made
# entity-registry copies. `core.restore_state` belongs to tier 2 — replicating
# it would have the file fighting the Valkey restore over the same entities.
DEFAULT_FILESET_EXCLUSIONS: Final = ("*.bak-*", "core.restore_state")
# Tier 1 measured 268 MB gross, ~187 MB after the .bak- exclusion. The cap is
# the AR-0041 lesson: a payload that can silently grow is how you ship 92.5 GB
# to copy 108 MB.
DEFAULT_FILESET_MAX_BYTES: Final = 512 * 1024 * 1024
DEFAULT_FILESET_STALE_AFTER: Final = 1800

#: Staging lives under the config dir, dot-prefixed so Home Assistant never
#: walks it looking for integrations, and on the same filesystem as `/config`
#: so the promotion swap is a rename().
STAGED_DIR_NAME: Final = ".cluster_sync_staged"
#: Written by the host-side swap script before the container starts; read by
#: the integration at startup to raise a repair issue.
DEGRADED_MARKER_NAME: Final = ".cluster_sync_degraded.json"


def fileset_manifest_key(namespace: str) -> str:
    """Return the key holding the encrypted fileset manifest."""
    return f"ha:cluster_state_sync:{namespace}:fileset:manifest"


def fileset_blob_key(namespace: str, ref: str) -> str:
    """Return the key holding one encrypted file body."""
    return f"ha:cluster_state_sync:{namespace}:fileset:blob:{ref}"


# Internal hass.data slots
DATA_BACKEND: Final = "backend"
DATA_UNSUB: Final = "unsub"
DATA_MIRROR: Final = "mirror"
DATA_CONFIG: Final = "config"
DATA_STATS: Final = "stats"
DATA_COORDINATOR: Final = "coordinator"
DATA_CLUSTER_VIEW: Final = "cluster_view"

#: Services. Deliberately only two, and neither of them touches leadership:
#: the one operator action that bypasses the split-brain guard is
#: `force-master`, and that stays a host-side file you have to be on the box to
#: create. A web button for it would be the easiest possible way to end up with
#: two leaders.
SERVICE_FLUSH_SNAPSHOT: Final = "flush_snapshot"
SERVICE_CLEAR_DEGRADED: Final = "clear_degraded"
DATA_LEADERSHIP: Final = "leadership"
DATA_GATE: Final = "gate"
DATA_FILESET: Final = "fileset"
# The parsed degraded marker (or None), read once at setup. Fixed at boot,
# not live: the host-side swap writes the marker before the container starts
# and a clean swap removes it, so nothing changes the answer again until the
# next restart. Reading it once here and handing the dict to the diagnostic
# entities means they read a plain attribute rather than re-opening the file
# from a synchronous entity property, which would be blocking I/O the event
# loop cannot afford.
DATA_DEGRADED_MARKER: Final = "degraded_marker"

# How often the diagnostic coordinator pings the backend (AR-0019). Frequent
# enough that a dead backend surfaces well inside the failover budget, rare
# enough that it is not itself load.
HEALTH_POLL_INTERVAL: Final = 60
