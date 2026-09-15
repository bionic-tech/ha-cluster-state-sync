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


# -- recorder history continuity (ADR-010) ---------------------------------
#
# Off by default. It writes a whole compacted copy of the history database on
# every tick, which is real disk wear on flash, and a great many installs will
# not care whether graphs survive a failover. Opting in is the honest default.
CONF_RECORDER_SNAPSHOT_ENABLED: Final = "recorder_snapshot_enabled"
DEFAULT_RECORDER_SNAPSHOT_ENABLED: Final = False

#: Minutes between snapshots. The wizard proposes a value from the detected
#: disk (see `storage.py`) rather than a fixed default, because the cost of a
#: short interval is write endurance and that depends entirely on the hardware.
CONF_RECORDER_SNAPSHOT_MINUTES: Final = "recorder_snapshot_minutes"

#: Floor and ceiling for the interval. The floor is not a performance limit --
#: a snapshot takes ~10s on a 2.2 GB database -- it is a wear limit. Five
#: minutes on flash is 455 GB/day, which no default should permit by accident.
MIN_RECORDER_SNAPSHOT_MINUTES: Final = 5
MAX_RECORDER_SNAPSHOT_MINUTES: Final = 1440

#: Does the operator care whether history survives a failover, and where does
#: that history live? Asked EARLY, because the answer restricts which standby
#: models are even offered (ADR-010 §6) -- and a wizard that lets someone build
#: a combination which cannot work has failed them before they start.
CONF_HISTORY_MATTERS: Final = "history_matters"
CONF_HISTORY_DATABASE: Final = "history_database"

#: One database, both nodes. Nothing to replicate; works in cold AND warm.
HISTORY_DB_SHARED: Final = "shared"
#: A SQLite recorder per node. Only cold can carry history across a failover,
#: because swapping the file needs Home Assistant stopped.
HISTORY_DB_DEDICATED: Final = "dedicated"
HISTORY_DATABASES: Final = [HISTORY_DB_SHARED, HISTORY_DB_DEDICATED]

#: Home Assistant's own recorder documentation. Linked rather than paraphrased:
#: their page is the authority on `db_url` and stays current when we do not.
RECORDER_DOCS_URL: Final = "https://www.home-assistant.io/integrations/recorder/"

# --- Alerting (v0.4.2) ----------------------------------------------------
#
# Everything this integration knows has, until now, been visible only to
# someone LOOKING at Home Assistant: 18 diagnostic entities, a panel, and
# repairs. For a product whose premise is "the house keeps working while you
# are away", the observability story ended one step short of reaching the
# person who is away.

#: Conditions that can notify. Two tiers by default, and the operator may move
#: any condition between them -- the defaults are an opinion, not a rule.
#:
#: The line the defaults draw: **push what changes whether the house is
#: protected right now.** Anything that can wait until Saturday is a repair
#: card, not an interruption. An alarm that cries wolf at setup is one nobody
#: believes at 3am, which is the only time it matters.
NOTIFY_PROMOTED: Final = "promoted"
NOTIFY_RECOVERED: Final = "recovered"
NOTIFY_BACKEND_LOST: Final = "backend_lost"
NOTIFY_FILESET_DEGRADED: Final = "fileset_degraded"
NOTIFY_STATISTICS_SCHEMA: Final = "statistics_schema_mismatch"
NOTIFY_STATISTICS_GAP: Final = "statistics_gap"
NOTIFY_STATISTICS_NOT_SEEDED: Final = "statistics_not_seeded"
NOTIFY_STATISTICS_STALLED: Final = "statistics_stalled"
#: AR-0065. The one that has now been silent twice.
NOTIFY_RESTORED_NOTHING: Final = "restored_nothing"
#: Promoted with hardware missing, so the pre-flight switched entries off.
NOTIFY_DEVICES_DISABLED: Final = "devices_disabled"
#: AR-0060. The failover that worked and left nobody a way in.
NOTIFY_INGRESS_UNREACHABLE: Final = "ingress_unreachable"

NOTIFY_CONDITIONS: Final = (
    NOTIFY_PROMOTED,
    NOTIFY_RECOVERED,
    NOTIFY_BACKEND_LOST,
    NOTIFY_FILESET_DEGRADED,
    NOTIFY_INGRESS_UNREACHABLE,
    NOTIFY_STATISTICS_SCHEMA,
    NOTIFY_STATISTICS_GAP,
    NOTIFY_STATISTICS_NOT_SEEDED,
    NOTIFY_STATISTICS_STALLED,
    NOTIFY_RESTORED_NOTHING,
    NOTIFY_DEVICES_DISABLED,
)

