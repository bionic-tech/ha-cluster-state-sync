"""Wizard flow tests (ADR-001 wizard steps 2, 4 and 5).

ADR-001's wizard principles are clear, concise, easy: recommend Cold by
default, hide the warm-only fields unless Warm is chosen, and *generate* the
host configuration rather than documenting it. These test all three.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
import voluptuous as vol

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
    CONF_HA_CONFIG_PATH,
    CONF_HA_CONTAINER,
    CONF_HA_CONTAINER_IP,
    CONF_HA_UID,
    CONF_IOT_SUBNETS,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
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
    # Every flow now opens with the no-warranty acknowledgement, and the suite
    # goes through it rather than around it -- routing around the step would
    # stop it proving that nobody reaches configuration without being told.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_risk": True}
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
    # ADR-010 inserted a history step between backend and topology: the answer
    # decides which standby models topology may even offer, so it has to come
    # first. Default here is the permissive combination (shared database), so
    # existing tests still see both models on the next screen.
    assert result["step_id"] == "history", result
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"history_matters": False, "history_database": "shared"}
    )
    assert result["step_id"] == "topology", result
    return result["flow_id"]


DOMAINS_INPUT: dict[str, object] = {"include_domains": [], "include_entities": []}
CONTAINER_INPUT: dict[str, object] = {"ha_start_mode": "docker"}
ALERTS_INPUT: dict[str, object] = {"notify_conditions": [], "notify_services": []}
"""v0.4.2. Deliberately empty: the step is optional and this walks the path of
an operator who clicks straight past it, which must still produce a working
entry -- the persistent notification needs no configuration at all."""
"""`docker start`, the default: most installs never touch compose mode."""
"""Empty on purpose: `async_setup_entry` falls back to DEFAULT_INCLUDE_DOMAINS,
so this walks the path an operator takes when they accept the defaults."""


async def reach_fileset_step(hass: HomeAssistant, model: str, **backend_overrides: object) -> str:
    """Walk the flow to the fileset step via either the cold or warm path.

    Cold goes `topology -> domains -> fileset`; warm goes
    `topology -> warm -> domains -> fileset`. Exercising both here is the
    point: `_fileset_schema` existing is not evidence the step is wired into
    either route, and the `domains` step sits on both.
    """
    flow_id = await reach_topology_step(hass, **backend_overrides)
    result = await hass.config_entries.flow.async_configure(flow_id, topology_input(model))
    if model == TOPOLOGY_WARM:
        assert result["step_id"] == "warm", result
        result = await hass.config_entries.flow.async_configure(flow_id, WARM_INPUT)
    assert result["step_id"] == "domains", "both routes pass through the domains step"
    result = await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    assert result["step_id"] == "alerts", "the alerting step sits between domains and container"
    result = await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    assert result["step_id"] == "container"
    result = await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
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

    assert result["step_id"] == "domains", "cold reaches domains before the fileset step"


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
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})

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
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})

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
    assert result["step_id"] == "domains"
    result = await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    assert result["step_id"] == "alerts"
    result = await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    assert result["step_id"] == "container"
    result = await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    assert result["step_id"] == "fileset"
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    assert result["step_id"] == "bundle"

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})

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
    assert result["step_id"] == "domains"
    result = await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    assert result["step_id"] == "alerts"
    result = await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    assert result["step_id"] == "container"
    result = await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    assert result["step_id"] == "fileset"
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    assert result["step_id"] == "bundle"

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})

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
    await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
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
    await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())

    placeholders = result.get("description_placeholders") or {}
    assert BUNDLE_DIR_NAME in str(placeholders.get("bundle_path", ""))
    assert placeholders.get("file_list")


async def test_topology_choice_is_persisted_on_the_entry(hass: HomeAssistant) -> None:
    """The entry must record the model, so options-flow edits regenerate correctly."""
    flow_id = await reach_topology_step(hass)
    await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_COLD))
    await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})

    assert result["data"][CONF_TOPOLOGY_MODEL] == TOPOLOGY_COLD
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
    # v0.4.2 split this into a menu: connection settings and alerting have
    # nothing to do with each other, and reading past six connection fields to
    # change a phone number is how an operator edits the wrong one.
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection"}
    )
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


@pytest.mark.parametrize("model", [TOPOLOGY_COLD, TOPOLOGY_WARM])
async def test_every_wizard_step_survives_the_frontend(hass: HomeAssistant, model: str) -> None:
    """🚨 Renders EVERY step, not just the first form.

    Home Assistant serialises each step's schema to JSON before sending it to
    the browser, and `voluptuous_serialize` cannot convert a bare callable. Any
    step carrying one raises `ValueError: Unable to convert schema`, the request
    500s, and the operator gets "unknown error occurred" with nothing useful in
    it.

    This has now happened twice on the same integration. The first fix added a
    guard that walked only as far as the backend step, so `cv.ensure_list` on the
    fileset step sailed straight past it and broke a live setup a day later. A
    guard that covers some of the steps is worth very little: the operator meets
    all of them.

    Both topology models, because warm reaches a step cold never shows.

    Production change this catches: any custom validator in any step's schema.
    """
    from homeassistant.helpers import config_validation as cv
    import voluptuous_serialize

    def render(result: dict) -> None:
        schema = result.get("data_schema")
        if schema is not None:
            voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    render(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_risk": True}
    )
    render(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "direct"}
    )
    render(result)

    flow_id = result["flow_id"]
    with patch("custom_components.cluster_state_sync.config_flow._test_backend", return_value=None):
        result = await hass.config_entries.flow.async_configure(flow_id, BACKEND_INPUT)
    render(result)  # history (ADR-010)

    # A shared database keeps BOTH standby models on offer, which this test
    # needs -- it is parameterised over cold and warm. The dedicated-database
    # path deliberately hides warm, and is covered by its own test below.
    assert result["step_id"] == "history", result
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"history_matters": True, "history_database": "shared"}
    )
    render(result)  # topology

    result = await hass.config_entries.flow.async_configure(flow_id, topology_input(model))
    render(result)  # warm-only step, or fileset

    if result["step_id"] == "warm":
        result = await hass.config_entries.flow.async_configure(flow_id, WARM_INPUT)
        render(result)

    assert result["step_id"] == "domains", result
    result = await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    result = await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    render(result)
    result = await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    render(result)

    assert result["step_id"] == "fileset", result
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    render(result)  # bundle


# -- numeric fields show their value (owner report, 2026-09-03) --------------


def _serialised_fields(schema) -> dict[str, dict]:
    """What the frontend actually receives for a step."""
    from homeassistant.helpers import config_validation as cv
    import voluptuous_serialize

    return {
        f["name"]: f
        for f in voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
    }


def test_no_numeric_field_renders_as_a_naked_slider() -> None:
    """Production change that would make this fail: reverting any numeric field
    to `vol.All(int, vol.Range(...))`.

    That form serialises to a bare bounded integer, which the frontend draws as
    a slider with no value on it -- the number is invisible until you grab the
    handle. Every field here has to match the other node, and a value you cannot
    read without changing it is a value nobody checks. Reported against the
    database field, fixed for all of them.
    """
    from custom_components.cluster_state_sync.config_flow import (
        _direct_schema,
        _domains_schema,
        _fileset_schema,
        _warm_schema,
    )

    for name, schema in (
        ("direct", _direct_schema({})),
        ("warm", _warm_schema({})),
        ("fileset", _fileset_schema({})),
        ("domains", _domains_schema({})),
    ):
        for field, spec in _serialised_fields(schema).items():
            if spec.get("type") != "integer":
                continue
            assert "selector" in spec, (
                f"{name}.{field} serialises as a bare integer, so the frontend "
                f"renders it as a slider with no visible value"
            )


def test_the_database_field_keeps_its_value_on_screen() -> None:
    """The field the report was about: a short 0-15 range, so it keeps the
    slider, but a NumberSelector puts the number in a box beside it."""
    from custom_components.cluster_state_sync.config_flow import _direct_schema
    from custom_components.cluster_state_sync.const import CONF_REDIS_DB

    spec = _serialised_fields(_direct_schema({}))[CONF_REDIS_DB]
    number = spec["selector"]["number"]
    assert number["mode"] == "slider"
    assert number["min"] == 0
    assert number["max"] == 15
    assert number["step"] == 1


def test_numeric_fields_still_yield_ints_not_floats() -> None:
    """Production change that would make this fail: dropping the `vol.Coerce(int)`
    from `_number`.

    A NumberSelector validates to float. Without the coercion the database is
    stored as `2.0`, which reaches the Redis client and every equality check
    against the peer's value as a float where an int is meant.
    """
    from custom_components.cluster_state_sync.config_flow import _direct_schema
    from custom_components.cluster_state_sync.const import (
        CONF_REDIS_DB,
        CONF_REDIS_PORT,
        CONF_SNAPSHOT_INTERVAL,
    )

    out = _direct_schema({})(
        {
            "redis_host": "valkey.lan",
            "redis_port": 6380,
            "redis_username": "u",
            "redis_password": "p",
            "redis_db": 2,
            "cluster_namespace": "prod",
            "node_id": "n1",
            "cluster_secret": "s",
            "redis_use_tls": False,
            "redis_tls_ca_certs": "",
            "snapshot_interval": 5,
            "restore_max_age": 1800,
        }
    )
    for field in (CONF_REDIS_DB, CONF_REDIS_PORT, CONF_SNAPSHOT_INTERVAL):
        assert isinstance(out[field], int), f"{field} came back as {type(out[field])}"
        assert not isinstance(out[field], bool)


def test_every_field_on_every_step_has_a_label() -> None:
    """Production change that would make this fail: adding a field to a schema
    without adding its string.

    Home Assistant falls back to the raw key, so the operator is asked for
    `redis_username` with no explanation of what it wants or that it may be
    left blank. That shipped: the field sat third on the connection form,
    unlabelled, through every install so far.

    The reverse direction matters too. Four labels lingered on the `direct` and
    `sentinel` steps for fields that live on `topology` -- harmless to the
    frontend, but they are what a crib sheet written from the translations gets
    wrong, and one was.
    """
    import json
    import pathlib

    from custom_components.cluster_state_sync.config_flow import (
        _direct_schema,
        _domains_schema,
        _fileset_schema,
        _sentinel_schema,
        _topology_schema,
        _warm_schema,
    )

    steps = {
        "direct": _direct_schema({}),
        "sentinel": _sentinel_schema({}),
        "topology": _topology_schema({}),
        "warm": _warm_schema({}),
        "fileset": _fileset_schema({}),
        "domains": _domains_schema({}),
    }
    root = pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync"
    for name in ("strings.json", "translations/en.json"):
        blob = json.loads((root / name).read_text(encoding="utf-8"))
        for step, schema in steps.items():
            labels = blob["config"]["step"][step].get("data") or {}
            fields = {str(k) for k in schema.schema}
            assert not fields - set(labels), (
                f"{name}: {step} fields with no label: {sorted(fields - set(labels))}"
            )
            assert not set(labels) - fields, (
                f"{name}: {step} labels with no field: {sorted(set(labels) - fields)}"
            )


# -- reconfigure (owner ask, 2026-09-04) --------------------------------------


async def _entry_for_reconfigure(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{BACKEND_INPUT[CONF_CLUSTER_NAMESPACE]}@{BACKEND_INPUT[CONF_NODE_ID]}",
        data={
            **BACKEND_INPUT,
            **topology_input(TOPOLOGY_COLD),
            "redis_use_sentinel": False,
            CONF_HA_CONFIG_PATH: "/wrong/path",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_can_fix_a_host_path_without_deleting_the_entry(
    hass: HomeAssistant,
) -> None:
    """Production change that would make this fail: removing async_step_reconfigure.

    Before it existed, correcting ha_config_path, ha_container, topology_model,
    peer_host or fileset_enabled meant deleting the entry and starting over --
    and starting over means retyping the cluster secret, whose failure mode is
    silence: every entry fails verification, the restore skips all of them, and
    both nodes still report themselves healthy.

    So fixing a one-character path mistake risked the one value that must never
    be retyped. Two of this fleet's four host-specific fields were wrong at
    first attempt, which is the measured version of that argument.
    """
    entry = await _entry_for_reconfigure(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    # Straight to the connection menu: the no-warranty gate was passed when the
    # entry was created and is not re-asked.
    assert result["step_id"] == "choose"

    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "direct"})
    assert result["step_id"] == "direct"

    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, BACKEND_INPUT)
    # ADR-010: history is asked before topology, on reconfigure too, because it
    # decides which standby models topology may offer.
    assert result["step_id"] == "history"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"history_matters": False, "history_database": "shared"}
    )
    assert result["step_id"] == "topology"

    fixed = {**topology_input(TOPOLOGY_COLD), CONF_HA_CONFIG_PATH: "/mnt/data/homeassistant"}
    result = await hass.config_entries.flow.async_configure(flow_id, fixed)
    assert result["step_id"] == "domains"
    result = await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    assert result["step_id"] == "alerts"
    result = await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    assert result["step_id"] == "container"
    result = await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    assert result["step_id"] == "fileset"
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    assert result["step_id"] == "bundle"

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HA_CONFIG_PATH] == "/mnt/data/homeassistant"


async def test_reconfigure_prefills_from_the_existing_entry(
    hass: HomeAssistant,
) -> None:
    """The point of re-walking rather than re-asking: every value the operator
    is not changing is already there, including the cluster secret."""
    entry = await _entry_for_reconfigure(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "direct"}
    )
    defaults = {
        str(key): key.default()
        for key in result["data_schema"].schema
        if key.default is not vol.UNDEFINED
    }
    assert defaults[CONF_CLUSTER_SECRET] == BACKEND_INPUT[CONF_CLUSTER_SECRET]
    assert defaults[CONF_NODE_ID] == BACKEND_INPUT[CONF_NODE_ID]
    assert defaults[CONF_CLUSTER_NAMESPACE] == BACKEND_INPUT[CONF_CLUSTER_NAMESPACE]


async def test_reconfigure_regenerates_the_bundle(hass: HomeAssistant) -> None:
    """A corrected ha_config_path is only half a fix: every host-side script
    embeds it, so the bundle has to be rebuilt or the files on disk still carry
    the wrong path."""
    entry = await _entry_for_reconfigure(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "direct"})
    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        return_value=None,
    ):
        await hass.config_entries.flow.async_configure(flow_id, BACKEND_INPUT)
    # ADR-010's history step sits between backend and topology on reconfigure
    # too -- the answer restricts which standby models topology offers, so it
    # cannot be skipped just because the entry already exists.
    await hass.config_entries.flow.async_configure(
        flow_id, {"history_matters": False, "history_database": "shared"}
    )
    fixed = {**topology_input(TOPOLOGY_COLD), CONF_HA_CONFIG_PATH: "/mnt/data/homeassistant"}
    await hass.config_entries.flow.async_configure(flow_id, fixed)
    await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, fileset_input())

    notify = Path(hass.config.path(BUNDLE_DIR_NAME)) / "notify_master.sh"
    assert notify.exists()
    assert "/mnt/data/homeassistant" in notify.read_text()


def test_the_bundle_screen_only_mentions_files_it_generated() -> None:
    """Production change that would make this fail: going back to one static
    sentence for every model.

    It used to tell every operator to load the firewall rules with
    `nft-safety-revert.sh` armed. Cold bundles contain neither, so most
    installs were pointed at a file that was not there — while `install.sh`,
    the one thing that mattered to them, went unmentioned.
    """
    from custom_components.cluster_state_sync.config_flow import _bundle_next_steps

    cold = _bundle_next_steps({}, ["install.sh", "INSTALL.md", "notify_master.sh"])
    assert "nft-safety-revert.sh" not in cold
    assert "install.sh" in cold
    assert "Standby first" in cold, "the install order is load-bearing"

    warm = _bundle_next_steps({}, ["install.sh", "nft-safety-revert.sh", "leader-host.nft"])
    assert "nft-safety-revert.sh" in warm


def test_the_bundle_screen_degrades_without_an_installer() -> None:
    """An older bundle, or one regenerated by a future change that drops the
    installer, must still say something true rather than nothing."""
    from custom_components.cluster_state_sync.config_flow import _bundle_next_steps

    text = _bundle_next_steps({}, ["INSTALL.md", "notify_master.sh"])
    assert "/etc/cluster-sync/" in text
    assert "install.sh" not in text


# -- the install assistant (owner ask, 2026-09-03) ----------------------------


def test_the_transfer_commands_move_this_nodes_bundle_to_this_nodes_host() -> None:
    """Production change that would make this fail: turning this into a
    copy-to-the-peer helper, which is what was originally asked for and is
    actively dangerous.

    Nine of the bundle's twenty-four files differ between nodes.
    `cluster-promoter.sh` embeds the node id; the notify scripts and the swap
    embed this host's config directory and container name. Installed on the
    peer, this bundle makes the peer present THIS node's id to the lease — and
    the lease renews on identity, so both would renew the same one and both
    would believe they lead. AR-0025's collision, reached by scp.
    """
    from custom_components.cluster_state_sync.config_flow import _transfer_commands

    text = _transfer_commands({CONF_HA_CONTAINER: "home-assistant-2"}, "/config/bundle", {})
    assert "docker exec home-assistant-2 tar" in text
    assert "/etc/cluster-sync" in text
    assert "install.sh --dry-run" in text


def test_the_transfer_uses_tar_not_docker_cp() -> None:
    """`docker cp` does not preserve permissions, and two files in the bundle
    are 0600 — the fileset key and the Valkey password."""
    from custom_components.cluster_state_sync.config_flow import _transfer_commands

    text = _transfer_commands({CONF_HA_CONTAINER: "ha"}, "/config/bundle", {})
    # Assert the mechanism, not the prose — the explanation names `docker cp`
    # precisely to say why it is not used.
    commands = [ln for ln in text.splitlines() if ln.startswith(("docker ", "sudo ", "ssh"))]
    assert any("-cf -" in ln and " tar " in ln for ln in commands)
    assert not any(ln.startswith("docker cp") for ln in commands)
    assert "0600" in text, "says why tar, so nobody 'simplifies' it back"


def test_a_remote_host_gets_ssh_commands_and_a_warning() -> None:
    from custom_components.cluster_state_sync.config_flow import _transfer_commands

    text = _transfer_commands(
        {CONF_HA_CONTAINER: "ha"},
        "/config/bundle",
        {"host": "tiger2.lan", "user": "deploy", "key_path": "/home/me/.ssh/id_ed25519"},
    )
    assert "deploy@tiger2.lan" in text
    assert "-i /home/me/.ssh/id_ed25519" in text
    assert "runs its own wizard" in text


def test_a_missing_key_path_offers_a_way_to_find_it() -> None:
    """The novice case the owner asked for: someone who does not know where
    their key lives should get a command, not a shrug."""
    from custom_components.cluster_state_sync.config_flow import _transfer_commands

    text = _transfer_commands({CONF_HA_CONTAINER: "ha"}, "/config/bundle", {"host": "tiger2.lan"})
    assert "find ~/.ssh" in text
    assert "-i " not in text, "no key flag when no key was given"


async def test_the_ssh_answers_never_reach_the_config_entry(
    hass: HomeAssistant,
) -> None:
    """Production change that would make this fail: merging the transfer form
    into `self._data`.

    Home Assistant stores config entries as unencrypted JSON, so a remembered
    hostname and key path would be two more facts sitting in `.storage` for
    anyone who can read `/config`. They are rendered and discarded.
    """
    flow_id = await reach_topology_step(hass)
    await hass.config_entries.flow.async_configure(flow_id, topology_input(TOPOLOGY_COLD))
    await hass.config_entries.flow.async_configure(flow_id, DOMAINS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, ALERTS_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, CONTAINER_INPUT)
    result = await hass.config_entries.flow.async_configure(flow_id, fileset_input())
    assert result["step_id"] == "bundle"

    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "transfer"})
    assert result["step_id"] == "transfer"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {"host": "secret-host.lan", "user": "root", "key_path": "/root/.ssh/id_rsa"},
    )
    assert result["step_id"] == "transfer_commands"
    assert "secret-host.lan" in result["description_placeholders"]["commands"]

    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["step_id"] == "bundle", "returns to the menu"

    with patch("custom_components.cluster_state_sync.RedisBackend") as backend_cls:
        backend_cls.return_value.connect.return_value = None
        result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "finish"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = json.dumps(result["data"])
    assert "secret-host.lan" not in stored
    assert "/root/.ssh/id_rsa" not in stored


# -- ADR-010: the history step FILTERS the standby models --------------------


async def test_dedicated_database_plus_history_offers_ONLY_cold(
    hass: HomeAssistant,
) -> None:
    """🚨 The combination that cannot work is not offered at all.

    Warm standby cannot carry history on a per-node database: swapping the
    recorder file requires Home Assistant stopped, and `recorder.disable` only
    makes it drop events rather than closing the file. A warm promotion that
    did the swap would be SLOWER than cold, because it adds a stop it would not
    otherwise pay.

    So the wizard removes the option rather than warning about it. Nobody has
    to understand why, and nobody can assemble a broken cluster by clicking
    past a caveat.
    """
    from homeassistant.helpers import config_validation as cv
    import voluptuous_serialize

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"accept_risk": True})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "direct"})
    with patch("custom_components.cluster_state_sync.config_flow._test_backend", return_value=None):
        result = await hass.config_entries.flow.async_configure(flow_id, BACKEND_INPUT)
    assert result["step_id"] == "history"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"history_matters": True, "history_database": "dedicated"}
    )
    # These same two answers are what makes statistics replication worth
    # offering, so the wizard asks about it here before reaching topology.
    assert result["step_id"] == "statistics"
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "statistics_enabled": True,
            "statistics_window_days": 30,
            "statistics_interval_minutes": 30,
        },
    )
    assert result["step_id"] == "topology"

    rendered = voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    field = next(f for f in rendered if f["name"] == "topology_model")
    # The selector nests its options under `selector.select.options` once
    # serialised for the frontend, which is the shape the UI actually receives.
    sel = field.get("selector", {}).get("select", {})
    raw = sel.get("options", field.get("options", []))
    offered = {o["value"] if isinstance(o, dict) else o for o in raw}
    assert offered == {"cold"}, f"warm was offered with a dedicated database: {offered}"


async def test_a_shared_database_keeps_both_models_available(
    hass: HomeAssistant,
) -> None:
    """One database both nodes use has nothing to swap, so warm is fine."""
    from homeassistant.helpers import config_validation as cv
    import voluptuous_serialize

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"accept_risk": True})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "direct"})
    with patch("custom_components.cluster_state_sync.config_flow._test_backend", return_value=None):
        await hass.config_entries.flow.async_configure(flow_id, BACKEND_INPUT)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"history_matters": True, "history_database": "shared"}
    )
    rendered = voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    field = next(f for f in rendered if f["name"] == "topology_model")
    # The selector nests its options under `selector.select.options` once
    # serialised for the frontend, which is the shape the UI actually receives.
    sel = field.get("selector", {}).get("select", {})
    raw = sel.get("options", field.get("options", []))
    offered = {o["value"] if isinstance(o, dict) else o for o in raw}
    assert offered == {"cold", "warm"}, offered


async def test_not_caring_about_history_leaves_both_models_available(
    hass: HomeAssistant,
) -> None:
    """The restriction exists to protect history. No history, no restriction."""
    from homeassistant.helpers import config_validation as cv
    import voluptuous_serialize

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {"accept_risk": True})
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "direct"})
    with patch("custom_components.cluster_state_sync.config_flow._test_backend", return_value=None):
        await hass.config_entries.flow.async_configure(flow_id, BACKEND_INPUT)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"history_matters": False, "history_database": "dedicated"}
    )
    rendered = voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    field = next(f for f in rendered if f["name"] == "topology_model")
    # The selector nests its options under `selector.select.options` once
    # serialised for the frontend, which is the shape the UI actually receives.
    sel = field.get("selector", {}).get("select", {})
    raw = sel.get("options", field.get("options", []))
    offered = {o["value"] if isinstance(o, dict) else o for o in raw}
    assert offered == {"cold", "warm"}, offered


def test_the_transfer_screen_says_how_to_get_itself_back() -> None:
    """AR-0059's last gap, and it was circular.

    INSTALL.md now explains how to recover an interrupted install — but it
    lives inside the container the bundle has not reached yet. An operator who
    loses this screen *before* running it cannot read the file that would tell
    them what to do. Telling someone to read a file they cannot get to is not
    instructions, so the screen carries the recovery itself.
    """
    from custom_components.cluster_state_sync.config_flow import _transfer_commands

    for peer in ({}, {"host": "peer.lan", "user": "ops"}):
        block = _transfer_commands(
            {"ha_container": "homeassistant"}, "/config/cluster_state_sync_bundle", peer
        )
        assert "Lost this before you ran it?" in block
        # It must name the command that reads the instructions without needing
        # the bundle to have been transferred first.
        assert "cat /config/cluster_state_sync_bundle/INSTALL.md" in block
        # And say the screen itself is reproducible.
        assert "reconfigur" in block.lower()


def test_the_transfer_screen_quotes_a_hostile_container_name() -> None:
    """The recovery line interpolates the container name into a shell command.

    It is validated at bundle time (AR-0043/0056), but this text is rendered
    from the config entry directly, before any bundle is built.
    """
    from custom_components.cluster_state_sync.config_flow import _transfer_commands

    block = _transfer_commands(
        {"ha_container": 'ha"; rm -rf /; #'}, "/config/cluster_state_sync_bundle", {}
    )
    assert 'rm -rf /; #"' not in block.replace("'", ""), "an unquoted container name"
    assert "'ha\"; rm -rf /; #'" in block or '"ha' in block
