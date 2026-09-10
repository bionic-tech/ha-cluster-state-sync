"""Cluster State Sync — shared state mirror for active-passive Home Assistant.

This integration writes a periodic snapshot of selected entity states to a
shared backend (Redis/Valkey today; recorder/Postgres planned). On startup,
each HA instance reads the snapshot and seeds its in-memory state machine,
so a freshly-promoted standby node doesn't act on stale assumptions during
the failover window.

Design constraints — read these before changing anything:

* Uses only public HA APIs. No monkey-patching of core.
* Backend failures never raise into HA — they log and continue.
* Snapshot frequency is bounded; a busy mesh can't DoS the backend.
* On restore at boot, the peer's snapshot is the authority for the entities
  we track. It used to say the opposite — that local state wins when fresher —
  and that turned out to mean the restore did nothing at all, because Home
  Assistant stamps everything it replays from disk with the boot time. See
  AR-0040 in `_restore_from_snapshot`. Off the boot path, local state does
  still win, because there the timestamps mean something.
* Automation double-firing during failover is *not* solved here. That
  needs leader election, which is a v0.2 problem.


Copyright (C) 2026 Maurice Manning.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file
and sections 15 and 16 of that licence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import logging
import pathlib
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    Platform,
)
from homeassistant.core import (
    Context,
    CoreState,
    Event,
    HomeAssistant,
    ServiceCall,
    State,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import recorder_snapshot, storage
from .alerts import AlertRouter
from .area import async_assign_area
from .backend import ClusterBackend, RedisBackend, SnapshotEntry, default_node_id
from .const import (
    CLOCK_SKEW_TOLERANCE,
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_FILESET_ENABLED,
    CONF_FILESET_EXCLUSIONS,
    CONF_FILESET_EXTRA_CUSTOM,
    CONF_FILESET_EXTRA_PATHS,
    CONF_FILESET_HOT_INTERVAL,
    CONF_FILESET_MAX_BYTES,
    CONF_GATE_AUTOMATIONS,
    CONF_GATE_RECORDER,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_INGRESS_URL,
    CONF_INGRESS_VERIFY_TLS,
    CONF_LEADERSHIP_ENTITY,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_NOTIFY_CONDITIONS,
    CONF_NOTIFY_SERVICES,
    CONF_RECORDER_SNAPSHOT_ENABLED,
    CONF_RECORDER_SNAPSHOT_MINUTES,
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
    CONF_SNAPSHOT_INTERVAL,
    CONF_STATISTICS_ENABLED,
    CONF_STATISTICS_INTERVAL_MINUTES,
    CONF_STATISTICS_MAX_BYTES,
    CONF_STATISTICS_WINDOW_DAYS,
    CONFIG_ENTRY_VERSION,
    DATA_ALERTS,
    DATA_BACKEND,
    DATA_CLUSTER_VIEW,
    DATA_CONFIG,
    DATA_COORDINATOR,
    DATA_DEGRADED_MARKER,
    DATA_EXCLUDED_IDS,
    DATA_FILESET,
    DATA_GATE,
    DATA_INGRESS,
    DATA_LEADERSHIP,
    DATA_MIRROR,
    DATA_RESTORE_DONE,
    DATA_STATISTICS,
    DATA_STATS,
    DATA_UNSUB,
    DEFAULT_CLUSTER_NAMESPACE,
    DEFAULT_FILESET_ENABLED,
    DEFAULT_FILESET_EXCLUSIONS,
    DEFAULT_FILESET_EXTRA_CUSTOM,
    DEFAULT_FILESET_EXTRA_PATHS,
    DEFAULT_FILESET_HOT_INTERVAL,
    DEFAULT_FILESET_MAX_BYTES,
    DEFAULT_INCLUDE_DOMAINS,
    DEFAULT_INGRESS_URL,
    DEFAULT_INGRESS_VERIFY_TLS,
    DEFAULT_LEADERSHIP_SOURCE,
    DEFAULT_NOTIFY_CONDITIONS,
    DEFAULT_NOTIFY_SERVICES,
    DEFAULT_RECORDER_SNAPSHOT_ENABLED,
    DEFAULT_RESTORE_MAX_AGE,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_STATISTICS_ENABLED,
    DEFAULT_STATISTICS_INTERVAL_MINUTES,
    DEFAULT_STATISTICS_MAX_BYTES,
    DEFAULT_STATISTICS_WINDOW_DAYS,
    DEGRADED_MARKER_NAME,
    DOMAIN,
    FINAL_FLUSH_TIMEOUT,
    LEGACY_DEFAULT_INCLUDE_DOMAINS,
    MAX_ATTRIBUTE_BYTES,
    MAX_RECORDER_SNAPSHOT_MINUTES,
    MAX_RESTORE_ENTRIES,
    MAX_STATISTICS_INTERVAL_MINUTES,
    MAX_STATISTICS_WINDOW_DAYS,
    MIN_RECORDER_SNAPSHOT_MINUTES,
    MIN_STATISTICS_INTERVAL_MINUTES,
    MIN_STATISTICS_WINDOW_DAYS,
    NOTIFY_BACKEND_LOST,
    NOTIFY_DEVICES_DISABLED,
    NOTIFY_FILESET_DEGRADED,
    NOTIFY_INGRESS_UNREACHABLE,
    NOTIFY_RESTORED_NOTHING,
    NOTIFY_STATISTICS_GAP,
    NOTIFY_STATISTICS_NOT_SEEDED,
    NOTIFY_STATISTICS_SCHEMA,
    NOTIFY_STATISTICS_STALLED,
    PREFLIGHT_MARKER_NAME,
    RESTORE_BY_SERVICE,
    RESTORE_GATE_TIMEOUT,
    SENSITIVE_DOMAINS,
    SERVICE_CLEAR_DEGRADED,
    SERVICE_FLUSH_SNAPSHOT,
    STALE_SNAPSHOT_FRACTION,
)
from .coordinator import BackendHealthCoordinator, ClusterViewCoordinator, SyncStats
from .crypto import (
    STATISTICS_STATUS_AAD,
    FilesetCryptoError,
    derive_fileset_key,
    open_sealed,
)
from .fileset import FilesetPublisher
from .gating import ServiceGate
from .hold import read_hold
from .ingress import (
    INGRESS_FAILURES_BEFORE_ALARM,
    INGRESS_PROBE_INTERVAL,
    IngressProbe,
    InvalidIngressURL,
    validate_ingress_url,
)
from .leadership import LeadershipMonitor
from .panel import async_register_panel
from .scope import resolve_device_entities
from .statistics_publisher import StatisticsPublisher
from .util import parse_sentinel_hosts

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once, before any entry is set up.

    Quality-scale rule `action-setup`, and it is a real bug rather than
    paperwork. Registered from `async_setup_entry`, the services disappear the
    moment the entry unloads -- so an automation calling
    `cluster_state_sync.flush_snapshot` fails with "unknown service" instead of
    something that names the actual problem. Registered here they always exist,
    and calling one with nothing loaded raises an error that says so.
    """
    await _async_register_services(hass)
    return True


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register the two operator actions, once per Home Assistant.

    Both iterate every loaded entry rather than closing over one. A second entry
    is unusual but not forbidden, and a service that silently acted on whichever
    one happened to be set up first would be a genuinely nasty surprise.
    """
    if hass.services.has_service(DOMAIN, SERVICE_FLUSH_SNAPSHOT):
        return

    async def _flush_snapshot(_call: ServiceCall) -> None:
        """Write the snapshot now, if this node is entitled to.

        Leader-gated, exactly as the scheduled flush is. A follower writing is
        the split-brain this integration exists to prevent, and being asked
        nicely by an operator does not make it safe -- the peer would have its
        identity overwritten by a node that does not hold the lease.
        """
        acted = False
        for loaded in hass.config_entries.async_loaded_entries(DOMAIN):
            runtime = getattr(loaded, "runtime_data", None) or {}
            leadership = runtime.get(DATA_LEADERSHIP)
            mirror = runtime.get(DATA_MIRROR)
            if leadership is None or mirror is None:
                continue
            if not await leadership.async_is_leader():
                raise HomeAssistantError(
                    "Refusing to flush: this node does not hold cluster leadership. "
                    "Flushing from a follower would overwrite the leader's snapshot."
                )
            await mirror.async_flush()
            acted = True
        if not acted:
            raise HomeAssistantError("No configured Cluster State Sync entry is set up.")

    async def _clear_degraded(_call: ServiceCall) -> None:
        """Acknowledge the degraded go-bag marker.

        Clears the marker file and the repair issue it raised. It does not fix
        anything -- the next promotion is what proves the go-bag is healthy
        again -- so this is an acknowledgement, not a repair, and the marker
        comes straight back if the underlying condition persists.
        """
        marker = pathlib.Path(hass.config.path(DEGRADED_MARKER_NAME))

        def _unlink() -> None:
            marker.unlink(missing_ok=True)

        await hass.async_add_executor_job(_unlink)
        for loaded in hass.config_entries.async_loaded_entries(DOMAIN):
            runtime = getattr(loaded, "runtime_data", None) or {}
            runtime[DATA_DEGRADED_MARKER] = None
            router: AlertRouter | None = runtime.get(DATA_ALERTS)
            if router is not None:
                # An acknowledgement, not a repair -- so no all-clear is pushed.
                # Telling somebody their alarm has cleared when all that happened
                # is that they dismissed it is how a channel stops being trusted.
                router.forget(NOTIFY_FILESET_DEGRADED)
        ir.async_delete_issue(hass, DOMAIN, "fileset_degraded")

    hass.services.async_register(DOMAIN, SERVICE_FLUSH_SNAPSHOT, _flush_snapshot)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_DEGRADED, _clear_degraded)


async def _surface_follower_status(
    hass: HomeAssistant,
    backend: Any,
    secret: str | None = None,
    *,
    node_id: str | None = None,
    stale_after: float | None = None,
    publishing_since: datetime | None = None,
    alerts: AlertRouter | None = None,
) -> None:
    """Raise the standby's findings as repairs on this node.

    The standby has no logbook, no repairs panel and no entities -- in the cold
    model its Home Assistant is stopped. Everything it discovers would die on a
    host nobody reads, so it leaves a sealed status line in Valkey and this
    raises the alarm on its behalf.

    Every issue is created OR deleted on every pass, so a fixed standby clears
    its own alarm without anyone reloading anything: the issue registry is
    storage-backed and survives restarts, so creating without deleting would
    leave a recovered cluster carrying an ERROR forever.
    """
    mapping = {
        "schema_mismatch": (
            "statistics_schema_mismatch",
            ir.IssueSeverity.ERROR,
            NOTIFY_STATISTICS_SCHEMA,
            "Statistics replication has stopped",
        ),
        "gap": (
            "statistics_gap",
            ir.IssueSeverity.WARNING,
            NOTIFY_STATISTICS_GAP,
            "Statistics history has a gap",
        ),
        "not_seeded": (
            "statistics_not_seeded",
            ir.IssueSeverity.WARNING,
            NOTIFY_STATISTICS_NOT_SEEDED,
            "Statistics have never been seeded",
        ),
    }
    status = await _read_follower_status(backend, secret)
    state = str((status or {}).get("state") or "")

    for reported, (issue_id, severity, condition, title) in mapping.items():
        detail = str((status or {}).get("detail", "no detail given"))
        if state == reported:
            if alerts is not None:
                await alerts.async_raise(condition, title, detail)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=severity,
                translation_key=issue_id,
                translation_placeholders={
                    "detail": str((status or {}).get("detail", "no detail given"))
                },
            )
        else:
            # Includes the no-status case: a standby that has said nothing at
            # all is not a standby with a schema mismatch, and leaving the
            # issue up would train the operator to ignore it.
            if alerts is not None:
                await alerts.async_clear(
                    condition, f"Resolved: {title.lower()}", "The standby is reporting healthy."
                )
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    # AR-0046. The above is why a silent follower raises nothing -- and on its
    # own that made a follower whose replication has DIED indistinguishable
    # from a healthy one. The status key expires, the issues are deleted, and
    # the quiet state is the good state.
    #
    # 🚨 That is AR-0040's shape, in a mechanism built after AR-0040: a thing
    # reporting success by saying nothing. So silence is now measured.
    if stale_after is None:
        await _clear(hass, "statistics_stalled", alerts)
        return

    # AR-0057. Silence only means something once there has been something to
    # be silent about. On the pass that first switches this on, no follower has
    # had a window to fetch yet -- raising "replication has stalled" there
    # accuses a correct setup of a fault, on the one day its owner is least
    # able to tell the difference. An alarm that cries wolf at installation is
    # an alarm nobody believes at 3am.
    if (
        publishing_since is None
        or (datetime.now(tz=UTC) - publishing_since).total_seconds() < stale_after
    ):
        await _clear(hass, "statistics_stalled", alerts)
        return

    stale = _status_is_stale(status, stale_after)
    if not stale:
        await _clear(hass, "statistics_stalled", alerts)
        return

    # A stopped node and a stopped replication need opposite things from an
    # operator, and only the promoter heartbeat separates them: it is written
    # by a host-side timer on a node whose Home Assistant may be off, which is
    # exactly the case in question.
    beating = await backend.read_promoter_nodes()
    peers = {n for n in beating if n != node_id}
    if peers:
        detail = (
            f"the standby ({', '.join(sorted(peers))}) is up — its promoter is beating — "
            "but it has not reported applying statistics recently. Its history has "
            "stopped advancing. Check cluster-statistics-pull.timer on that host."
        )
    else:
        detail = (
            "no standby has reported, and no peer promoter heartbeat is present either, "
            "so the standby is most likely switched off. History replication is not "
            "running; nothing else is implied about the cluster."
        )
    if alerts is not None:
        await alerts.async_raise(
            NOTIFY_STATISTICS_STALLED, "Statistics replication has stalled", detail
        )
    ir.async_create_issue(
        hass,
        DOMAIN,
        "statistics_stalled",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="statistics_stalled",
        translation_placeholders={"detail": detail},
    )


async def _clear(hass: HomeAssistant, issue_id: str, alerts: AlertRouter | None = None) -> None:
    """Deleting an issue that does not exist is a documented no-op."""
    if alerts is not None:
        await alerts.async_clear(
            NOTIFY_STATISTICS_STALLED,
            "Resolved: statistics replication",
            "The standby is applying statistics again.",
        )
    ir.async_delete_issue(hass, DOMAIN, issue_id)


def _status_is_stale(status: dict[str, Any] | None, stale_after: float) -> bool:
    """Has the follower gone quiet for longer than it should have?

    A missing status counts as stale: the key carries a TTL, so its absence is
    itself the signal that nothing has reported for a day.
    """
    if not status:
        return True
    raw = status.get("ts")
    if not isinstance(raw, str):
        return True
    try:
        reported = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if reported.tzinfo is None:
        reported = reported.replace(tzinfo=UTC)
    return (datetime.now(tz=UTC) - reported).total_seconds() > stale_after


async def _read_follower_status(backend: Any, secret: str | None) -> dict[str, Any] | None:
    """Open the follower's sealed status line (AR-0045).

    Returns None for every failure -- absent, unopenable, or not an object.
    An unopenable status is NOT reported as a follower problem: it was written
    by something without the cluster key, which makes it noise rather than
    news, and turning noise into a repair is how operators learn to ignore the
    panel.
    """
    sealed = await backend.read_statistics_status()
    if sealed is None or not secret:
        return None
    try:
        plain = open_sealed(derive_fileset_key(secret), sealed, aad=STATISTICS_STATUS_AAD)
        status = json.loads(plain.decode("utf-8"))
    except (FilesetCryptoError, ValueError, UnicodeDecodeError):
        _LOGGER.warning(
            "The statistics status in Valkey did not authenticate; ignoring it. "
            "Something without the cluster key wrote to that key."
        )
        return None
    return status if isinstance(status, dict) else None


async def _setup_recorder_snapshots(
    hass: HomeAssistant,
    cfg: dict[str, Any],
    runtime: dict[str, Any],
    leadership: Any,
) -> None:
    """Schedule the leader's consistent recorder copies, if opted in.

    Extracted from `async_setup_entry` (AR-0050) without changing what it
    does: the same opt-in guard, the same clamp, the same interval, the
    same executor, in the same place in the sequence.
    """
    # 3a-bis. Recorder history continuity (ADR-010). Leader-only, for the same
    #         reason the flush is: only the leader's history is the house's
    #         history, and a follower snapshotting its own neutered instance
    #         would ship noise. Off unless the operator opted in.
    if cfg.get(CONF_RECORDER_SNAPSHOT_ENABLED, DEFAULT_RECORDER_SNAPSHOT_ENABLED):
        snap_minutes = int(
            cfg.get(CONF_RECORDER_SNAPSHOT_MINUTES)
            or storage.inspect_path(hass.config.path()).default_minutes
        )
        snap_minutes = max(
            MIN_RECORDER_SNAPSHOT_MINUTES, min(MAX_RECORDER_SNAPSHOT_MINUTES, snap_minutes)
        )

        async def _scheduled_recorder_snapshot(_now: datetime) -> None:
            if not await leadership.async_is_leader():
                return
            # `VACUUM INTO` took ~10s on a 2.2 GB database. That must not sit
            # on the event loop, so it goes to an executor like every other
            # blocking call in this integration.
            result = await hass.async_add_executor_job(
                recorder_snapshot.take_snapshot, hass.config.path()
            )
            stats = runtime.get(DATA_STATS)
            if stats is not None:
                stats.record_recorder_snapshot(result)
            if not result.ok and result.error:
                # A shared-database install has no SQLite file, which is
                # ordinary rather than broken -- debug, not error, so a valid
                # configuration does not log a failure every interval.
                _LOGGER.debug("Recorder snapshot skipped: %s", result.error)

        runtime[DATA_UNSUB].append(
            async_track_time_interval(
                hass,
                _scheduled_recorder_snapshot,
                timedelta(minutes=snap_minutes),
                name=f"{DOMAIN}_recorder_snapshot",
                cancel_on_shutdown=True,
            )
        )
        _LOGGER.info(
            "Recorder history continuity on: a consistent copy every %d minutes (%s)",
            snap_minutes,
            storage.inspect_path(hass.config.path()).describe(),
        )


async def _setup_fileset_replication(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cfg: dict[str, Any],
    runtime: dict[str, Any],
    backend: Any,
    leadership: Any,
    node_id: str,
) -> None:
    """Publish the config go-bag, if opted in. Leader-only.

    Extracted from `async_setup_entry` (AR-0050). Sets `runtime[DATA_FILESET]`
    itself, exactly as the inline block did, including to None when the
    feature is off — a caller that forgot that assignment would leave the
    diagnostics reading a key that never existed.
    """
    # 3b. Fileset replication (design 2026-08-29). Off unless configured: it
    #     reads every credential in the config directory, so the operator opts
    #     in. Leader-only for the same reason the flush is — a follower
    #     publishing would overwrite the leader's identity with its own.
    publisher: FilesetPublisher | None = None
    if cfg.get(CONF_FILESET_ENABLED, DEFAULT_FILESET_ENABLED):
        secret = cfg.get(CONF_CLUSTER_SECRET)
        if not secret:
            # Without a secret there is no key, and an unsealed `.storage` in
            # shared Valkey is not a degraded mode worth offering.
            _LOGGER.error(
                "Fileset replication is enabled but no cluster secret is set; "
                "not publishing. Reconfigure the integration to mint one."
            )
        else:
            publisher = FilesetPublisher(
                backend,
                config_dir=hass.config.path(),
                node_id=node_id,
                secret=secret,
                exclusions=cfg.get(CONF_FILESET_EXCLUSIONS, DEFAULT_FILESET_EXCLUSIONS),
                extra_paths=(
                    *cfg.get(CONF_FILESET_EXTRA_PATHS, DEFAULT_FILESET_EXTRA_PATHS),
                    *cfg.get(CONF_FILESET_EXTRA_CUSTOM, DEFAULT_FILESET_EXTRA_CUSTOM),
                ),
                max_bytes=cfg.get(CONF_FILESET_MAX_BYTES, DEFAULT_FILESET_MAX_BYTES),
            )

            async def _scheduled_fileset(_now: datetime) -> None:
                if not await leadership.async_is_leader():
                    return
                try:
                    await publisher.async_publish()
                # 🚨 This does NOT keep the timer alive, though the comment here
                # said so until 2026-09-10. `_TrackTimeInterval` calls
                # `_schedule_timer()` BEFORE running the job, and runs it as a
                # background task — so an escaping exception never reaches the
                # timer and the interval survives regardless.
                #
                # What it actually buys is a NAMED failure. Without it the error
                # surfaces as an anonymous unretrieved-task traceback whenever the
                # garbage collector gets to it, detached from the thing that
                # caused it. That is the difference between a diagnosable fault
                # and a mystery in the log an hour later.
                except Exception:  # noqa: BLE001 — name the failure, do not swallow it
                    _LOGGER.exception("Fileset publish failed; will retry next interval")

            runtime[DATA_UNSUB].append(
                async_track_time_interval(
                    hass,
                    _scheduled_fileset,
                    timedelta(
                        seconds=cfg.get(CONF_FILESET_HOT_INTERVAL, DEFAULT_FILESET_HOT_INTERVAL)
                    ),
                    name=f"{DOMAIN}_fileset_publish",
                    cancel_on_shutdown=True,
                )
            )

    runtime[DATA_FILESET] = publisher


async def _setup_statistics_replication(
    hass: HomeAssistant,
    cfg: dict[str, Any],
    runtime: dict[str, Any],
    backend: Any,
    leadership: Any,
    node_id: str,
) -> None:
    """Publish the long-term statistics window, if opted in. Leader-only.

    Extracted from `async_setup_entry` (AR-0050). Sets
    `runtime[DATA_STATISTICS]` itself, on both paths.
    """
    # 3b-ii. Long-term statistics replication (ADR-010). Rides on the fileset's
    #        key and secret, and is separately switchable: an estate on a shared
    #        Postgres recorder wants the go-bag and not this.
    #
    #        Only `statistics` crosses. Measured on this estate: long-term
    #        statistics grow ~5,500 rows a day, raw `states` churns ~324,000,
    #        and block-replicating the latter cost 4 GB a day through Valkey.
    #        So the years of energy and climate history survive a failover and
    #        the recent logbook does not -- stated plainly rather than implied.
    statistics: StatisticsPublisher | None = None
    if cfg.get(CONF_STATISTICS_ENABLED, DEFAULT_STATISTICS_ENABLED):
        secret = cfg.get(CONF_CLUSTER_SECRET)
        if not secret:
            _LOGGER.error(
                "Statistics replication is enabled but no cluster secret is set; "
                "not publishing. Reconfigure the integration to mint one."
            )
        else:
            window_days = max(
                MIN_STATISTICS_WINDOW_DAYS,
                min(
                    MAX_STATISTICS_WINDOW_DAYS,
                    int(cfg.get(CONF_STATISTICS_WINDOW_DAYS) or DEFAULT_STATISTICS_WINDOW_DAYS),
                ),
            )
            statistics = StatisticsPublisher(
                backend,
                config_dir=hass.config.path(),
                secret=secret,
                window_days=window_days,
                max_bytes=cfg.get(CONF_STATISTICS_MAX_BYTES, DEFAULT_STATISTICS_MAX_BYTES),
            )

            async def _scheduled_statistics(_now: datetime) -> None:
                if not await leadership.async_is_leader():
                    return
                try:
                    await statistics.async_publish()
                # 🚨 This does NOT keep the timer alive, though the comment here
                # said so until 2026-09-10. `_TrackTimeInterval` calls
                # `_schedule_timer()` BEFORE running the job, and runs it as a
                # background task — so an escaping exception never reaches the
                # timer and the interval survives regardless.
                #
                # What it actually buys is a NAMED failure. Without it the error
                # surfaces as an anonymous unretrieved-task traceback whenever the
                # garbage collector gets to it, detached from the thing that
                # caused it. That is the difference between a diagnosable fault
                # and a mystery in the log an hour later.
                except Exception:  # noqa: BLE001 — name the failure, do not swallow it
                    _LOGGER.exception("Statistics publish failed; will retry next interval")
                # Then read what the standby made of the LAST window. This is
                # the only place in this integration where data flows standby
                # to leader, and it has to exist: in the cold model the standby
                # has no Home Assistant, so a schema mismatch or a gap it
                # discovers has no logbook, no repairs panel and no entity to
                # land on. It leaves a status line in Valkey; this raises the
                # alarm on its behalf. Without it the standby's history stops
                # advancing and nobody learns until a promotion -- the AR-0040
                # shape exactly.
                await _surface_follower_status(
                    hass,
                    backend,
                    secret,
                    node_id=node_id,
                    # Two publish intervals plus a margin: one missed pass
                    # is a timer that drifted, three is a mechanism that
                    # has stopped.
                    stale_after=interval * 60 * 3,
                    publishing_since=statistics.first_success_at,
                    alerts=runtime.get(DATA_ALERTS),
                )

            interval = max(
                MIN_STATISTICS_INTERVAL_MINUTES,
                min(
                    MAX_STATISTICS_INTERVAL_MINUTES,
                    int(
                        cfg.get(CONF_STATISTICS_INTERVAL_MINUTES)
                        or DEFAULT_STATISTICS_INTERVAL_MINUTES
                    ),
                ),
            )
            runtime[DATA_UNSUB].append(
                async_track_time_interval(
                    hass,
                    _scheduled_statistics,
                    timedelta(minutes=interval),
                    name=f"{DOMAIN}_statistics_publish",
                    cancel_on_shutdown=True,
                )
            )
            _LOGGER.info(
                "Long-term statistics replication on: a %d-day window every %d minutes. "
                "Raw states history does NOT cross — see ADR-010.",
                window_days,
                interval,
            )

    runtime[DATA_STATISTICS] = statistics


async def _setup_degraded_alarm(
    hass: HomeAssistant,
    runtime: dict[str, Any],
) -> None:
    """Raise, or clear, the degraded-go-bag repair for this boot.

    Extracted from `async_setup_entry` (AR-0050). Sets
    `runtime[DATA_DEGRADED_MARKER]` itself.

    🚨 It both creates AND deletes the issue. The registry is storage-backed,
    so creating without deleting would leave a fully recovered node carrying
    an ERROR for ever.
    """
    # 3c. The degraded-fileset alarm. Decision D4 has the standby promote on a
    #     stale or missing go-bag rather than refuse to start, which makes
    #     this the only safety net: every HTTP health check on a node in that
    #     state is green anyway. `cluster-fileset-swap.sh` writes this marker
    #     on the host, before the container starts, on every degraded path,
    #     and removes it on a clean swap — so its mere presence is the signal,
    #     and it does not go away on its own.
    #
    #     AR-0040 is why this cannot be a log line: a defect of exactly this
    #     shape — a promotion that quietly did not do its job — lived in this
    #     project for its entire life, and its only symptom was an INFO line
    #     nobody read. This goes to the repairs panel instead.
    marker_path = hass.config.path(DEGRADED_MARKER_NAME)

    def _read_degraded_marker() -> dict[str, Any] | None:
        try:
            with open(marker_path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            # The normal case on a healthy node, and the only one that is not
            # worth a word.
            return None
        except (OSError, ValueError):
            # A marker that exists but cannot be read is NOT the same as no
            # marker, and returning None silently makes them identical. This is
            # the only safety net on decision D4's "promote anyway": the swap
            # wrote this file because something went wrong, and a truncated
            # write or a permission problem would otherwise turn the alarm off
            # and leave the node looking clean. Say so, because the alternative
            # is a degraded promotion nobody ever hears about.
            _LOGGER.warning(
                "Could not read the degraded-fileset marker at %s; treating this boot as "
                "clean. If the host-side swap wrote one, its alarm has been lost -- check "
                "the swap log on the host.",
                marker_path,
                exc_info=True,
            )
            return None

    degraded_marker = await hass.async_add_executor_job(_read_degraded_marker)
    runtime[DATA_DEGRADED_MARKER] = degraded_marker
    alerts: AlertRouter | None = runtime.get(DATA_ALERTS)
    if degraded_marker:
        if alerts is not None:
            await alerts.async_raise(
                NOTIFY_FILESET_DEGRADED,
                "Promoted with a degraded fileset",
                (
                    "This node took over using an incomplete copy of the configuration "
                    f"({degraded_marker.get('reason', 'unknown')}). Decision D4 promotes "
                    "anyway -- a degraded house beats no house -- but something is missing "
                    "and only this message says so."
                ),
            )
        ir.async_create_issue(
            hass,
            DOMAIN,
            "fileset_degraded",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="fileset_degraded",
            translation_placeholders={"reason": str(degraded_marker.get("reason", "unknown"))},
        )
    else:
        # The issue registry is storage-backed and survives restarts, so
        # creating the issue is only half the job. `strings.json` tells the
        # operator that reloading the integration once a fresh fileset is
        # staged clears it — without this, that is a false promise, and a
        # fully-recovered node would carry a stale ERROR forever. Deleting an
        # issue that does not exist is a documented no-op, so this is safe to
        # run on every setup, degraded or not.
        if alerts is not None:
            await alerts.async_clear(
                NOTIFY_FILESET_DEGRADED,
                "Resolved: fileset is complete",
                "This node's copy of the configuration is whole again.",
            )
        ir.async_delete_issue(hass, DOMAIN, "fileset_degraded")


async def _setup_ingress_probe(
    hass: HomeAssistant,
    cfg: dict[str, Any],
    runtime: dict[str, Any],
    leadership: Any,
) -> None:
    """Watch the operator's own front door, if they told us where it is (AR-0060).

    Sets `runtime[DATA_INGRESS]` itself, on every path -- to the probe, or to
    None when the feature is off or the URL is unusable. `binary_sensor.py`
    decides whether the entity exists from that slot, and an entity that
    watches nothing reads as a check that passed.

    Leader-only, for the reason the flush and the statistics publish are:
    a follower probing the shared address proves nothing about the node that
    is supposed to be answering, and doubles the traffic against a tunnel that
    may well be metered. The follower's reading becomes an explicit "not
    checked" rather than the leader's last answer left to go stale.

    🚨 Read `ingress.py`'s module docstring before believing a green reading.
    A probe leaving this container runs inside the LAN, so split-horizon DNS
    can resolve the same hostname to a local address and make the probe green
    while the tunnel every external user depends on is down. This closes
    AR-0060's *reporting* gap -- the cluster now looks -- and cannot close the
    infrastructure one, which is the operator's and stays theirs.
    """
    runtime[DATA_INGRESS] = None
    try:
        url = validate_ingress_url(cfg.get(CONF_INGRESS_URL, DEFAULT_INGRESS_URL))
    except InvalidIngressURL as err:
        # Refuse this one feature, never the setup. A typo in a diagnostic's
        # address must not be able to stop the cluster that diagnostic
        # watches -- and the options page rejects it at the point of typing,
        # so reaching here means the value was written some other way.
        _LOGGER.error(
            "The configured front-door address cannot be checked and no ingress probe "
            "will run: %s. Fix it under the integration's options, or clear it to turn "
            "the check off.",
            err,
        )
        return
    if url is None:
        return

    probe = IngressProbe(
        hass,
        url,
        verify_tls=bool(cfg.get(CONF_INGRESS_VERIFY_TLS, DEFAULT_INGRESS_VERIFY_TLS)),
    )

    async def _scheduled_ingress_probe(_now: datetime) -> None:
        if not await leadership.async_is_leader():
            probe.async_note_not_leader()
            return
        result = await probe.async_probe()
        alerts: AlertRouter | None = runtime.get(DATA_ALERTS)
        if alerts is None:
            return
        if result.reachable:
            await alerts.async_clear(
                NOTIFY_INGRESS_UNREACHABLE,
                "Home Assistant is reachable again",
                f"{probe.display_url} is answering. The front door is back.",
            )
        elif probe.consecutive_failures >= INGRESS_FAILURES_BEFORE_ALARM:
            # Waits for a pattern deliberately. The entity moved on the first
            # failure and the repairs card follows this raise; only the
            # interruption waits, because a proxy reloading its configuration
            # is not a reason to wake somebody.
            await alerts.async_raise(
                NOTIFY_INGRESS_UNREACHABLE,
                "Home Assistant cannot be reached at its own address",
                (
                    f"The house is running here, but {probe.display_url} has not "
                    f"answered for {probe.consecutive_failures} checks "
                    f"({result.error or 'no reason given'}). Nothing is wrong with the "
                    "cluster -- the way IN to it is broken, which looks identical from "
                    "a phone. Check DNS, the reverse proxy or the tunnel."
                ),
            )

    async def _guarded_ingress_probe(now: datetime) -> None:
        """Make an unforeseen failure name itself, in our own log.

        `async_probe` is written not to raise and the alert router swallows its
        own failures, so arriving here at all means something nobody predicted.

        🚨 Not what it sounds like, and said plainly because the first draft of
        this comment claimed the opposite. The *timer survives without this*: measured
        against the Home Assistant version this suite pins, `_TrackTimeInterval`
        re-arms itself before it runs the job, and runs it as a background
        task. So an escaping exception does not end the checking.

        What it does instead is worse to diagnose. The exception becomes an
        unretrieved task error, reported by asyncio whenever that task is
        finally collected, with a traceback attributed to a `HassJob` and no
        mention of which integration or which check produced it -- detached in
        both time and name from the thing that failed. A fault reported that
        way is most of the way to not being reported at all, which is the
        AR-0040 shape. This costs four lines and turns it into a sentence.
        """
        try:
            await _scheduled_ingress_probe(now)
        except Exception:  # noqa: BLE001 -- see this function's docstring
            _LOGGER.exception("Ingress probe failed unexpectedly; will retry next interval")

    runtime[DATA_UNSUB].append(
        async_track_time_interval(
            hass,
            _guarded_ingress_probe,
            timedelta(seconds=INGRESS_PROBE_INTERVAL),
            name=f"{DOMAIN}_ingress_probe",
            cancel_on_shutdown=True,
        )
    )
    # Not probed here, on purpose. Setup runs before Home Assistant has
    # finished starting, so its own HTTP is not yet serving and a proxy in
    # front of it would legitimately answer 502 -- an alarm raised by the
    # act of restarting, which is how an operator learns to ignore one.
    _LOGGER.info(
        "Ingress check on: %s every %d seconds, from whichever node holds the lease. "
        "A green reading does NOT prove an external user can reach you -- see "
        "GUIDE-ingress.md.",
        probe.display_url,
        INGRESS_PROBE_INTERVAL,
    )
    runtime[DATA_INGRESS] = probe


async def _surface_preflight_disables(hass: HomeAssistant, runtime: dict[str, Any]) -> None:
    """Say out loud which radios this node came up without.

    🚨 `ha_device_preflight.py` disables config entries whose hardware is
    absent, and that is the right call -- a standby missing three radios is
    useful, one whose `rfxtrx` setups fail and stay failed is not. But it did
    it SILENTLY, and a promoted node missing three radios looks exactly like a
    healthy one from every surface this integration offers.

    The failure that shape produces: somebody buys a second transceiver for the
    standby, gets the device mapping wrong -- easily done, since the path
    embeds the unit's serial -- promotes, and is told nothing at all. They find
    out when they need the radio.

    Read-and-clear on every setup, like the degraded-fileset marker: the
    pre-flight rewrites it on each promotion and removes it when it disabled
    nothing, so a stale one cannot outlive the problem and start crying wolf.
    """
    marker_path = pathlib.Path(hass.config.path(".storage", PREFLIGHT_MARKER_NAME))
    alerts: AlertRouter | None = runtime.get(DATA_ALERTS)

    def _read() -> dict[str, Any] | None:
        try:
            return json.loads(marker_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # Same posture as the degraded marker: a marker that exists and
            # cannot be read is NOT the same as no marker, and returning None
            # makes them identical.
            _LOGGER.warning(
                "Could not read the device pre-flight marker at %s. If this promotion "
                "disabled any hardware, its record has been lost -- check the entries "
                "on this node by hand.",
                marker_path,
                exc_info=True,
            )
            return None

    marker = await hass.async_add_executor_job(_read)
    disabled = list((marker or {}).get("disabled") or [])
    if not disabled:
        ir.async_delete_issue(hass, DOMAIN, "devices_disabled")
        if alerts is not None:
            await alerts.async_clear(
                NOTIFY_DEVICES_DISABLED,
                "Resolved: all hardware is present",
                "Every config entry's device was found on this node.",
            )
        return

    detail = ", ".join(
        f"{item.get('domain', '?')} ({pathlib.Path(str(item.get('device', '?'))).name})"
        for item in disabled
    )
    if alerts is not None:
        await alerts.async_raise(
            NOTIFY_DEVICES_DISABLED,
            f"Promoted without {len(disabled)} device(s)",
            (
                f"This node started with {len(disabled)} config entry(s) switched off "
                f"because their hardware is not attached here: {detail}. That is "
                f"deliberate -- a node missing a radio is more use than one that fails "
                f"to start -- but whatever those devices do is not happening."
            ),
        )
    ir.async_create_issue(
        hass,
        DOMAIN,
        "devices_disabled",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="devices_disabled",
        translation_placeholders={"count": str(len(disabled)), "detail": detail},
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an older config entry forward to the current schema version.

    v1 -> v2 (0.4.4): `automation` joined `DEFAULT_INCLUDE_DOMAINS`.

    It should have been there from the start. It is the only domain applied by
    *calling a service* (`RESTORE_BY_SERVICE`) rather than by writing state, so
    it is the only one whose restore actually takes effect instead of being
    corrected by the next device poll — and it shipped absent, which meant no
    install replicated it unless its operator noticed and ticked the box.

    Adding it to the constant fixes every entry that never wrote an explicit
    allowlist, because `async_setup_entry` falls back to the constant. It does
    not fix entries the wizard wrote the defaults into, which is most of them.

    The migration is deliberately narrow: an allowlist is rewritten **only** if
    it is exactly `LEGACY_DEFAULT_INCLUDE_DOMAINS`. That set is a historical
    fact, frozen, and never tracks the current default. Matching it proves the
    operator accepted the shipped default and never revisited it, so adding
    `automation` restores an intended default. Any other value — one domain
    removed, one added, an empty list — is a decision somebody made, and this
    migration does not overrule decisions. Those entries are version-stamped
    and otherwise left exactly as they are.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # Written by a newer release than this one. Refuse rather than guess,
        # the same stance `backend.py` takes on a future snapshot schema: a
        # downgrade that silently reinterprets config is worse than one that
        # declines to start.
        _LOGGER.error(
            "Config entry is version %d but this build understands at most "
            "v%d. Refusing to migrate rather than guess at its meaning — "
            "upgrade the integration, or remove and re-add the entry.",
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False

    if entry.version == 1:
        data = dict(entry.data)
        options = dict(entry.options)
        migrated: list[str] = []
        for label, src in (("options", options), ("data", data)):
            current = src.get(CONF_INCLUDE_DOMAINS)
            if current and set(current) == LEGACY_DEFAULT_INCLUDE_DOMAINS:
                src[CONF_INCLUDE_DOMAINS] = list(DEFAULT_INCLUDE_DOMAINS)
                migrated.append(label)

        if migrated:
            _LOGGER.info(
                "Migrating config entry to v%d: added `automation` to the "
                "replicated domains in %s. This entry carried the pre-v%d "
                "default unchanged, so `automation` was never replicated — "
                "the one domain whose restore actually takes effect. Untick "
                "it in the options flow if that is not what you want.",
                CONFIG_ENTRY_VERSION,
                " and ".join(migrated),
                CONFIG_ENTRY_VERSION,
            )
        else:
            _LOGGER.debug(
                "Migrating config entry to v%d: the domain allowlist is not "
                "the pre-v%d default, so it is a deliberate choice and is "
                "left untouched.",
                CONFIG_ENTRY_VERSION,
                CONFIG_ENTRY_VERSION,
            )

        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=CONFIG_ENTRY_VERSION
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Cluster State Sync from a config entry."""
    cfg = {**entry.data, **entry.options}
    namespace = cfg.get(CONF_CLUSTER_NAMESPACE, DEFAULT_CLUSTER_NAMESPACE)
    node_id = cfg.get(CONF_NODE_ID) or default_node_id()

    backend = RedisBackend(
        namespace=namespace,
        host=cfg.get(CONF_REDIS_HOST),
        port=cfg.get(CONF_REDIS_PORT, 6379),
        username=cfg.get(CONF_REDIS_USERNAME),
        password=cfg.get(CONF_REDIS_PASSWORD),
        db=cfg.get(CONF_REDIS_DB, 2),
        use_sentinel=cfg.get(CONF_REDIS_USE_SENTINEL, False),
        sentinel_hosts=parse_sentinel_hosts(cfg.get(CONF_REDIS_SENTINEL_HOSTS, "")),
        sentinel_service=cfg.get(CONF_REDIS_SENTINEL_SERVICE),
        use_tls=cfg.get(CONF_REDIS_USE_TLS, False),
        tls_ca_certs=cfg.get(CONF_REDIS_TLS_CA_CERTS) or None,
        secret=cfg.get(CONF_CLUSTER_SECRET) or None,
    )

    if not cfg.get(CONF_CLUSTER_SECRET):
        # AR-0005. Entries cannot be authenticated without a shared secret, and
        # the restore path hands whatever it reads straight to async_set. An
        # entry with no provenance is not a degraded snapshot, it is an
        # instruction from an unknown party — so we mirror outward but refuse
        # to take anything back in.
        _LOGGER.error(
            "No cluster secret is configured, so snapshot entries cannot be "
            "verified. This node will keep publishing its own state but will "
            "REFUSE TO RESTORE anything, because an unauthenticated entry is "
            "applied verbatim to the state machine. Reconfigure the "
            "integration to set a cluster secret shared with the peer node."
        )

    tracked_sensitive = SENSITIVE_DOMAINS.intersection(
        cfg.get(CONF_INCLUDE_DOMAINS) or DEFAULT_INCLUDE_DOMAINS
    )
    if tracked_sensitive and not cfg.get(CONF_REDIS_USE_TLS):
        # Review conflict C3, resolved as "warn loudly, don't silently upgrade".
        # Flipping TLS on by ourselves would break every deployment whose Valkey
        # has no TLS listener, turning a privacy warning into an outage.
        _LOGGER.warning(
            "Mirroring %s without TLS. These domains describe where people are "
            "and whether the house is armed, and they are crossing the network "
            "in the clear to a shared backend. Enable TLS on the integration, "
            "or stop tracking these domains.",
            ", ".join(sorted(tracked_sensitive)),
        )

    try:
        await backend.connect()
    except Exception as err:
        # ConfigEntryNotReady triggers HA's automatic retry-with-backoff. If
        # Redis is briefly down on boot, the integration will come up later.
        raise ConfigEntryNotReady(f"Cannot reach state-sync backend: {err}") from err

    # Alerting (v0.4.2). Built before anything can raise a condition, because
    # the conditions raised during setup -- a degraded fileset above all -- are
    # the ones most worth hearing about: they describe the promotion that has
    # just happened.
    alerts = AlertRouter(
        hass,
        conditions=cfg.get(CONF_NOTIFY_CONDITIONS, DEFAULT_NOTIFY_CONDITIONS),
        services=cfg.get(CONF_NOTIFY_SERVICES, DEFAULT_NOTIFY_SERVICES),
        node_id=node_id,
    )

    runtime: dict[str, Any] = {
        DATA_BACKEND: backend,
        DATA_CONFIG: cfg,
        DATA_UNSUB: [],
        DATA_MIRROR: None,
        DATA_STATS: SyncStats(),
        DATA_ALERTS: alerts,
    }
    # Quality-scale `runtime-data`. Kept in hass.data as well, for now, only
    # because the unload path and several tests still reach for it; the
    # entry is the source of truth.
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    # 1. Seed local state from the snapshot BEFORE automations start.
    #    HA fires EVENT_HOMEASSISTANT_START after integrations have loaded
    #    but before the automation engine, which is exactly the window we want.
    # AR-0065. Closed until the restore has run, so the flush cannot publish
    # this node's own state over the snapshot it is about to read.
    runtime[DATA_RESTORE_DONE] = False
    if hass.state is CoreState.running:
        # AR-0012: added to a live HA, not at boot. Restoring here would seed
        # dozens of entities into a running system and every automation
        # watching them would fire at once — an automation storm caused by the
        # thing that exists to make failover uneventful. The snapshot will be
        # applied at the next restart, which is the only moment it is safe.
        _LOGGER.info(
            "Added while Home Assistant is already running — skipping restore. "
            "The shared snapshot will be applied on the next restart."
        )
        # No restore is coming, so the gate must not hold the flush shut.
        runtime[DATA_RESTORE_DONE] = True
    else:
        restored_already = False

        async def _on_start(_event: Event) -> None:
            nonlocal restored_already
            restored_already = True
            try:
                await _restore_from_snapshot(
                    hass,
                    backend,
                    cfg,
                    node_id,
                    runtime[DATA_STATS],
                    at_boot=True,
                    alerts=runtime.get(DATA_ALERTS),
                )
            finally:
                # In a `finally` deliberately: a restore that RAISED must still
                # release the flush. Leaving the gate shut on an error would
                # turn a failed restore into a node that never publishes at
                # all, and starve the standby of state for as long as it runs.
                runtime[DATA_RESTORE_DONE] = True

        unsub_start = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _on_start)

        @callback
        def _remove_start_listener() -> None:
            # A one-time listener removes itself once it fires; calling the
            # returned unsub afterwards makes HA log an "unknown job listener"
            # error. Only unsubscribe if the event never arrived.
            if not restored_already:
                unsub_start()

        runtime[DATA_UNSUB].append(_remove_start_listener)

        @callback
        def _open_gate_anyway(_now: datetime) -> None:
            if runtime.get(DATA_RESTORE_DONE):
                return
            _LOGGER.warning(
                "Home Assistant has not finished starting after %.0fs and the restore has "
                "not run, so state publishing is being released anyway. The standby needs a "
                "fresh snapshot more than this node needs to finish booting first — but a "
                "boot this slow is worth investigating.",
                RESTORE_GATE_TIMEOUT,
            )
            runtime[DATA_RESTORE_DONE] = True

        runtime[DATA_UNSUB].append(async_call_later(hass, RESTORE_GATE_TIMEOUT, _open_gate_anyway))

    # 2. Build the authoritative mirror and seed it from current state.
    #    Seeding is AR-0002: without it, an entity that never fires a
    #    state-changed event after boot would never be mirrored at all.
    # Device exclusions, resolved to entity ids once and refreshed when either
    # registry changes. Recomputing per entity per flush would mean 3,595
    # registry lookups every thirty seconds to answer a question that only
    # moves when a device gains or loses an entity.
    @callback
    def _refresh_excluded_ids(_event: Event | None = None) -> None:
        resolved = resolve_device_entities(hass, cfg.get(CONF_EXCLUDE_DEVICES) or [])
        if resolved == runtime.get(DATA_EXCLUDED_IDS):
            return
        runtime[DATA_EXCLUDED_IDS] = resolved
        current = runtime.get(DATA_MIRROR)
        if current is not None:
            current.excluded_ids = resolved
        _LOGGER.debug("Device exclusions resolve to %d entities", len(resolved))

    _refresh_excluded_ids()
    runtime[DATA_UNSUB].append(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _refresh_excluded_ids)
    )
    # 🚨 Both registries, not just the entity one. An integration that adds a
    # device and its entities in one go can fire only the device event, and a
    # missed refresh means an entity of an EXCLUDED device silently replicating
    # -- a failure whose only symptom is data crossing that the operator
    # explicitly asked to keep local.
    runtime[DATA_UNSUB].append(
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _refresh_excluded_ids)
    )

    mirror = StateMirror(hass, backend, cfg, node_id, stats=runtime[DATA_STATS])
    mirror.excluded_ids = runtime.get(DATA_EXCLUDED_IDS) or frozenset()
    mirror.seed_from_current_states()
    runtime[DATA_MIRROR] = mirror

    @callback
    def _on_state_changed(event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            # Entity removed; skip. Could optionally tombstone in v0.2.
            return
        if not _should_track(new_state.entity_id, cfg, runtime.get(DATA_EXCLUDED_IDS)):
            return
        # No lock needed — the asyncio event loop is single-threaded, so this
        # callback runs to completion atomically.
        mirror.record(new_state)

    runtime[DATA_UNSUB].append(hass.bus.async_listen(EVENT_STATE_CHANGED, _on_state_changed))

    # 3. Periodic flush, driven by HA's clock rather than a bare asyncio.sleep
    #    so it cancels cleanly on shutdown and is drivable in tests.
    interval = cfg.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)

    # AR-0017: leadership gates the write. Phase 1's merge-instead-of-erase
    # write stays underneath as the interim guard, but merging two nodes' views
    # is damage control — the follower's state is by definition the stale one,
    # so the real answer is that it does not write at all.
    leadership = LeadershipMonitor(
        hass,
        backend,
        node_id,
        source=cfg.get(CONF_LEADERSHIP_SOURCE, DEFAULT_LEADERSHIP_SOURCE),
        entity_id=cfg.get(CONF_LEADERSHIP_ENTITY),
        config_dir=hass.config.path(),
    )
    runtime[DATA_LEADERSHIP] = leadership

    # ADR-001 layers 3 and 4, sharing layer 2's signal and its cadence. Both
    # off unless asked for — see gating.py for why they must not inherit the
    # flush's fail-closed posture.
    gate = ServiceGate(
        hass,
        gate_recorder=bool(cfg.get(CONF_GATE_RECORDER)),
        gate_automations=bool(cfg.get(CONF_GATE_AUTOMATIONS)),
    )
    runtime[DATA_GATE] = gate

    # AR-0039: apply the gate once as soon as leadership can be evaluated,
    # instead of waiting for the first flush interval.
    #
    # The restore is wired to EVENT_HOMEASSISTANT_START precisely because that
    # is the window after integrations load and before the automation engine
    # attaches its triggers. Layer 4 decides whether those automations should
    # be running at all, and it was only ever consulted from the flush timer —
    # so on a follower the order was: restore a batch of peer states, start
    # every automation, let them act on what was just restored, and only then
    # turn them off. The two halves disagreed about when "before automations"
    # is. The interval is configurable up to 300 seconds, so the window was not
    # bounded by the 5-second default either.
    #
    # Registered after the restore listener, so it runs after it: listeners on
    # the same event fire in registration order.
    async def _apply_gate(_event: Event | None = None) -> None:
        await gate.async_apply(await leadership.async_is_leader())

    if gate.enabled:
        if hass.state is CoreState.running:
            # Added to a live system. Unlike the restore (AR-0012), acting now
            # is the point: the operator asked for followers to be quiet, and
            # the automations they mean are already running.
            hass.async_create_task(_apply_gate())
        else:
            gated_already = False

            async def _on_start_gate(event: Event) -> None:
                nonlocal gated_already
                gated_already = True
                await _apply_gate(event)

            unsub_gate = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _on_start_gate)

            @callback
            def _remove_gate_listener() -> None:
                # A one-time listener removes itself once it fires; calling the
                # returned unsub afterwards makes HA log an "unknown job
                # listener" error.
                if not gated_already:
                    unsub_gate()

            runtime[DATA_UNSUB].append(_remove_gate_listener)

    async def _scheduled_flush(_now: datetime) -> None:
        is_leader = await leadership.async_is_leader()
        # Reconcile before returning: a follower needs gating applied precisely
        # when it is NOT flushing, so this cannot sit after the early return.
        await gate.async_apply(is_leader)
        if not is_leader:
            return
        if not runtime.get(DATA_RESTORE_DONE):
            # 🚨 AR-0065. Publishing now would write THIS node's state over the
            # peer's snapshot moments before the restore reads it -- every
            # entry would then carry our own `source_node`, be skipped as
            # `own-node`, and a promotion would restore nothing at all.
            #
            # Measured on the reference pair before this gate existed:
            #   leadership 18:08:20.786 -> publish .787 -> restore 18:08:35.703
            #   28 own-node, restored 0
            #
            # Gating on leadership alone was not enough, because a promoted
            # node IS the leader from its first tick and the restore is
            # deliberately deferred until the whole of Home Assistant has
            # started. The gate is what separates the two.
            _LOGGER.debug("Holding the flush until the boot restore has run")
            return
        await mirror.async_flush()

    runtime[DATA_UNSUB].append(
        async_track_time_interval(
            hass,
            _scheduled_flush,
            timedelta(seconds=interval),
            name=f"{DOMAIN}_snapshot_flush",
            cancel_on_shutdown=True,
        )
    )

    await _setup_recorder_snapshots(hass, cfg, runtime, leadership)

    await _setup_fileset_replication(hass, entry, cfg, runtime, backend, leadership, node_id)

    await _setup_statistics_replication(hass, cfg, runtime, backend, leadership, node_id)

    await _setup_degraded_alarm(hass, runtime)
    await _surface_preflight_disables(hass, runtime)

    await _setup_ingress_probe(hass, cfg, runtime, leadership)

    # 4. On stop, write a final snapshot — the gift the dying node leaves the
    #    standby. AR-0016: this is the *only* shutdown flush path. v0.1 had two
    #    (a CancelledError handler in the loop and this listener), which could
    #    both fire and race over the same live dict.
    async def _on_stop(_event: Event) -> None:
        if await leadership.async_is_leader():
            await _final_flush(mirror)
        # Hand the lease back on the way out so the peer promotes immediately
        # rather than waiting out the TTL -- unless the operator has said this
        # is planned work rather than a departure.
        if not await _hold_blocks_release(hass, hass.config.path(), "shutdown"):
            await _release_leadership(backend, node_id)

    runtime[DATA_UNSUB].append(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))

    # 5. Diagnostics (AR-0019/AR-0032). Set up last so the entities find a
    #    fully-populated runtime dict.
    coordinator = BackendHealthCoordinator(hass, backend)
    await coordinator.async_config_entry_first_refresh()
    runtime[DATA_COORDINATOR] = coordinator

    # The cluster-wide view, from the shared store rather than from this node.
    # `async_refresh` and not `async_config_entry_first_refresh`: the latter
    # raises ConfigEntryNotReady on a failed first poll, and these entities are
    # exactly the ones that should still appear -- reading "unknown" -- when the
    # backend is unreachable. Failing setup over a diagnostic would hide the
    # diagnosis.
    cluster_view = ClusterViewCoordinator(hass, backend, node_id)
    await cluster_view.async_refresh()
    runtime[DATA_CLUSTER_VIEW] = cluster_view

    # Promotion is announced from the cluster view rather than from our own
    # leadership resolution, because only the view carries `snapshot_source` --
    # the field that separates "the house moved here" from "this node was
    # restarted", which are otherwise identical from inside a starting HA.
    @callback
    def _watch_for_promotion() -> None:
        view = cluster_view.data
        if view is None:
            return
        hass.async_create_task(
            alerts.async_check_promotion(leader=view.leader, snapshot_source=view.snapshot_source)
        )

    _watch_for_promotion()
    runtime[DATA_UNSUB].append(cluster_view.async_add_listener(_watch_for_promotion))

    # Losing Valkey does not stop the house; it stops the house being able to
    # FAIL OVER, silently, which is the failure this integration exists to
    # prevent and the one nothing else would report.
    @callback
    def _watch_backend_health() -> None:
        healthy = coordinator.data
        if healthy:
            hass.async_create_task(
                alerts.async_clear(
                    NOTIFY_BACKEND_LOST,
                    "Cluster backend recovered",
                    "Valkey is reachable again. Failover protection is restored.",
                )
            )
        elif healthy is False:
            hass.async_create_task(
                alerts.async_raise(
                    NOTIFY_BACKEND_LOST,
                    "Cluster backend unreachable",
                    (
                        "Home Assistant cannot reach Valkey, so state is no longer being "
                        "replicated and this cluster CANNOT fail over. The house keeps "
                        "running; its safety net does not."
                    ),
                )
            )

    _watch_backend_health()
    runtime[DATA_UNSUB].append(coordinator.async_add_listener(_watch_backend_health))

    # Everything above may have queued a push. Release them once the rest of
    # Home Assistant has loaded, because the `notify.` service the operator
    # chose belongs to an integration that may well set up after we do.
    runtime[DATA_UNSUB].append(alerts.async_arm())
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The sidebar panel. After the platforms, so the entities it discovers
    # exist by the time anyone can open it, and best-effort inside its own
    # module: a convenience view must never be able to stop the thing that
    # keeps the house running from starting.
    await async_register_panel(hass)

    # File the device under its own area, so the switches -- which are
    # deliberately not diagnostic, and therefore DO appear on the default
    # dashboard -- arrive somewhere meaningful rather than loose among the
    # lamps. After the platforms, because the device does not exist until they
    # have run. Never overrides an area the operator has chosen.
    await async_assign_area(hass, entry)

    # 🚨 Without this, every options page in this integration is a lie.
    #
    # `async_setup_entry` reads `cfg` ONCE and hands it to the mirror, the
    # filter and the alert router. Nothing re-reads it, so an operator who
    # changes a setting is told it saved and sees no change until the next
    # restart -- and for alerting that means configuring a phone, believing the
    # house can reach you, and getting nothing.
    #
    # Found on the live pair during the v0.4.2 deploy: the options flow stored
    # `notify_services` correctly and the running router never saw it. The gap
    # predates the alerting -- the connection settings had it too -- but adding
    # two more sections to a flow that does not take effect made it worse.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.info(
        "Cluster State Sync ready — node=%s namespace=%s interval=%ds tracking=%d entities",
        node_id,
        namespace,
        interval,
        mirror.tracked_count,
    )
    return True