#: Pushed unless the operator says otherwise. The house moving machines is
#: included even though nothing is broken: a failover measured at 21 seconds is
#: one you would otherwise learn about from a gap in the logbook, and knowing
#: the house moved matters even when it moved correctly.
DEFAULT_NOTIFY_CONDITIONS: Final = (
    NOTIFY_PROMOTED,
    NOTIFY_RECOVERED,
    NOTIFY_BACKEND_LOST,
    NOTIFY_FILESET_DEGRADED,
    # AR-0065: a promotion that restored nothing is the exact failure this
    # product exists to prevent, and it has now gone unnoticed twice --
    # once as AR-0040, once as AR-0065. If anything earns an interruption
    # it is this.
    NOTIFY_RESTORED_NOTHING,
    # A promotion that came up missing radios changes what the house can DO,
    # which is the tier this list is for. The pre-flight disables them on
    # purpose -- degraded beats not starting -- and doing that quietly is how
    # somebody finds out a fortnight later, when they needed one.
    NOTIFY_DEVICES_DISABLED,
    # AR-0060. Safe to have on by default because it can never fire unless
    # somebody has typed a URL: the probe does not exist otherwise. So this
    # costs a default install nothing, and spares the operator who DID
    # configure a front door a second trip through the options to be told
    # when it stops answering.
    NOTIFY_INGRESS_UNREACHABLE,
)

#: The alert conditions as they shipped before `ingress_unreachable` was added
#: to the defaults. An entry whose choices are exactly this set accepted the
#: shipped default and never revisited it, so adding the newer condition
#: restores an intended default rather than overruling a decision. Frozen: it is
#: a historical fact and must never track DEFAULT_NOTIFY_CONDITIONS.
#:
#: Filed after a real outage on 2026-09-11. The front door was unreachable for
#: 61 minutes; the probe caught it in 105 seconds and raised at ~3 minutes, the
#: card appeared in Home Assistant -- and the push was suppressed, because this
#: estate's entry predated the condition existing. Detection worked perfectly
#: and nobody was told.
LEGACY_DEFAULT_NOTIFY_CONDITIONS: Final[frozenset[str]] = frozenset(
    {
        "promoted",
        "restored_nothing",
        "devices_disabled",
        "recovered",
        "backend_lost",
        "fileset_degraded",
    }
)

CONF_NOTIFY_CONDITIONS: Final = "notify_conditions"
#: `notify.*` service names to call. Empty is the normal case and is not a
#: misconfiguration: a persistent notification always goes to every admin, so
#: zero configuration still tells somebody.
CONF_NOTIFY_SERVICES: Final = "notify_services"
DEFAULT_NOTIFY_SERVICES: Final = ()

# --- Ingress reachability probe (AR-0060) ---------------------------------
#
# A failover on the reference pair succeeded completely -- lease moved, radios
# followed, Home Assistant healthy -- while `home.<domain>` still resolved to
# the dead node and returned 502. Every probe this cluster owned was green,
# because every probe this cluster owned looked inwards. Nothing anywhere asked
# the one question the household actually cares about: can we get in?
#
# The infrastructure half of AR-0060 -- DNS, proxies, tunnels, floating
# addresses -- is the operator's, and `GUIDE-ingress.md` covers it with an
# explicit no-warranty notice. This is the other half, and only the other half:
# the cluster now *looks*, and says what it saw.

#: The address a person actually types. Empty by default, which switches the
#: whole feature off: no entity, no timer, no alert. A probe of a URL nobody
#: supplied would be a green tick for a check that never ran, which is the
#: shape of failure this project keeps finding (AR-0040).
CONF_INGRESS_URL: Final = "ingress_url"
DEFAULT_INGRESS_URL: Final = ""

#: Whether to verify the certificate at that address.
#:
#: On by default, and worth leaving on. It exists because a great many home
#: setups terminate TLS on an internal certificate the container does not
#: trust, and against those a verifying probe is red for ever -- which teaches
#: the operator to switch the feature off, taking the real alarm with it. An
#: unverified probe still answers AR-0060's question ("did anything answer at
#: that address"); it simply cannot tell you *who* answered.
CONF_INGRESS_VERIFY_TLS: Final = "ingress_verify_tls"
DEFAULT_INGRESS_VERIFY_TLS: Final = True

