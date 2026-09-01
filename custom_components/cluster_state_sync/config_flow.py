"""Config flow for Cluster State Sync."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import instance_id
from homeassistant.helpers.selector import (
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
    CONF_BLOCK_DISCOVERY,
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_DOCKER_NETWORK,
    CONF_FILESET_ENABLED,
    CONF_FILESET_EXCLUSIONS,
    CONF_FILESET_HOT_INTERVAL,
    CONF_FILESET_MAX_BYTES,
    CONF_FILESET_STALE_AFTER,
    CONF_GATE_AUTOMATIONS,
    CONF_GATE_RECORDER,
    CONF_HA_CONFIG_PATH,
    CONF_HA_CONTAINER,
    CONF_HA_CONTAINER_IP,
    CONF_HA_UID,
    CONF_IOT_SUBNETS,
    CONF_LEADERSHIP_ENTITY,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_PEER_HOST,
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
    DEFAULT_FILESET_HOT_INTERVAL,
    DEFAULT_FILESET_MAX_BYTES,
    DEFAULT_FILESET_STALE_AFTER,
    DEFAULT_HA_CONFIG_PATH,
    DEFAULT_LEADERSHIP_SOURCE,
    DEFAULT_REDIS_DB,
    DEFAULT_REDIS_PORT,
    DEFAULT_RESTORE_MAX_AGE,
    DEFAULT_SETTLE_DELAY,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEFAULT_TOPOLOGY_MODEL,
    DOCKER_NETWORK_MODES,
    DOMAIN,
    LEADERSHIP_SOURCES,
    TOPOLOGY_MODELS,
    TOPOLOGY_WARM,
    validate_namespace,
)
from .util import parse_sentinel_hosts

_LOGGER = logging.getLogger(__name__)

# AR-0006/AR-0007: these fields are rendered as password inputs so they are not
# shouldered off the screen during setup. Note what that does and does not buy
# you — HA stores config entries as plaintext JSON under `.storage/`, so masking
# is a shoulder-surfing control, not encryption at rest. The README says so
# explicitly rather than letting the dots imply a protection that isn't there.
_SECRET_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

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


def _direct_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_REDIS_HOST, default=d.get(CONF_REDIS_HOST, "valkey.lan")): str,
            vol.Required(
                CONF_REDIS_PORT, default=d.get(CONF_REDIS_PORT, DEFAULT_REDIS_PORT)
            ): vol.All(int, vol.Range(min=1, max=65535)),
            vol.Optional(CONF_REDIS_USERNAME, default=d.get(CONF_REDIS_USERNAME, "")): str,
            vol.Optional(
                CONF_REDIS_PASSWORD, default=d.get(CONF_REDIS_PASSWORD, "")
            ): _SECRET_FIELD,
            vol.Required(CONF_REDIS_DB, default=d.get(CONF_REDIS_DB, DEFAULT_REDIS_DB)): vol.All(
                int, vol.Range(min=0, max=15)
            ),
            vol.Required(
                CONF_CLUSTER_NAMESPACE,
                default=d.get(CONF_CLUSTER_NAMESPACE, DEFAULT_CLUSTER_NAMESPACE),
            ): vol.All(str, validate_namespace),
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
            ): vol.All(int, vol.Range(min=1, max=300)),
            vol.Required(
                CONF_RESTORE_MAX_AGE,
                default=d.get(CONF_RESTORE_MAX_AGE, DEFAULT_RESTORE_MAX_AGE),
            ): vol.All(int, vol.Range(min=60, max=86400)),
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
            vol.Required(CONF_REDIS_DB, default=d.get(CONF_REDIS_DB, DEFAULT_REDIS_DB)): vol.All(
                int, vol.Range(min=0, max=15)
            ),
            vol.Required(
                CONF_CLUSTER_NAMESPACE,
                default=d.get(CONF_CLUSTER_NAMESPACE, DEFAULT_CLUSTER_NAMESPACE),
            ): vol.All(str, validate_namespace),
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
            ): vol.All(int, vol.Range(min=1, max=300)),
            vol.Required(
                CONF_RESTORE_MAX_AGE,
                default=d.get(CONF_RESTORE_MAX_AGE, DEFAULT_RESTORE_MAX_AGE),
            ): vol.All(int, vol.Range(min=60, max=86400)),
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
            vol.Required(CONF_PEER_HOST, default=d.get(CONF_PEER_HOST, "")): str,
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
            vol.Required(CONF_HA_UID, default=d.get(CONF_HA_UID, 1000)): vol.All(
                int, vol.Range(min=0, max=65535)
            ),
            vol.Optional(CONF_HA_CONTAINER_IP, default=d.get(CONF_HA_CONTAINER_IP, "")): str,
            vol.Required(
                CONF_SETTLE_DELAY,
                default=d.get(CONF_SETTLE_DELAY, DEFAULT_SETTLE_DELAY),
            ): vol.All(int, vol.Range(min=0, max=300)),
        }
    )


def _fileset_schema(d: dict[str, Any]) -> vol.Schema:
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
            ): vol.All(int, vol.Range(min=10, max=3600)),
            vol.Required(
                CONF_FILESET_EXCLUSIONS,
                default=list(d.get(CONF_FILESET_EXCLUSIONS, DEFAULT_FILESET_EXCLUSIONS)),
            ): vol.All(cv.ensure_list, [str]),
            vol.Required(
                CONF_FILESET_MAX_BYTES,
                default=d.get(CONF_FILESET_MAX_BYTES, DEFAULT_FILESET_MAX_BYTES),
            ): vol.All(int, vol.Range(min=1024 * 1024)),
            vol.Required(
                CONF_FILESET_STALE_AFTER,
                default=d.get(CONF_FILESET_STALE_AFTER, DEFAULT_FILESET_STALE_AFTER),
            ): vol.All(int, vol.Range(min=60)),
        }
    )


class ClusterStateSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial UI setup."""

    VERSION = 1

    def __init__(self) -> None:
        # Accumulates across steps; each step folds its answers in.
        self._data: dict[str, Any] = {}
        self._bundle_files: list[str] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """First step: choose direct or sentinel mode."""
        return self.async_show_menu(
            step_id="user",
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
            data = {**user_input, CONF_REDIS_USE_SENTINEL: use_sentinel}
            # Test the connection before going further — no point walking the
            # operator through four more steps to fail on a typo in step one.
            try:
                await _test_backend(data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Backend connectivity test failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._data = data
                return await self.async_step_topology()

        defaults = {CONF_NODE_ID: await _default_node_id(self.hass)}
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
            return await self.async_step_fileset()

        return self.async_show_form(step_id="topology", data_schema=_topology_schema(self._data))

    # -- ADR-001 wizard step 4: warm-only --------------------------------

    async def async_step_warm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Firewall inputs. Only reached when Warm was chosen."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_fileset()

        return self.async_show_form(step_id="warm", data_schema=_warm_schema(self._data))

    # -- wizard step 4b: fileset replication (design 2026-08-29) -----------

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
            step_id="fileset", data_schema=_fileset_schema(self._data), errors=errors
        )

    # -- ADR-001 wizard step 5: generate the bundle ------------------------

    async def async_step_bundle(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Write the host bundle and show the operator where it landed.

        The files are written before the confirmation form rather than after,
        so the step can list what was actually produced instead of promising
        what it intends to produce.
        """
        if self._bundle_files is None:
            self._bundle_files = await self.hass.async_add_executor_job(
                write_bundle, self.hass.config.path(BUNDLE_DIR_NAME), self._data
            )

        if user_input is not None:
            await self.async_set_unique_id(
                f"{self._data[CONF_CLUSTER_NAMESPACE]}@{self._data[CONF_NODE_ID]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Cluster Sync ({self._data[CONF_NODE_ID]})",
                data=self._data,
            )

        return self.async_show_form(
            step_id="bundle",
            data_schema=vol.Schema({}),
            description_placeholders={
                "bundle_path": self.hass.config.path(BUNDLE_DIR_NAME),
                "file_list": "\n".join(f"- {name}" for name in self._bundle_files),
                "model": self._data.get(CONF_TOPOLOGY_MODEL, DEFAULT_TOPOLOGY_MODEL),
            },
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