async def _hold_blocks_release(hass: HomeAssistant, config_dir: str, why: str) -> bool:
    """Should this shutdown keep the lease rather than hand it back?

    Handing the lease back on a clean stop is right for a shutdown and wrong
    for a restart, and from inside `_on_stop` the two are indistinguishable --
    Home Assistant is going away either way. The maintenance hold is the
    operator saying which one this is.

    This is the most important of the hold's four behaviours: the release
    happens in milliseconds, long before any promoter tick could intervene, so
    a hold that suspended the promoter but not this would still hand the
    cluster away on every restart.
    """
    # Executor, not the loop: this is blocking file I/O and Home Assistant
    # flags it as such. Cheap, but shutdown is exactly when the loop is
    # busiest.
    held, raw = await hass.async_add_executor_job(read_hold, config_dir)
    if not held:
        return False
    reason = raw or "no reason given"
    _LOGGER.warning(
        "Maintenance hold is set (%s) - keeping the cluster lease through %s "
        "instead of handing it to the peer. The host-side promoter renews it "
        "while this node is down, so failover will NOT happen until the hold "
        "is removed.",
        reason,
        why,
    )
    return True


async def _release_leadership(backend: ClusterBackend, node_id: str) -> None:
    """Best-effort lease handback. Never raises."""
    release = getattr(backend, "release_leadership", None)
    if release is None:
        return
    try:
        await asyncio.wait_for(release(node_id), timeout=FINAL_FLUSH_TIMEOUT)
    except Exception:  # noqa: BLE001 — teardown must not be blocked by Redis
        _LOGGER.debug("Could not release the cluster lease", exc_info=True)