# --- Long-term statistics replication (ADR-010) ---------------------------
#
# Measured on a 3,595-entity estate, 2026-09-08: long-term `statistics` grows
# ~5,500 rows a day (~0.5 MB), while raw `states` churns ~324,000 a day and
# block-replicating it cost 4 GB a day through Valkey. So only statistics
# cross, and they cross as rows rather than as a database file.
CONF_STATISTICS_ENABLED: Final = "statistics_enabled"
DEFAULT_STATISTICS_ENABLED: Final = False

#: How far back each publish reaches. The whole window is republished every
#: pass, so this is also **how long the standby may be offline and still catch
#: up completely**. Measured against the live database: 7 days = 0.99 MB
#: gzipped, 30 days = 4.48 MB, 90 days = 12.51 MB. Thirty days is the default
#: because it covers a holiday with room to spare and still costs under 5 MB in
#: a Valkey the runbook caps at 1 GB.
CONF_STATISTICS_WINDOW_DAYS: Final = "statistics_window_days"
DEFAULT_STATISTICS_WINDOW_DAYS: Final = 30
MIN_STATISTICS_WINDOW_DAYS: Final = 1
MAX_STATISTICS_WINDOW_DAYS: Final = 365

#: Publish cadence. Statistics are compiled by Home Assistant once every five
#: minutes and hourly rows land on the hour, so anything under five minutes
#: republishes an unchanged window. Thirty minutes bounds the loss at half an
#: hour of history -- against a failover budget of 2.5 minutes, that is the
#: cheapest part of the whole design.
CONF_STATISTICS_INTERVAL_MINUTES: Final = "statistics_interval_minutes"
DEFAULT_STATISTICS_INTERVAL_MINUTES: Final = 30
MIN_STATISTICS_INTERVAL_MINUTES: Final = 5
MAX_STATISTICS_INTERVAL_MINUTES: Final = 1440

#: Refuse to publish above this. A window that has grown past the cap means
#: the estate outgrew the setting; publishing a truncated one would look like
#: success. Sized well above the 90-day measurement so it never trips by
#: accident, and low enough that it cannot fill a 1 GB Valkey.
CONF_STATISTICS_MAX_BYTES: Final = "statistics_max_bytes"
DEFAULT_STATISTICS_MAX_BYTES: Final = 64 * 1024 * 1024

#: The standby's accumulating history, in ITS config directory. Deliberately
#: not `home-assistant_v2.db`: on a warm standby that file is open by a live
#: recorder, and this is written by a program running outside Home Assistant.
#: `cluster-fileset-swap.sh` installs it under the real name at promotion.
STATISTICS_DB_NAME: Final = ".cluster_sync_statistics.db"

#: Where an operator drops the one-off seed. Named as an inbox rather than as
#: the store itself so that "the copy is still running" and "the copy
#: finished" are distinguishable states -- a half-copied 484 MB file renamed
#: into place would be a corrupt database that opens.
STATISTICS_SEED_NAME: Final = ".cluster_sync_statistics_seed.db"
CONF_SETTLE_DELAY: Final = "settle_delay"
CONF_LEADERSHIP_ENTITY: Final = "leadership_entity"
CONF_SNAPSHOT_INTERVAL: Final = "snapshot_interval"
CONF_RESTORE_MAX_AGE: Final = "restore_max_age"
CONF_INCLUDE_DOMAINS: Final = "include_domains"
CONF_INCLUDE_ENTITIES: Final = "include_entities"
CONF_EXCLUDE_ENTITIES: Final = "exclude_entities"

#: Devices whose entities never cross, whatever the domain allowlist says.
#:
#: A domain is the wrong unit for "not this thermostat". `climate` is
#: replicated because a promoted node genuinely needs to know the setpoints --
#: but one climate device might be a guest annexe on its own schedule, or a
#: test device, or the one piece of hardware wired to the node that does NOT
#: fail over. Excluding it entity-by-entity means listing every entity it
#: exposes and remembering to come back when a firmware update adds a ninth.
#:
#: Stored as device registry IDs rather than names, because a device rename is
#: an ordinary thing an operator does and must not silently re-enable
#: replication for it.
CONF_EXCLUDE_DEVICES: Final = "exclude_devices"

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
# Config-entry schema version. `ClusterStateSyncConfigFlow.VERSION` and
# `async_migrate_entry` both read this so they cannot drift apart; a test pins
# that they agree. Distinct from SCHEMA_VERSION below, which versions the
# on-the-wire snapshot format, not the config entry.
CONFIG_ENTRY_VERSION: Final = 3

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
#: Domains where writing the state machine is a LIE, so the restore must call
#: the component's own service instead.
#:
#: 🚨 `AutomationEntity.is_on` reads `self._async_detach_triggers is not None or
#: self._is_enabled` -- its OWN state, not `hass.states`. So
#: `hass.states.async_set("automation.x", "off")` shows "off" in the UI and to
#: every template while the triggers stay attached and the automation keeps
#: firing, until the entity next writes its state and silently corrects the
#: display back to "on".
#:
#: That is strictly worse than not replicating the domain at all: it tells an
#: operator something is parked when it is running. Restoring by service is the
#: only honest way to cross a domain whose component owns its own state.
#:
#: Deliberately NOT extended to `switch` or `light`. Their services command
#: real hardware, and a restore that turns things on in someone's house is a
#: much larger decision than a restore that seeds a value.
RESTORE_BY_SERVICE: Final = {
    "automation": ("automation", "turn_on", "turn_off"),
}

