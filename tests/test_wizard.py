"""Wizard flow tests (ADR-001 wizard steps 2, 4 and 5).

ADR-001's wizard principles are clear, concise, easy: recommend Cold by
default, hide the warm-only fields unless Warm is chosen, and *generate* the
host configuration rather than documenting it. These test all three.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.cluster_state_sync.const import (
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
    CONF_HA_CONTAINER,
    CONF_HA_CONTAINER_IP,
    CONF_HA_UID,
    CONF_IOT_SUBNETS,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_PEER_HOST,
    CONF_REDIS_HOST,
    CONF_REDIS_PORT,
    CONF_SETTLE_DELAY,
    CONF_TOPOLOGY_MODEL,
    DEFAULT_FILESET_EXCLUSIONS,
    DEFAULT_FILESET_HOT_INTERVAL,
    DEFAULT_FILESET_MAX_BYTES,
    DEFAULT_FILESET_STALE_AFTER,
    DOCKER_ALL,
    DOMAIN,
    LEADERSHIP_LEASE,
    TOPOLOGY_COLD,
    TOPOLOGY_WARM,
)

BACKEND_INPUT = {
    CONF_REDIS_HOST: "valkey.invalid",
    CONF_REDIS_PORT: 6379,
    "redis_password": "hunter2",
    "redis_db": 2,
    CONF_CLUSTER_NAMESPACE: "testns",
    CONF_CLUSTER_SECRET: "a-shared-cluster-secret",
    "redis_use_tls": True,
    "redis_tls_ca_certs": "",
    CONF_NODE_ID: "tiger1",
    "snapshot_interval": 5,
    "restore_max_age": 1800,
}

WARM_INPUT = {
    CONF_IOT_SUBNETS: "192.168.50.0/24",
    CONF_BLOCK_DISCOVERY: True,
    CONF_DOCKER_NETWORK: DOCKER_ALL,
    CONF_HA_UID: 1000,
    CONF_HA_CONTAINER_IP: "192.168.1.60",
    CONF_SETTLE_DELAY: 15,
}


def topology_input(model: str) -> dict[str, object]:
    return {
        CONF_TOPOLOGY_MODEL: model,
        CONF_LEADERSHIP_SOURCE: LEADERSHIP_LEASE,
        CONF_PEER_HOST: "tiger2.lan",
        CONF_HA_CONTAINER: "homeassistant",
    }


def fileset_input(**overrides: object) -> dict[str, object]:
    """A full submission for the fileset step's Required fields.

    Every field is `vol.Required` with a default, but `async_configure` in
    these tests calls the flow directly rather than through a form that would
    have pre-filled those defaults, so the submitted dict has to carry all of
    them itself -- same reason `WARM_INPUT` above is a complete dict rather
    than a partial one.
    """
    data: dict[str, object] = {
        CONF_FILESET_ENABLED: False,
        CONF_FILESET_HOT_INTERVAL: DEFAULT_FILESET_HOT_INTERVAL,
        CONF_FILESET_EXCLUSIONS: list(DEFAULT_FILESET_EXCLUSIONS),
        CONF_FILESET_MAX_BYTES: DEFAULT_FILESET_MAX_BYTES,
        CONF_FILESET_STALE_AFTER: DEFAULT_FILESET_STALE_AFTER,
    }
    data.update(overrides)
    return data


async def reach_topology_step(hass: HomeAssistant, **backend_overrides: object) -> str:
    """Walk the flow as far as the topology step and return the flow id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "direct"}
    )
    backend_input = {**BACKEND_INPUT, **backend_overrides}
    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], backend_input)
    assert result["step_id"] == "topology", result
    return result["flow_id"]


async def reach_fileset_step(hass: HomeAssistant, model: str, **backend_overrides: object) -> str:
    """Walk the flow to the fileset step via either the cold or warm path.

    Cold goes `topology -> fileset`; warm goes `topology -> warm -> fileset`.
    Exercising both here is the point: `_fileset_schema` existing is not
    evidence the step is wired into either route.
    """
    flow_id = await reach_topology_step(hass, **backend_overrides)
    result = await hass.config_entries.flow.async_configure(flow_id, topology_input(model))
    if model == TOPOLOGY_WARM:
        assert result["step_id"] == "warm", result
        result = await hass.config_entries.flow.async_configure(flow_id, WARM_INPUT)
    assert result["step_id"] == "fileset", result
    return flow_id


# -- step 2: topology -------------------------------------------------------


async def test_backend_step_leads_to_the_topology_step(hass: HomeAssistant) -> None:
    """The flow no longer ends at the backend form; it continues into the wizard."""
    flow_id = await reach_topology_step(hass)
    assert flow_id


