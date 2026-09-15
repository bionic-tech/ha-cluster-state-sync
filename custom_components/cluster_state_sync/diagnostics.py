"""What to send when you open an issue, and what is deliberately withheld.

Home Assistant puts a **Download diagnostics** button on an integration's page
once this platform exists. It hands the operator a JSON file, which they open,
read, and *then* decide whether to attach. That ordering is the entire point:
nothing leaves the house without somebody having looked at it first.

🚨 **An allowlist, never a denylist.** Every config value is withheld unless it
is named in `SAFE_FIELDS` below. The alternative — listing what to hide — fails
the moment somebody adds a setting and forgets, and the failure is silent and
permanent, because the file is already in a public issue by the time anyone
notices. `scripts/export_public.py` makes the same argument about files; this is
the same argument about values.

`tests/test_diagnostics_download.py` fails when a config key exists that is in neither
list, so a new setting cannot be added without somebody deciding which it is.

**Identifying values are hashed rather than dropped.** A bare `**REDACTED**`
everywhere would be safe and useless: half of what makes a cluster problem
legible is *which node is which*, and whether the leader the standby names is
the node you are looking at. So node ids and hostnames appear as a short stable
digest — `node-3f2a1b` — which cannot be turned back into a hostname but keeps
every relationship between them intact.
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import (
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_NODE_ID,
    CONF_NOTIFY_CONDITIONS,
    DATA_ALERTS,
    DATA_CLUSTER_VIEW,
    DATA_CONFIG,
    DATA_INGRESS,
    DATA_STATS,
    DOMAIN,
)

#: Config keys reproduced verbatim. Everything here is a tunable, a boolean or
#: a list of Home Assistant domains — nothing that names a machine, a person, a
#: network or a secret.
#:
#: Adding a key here is a decision to publish it. The test that pins this list
#: exists so that decision is made on purpose rather than by omission.
SAFE_FIELDS: frozenset[str] = frozenset(
    {
        # what crosses
        CONF_INCLUDE_DOMAINS,
        CONF_NOTIFY_CONDITIONS,
        # timings and limits — the numbers most issues turn out to be about
        "snapshot_interval",
        "restore_max_age",
        "fileset_enabled",
        "fileset_hot_interval",
        "fileset_stale_after",
        "fileset_max_bytes",
        "statistics_enabled",
        # Named from the live entry rather than guessed: the first draft had
        # `statistics_interval`, which does not exist, so a harmless number came
        # out **REDACTED** on a real cluster. The allowlist erring toward
        # withholding is correct; withholding a tunable an issue turns on is
        # still a cost worth removing.
        "statistics_interval_minutes",
        "statistics_window_days",
        "statistics_max_bytes",
        "gate_recorder",
        "gate_automations",
        "ingress_verify_tls",
        "ingress_interval",
        "redis_port",
        "redis_db",
        "redis_use_tls",
        "redis_use_sentinel",
        "topology_model",
        "leadership_source",
        "history_matters",
        "history_database",
    }
)

#: Keys whose *shape* is useful but whose *value* is not ours to publish. These
#: are reported as a count or a hash rather than omitted, because "you have 4
#: excluded entities" answers a support question that "**REDACTED**" does not.
COUNTED_FIELDS: frozenset[str] = frozenset(
    {
        CONF_INCLUDE_ENTITIES,
        CONF_EXCLUDE_ENTITIES,
        CONF_EXCLUDE_DEVICES,
        "fileset_extra_paths",
        "fileset_extra_custom",
        "fileset_exclusions",
        "notify_services",
        "radio_watch",
    }
)


def _alias(value: str | None) -> str | None:
    """A stable, non-reversible stand-in for a name.

    `node-a` becomes `node-9c4e17`: the same input always gives the
    same alias, so "the leader is X and this node is X" survives, while the
    hostname does not. Truncated to six hex characters deliberately — long
    enough not to collide across a handful of nodes, short enough that nobody
    mistakes it for something they can reverse.
    """
    if not value:
        return None
    return "node-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:6]


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Allowlist the config: named keys pass, everything else is withheld."""
    out: dict[str, Any] = {}
    for key in sorted(cfg):
        value = cfg[key]
        if key in SAFE_FIELDS:
            out[key] = value
        elif key in COUNTED_FIELDS:
            out[key] = f"{len(value)} item(s), values withheld" if value else "none set"
        elif key == CONF_NODE_ID:
            out[key] = _alias(value)
        else:
            out[key] = REDACTED
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Everything worth sending with a bug report, and nothing else."""
    runtime: dict[str, Any] = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}) or {}
    cfg: dict[str, Any] = runtime.get(DATA_CONFIG) or {**entry.data, **entry.options}

    report: dict[str, Any] = {
        "_what_this_is": [
            "Diagnostics for the cluster_state_sync integration.",
            "",
            "SAFE TO ATTACH TO AN ISSUE, and you should read it first anyway --",
            "that is why Home Assistant hands you the file instead of uploading it.",
            "",
            "Withheld: every password, the cluster secret, TLS material, hostnames,",
            "addresses, file paths, your ingress URL, and the names of your notify",
            "services. Config values are ALLOWLISTED -- anything not explicitly",
            "marked safe shows as **REDACTED**, so a setting added later is withheld",
            "by default rather than published by accident.",
            "",
            "Node names appear as a stable digest such as node-9c4e17. The same node",
            "always gets the same one, so you can still see which node leads and",
            "whether the peer agrees, without publishing a hostname.",
            "",
            "Entity IDs are never included. Counts are.",
        ],
        "integration": {
            "version": (await _manifest_version(hass)),
            "config_entry_version": entry.version,
            "config_entry_state": str(entry.state),
        },
        "home_assistant": {"version": HA_VERSION},
        "config": _redact_config(cfg),
    }

    view = runtime.get(DATA_CLUSTER_VIEW)
    if view is not None:
        report["cluster"] = {
            "this_node": _alias(cfg.get(CONF_NODE_ID)),
            "leader": _alias(getattr(view, "leader", None)),
            "this_node_is_leader": bool(
                getattr(view, "leader", None)
                and getattr(view, "leader", None) == cfg.get(CONF_NODE_ID)
            ),
            "members_seen": getattr(view, "members", None),
            "snapshot_written_by": _alias(getattr(view, "snapshot_source", None)),
            "snapshot_entries": getattr(view, "entry_count", None),
            "snapshot_at": str(getattr(view, "snapshot_at", None) or ""),
        }

    stats = runtime.get(DATA_STATS)
    if stats is not None:
        report["last_restore"] = {
            "restored_count": getattr(stats, "restored_count", None),
            "last_restore_at": str(getattr(stats, "last_restore_at", None) or ""),
            "oversized_entities": getattr(stats, "oversized", None),
        }

    probe = runtime.get(DATA_INGRESS)
    if probe is not None:
        # The URL itself is the operator's own domain: withheld, but whether it
        # answers is the whole question in an ingress issue.
        report["ingress"] = {
            "url": REDACTED,
            "consecutive_failures": getattr(probe, "consecutive_failures", None),
        }

    alerts = runtime.get(DATA_ALERTS)
    if alerts is not None:
        report["alerts"] = {
            "conditions_chosen": sorted(cfg.get(CONF_NOTIFY_CONDITIONS) or []),
            "services_configured": len(cfg.get("notify_services") or []),
            "currently_active": sorted(getattr(alerts, "_active", set()) or []),
        }

    return report


async def _manifest_version(hass: HomeAssistant) -> str | None:
    """The shipped version, read the way Home Assistant reads it."""
    try:
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(hass, DOMAIN)
    except Exception:  # noqa: BLE001 -- diagnostics must never fail to render
        return None
    return integration.version and str(integration.version)