async def _final_flush(mirror: StateMirror) -> None:
    """Best-effort, time-bounded last write. Never raises (AR-0016)."""
    try:
        await asyncio.wait_for(mirror.async_flush(), timeout=FINAL_FLUSH_TIMEOUT)
    except Exception:  # noqa: BLE001 — teardown must not be blocked by Redis
        _LOGGER.debug("Final flush failed", exc_info=True)


#: Entries this integration is reloading on purpose, so the unload below can
#: tell a reload from a removal.
#:
#: 🚨 Without this, changing ANY option on the leader fails the house over.
#:
#: `async_unload_entry` releases the cluster lease, and its comment has always
#: said "unload covers reloads and reconfiguration" -- correct, and harmless
#: while nothing reloaded on an options change. Adding the update listener
#: (which fixed options silently doing nothing) turned every settings change on
#: the leader into a real promotion. Observed on the reference pair 2026-09-10:
#: two options submissions, and the house moved to the standby.
#:
#: A removal must still release the lease -- a node that has genuinely stopped
#: running the integration should not make the peer wait out the full TTL. Only
#: a reload we initiated keeps it.
_RELOADING: set[str] = set()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload so a changed setting actually applies.

    A full reload rather than mutating the live objects: the config reaches a
    dozen places -- the mirror's filter, the leadership monitor, the fileset
    and statistics publishers, the alert router -- and a partial update that
    refreshed some of them would be harder to reason about than a restart of
    the entry, and would fail in exactly the way this listener exists to stop.

    The cost is a few seconds without replication on a node whose operator is
    deliberately changing its configuration, which is the right moment to pay.
    """
    _RELOADING.add(entry.entry_id)
    try:
        await hass.config_entries.async_reload(entry.entry_id)
    finally:
        # In a `finally`: a reload that raised must not leave the entry marked,
        # or the NEXT unload -- possibly a real removal -- would keep the lease
        # and cost the peer a full TTL to take over.
        _RELOADING.discard(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down on entry removal."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not runtime:
        return True

    # Stop the timer and the event listener before flushing, so nothing new
    # lands in the mirror while the last write is in flight.
    for unsub in runtime.get(DATA_UNSUB, []):
        unsub()

    # Undo the gating before anything else. If this node was a follower with
    # layers 3 or 4 enabled, its recorder and automations are switched off, and
    # after this unload nothing remains that would turn them back on. Removing
    # an integration must not be how a house loses its automations.
    gate: ServiceGate | None = runtime.get(DATA_GATE)
    if gate is not None:
        await gate.async_restore()

    mirror: StateMirror | None = runtime.get(DATA_MIRROR)
    if mirror is not None:
        await _final_flush(mirror)

    backend = runtime.get(DATA_BACKEND)
    if backend is not None:
        # Hand the lease back here too, not only on Home Assistant shutdown.
        # Unload covers reloads and reconfiguration, and a lease left held by a
        # node that is no longer running the integration costs the peer the
        # full TTL before it can promote — failover budget burned for nothing,
        # on the one kind of shutdown that was entirely orderly.
        cfg = runtime.get(DATA_CONFIG) or {}
        node_id = cfg.get(CONF_NODE_ID) or default_node_id()
        if entry.entry_id in _RELOADING:
            # 🚨 Our own options reload. Releasing here hands the lease to the
            # peer and the house moves because somebody changed a setting --
            # observed on the reference pair 2026-09-10, twice in a row.
            #
            # The entry comes straight back up a second later with the same
            # node_id, so keeping the lease is not a lie: this node is still
            # the leader and is about to prove it by renewing.
            _LOGGER.debug("Reloading on an options change; keeping the lease")
        elif not await _hold_blocks_release(hass, hass.config.path(), "reload"):
            await _release_leadership(backend, node_id)
        await backend.close()
    return True


# ---------------------------------------------------------------------------
# Authoritative state mirror
# ---------------------------------------------------------------------------


class StateMirror:
    """Full in-memory mirror of every tracked entity, and the flush cycle.

    This replaces v0.1's `pending` change-buffer, which was the subject of the
    v1 review's headline finding. The old buffer held only the entities that
    changed since the last flush, and the flush wiped the shared hash before
    rewriting it from that buffer — so the "snapshot" a promoted standby read
    back was whatever happened to move in the preceding five seconds, and a
    quiet entity was never in it at all (AR-0001, AR-0002).

    Here the map is authoritative: seeded from current state at setup, updated
    on every tracked change, and written *whole* on every flush. That also
    dissolves AR-0015 — the map is bounded by the number of tracked entities,
    not by how fast they change, so no amount of churn can grow it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        backend: ClusterBackend,
        cfg: dict[str, Any],
        node_id: str,
        stats: SyncStats | None = None,
    ) -> None:
        self._hass = hass
        self._backend = backend
        self._cfg = cfg
        self._node_id = node_id
        #: Entities excluded because their DEVICE is excluded. Public and
        #: mutable: it is refreshed from a registry listener, and a mirror
        #: constructed in a test simply never has any.
        self.excluded_ids: frozenset[str] = frozenset()
        # Optional so every existing construction site and test keeps working;
        # a mirror with no stats simply records nothing, which is the old
        # behaviour rather than a crash.
        self._stats = stats
        self._tracked: dict[str, SnapshotEntry] = {}
        # Monotonic revision rather than a dirty *flag*: changes that arrive
        # while a flush is awaiting the backend bump the revision past the one
        # being written, so they are never mistaken for already-persisted.
        self._revision = 0
        self._flushed_revision = 0
        self._last_successful_flush: datetime | None = None
        self._listeners: list[Callable[[], None]] = []

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to mirror changes; returns an unsubscribe callable.

        Push rather than poll, matching the integration's `local_push` iot
        class. Listeners fire when the tracked set changes size or a flush
        succeeds — not on every individual state change, which at a few hundred
        entities would be a needless entity write per event.
        """
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @property
    def last_successful_flush(self) -> datetime | None:
        """When the snapshot last actually reached the backend.

        Drives the `last_snapshot_age` diagnostic. Stays None until a write
        genuinely succeeds, so "never written" reads as unknown rather than as
        a reassuring zero.
        """
        return self._last_successful_flush

    @property
    def tracked_count(self) -> int:
        """Number of entities currently mirrored."""
        return len(self._tracked)

    @property
    def has_unflushed_changes(self) -> bool:
        return self._revision != self._flushed_revision

    @callback
    def seed_from_current_states(self) -> None:
        """Capture every already-present tracked entity (AR-0002)."""
        for state in self._hass.states.async_all():
            if _should_track(state.entity_id, self._cfg, self.excluded_ids):
                self._tracked[state.entity_id] = _entry_from_state(state, self._node_id)
        if self._tracked:
            self._revision += 1
        _LOGGER.debug("Seeded mirror with %d entities", len(self._tracked))

    @callback
    def record(self, state: State) -> None:
        """Fold one state change into the authoritative map."""
        is_new = state.entity_id not in self._tracked
        self._tracked[state.entity_id] = _entry_from_state(state, self._node_id)
        self._revision += 1
        if is_new:
            self._notify()

    async def async_flush(self) -> bool:
        """Write the full map to the backend. Returns True on a successful write.

        Skips entirely when nothing has changed since the last successful
        flush, so an idle cluster does not rewrite an identical snapshot every
        interval.
        """
        if not self.has_unflushed_changes or not self._tracked:
            return False

        revision = self._revision
        payload = dict(self._tracked)

        # Drop entries the restore would refuse anyway. The budget used to be
        # applied on READ only, which meant an oversized entity was written on
        # every single flush and then declined on every single restore -- pure
        # write cost for something guaranteed unusable. Measured on a real
        # estate: one entity carried 100,911 bytes against a 16,384 byte cap.
        #
        # Recorded, not merely dropped: an entity that never replicates and
        # says so nowhere is the exact shape this project keeps finding
        # (ADR-008). `oversized` reaches the operator as a diagnostic.
        oversized = [
            entity_id
            for entity_id, entry in payload.items()
            if not _attributes_within_budget(entity_id, entry.attributes)
        ]
        for entity_id in oversized:
            del payload[entity_id]
            if self._stats is not None:
                self._stats.record_oversized(entity_id)
        if oversized and not payload:
            # Everything we had was oversized. Writing an empty map would
            # publish "this node tracks nothing", which is a different and
            # much worse claim than "some entries did not fit".
            return False

        if await self._backend.write_snapshot(payload, self._node_id):
            # Only the revision we actually wrote is marked clean; anything
            # recorded during the await stays pending for the next interval.
            self._flushed_revision = revision
            self._last_successful_flush = datetime.now(tz=UTC)
            self._notify()
            return True

        # AR-0011: leave the revision dirty so the next interval retries.
        # A backend outage must degrade the snapshot's freshness, never
        # silently discard the changes that happened during it.
        _LOGGER.debug("Snapshot write failed; %d entities still pending", len(payload))
        return False


def _entry_from_state(state: State, node_id: str) -> SnapshotEntry:
    """Convert an HA state object into a storable snapshot entry."""
    return SnapshotEntry(
        entity_id=state.entity_id,
        state=state.state,
        attributes=dict(state.attributes),
        last_changed=state.last_changed.isoformat(),
        last_updated=state.last_updated.isoformat(),
        source_node=node_id,
    )


# ---------------------------------------------------------------------------
# Restore logic
# ---------------------------------------------------------------------------


def _initial_state_is_pinned(hass: HomeAssistant, entity_id: str) -> bool:
    """Has this automation's config pinned its startup state?

    `initial_state` is Home Assistant's documented way of saying "force this
    automation on or off at every startup, *regardless of what was restored*".
    HA applies it in `async_added_to_hass`, where it deliberately overrides
    `async_get_last_state()`.

    Our restore runs later — on `EVENT_HOMEASSISTANT_START` — and applies
    automations by calling a service. A command lands after a pin, so without
    this check we win, and we should not: an operator who wrote
    `initial_state: false` opted that automation out of restore. Re-enabling it
    at every promotion is the integration overriding an explicit instruction,
    and on the estate this was found on it would have armed an automation whose
    hardware is not wired.

    🚨 This reads a private attribute, which is against this repo's
    public-APIs-only rule, and it is a deliberate exception. Home Assistant
    exposes `initial_state` on no public API at all — not a property, not in
    `extra_state_attributes` — so the alternatives were to read it or to keep
    overriding operators. `tests/test_initial_state.py` pins that
    `_initial_state` still exists on the tested HA version, so a bump that
    moves it fails CI loudly. If it ever moves without that test catching it,
    this returns False and we are back to the old behaviour — no worse than
    before, which is why the fallback is silent rather than fatal.
    """
    if not entity_id.startswith("automation."):
        return False
    try:
        from homeassistant.components.automation import DATA_COMPONENT

        component = hass.data.get(DATA_COMPONENT)
        if component is None:
            return False
        entity = component.get_entity(entity_id)
    except (ImportError, AttributeError, KeyError):  # pragma: no cover - defensive
        return False
    return entity is not None and getattr(entity, "_initial_state", None) is not None


async def _restore_from_snapshot(
    hass: HomeAssistant,
    backend: ClusterBackend,
    cfg: dict[str, Any],
    node_id: str,
    stats: SyncStats,
    *,
    at_boot: bool,
    alerts: AlertRouter | None = None,
) -> None:
    """Seed the local state machine from the shared snapshot.

    We only restore entities that:
      * match the include filter (no point restoring things we don't track),
      * have a snapshot age within the max-age window (don't restore ancient),
      * fit within the size bounds (AR-0009),
      * were not written by this node itself,
      * carry a valid signature (AR-0005).

    AR-0040 — what is deliberately *not* on that list any more, and why.

    It used to also require that the entry be newer than the local state, to
    "avoid clobbering fresh data". On two real Home Assistant containers that
    guard skipped every single entry of an eighteen-second-old snapshot:

        Snapshot age at restore: 18s (max 1800s)
        Restored 0 entities from snapshot (skipped: 2 local-newer, ...)

    Every domain in the default allowlist is a RestoreEntity. At boot Home
    Assistant replays each one's previous value from this node's own disk and
    stamps `last_updated` with **the boot time**, not the time the value last
    actually changed. The restore runs at `EVENT_HOMEASSISTANT_START`, so by
    then *all* local state is stamped in this run — the comparison had no
    information in it, and resolved against the snapshot every time.

    It then compounded: the standby restored nothing, took the lease, and
    flushed its own cold state over the peer's good snapshot. The integration
    destroyed the snapshot it had failed to use.

    The replacement is a decision rather than a computation, because at boot
    the timestamps genuinely cannot tell a replayed value from a freshly polled
    one. On a promoting standby the peer's snapshot is the authority for the
    entities we track — that is the premise of the whole integration — and it
    stays bounded by max-age, the signature, the size cap, the future-stamp
    refusal and the include filter. A device-backed entity that really did poll
    fresher state corrects itself on its next poll; a helper never does, and
    helpers are most of what this tracks.

    The comparison is kept behind `at_boot` for a caller that does not exist
    yet: a manual restore invoked against a running system, where local
    timestamps *are* evidence because they were set by real events rather
    than by startup. `at_boot` rather than a timestamp boundary on purpose —
    there is no instant we can compare against, because Home Assistant
    replays entity state as each integration sets up, which may be before or
    after this one. The thing we actually know is which path we are on.

    Every entry here is attacker-controllable until AR-0005 lands: anything
    able to write to the shared hash can put an arbitrary state in front of
    `async_set`. These guards bound the damage; they do not establish
    authenticity. That is the signature's job, not the timestamp's.
    """
    # Resolved once here rather than per entity: the restore is a single pass
    # at boot, and the registries are loaded by the time it runs.
    excluded_ids = resolve_device_entities(hass, cfg.get(CONF_EXCLUDE_DEVICES) or [])

    if not cfg.get(CONF_CLUSTER_SECRET):
        # AR-0005. Refusing here rather than restoring unverified entries is
        # the whole point: a snapshot we cannot authenticate is not a weaker
        # snapshot, it is input from an unknown writer that we would otherwise
        # apply verbatim to the state machine. Starting cold is a worse
        # failover; obeying a forged alarm state is a worse outcome.
        _LOGGER.error(
            "Refusing to restore: no cluster secret is configured, so snapshot "
            "entries cannot be authenticated. Starting cold instead."
        )
        return

    entries, meta = await backend.read_snapshot()
    if not entries:
        _LOGGER.info("No snapshot available — starting cold")
        return

    if len(entries) > MAX_RESTORE_ENTRIES:
        # Applying an unbounded hash blocks the event loop during precisely the
        # window a failover needs it responsive.
        _LOGGER.warning(
            "Snapshot holds %d entries, above the %d cap — restoring the first "
            "%d only. A legitimate snapshot should not be this large; check "
            "whether the namespace is shared or the hash has been tampered with.",
            len(entries),
            MAX_RESTORE_ENTRIES,
            MAX_RESTORE_ENTRIES,
        )
        entries = dict(list(entries.items())[:MAX_RESTORE_ENTRIES])

    max_age = cfg.get(CONF_RESTORE_MAX_AGE, DEFAULT_RESTORE_MAX_AGE)
    now = datetime.now(tz=UTC)

    # The age gate belongs to the SNAPSHOT, not to each entry in it.
    #
    # It used to be per-entry, compared against each entity's own
    # `last_updated`, and that inverted its purpose. `async_flush` deliberately
    # skips when nothing has changed, so on a quiet system the entries simply
    # get older together -- and the states most worth carrying across a
    # failover are exactly the stable ones: a setpoint that has held all day, a
    # boolean nobody has touched since Tuesday. Those were discarded while a
    # sensor that flickered ten seconds ago sailed through.
    #
    # Observed on node-b, 2026-09-04, on an ordinary restart: "Restored
    # NOTHING from a snapshot that held 28 entries. Skipped: 28 too-old." A
    # perfectly healthy cluster, idle for half an hour, restoring nothing at
    # all -- and blaming the peer for it.
    #
    # What the setting says it is ("Maximum snapshot age to restore") is what
    # it now does. The freshness comparison that stays per-entry is
    # `existing.last_updated > entry_updated` below: do not clobber local state
    # that is newer than the snapshot's. That is a different question and it
    # was never the broken one.
    snapshot_at = _parse_entry_timestamp(meta.get("last_snapshot_at", ""))
    if snapshot_at is not None and snapshot_at < now - timedelta(seconds=max_age):
        _LOGGER.warning(
            "Refusing to restore: the shared snapshot was written %.0fs ago, "
            "against a %ds limit. On an idle cluster this is normal and "
            "harmless -- nothing changed, so nothing was written. Raise "
            "`restore_max_age` if this node should still seed from it.",
            (now - snapshot_at).total_seconds(),
            max_age,
        )
        return
    # One context shared by every state this restore seeds, so the whole
    # operation is attributable in the logbook and distinguishable by
    # automations from a genuine device report (AR-0027).
    context = Context()
    restored = 0
    skipped_local_newer = 0
    skipped_own_node = 0
    skipped_oversized = 0
    skipped_unparseable = 0
    skipped_refused = 0
    skipped_future = 0
    skipped_pinned = 0
    clamped_future = 0

    for entity_id, entry in entries.items():
        # Don't restore states this node itself wrote — it'll already
        # have them in memory from a previous lifetime, or its integration
        # will. Only restore from the peer.
        if entry.source_node == node_id:
            skipped_own_node += 1
            continue

        entry_updated = _parse_entry_timestamp(entry.last_updated)
        if entry_updated is None:
            _LOGGER.debug("Snapshot entry for %s has bad timestamp", entity_id)
            skipped_unparseable += 1
            continue

        if entry_updated > now + timedelta(seconds=CLOCK_SKEW_TOLERANCE):
            # AR-0035. The review prescribed clamping future stamps to "now",
            # but clamping alone does not achieve what it was asked to: a stamp
            # clamped to now is still newer than every pre-existing local
            # state, so the poisoned value keeps winning the freshness guard.
            # (A regression test asserting the security property rather than
            # the mechanism is what surfaced this.)
            #
            # So: honest clock skew is tolerated and clamped below; a stamp
            # implausibly far ahead is refused outright. No legitimate writer
            # produces one, and the timestamp is not evidence of authenticity
            # in any case — that is the AR-0005 signature's job.
            skipped_future += 1
            continue

        if entry_updated > now:
            # Within tolerance: the peer's clock is a little ahead. Clamp so
            # the comparison below stays meaningful.
            clamped_future += 1
            entry_updated = now

        if not _should_track(entity_id, cfg, excluded_ids):
            continue

        if not _attributes_within_budget(entity_id, entry.attributes):
            skipped_oversized += 1
            continue

        existing = hass.states.get(entity_id)
        if existing is not None and existing.last_updated > entry_updated and not at_boot:
            skipped_local_newer += 1
            continue

        # async_set is the documented public API for seeding state.
        #
        # AR-0037: and it is the one step of this loop that runs arbitrary Home
        # Assistant code, so it is the one step that can refuse. It validates
        # the entity_id it is handed and raises on a malformed one — the field
        # names come from a shared hash, so "no legitimate node writes that" is
        # a statement about the writer, not a guarantee about the reader.
        #
        # Every other guard above already isolates one bad entry (AR-0013).
        # Leaving this call outside a try meant the last step could still
        # abandon every entry after the offending one and skip the summary that
        # would have said so: a promotion that comes up silently cold, which is
        # the exact failure AR-0013 was raised to prevent.
        service = RESTORE_BY_SERVICE.get(entity_id.split(".", 1)[0])
        if _initial_state_is_pinned(hass, entity_id):
            # The operator pinned this one's startup state in config. That is
            # an explicit opt-out of restore; honour it rather than command
            # over the top of it.
            _LOGGER.info(
                "Not restoring %s — its config pins `initial_state`, which "
                "Home Assistant treats as authoritative at every startup. "
                "Remove the pin if this automation should follow the cluster.",
                entity_id,
            )
            skipped_pinned += 1
            continue
        try:
            if service is not None and entry.state in (STATE_ON, STATE_OFF):
                # This domain's component owns its own state, so writing the
                # state machine would show the value without applying it. Call
                # the service and let the component do the real work.
                domain, on_service, off_service = service
                await hass.services.async_call(
                    domain,
                    on_service if entry.state == STATE_ON else off_service,
                    {"entity_id": entity_id},
                    blocking=False,
                    context=context,
                )
            else:
                hass.states.async_set(
                    entity_id=entity_id,
                    new_state=entry.state,
                    attributes=entry.attributes,
                    force_update=False,
                    context=context,
                )
        except Exception:  # noqa: BLE001 — one refused entry, not a failed failover
            skipped_refused += 1
            _LOGGER.debug("State machine refused entry %s", entity_id, exc_info=True)
            continue
        restored += 1

    stats.record_restore(restored)

    # AR-0040: "a snapshot was there and none of it was applied" is the exact
    # signature of the bug the node-ops rehearsal found, and it was logged at
    # INFO — indistinguishable at a glance from a healthy restore. It is the
    # one outcome that means this integration did nothing during the only event
    # it exists for, so it is a warning with the reason breakdown attached.
    # Whose snapshot was it? Every entry skipped as `own-node` means this node
    # read back its OWN state, which is the ordinary case on a restart and not
    # a failure at all. Only a snapshot written by the PEER can represent state
    # that should have crossed and did not.
    #
    # Getting this wrong in the noisy direction would fire "Failover restored
    # NOTHING" on every single restart of the leader -- the cry-wolf that makes
    # an operator stop reading the one alert that matters.
    peer_snapshot = bool(entries) and skipped_own_node < len(entries)

    if entries and restored == 0:
        # AR-0040's rule holds: a snapshot was there and none of it was
        # applied, so this is visible without anyone knowing to look. What
        # changed is that the two cases no longer read identically.
        if peer_snapshot:
            if alerts is not None:
                # 🚨 AR-0065. This was a WARNING and nothing else, and it went
                # unread for hours on a live cluster that had just failed over
                # into an empty state machine. A log line has now twice proved
                # to be no surface at all.
                await alerts.async_event(
                    NOTIFY_RESTORED_NOTHING,
                    "Failover restored NOTHING",
                    (
                        f"This node took over and restored 0 of {len(entries)} entities "
                        f"from the shared snapshot, so it is running on its own local "
                        f"state rather than the state of the node it replaced. Skipped: "
                        f"{skipped_local_newer} local-newer, {skipped_own_node} own-node, "
                        f"{skipped_oversized} oversized, {skipped_unparseable} "
                        f"unparseable, {skipped_future} implausibly-future, "
                        f"{skipped_refused} refused."
                    ),
                )
            reason = "This node has promoted with no state from its peer."
        else:
            # 🚨 The wording that cost hours during the AR-0065 investigation.
            #
            # Every entry was written by this node, which is the ordinary case
            # on a restart that is not a promotion -- there was simply no peer
            # state to apply. The old text said "promoted with no state from
            # its peer" here too, so a routine restart and a real failure read
            # exactly alike, on the one message that distinguishes them.
            reason = (
                "All of them were written by this node, so there was no peer state to "
                "apply — normal on a restart that is not a promotion."
            )
        _LOGGER.warning(
            "Restored NOTHING from a snapshot that held %d entries. %s Skipped: "
            "%d local-newer, %d own-node, %d oversized, %d unparseable, %d "
            "implausibly-future, %d refused, %d initial_state-pinned.",
            len(entries),
            reason,
            skipped_local_newer,
            skipped_own_node,
            skipped_oversized,
            skipped_unparseable,
            skipped_future,
            skipped_refused,
            skipped_pinned,
        )

    # AR-0020: make staleness explicit rather than implicit. A promotion
    # restoring 3-second-old state and one restoring 29-minute-old state used
    # to log identically, so an operator could not tell a healthy failover from
    # a barely-legal one.
    snapshot_age = _snapshot_age_seconds(meta, now)
    age_text = f"{snapshot_age:.0f}s" if snapshot_age is not None else "unknown"
    _LOGGER.info("Snapshot age at restore: %s (max %ds)", age_text, max_age)
    if snapshot_age is not None and snapshot_age > max_age * STALE_SNAPSHOT_FRACTION:
        _LOGGER.warning(
            # Deliberately does not accuse the peer. This warning used to say
            # its flush loop had stopped, and said so about a completely
            # healthy node: `async_flush` skips when nothing has changed, so an
            # idle peer stops advancing the timestamp with nothing wrong at
            # all. The likely innocent cause goes first; the operator can
            # decide whether their peer should have been busy.
            "Restored from an AGEING snapshot — %.0fs old, against a %ds "
            "limit. On a quiet cluster this is expected: nothing changed, so "
            "nothing was written. Investigate only if the peer should have "
            "been busy in that window.",
            snapshot_age,
            max_age,
        )

    _LOGGER.info(
        "Restored %d entities from snapshot (skipped: %d local-newer, "
        "%d own-node, %d oversized, %d unparseable, %d "
        "implausibly-future, %d refused, %d initial_state-pinned) — "
        "source meta=%s",
        restored,
        skipped_local_newer,
        skipped_own_node,
        skipped_oversized,
        skipped_unparseable,
        skipped_future,
        skipped_refused,
        skipped_pinned,
        meta,
    )
    if skipped_refused:
        # Not a debug line: this is state the operator believes was restored
        # and was not, during the one event the integration exists for.
        _LOGGER.warning(
            "%d snapshot entries were refused by the state machine and were "
            "skipped. The rest of the snapshot was restored normally. Enable "
            "debug logging for this integration to see which entities.",
            skipped_refused,
        )
    if skipped_future:
        _LOGGER.warning(
            "%d snapshot entries were stamped implausibly far in the future and "
            "were refused. Nothing legitimate writes those — treat this as a "
            "sign the shared hash has been tampered with.",
            skipped_future,
        )
    if clamped_future:
        # Worth a warning rather than a debug line: either the peer's clock is
        # wrong or somebody is writing entries by hand.
        _LOGGER.warning(
            "%d snapshot entries were stamped in the future and were clamped to "
            "now. Check clock sync between nodes, or whether the shared hash is "
            "being written by something other than a cluster node.",
            clamped_future,
        )


def _snapshot_age_seconds(meta: dict[str, Any], now: datetime) -> float | None:
    """Age of the snapshot as a whole, from its metadata. None if unreadable."""
    written = _parse_entry_timestamp(str(meta.get("last_snapshot_at") or ""))
    if written is None:
        return None
    return max(0.0, (now - written).total_seconds())


def _parse_entry_timestamp(raw: str) -> datetime | None:
    """Parse a stored ISO-8601 timestamp into a tz-aware UTC datetime.

    Returns None if it cannot be read, so the caller can skip that one entry.

    AR-0014: a naive timestamp compared against a tz-aware cutoff raises
    TypeError, which — inside the restore loop — took the whole restore down
    with it. Anything naive is assumed UTC, which is what this integration
    writes anyway.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _attributes_within_budget(entity_id: str, attributes: dict[str, Any]) -> bool:
    """Reject entries whose attributes exceed the per-entry budget (AR-0009)."""
    try:
        size = len(json.dumps(attributes, default=str).encode())
    except (TypeError, ValueError):
        _LOGGER.debug("Attributes for %s are not serialisable — skipping", entity_id)
        return False
    if size > MAX_ATTRIBUTE_BYTES:
        _LOGGER.warning(
            "Snapshot entry for %s carries %d bytes of attributes, above the "
            "%d byte cap — skipping it",
            entity_id,
            size,
            MAX_ATTRIBUTE_BYTES,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _should_track(
    entity_id: str, cfg: dict[str, Any], excluded_ids: frozenset[str] | None = None
) -> bool:
    """Decide whether an entity is in scope for snapshot/restore.

    AR-0038: the leadership entity is refused first, ahead of every list.

    `input_boolean` is in the default domain allowlist, and the documented way
    to drive `leadership_source: entity` is an `input_boolean` that Keepalived
    toggles — so by default the leader published its own "I am the leader" flag
    into the shared snapshot, and the standby restored it at boot, before the
    first leadership evaluation, and concluded that it was the leader as well.
    The integration handed the follower the false belief itself, and the
    follower then un-gated its automations and started writing to the shared
    hash: precisely the split brain AR-0017 exists to prevent.

    Ahead of the include/exclude precedence rather than inside it, because this
    is not a default that happens to be wrong. The signal is a statement about
    *this node*; no configuration makes replicating it correct, so an operator
    listing it in `include_entities` is expressing a mistake rather than an
    intention.
    """
    leadership_entity = cfg.get(CONF_LEADERSHIP_ENTITY)
    if leadership_entity and entity_id == leadership_entity:
        return False

    excludes: list[str] = cfg.get(CONF_EXCLUDE_ENTITIES) or []
    if entity_id in excludes:
        return False

    # Excluding a device is shorthand for excluding its entities, so it sits at
    # the same precedence -- above `include_entities`, because an operator who
    # has said "not this device" has said something more specific and more
    # recent than a domain allowlist they set at install.
    #
    # Pre-resolved rather than looked up here: this runs per entity per flush
    # (3,595 of them on the reference estate) and on every state change. The
    # set is recomputed when either registry changes, which is the only time
    # the answer can move.
    if excluded_ids and entity_id in excluded_ids:
        return False

    explicit_includes: list[str] = cfg.get(CONF_INCLUDE_ENTITIES) or []
    if entity_id in explicit_includes:
        return True

    domain = entity_id.split(".", 1)[0]
    domains: list[str] = cfg.get(CONF_INCLUDE_DOMAINS) or DEFAULT_INCLUDE_DOMAINS
    return domain in domains