SENSITIVE_DOMAINS: Final = frozenset(
    {
        "person",
        "device_tracker",
        "alarm_control_panel",
    }
)

# Default entity-domain allowlist — survives failover well, low double-trigger risk.
# Stateful sensors, modes, vacation/away flags — not transient things like cameras.
#
# `automation` leads the list because it is the only domain here that is applied
# by *calling a service* (`RESTORE_BY_SERVICE`) rather than by writing state, and
# so the only one whose restore actually takes effect rather than being corrected
# by the next device poll. It shipped absent from this list until v0.4.4, which
# meant no fresh install replicated the single most valuable thing we carry —
# `LEGACY_DEFAULT_INCLUDE_DOMAINS` below exists to migrate those installs.
DEFAULT_INCLUDE_DOMAINS: Final = [
    "automation",
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

# The allowlist as it shipped before `automation` was added. An entry whose
# `include_domains` is exactly this set was written by the wizard and never
# touched by its operator, so migrating it is restoring an intended default
# rather than overriding a choice. Anything else is a decision somebody made,
# and the migration leaves it alone. Frozen because it is a historical fact:
# it must never track DEFAULT_INCLUDE_DOMAINS.
LEGACY_DEFAULT_INCLUDE_DOMAINS: Final = frozenset(
    {
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
    }
)

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


def promoter_key(namespace: str, node_id: str) -> str:
    """This node's PROMOTER heartbeat — written by the host-side timer.

    Distinct from `node_key` on purpose, because the two answer different
    questions and one of them cannot be answered by the integration at all.

    `nodes:*` is refreshed by the integration, so a **cold standby never
    appears in it** — its Home Assistant is deliberately stopped, which is why
    `cluster_members` reads 1 on a healthy two-node cold cluster. That is
    correct and useless for the question "is the other machine still able to
    promote?".

    The promoter is the only thing running on a cold standby, so it is the only
    thing that can answer. It writes this key on every tick regardless of
    leadership, which makes the residual custody gap visible: a promoter that
    has been stopped -- by an operator, or by a failed unit -- goes quiet here
    while everything else looks perfectly healthy (ADR-009).
    """
    return f"ha:cluster_state_sync:{namespace}:promoters:{node_id}"


def promoter_key_pattern(namespace: str) -> str:
    """Every promoter's heartbeat, for the scan that finds a quiet one."""
    return f"ha:cluster_state_sync:{namespace}:promoters:*"


#: How long a promoter heartbeat survives without a refresh. The timer ticks
#: every 10s, so this tolerates five missed ticks before the key disappears --
#: long enough that a slow Valkey round trip is not reported as a dead
#: promoter, short enough that a genuinely stopped one is obvious within a
#: minute.
PROMOTER_HEARTBEAT_TTL_SECONDS: Final = 60


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
#: 🚨 The recorder snapshot is excluded by default and must stay excluded.
#:
#: It is a whole compacted copy of the history database -- measured at 1.58 GB
#: against a 512 MB fileset cap, so carrying it would break the go-bag outright.
#: Worse, the go-bag is swapped **synchronously during a promotion**, so a
#: database in it would sit on the RTO critical path (~102s measured) for data
#: that is not needed to bring the house back. It travels by its own slower
#: path instead; see `recorder_snapshot.py`.
DEFAULT_FILESET_EXCLUSIONS: Final = (
    "*.bak-*",
    "core.restore_state",
    ".cluster_sync_recorder.db",
    ".cluster_sync_recorder.writing",
    ".cluster_sync_recorder.tmp",
    # The statistics store and its seed, for the same reason as the three
    # above. Nothing in `_candidates` reaches the config root today -- it
    # walks fixed lists plus what `configuration.yaml` includes -- but an
    # operator who adds the config directory itself to the extra paths would
    # otherwise sweep up a 484 MB seed and a growing store, blow the 512 MB
    # cap, and have the fileset publisher REFUSE every publish from then on.
    # That is AR-0041's shape exactly: a payload that can silently grow.
    ".cluster_sync_statistics.db",
    ".cluster_sync_statistics_seed.db",
    ".cluster_sync_statistics_seed.db.tmp",
)
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

#: Written by `ha_device_preflight.py` when it disables a config entry whose
#: hardware is absent. Lives in `.storage/`, beside the file it edits.
#:
#: 🚨 The pre-flight's whole design is 'degraded beats not starting' -- it
#: switches off entries whose radios are missing so the node comes up. That
#: is right, and on its own it is silent: a promoted node missing three
#: radios looks identical to a healthy one, and somebody finds out a
#: fortnight later when they needed one.
PREFLIGHT_MARKER_NAME: Final = ".cluster_sync_preflight.json"


def statistics_key(namespace: str) -> str:
    """Return the key holding the encrypted long-term statistics window."""
    return f"ha:cluster_state_sync:{namespace}:statistics:window"


def statistics_status_key(namespace: str) -> str:
    """Return the key the FOLLOWER writes its apply result to.

    The one place in this integration where data flows standby -> leader. It
    has to: only the follower can discover a recorder schema mismatch (it is
    the only node that sees both schemas), and in the cold model the follower
    has no Home Assistant to raise a repair on. So it leaves the finding here
    and the leader surfaces it -- otherwise the standby's history quietly
    stops advancing and nobody learns until a promotion.

    Carries no secret and is not sealed: it is a status line, and a follower
    with no cluster key must still be able to say "I could not apply".
    """
    return f"ha:cluster_state_sync:{namespace}:statistics:follower"


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
DATA_ALERTS: Final = "alerts"
#: Entity ids resolved from CONF_EXCLUDE_DEVICES, recomputed when either
#: registry changes. Cached because the filter runs per entity per flush.
DATA_EXCLUDED_IDS: Final = "excluded_ids"
#: AR-0065. False until the boot restore has run (or been positively
#: skipped). The flush refuses to publish while it is False, because a
#: leader that publishes first overwrites the very snapshot it is about to
#: read and restores nothing.
DATA_RESTORE_DONE: Final = "restore_done"

#: 🚨 How long the flush will wait for the restore before publishing anyway.
#:
#: The gate above is opened by `EVENT_HOMEASSISTANT_START`, which always
#: fires on a healthy boot. This exists for the boot that is not healthy:
#: a gate that never opens means the leader never publishes, the standby's
#: snapshot ages out, and the NEXT promotion restores stale state -- which
#: is worse than the bug the gate was added to fix. Generous, because a
#: large estate legitimately takes minutes to finish starting.
RESTORE_GATE_TIMEOUT: Final = 600.0

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
DATA_STATISTICS: Final = "statistics"
# The parsed degraded marker (or None), read once at setup. Fixed at boot,
# not live: the host-side swap writes the marker before the container starts
# and a clean swap removes it, so nothing changes the answer again until the
# next restart. Reading it once here and handing the dict to the diagnostic
# entities means they read a plain attribute rather than re-opening the file
# from a synchronous entity property, which would be blocking I/O the event
# loop cannot afford.
DATA_DEGRADED_MARKER: Final = "degraded_marker"
#: The ingress probe, or None when no URL is configured (AR-0060). Always
#: set, never merely absent: `binary_sensor.py` decides whether to create the
#: entity from this slot, and a missing key and a key holding None read the
#: same to `.get()` and very differently to `[...]`.
DATA_INGRESS: Final = "ingress"

# How often the diagnostic coordinator pings the backend (AR-0019). Frequent
# enough that a dead backend surfaces well inside the failover budget, rare
# enough that it is not itself load.
HEALTH_POLL_INTERVAL: Final = 60

#: Shown as the example address in the ingress step.
#:
#: 🚨 It lives here rather than in `strings.json` because Home Assistant's own
#: validator refuses a URL inside a translated string -- "use description
#: placeholders instead" -- and it is right to: a literal URL cannot be
#: localised, and a translator has no way to know whether it is an example or
#: something to click.
INGRESS_URL_EXAMPLE: Final = "https://home.example.com"
