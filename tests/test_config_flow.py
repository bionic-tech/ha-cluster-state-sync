"""Config-flow tests.

Phase 3 added required fields to both schemas (cluster secret, TLS) and
tightened validation on others. These check the flow still completes, and that
the new constraints actually reject bad input rather than decorating it.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
import voluptuous as vol

from custom_components.cluster_state_sync.const import (
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_NODE_ID,
    CONF_REDIS_HOST,
    CONF_REDIS_PASSWORD,
    CONF_REDIS_PORT,
    CONF_REDIS_USE_TLS,
    DOMAIN,
)


def direct_input(**overrides: Any) -> dict[str, Any]:
    data = {
        CONF_REDIS_HOST: "valkey.invalid",
        CONF_REDIS_PORT: 6379,
        CONF_REDIS_PASSWORD: "hunter2",
        "redis_db": 2,
        CONF_CLUSTER_NAMESPACE: "testns",
        CONF_CLUSTER_SECRET: "a-shared-cluster-secret",
        CONF_REDIS_USE_TLS: True,
        "redis_tls_ca_certs": "",
        CONF_NODE_ID: "tiger1",
        "snapshot_interval": 5,
        "restore_max_age": 1800,
    }
    data.update(overrides)
    return data


async def start_direct_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "direct"}
    )
    assert result["type"] is FlowResultType.FORM
    return result["flow_id"]


async def test_direct_backend_step_accepts_valid_input(hass: HomeAssistant) -> None:
    """A valid backend step advances into the wizard rather than saving.

    Phase 7 turned this into the first of several steps: the entry is created
    at the end of the topology/warm/bundle chain, not here. Entry creation is
    covered end to end in test_wizard.py.
    """
    flow_id = await start_direct_flow(hass)

    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, direct_input()
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "topology"


async def test_connection_failure_is_reported_not_saved(hass: HomeAssistant) -> None:
    """A backend we cannot reach must not become a config entry."""
    flow_id = await start_direct_flow(hass)

    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        side_effect=OSError("no route to host"),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, direct_input()
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_ar_0010_invalid_namespace_is_rejected_by_the_form(
    hass: HomeAssistant,
) -> None:
    """AR-0010 — a namespace with a colon must not reach the key builder.

    Production change that would make this fail: dropping `validate_namespace`
    from the schema, letting a crafted namespace address keys outside its own
    prefix.
    """
    flow_id = await start_direct_flow(hass)

    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(
            flow_id, direct_input(**{CONF_CLUSTER_NAMESPACE: "evil:namespace"})
        )


async def test_ar_0025_suggested_node_id_is_not_just_the_hostname(
    hass: HomeAssistant,
) -> None:
    """AR-0025 — the suggested node ID must distinguish two identical hosts.

    Production change that would make this fail: going back to a bare
    `default_node_id()` (the container hostname). Two nodes from the same
    compose file share a hostname, so both would claim one cluster identity and
    the restore's peer-trust check would answer wrong in both directions.
    """
    from custom_components.cluster_state_sync.backend import default_node_id
    from custom_components.cluster_state_sync.config_flow import _default_node_id

    suggested = await _default_node_id(hass)

    assert suggested != default_node_id()
    assert suggested.startswith(default_node_id())
    assert len(suggested) > len(default_node_id()) + 1


async def test_ar_0007_password_fields_are_masked(hass: HomeAssistant) -> None:
    """AR-0006/AR-0007 — credentials are not rendered as plain text inputs.

    Masking is a shoulder-surfing control only: HA writes config entries to
    `.storage/` as plaintext JSON either way, which the README states plainly
    rather than letting the dots imply encryption at rest.
    """
    from homeassistant.helpers.selector import TextSelector

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "direct"}
    )

    masked = {
        str(key)
        for key, value in result["data_schema"].schema.items()
        if isinstance(value, TextSelector)
        and value.config.get("type") == "password"
    }

    assert CONF_REDIS_PASSWORD in masked
    assert CONF_CLUSTER_SECRET in masked