async def test_cold_skips_the_warm_only_step(hass: HomeAssistant) -> None:
    """ADR-001: warm-only fields are hidden unless Warm is chosen.

    Production change that would make this fail: showing the firewall step
    unconditionally. Asking a cold-standby operator for IoT subnets and a
    container uid is asking them to configure a firewall they will never load.
    """
    flow_id = await reach_topology_step(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_COLD))

    assert result["step_id"] == "fileset", "cold must go straight to the fileset step"


async def test_warm_shows_the_warm_only_step(hass: HomeAssistant) -> None:
    flow_id = await reach_topology_step(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_WARM))

    assert result["step_id"] == "warm"


# -- step 4b: fileset replication -------------------------------------------


async def test_the_fileset_step_defaults_to_off(hass: HomeAssistant) -> None:
    """A new capability in an alpha integration that reads every credential in
    the config directory. The operator opts in."""
    from custom_components.cluster_state_sync.config_flow import _fileset_schema

    schema = _fileset_schema({})
    defaults = {str(k): k.default() for k in schema.schema if hasattr(k, "default")}
    assert defaults["fileset_enabled"] is False


async def test_the_fileset_step_offers_the_measured_exclusions(
    hass: HomeAssistant,
) -> None:
    from custom_components.cluster_state_sync.config_flow import _fileset_schema

    schema = _fileset_schema({})
    defaults = {str(k): k.default() for k in schema.schema if hasattr(k, "default")}
    assert "*.bak-*" in defaults["fileset_exclusions"]
    assert "core.restore_state" in defaults["fileset_exclusions"]


async def test_fileset_step_is_reached_from_the_cold_path_and_lands_on_the_entry(
    hass: HomeAssistant,
) -> None:
    """`_fileset_schema` existing does not mean the step is reachable.

    Drives the real flow, cold route (`topology -> fileset`), and confirms the
    operator's chosen fileset values -- not just the schema's defaults -- land
    in the entry `async_create_entry` actually produces.
    """
    flow_id = await reach_fileset_step(hass, TOPOLOGY_COLD)

    chosen = fileset_input(
        fileset_enabled=True,
        fileset_hot_interval=45,
        fileset_exclusions=["*.bak-*", "core.restore_state", "*.tmp"],
        fileset_max_bytes=256 * 1024 * 1024,
        fileset_stale_after=900,
    )
    result = await hass.config_entries.flow.async_configure(flow_id, chosen)
    assert result["step_id"] == "bundle", result

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FILESET_ENABLED] is True
    assert result["data"][CONF_FILESET_HOT_INTERVAL] == 45
    assert result["data"][CONF_FILESET_EXCLUSIONS] == [
        "*.bak-*",
        "core.restore_state",
        "*.tmp",
    ]
    assert result["data"][CONF_FILESET_MAX_BYTES] == 256 * 1024 * 1024
    assert result["data"][CONF_FILESET_STALE_AFTER] == 900


async def test_fileset_step_is_reached_from_the_warm_path_and_chains_to_bundle(
    hass: HomeAssistant,
) -> None:
    """The other route in: `topology -> warm -> fileset -> bundle`."""
    flow_id = await reach_fileset_step(hass, TOPOLOGY_WARM)

    chosen = fileset_input(fileset_enabled=True)
    result = await hass.config_entries.flow.async_configure(flow_id, chosen)
    assert result["step_id"] == "bundle", result

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FILESET_ENABLED] is True


async def test_enabling_fileset_without_a_cluster_secret_is_refused(
    hass: HomeAssistant,
) -> None:
    """`derive_fileset_key("")` is a perfectly valid, deterministic AES key.

    An empty cluster secret means publishing `.storage` -- every refresh token
    and integration credential -- into shared Valkey sealed with a key any
    attacker can derive independently. `async_setup_entry` already refuses to
    start the publisher in that case, but that is one runtime log line after
    the operator believes they finished the wizard. Catch it here instead: the
    step re-shows itself with an error rather than accepting the answer.
    """
    flow_id = await reach_fileset_step(hass, TOPOLOGY_COLD, cluster_secret="")

    result = await hass.config_entries.flow.async_configure(
        flow_id, fileset_input(fileset_enabled=True)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fileset"
    assert result["errors"] == {"base": "fileset_needs_secret"}


# -- step 5: bundle generation ---------------------------------------------


async def test_cold_flow_writes_a_bundle_and_creates_the_entry(
    hass: HomeAssistant,
) -> None:
    """The end-to-end cold path: five answers in, files on disk, entry created."""
    flow_id = await reach_topology_step(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_COLD))
    assert result["step_id"] == "fileset"
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    assert result["step_id"] == "bundle"

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] is FlowResultType.CREATE_ENTRY

    bundle_dir = Path(hass.config.path(BUNDLE_DIR_NAME))
    assert bundle_dir.is_dir()
    written = {p.name for p in bundle_dir.iterdir()}
    assert "notify_master.sh" in written
    assert "INSTALL.md" in written
    assert not [n for n in written if n.endswith(".nft")], "cold needs no firewall"


async def test_warm_flow_writes_the_firewall_variants(hass: HomeAssistant) -> None:
    """The warm path emits the rulesets, and the operator's answers reach them."""
    flow_id = await reach_topology_step(hass)
    await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_WARM))
    result = await hass.config_entries.flow.async_configure(flow_id, WARM_INPUT)
    assert result["step_id"] == "fileset"
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    assert result["step_id"] == "bundle"

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] is FlowResultType.CREATE_ENTRY

    bundle_dir = Path(hass.config.path(BUNDLE_DIR_NAME))
    written = {p.name for p in bundle_dir.iterdir()}
    assert "follower-host.nft" in written
    assert "nft-safety-revert.sh" in written
    assert "192.168.50.0/24" in (bundle_dir / "follower-host.nft").read_text()


async def test_generated_scripts_are_executable(hass: HomeAssistant) -> None:
    """Keepalived execs these directly; a non-executable script fails at failover.

    Production change that would make this fail: writing the files without
    setting the mode, which leaves the operator to discover it during a
    promotion.
    """
    flow_id = await reach_topology_step(hass)
    await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_COLD))
    await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        await hass.config_entries.flow.async_configure(flow_id, {})

    bundle_dir = Path(hass.config.path(BUNDLE_DIR_NAME))
    for script in bundle_dir.glob("*.sh"):
        assert script.stat().st_mode & 0o100, f"{script.name} is not executable"


async def test_bundle_step_tells_the_operator_where_the_files_are(
    hass: HomeAssistant,
) -> None:
    """ADR-001 wants the bundle shown on screen, not silently deposited."""
    flow_id = await reach_topology_step(hass)
    await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_COLD))
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())

    placeholders = result.get("description_placeholders") or {}
    assert BUNDLE_DIR_NAME in str(placeholders.get("bundle_path", ""))
    assert placeholders.get("file_list")


async def test_topology_choice_is_persisted_on_the_entry(hass: HomeAssistant) -> None:
    """The entry must record the model, so options-flow edits regenerate correctly."""
    flow_id = await reach_topology_step(hass)
    await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_COLD))
    await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["data"][CONF_TOPOLOGY_MODEL] == TOPOLOGY_COLD
    assert result["data"][CONF_PEER_HOST] == "tiger2.lan"
    # Backend config from the first step must survive the extra steps.
    assert result["data"][CONF_CLUSTER_SECRET] == "a-shared-cluster-secret"


# -- options flow and teardown ---------------------------------------------


async def test_options_flow_edits_tunables_without_removing_the_entry(
    hass: HomeAssistant,
) -> None:
    """Changing the snapshot interval must not mean re-running setup.

    The options flow is the only path that does not re-run the wizard, so it is
    the one an operator uses in anger.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **BACKEND_INPUT,
            **topology_input(TOPOLOGY_COLD),
            "redis_use_sentinel": False,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        return_value=None,
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**BACKEND_INPUT, "snapshot_interval": 30}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["snapshot_interval"] == 30


async def test_unload_stops_the_flush_loop_and_releases_the_lease(
    hass: HomeAssistant,
) -> None:
    """Teardown must leave nothing running and hand the lease back.

    Production change that would make this fail: unloading without releasing
    leadership. The peer would then wait out the full lease TTL before it could
    promote -- burning failover budget for no reason on an orderly shutdown.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.fakes import FakeBackend

    backend = FakeBackend()
    backend.lease_holder = "tiger1"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **BACKEND_INPUT,
            **topology_input(TOPOLOGY_COLD),
            "redis_use_sentinel": False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert backend.lease_holder is None, "lease should have been handed back"
    assert not backend.connected, "backend connection should be closed"
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_unload_is_safe_to_call_twice(hass: HomeAssistant) -> None:
    """A second unload must not raise on missing runtime data."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.fakes import FakeBackend

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **BACKEND_INPUT,
            **topology_input(TOPOLOGY_COLD),
            "redis_use_sentinel": False,
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.cluster_state_sync.RedisBackend",
        return_value=FakeBackend(),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
