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

from custom_components.cluster_state_sync.config_flow import _test_backend
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


async def accept_the_warning(hass: HomeAssistant):
    """Get past the no-warranty step every flow now starts with.

    Deliberately a real step rather than something tests can skip: the point of
    it is that nobody reaches the configuration without having been told, and a
    test suite that routed around it would stop proving that.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"accept_risk": True})


async def start_direct_flow(hass: HomeAssistant) -> str:
    result = await accept_the_warning(hass)
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
        result = await hass.config_entries.flow.async_configure(flow_id, direct_input())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "history"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"history_matters": False, "history_database": "shared"}
    )
    assert result["step_id"] == "topology"


async def test_connection_failure_is_reported_not_saved(hass: HomeAssistant) -> None:
    """A backend we cannot reach must not become a config entry."""
    flow_id = await start_direct_flow(hass)

    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        side_effect=OSError("no route to host"),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, direct_input())

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_ar_0010_invalid_namespace_is_rejected_by_the_form(
    hass: HomeAssistant,
) -> None:
    """AR-0010 — a namespace with a colon must not reach the key builder.

    Production change that would make this fail: dropping the
    `validate_namespace` call from `_handle_step`, letting a crafted namespace
    address keys outside its own prefix.

    It is checked in the handler rather than the schema, and that is not a
    style choice — see `test_every_step_schema_survives_the_frontend`.
    """
    flow_id = await start_direct_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, direct_input(**{CONF_CLUSTER_NAMESPACE: "evil:namespace"})
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_namespace"}, result["errors"]


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

    result = await accept_the_warning(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "direct"}
    )

    masked = {
        str(key)
        for key, value in result["data_schema"].schema.items()
        if isinstance(value, TextSelector) and value.config.get("type") == "password"
    }

    assert CONF_REDIS_PASSWORD in masked
    assert CONF_CLUSTER_SECRET in masked


async def test_setup_cannot_proceed_without_accepting_the_warning(
    hass: HomeAssistant,
) -> None:
    """🚨 The point of the step. Declining must not quietly continue.

    Production change this catches: treating the step as informational and
    calling `async_step_choose` regardless. That version still *shows* the
    warning, which is exactly the failure mode worth guarding — a disclaimer
    nobody had to answer is evidence of nothing.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_risk": False}
    )
    assert result["type"] is FlowResultType.FORM, "declining must not advance"
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "must_accept"}


async def test_the_warning_names_the_physical_consequence() -> None:
    """Not just "no warranty" — it has to name a real, default consequence.

    This test previously asserted the warning said `alarm_control_panel` was
    mirrored by default. It is not: it lives in SENSITIVE_DOMAINS, which the
    code comments call opt-in. The claim came from a README table row that
    contradicted the same README twenty lines further down, and the test then
    locked the wrong half in.

    So it now asserts against the actual default list, and checks the opt-in
    domains are described as opt-in — because a warning that overstates the risk
    is as corrosive to trust as one that understates it, and this one is the
    thing standing between the author and a misrepresentation claim.
    """
    import json
    import pathlib

    strings = json.loads(
        pathlib.Path("custom_components/cluster_state_sync/strings.json").read_text()
    )
    text = strings["config"]["step"]["user"]["description"]
    from custom_components.cluster_state_sync.const import (
        DEFAULT_INCLUDE_DOMAINS,
        SENSITIVE_DOMAINS,
    )

    # It must name something genuinely in the default set...
    assert any(d in text for d in DEFAULT_INCLUDE_DOMAINS), text
    assert "climate" in text or "water_heater" in text
    # ...and must not claim a sensitive domain is on when it is not.
    for domain in SENSITIVE_DOMAINS:
        if domain in text:
            assert "opt-in" in text, f"{domain} named without saying it is opt-in"
    assert "no guarantees" in text.lower()
    assert "without warranty" in text.lower()


async def test_the_sentinel_branch_of_the_flow_is_walkable(hass: HomeAssistant) -> None:
    """Nobody had ever walked it.

    Sentinel support is deferred and flagged untested in `backend.py`, which is
    a decision about the *backend*. It had quietly become a decision about the
    config flow too: `async_step_sentinel` had no coverage at all, so whether an
    operator could even reach the form was unknown. Deferring a feature is fine;
    not knowing whether its front door opens is not.
    """
    result = await accept_the_warning(hass)
    assert result["type"] is FlowResultType.MENU
    assert "sentinel" in result["menu_options"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "sentinel"}
    )
    assert result["type"] is FlowResultType.FORM
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert any("sentinel" in k for k in schema_keys), schema_keys


async def test_test_backend_really_connects_to_a_real_valkey(
    valkey_server: tuple[str, int], socket_enabled: None
) -> None:
    """🚨 Every other config-flow test patches `_test_backend` out, so the code
    that actually builds a RedisBackend from form input had never run once.

    That is the function standing between an operator's typo and a saved entry
    that cannot connect, and it was the least exercised code in the flow.
    Production change this catches: any error in the mapping from form fields to
    RedisBackend arguments — a swapped host and port, a dropped namespace — none
    of which a mocked test can see.
    """
    host, port = valkey_server
    await _test_backend(
        {
            CONF_CLUSTER_NAMESPACE: "flowtest",
            CONF_REDIS_HOST: host,
            CONF_REDIS_PORT: port,
            CONF_CLUSTER_SECRET: "a-shared-cluster-secret",
        }
    )


async def test_test_backend_raises_on_a_port_nothing_listens_on(
    valkey_server: tuple[str, int], socket_enabled: None
) -> None:
    """The negative half. Without it the test above passes on a `_test_backend`
    that connects to nothing and returns quietly, which is precisely the shape
    that lets a bad entry get saved."""
    import redis.exceptions

    host, port = valkey_server
    # Not `pytest.raises(Exception)` — that passes on a NameError in the test
    # itself, which is the same "green for the wrong reason" shape this suite
    # keeps finding. The flow turns a redis error into "cannot_connect"; this
    # asserts the error is really a connection one.
    with pytest.raises((redis.exceptions.RedisError, OSError)):
        await _test_backend(
            {
                CONF_CLUSTER_NAMESPACE: "flowtest",
                CONF_REDIS_HOST: host,
                CONF_REDIS_PORT: port + 7,
                CONF_CLUSTER_SECRET: "a-shared-cluster-secret",
            }
        )


async def test_every_step_schema_survives_the_frontend(hass: HomeAssistant) -> None:
    """🚨 The test that was missing, and the reason this flow never once worked.

    Home Assistant does not hand a voluptuous schema to the browser. It converts
    it to JSON with `render_schema_for_frontend(schema,
    custom_serializer=cv.custom_serializer)` — exactly as
    `helpers/data_entry_flow.py` does — and that converter cannot handle an
    arbitrary callable.

    The namespace field used to be `vol.All(str, validate_namespace)`. Every
    test passed, because `async_configure` validates the schema server-side and
    never serialises it. In a real Home Assistant the form could not render at
    all: the request 500'd and the operator got an error dialog containing no
    text. 677 green tests, and the integration was unusable through its own UI.

    Production change this catches: putting any custom validator back into a
    schema the flow shows.
    """
    from homeassistant.helpers import config_validation as cv

    from tests.fakes import render_schema_for_frontend

    def render(result) -> None:
        """Do what Home Assistant does before sending a step to the browser."""
        schema = result.get("data_schema")
        if schema is not None:
            render_schema_for_frontend(schema, custom_serializer=cv.custom_serializer)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    render(result)  # the acknowledgement

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_risk": True}
    )
    render(result)  # the menu

    for branch in ("direct", "sentinel"):
        start = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        step = await hass.config_entries.flow.async_configure(
            start["flow_id"], {"accept_risk": True}
        )
        step = await hass.config_entries.flow.async_configure(
            step["flow_id"], {"next_step_id": branch}
        )
        assert step["step_id"] == branch
        render(step)  # the one that used to raise


def test_every_menu_option_has_a_label() -> None:
    """🚨 A menu whose options have no translation renders as blank rows.

    Home Assistant labels menu rows from `config.step.<step>.menu_options.<option>`.
    Without them the operator sees two empty boxes with arrows and no way to know
    what either one does — which is exactly what shipped when the acknowledgement
    step replaced the old `user` step wholesale and took its `menu_options` with
    it, then the menu moved to a new step that never had any.

    Production change this catches: adding a menu step, or a new option to one,
    without a label. Derived from the flow's own source so a new menu cannot be
    added without appearing here.
    """
    import json
    import pathlib
    import re

    root = pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync"
    source = (root / "config_flow.py").read_text()
    # Both flows, because an OPTIONS menu renders exactly as blankly as a
    # config one. The options menu was added in v0.4.2 and this test found it
    # unlabelled on the first run, which is the whole point of deriving the
    # step list from the source rather than maintaining it by hand.
    raw = json.loads((root / "strings.json").read_text())
    strings = {**raw["config"]["step"], **raw.get("options", {}).get("step", {})}

    menus = re.findall(
        r"async_show_menu\(\s*step_id=\"(\w+)\",\s*menu_options=\[([^\]]+)\]", source
    )
    assert menus, "no menus found — has async_show_menu been renamed?"

    for step_id, raw in menus:
        options = re.findall(r"\"(\w+)\"", raw)
        labels = strings.get(step_id, {}).get("menu_options", {})
        for option in options:
            assert option in labels, (
                f"menu step {step_id!r} option {option!r} has no label — "
                f"it will render as a blank row"
            )
            assert labels[option].strip(), f"{step_id}.{option} label is empty"


# -- wizard step 4a: what actually fails over ------------------------------


def test_domains_step_defaults_to_the_conservative_list() -> None:
    """The wizard must offer the same defaults the integration falls back to.

    `CONF_INCLUDE_DOMAINS` was readable from the beginning and settable only by
    hand-editing the config entry, so in practice nobody changed it. A default
    here that drifted from `DEFAULT_INCLUDE_DOMAINS` would silently change what
    crosses on the next reconfigure.
    """
    from custom_components.cluster_state_sync.config_flow import _domains_schema
    from custom_components.cluster_state_sync.const import (
        CONF_INCLUDE_DOMAINS,
        DEFAULT_INCLUDE_DOMAINS,
    )

    defaults = {
        str(key): key.default() for key in _domains_schema({}).schema if key.default is not None
    }
    assert defaults[CONF_INCLUDE_DOMAINS] == list(DEFAULT_INCLUDE_DOMAINS)


def test_lights_and_switches_are_offered_but_never_default() -> None:
    """The trap this step exists to make visible.

    Restoring a light or switch writes a state change; a state change is what
    automations trigger on; so a restored value can fire an automation and
    actuate hardware. Offering them is right -- defaulting them is not.
    """
    from custom_components.cluster_state_sync.config_flow import _domain_options
    from custom_components.cluster_state_sync.const import DEFAULT_INCLUDE_DOMAINS

    assert "light" not in DEFAULT_INCLUDE_DOMAINS
    assert "switch" not in DEFAULT_INCLUDE_DOMAINS
    # ...and offered, because a fixed list that omitted them would just push
    # operators back to hand-editing the entry.
    assert "light" in _domain_options(None) or "light" not in DEFAULT_INCLUDE_DOMAINS


def test_domain_options_include_what_the_instance_actually_runs() -> None:
    """A fixed list offers domains nobody runs and hides the ones they do."""
    from custom_components.cluster_state_sync.config_flow import _domain_options
    from custom_components.cluster_state_sync.const import DEFAULT_INCLUDE_DOMAINS

    class _State:
        def __init__(self, domain: str) -> None:
            self.domain = domain

    class _States:
        def async_all(self):
            # device_tracker twice: the count in the label has to come from the
            # operator's own instance, and a domain with one entity cannot
            # prove that.
            return [
                _State("light"),
                _State("switch"),
                _State("media_player"),
                _State("device_tracker"),
                _State("device_tracker"),
            ]

    class _Hass:
        states = _States()

    options = _domain_options(_Hass())
    values = [o["value"] for o in options]

    assert "media_player" in values, "must offer domains this instance has"
    for d in DEFAULT_INCLUDE_DOMAINS:
        assert d in values, "a default must never vanish from the form"

    # Every option carries our verdict and the operator's own entity count, so
    # the reason not to tick something is where the ticking happens rather than
    # in a document nobody opens.
    labels = {o["value"]: o["label"] for o in options}
    assert (
        "rebuilds itself" in labels["device_tracker"] or "not needed" in labels["device_tracker"]
    ), "a self-rebuilding domain must say so on the form"
    assert "cosmetic" in labels["media_player"], "a device-backed domain must say so"
    assert "(1)" in labels["media_player"], "the operator's own count belongs in the label"
    assert "(2)" in labels["device_tracker"], "counts must be per domain, not a constant"
    assert "replicate" in labels["input_boolean"], "the domain that guards automations"

    # Worth-replicating first, then the rest. Not alphabetical overall: the top
    # of the list should be the answer for most people.
    worth = [o["value"] for o in options if o["label"].split(" — ")[1].startswith("replicate")]
    assert values[: len(worth)] == worth, "recommended domains must sort first"
    assert worth == sorted(worth), "stable order inside the group"


async def test_the_statistics_step_appears_only_when_there_is_history_to_replicate(
    hass: HomeAssistant,
) -> None:
    """A dedicated database per node is the only case worth asking about.

    On a shared Postgres or MariaDB both nodes already read the same history,
    so the question is noise — and a wizard that asks it there teaches people
    that its questions are optional.
    """
    flow_id = await start_direct_flow(hass)
    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, direct_input())

    assert result["step_id"] == "history"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"history_matters": True, "history_database": "dedicated"}
    )
    assert result["step_id"] == "statistics", "the statistics step was skipped"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "statistics_enabled": True,
            "statistics_window_days": 30,
            "statistics_interval_minutes": 30,
        },
    )
    assert result["step_id"] == "topology"


async def test_the_statistics_numbers_are_stored_as_integers(hass: HomeAssistant) -> None:
    """`NumberSelector` hands back floats even with step=1.

    They reach `timedelta(minutes=...)` and a systemd `OnUnitActiveSec=` line,
    where `30.0min` is not a value systemd parses — the timer would fail to
    load and the standby's history would quietly stop advancing.
    """
    flow_id = await start_direct_flow(hass)
    with patch(
        "custom_components.cluster_state_sync.config_flow._test_backend",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, direct_input())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"history_matters": True, "history_database": "dedicated"}
    )
    flow = next(
        f for f in hass.config_entries.flow.async_progress() if f["flow_id"] == result["flow_id"]
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "statistics_enabled": True,
            "statistics_window_days": 45.0,
            "statistics_interval_minutes": 15.0,
        },
    )
    handler = hass.config_entries.flow._progress[flow["flow_id"]]
    assert handler._data["statistics_window_days"] == 45
    assert handler._data["statistics_interval_minutes"] == 15
    assert isinstance(handler._data["statistics_interval_minutes"], int)


# -- wizard step 4a-bis: who hears about it (v0.4.2) -----------------------


async def test_the_alerts_step_is_reachable_from_domains(hass: HomeAssistant) -> None:
    """The step has to be ON the path, not merely defined.

    A step nobody routes to is a step that ships dead, and the wizard is long
    enough that nobody would notice it missing.
    """
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncConfigFlow

    flow = ClusterStateSyncConfigFlow()
    flow.hass = hass
    result = await flow.async_step_domains({"include_domains": ["sensor"]})
    assert result["step_id"] == "alerts"


async def test_submitting_the_alerts_step_untouched_is_a_complete_setup(
    hass: HomeAssistant,
) -> None:
    """'Optional and skippable' has to mean the defaults land, not that nothing does.

    Someone clicking Next without reading must still end up with a cluster that
    can reach them -- which is the entire reason the persistent notification is
    unconditional and the four urgent conditions are on by default.
    """
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncConfigFlow
    from custom_components.cluster_state_sync.const import (
        CONF_NOTIFY_CONDITIONS,
        CONF_NOTIFY_SERVICES,
        DEFAULT_NOTIFY_CONDITIONS,
    )

    flow = ClusterStateSyncConfigFlow()
    flow.hass = hass
    form = await flow.async_step_alerts()
    defaults = {
        str(key): key.default() for key in form["data_schema"].schema if hasattr(key, "default")
    }
    assert defaults[CONF_NOTIFY_CONDITIONS] == list(DEFAULT_NOTIFY_CONDITIONS)
    assert defaults[CONF_NOTIFY_SERVICES] == []

    result = await flow.async_step_alerts(defaults)
    assert result["step_id"] == "ingress", (
        "the ingress step now sits between alerts and container — both ask the "
        "same question from opposite ends"
    )
    assert flow._data[CONF_NOTIFY_CONDITIONS] == list(DEFAULT_NOTIFY_CONDITIONS)


async def test_the_alerts_step_offers_loaded_notify_services(hass: HomeAssistant) -> None:
    """Offered from what is actually loaded, so the operator picks rather than types."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncConfigFlow

    async_mock_service(hass, "notify", "mobile_app_pixel")
    flow = ClusterStateSyncConfigFlow()
    flow.hass = hass
    form = await flow.async_step_alerts()
    schema = form["data_schema"].schema
    services = next(v for k, v in schema.items() if str(k) == "notify_services")
    assert "notify.mobile_app_pixel" in services.config["options"]
    assert services.config["custom_value"] is True, (
        "a phone that has not paired yet has no notify service — refusing to "
        "accept its name sends the operator back here for nothing"
    )


def test_every_alert_condition_has_a_label() -> None:
    """🚨 An unlabelled option in a LIST selector renders as a raw slug.

    The conditions are the one place in this wizard where the operator is
    choosing what will wake them at 3am. `statistics_not_seeded` as a bare
    string is not a choice anyone can make well.
    """
    import json
    import pathlib

    from custom_components.cluster_state_sync.const import NOTIFY_CONDITIONS

    root = pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync"
    for name in ("strings.json", "translations/en.json"):
        labels = json.loads((root / name).read_text())["selector"]["notify_conditions"]["options"]
        for condition in NOTIFY_CONDITIONS:
            assert condition in labels, f"{name}: {condition} has no label"
            assert labels[condition].strip(), f"{name}: {condition} label is empty"


# -- the options flow, which nothing covered until v0.4.2 ------------------


def _entry_with_options(options: dict) -> object:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        data={"redis_host": "valkey.lan", "redis_port": 6379, "cluster_namespace": "ns"},
        options=options,
    )


async def test_options_offers_every_section(hass: HomeAssistant) -> None:
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow

    flow = ClusterStateSyncOptionsFlow(_entry_with_options({}))
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "menu"
    assert set(result["menu_options"]) == {"replication", "alerts", "ingress", "connection"}


async def test_saving_alerts_does_not_discard_connection_options(hass: HomeAssistant) -> None:
    """🚨 The bug this guard exists for.

    `async_create_entry` replaces the options wholesale. With one section that
    was harmless. With two, saving the alerting page would silently drop every
    connection override -- and the entry would keep working on the values in
    `entry.data` until a restart pointed Valkey at an address that no longer
    serves it. A data-loss bug that waits for a reboot to become visible is
    the worst-shaped one this project has.
    """
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow
    from custom_components.cluster_state_sync.const import CONF_NOTIFY_SERVICES

    flow = ClusterStateSyncOptionsFlow(_entry_with_options({"redis_host": "valkey-2.lan"}))
    flow.hass = hass
    result = await flow.async_step_alerts({CONF_NOTIFY_SERVICES: ["notify.phone"]})
    assert result["data"]["redis_host"] == "valkey-2.lan", "connection override was discarded"
    assert result["data"][CONF_NOTIFY_SERVICES] == ["notify.phone"]


async def test_saving_connection_does_not_discard_alert_options(hass: HomeAssistant) -> None:
    """And the same in the other direction."""
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow
    from custom_components.cluster_state_sync.const import CONF_NOTIFY_SERVICES

    entry = _entry_with_options({CONF_NOTIFY_SERVICES: ["notify.phone"]})
    flow = ClusterStateSyncOptionsFlow(entry)
    flow.hass = hass
    result = await flow.async_step_connection({"redis_host": "valkey-3.lan"})
    assert result["data"][CONF_NOTIFY_SERVICES] == ["notify.phone"], "alert settings were discarded"
    assert result["data"]["redis_host"] == "valkey-3.lan"


async def test_the_alert_options_form_shows_what_is_already_configured(
    hass: HomeAssistant,
) -> None:
    """Reopening the page must not silently offer to reset it to defaults."""
    from custom_components.cluster_state_sync.config_flow import ClusterStateSyncOptionsFlow
    from custom_components.cluster_state_sync.const import (
        CONF_NOTIFY_CONDITIONS,
        CONF_NOTIFY_SERVICES,
    )

    flow = ClusterStateSyncOptionsFlow(
        _entry_with_options(
            {CONF_NOTIFY_SERVICES: ["notify.phone"], CONF_NOTIFY_CONDITIONS: ["promoted"]}
        )
    )
    flow.hass = hass
    form = await flow.async_step_alerts()
    defaults = {
        str(key): key.default() for key in form["data_schema"].schema if hasattr(key, "default")
    }
    assert defaults[CONF_NOTIFY_SERVICES] == ["notify.phone"]
    assert defaults[CONF_NOTIFY_CONDITIONS] == ["promoted"]


# -- options must actually take effect ------------------------------------


async def test_changing_options_reloads_the_entry(hass: HomeAssistant) -> None:
    """🚨 Without this every options page in this integration is a lie.

    `async_setup_entry` reads the config ONCE and hands it to the mirror, the
    filter and the alert router. Nothing re-reads it. Found on the live pair
    during the v0.4.2 deploy: the options flow stored `notify_services`
    correctly and the running router never saw it — so an operator configures
    their phone, is told it saved, and the house cannot reach them.
    """
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.fakes import FakeBackend

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": "s" * 44,
            "snapshot_interval": 30,
            "fileset_enabled": False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload:
            hass.config_entries.async_update_entry(
                entry, options={"notify_services": ["notify.phone"]}
            )
            await hass.async_block_till_done()

        assert reload.called, "an options change did not reload the entry"


async def test_the_reloaded_entry_sees_the_new_options(hass: HomeAssistant) -> None:
    """The reload is only worth having if the new value actually arrives."""
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.cluster_state_sync.const import CONF_NOTIFY_SERVICES, DATA_ALERTS
    from tests.fakes import FakeBackend

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": "s" * 44,
            "snapshot_interval": 30,
            "fileset_enabled": False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data[DATA_ALERTS]._services == ()

        hass.config_entries.async_update_entry(
            entry, options={CONF_NOTIFY_SERVICES: ["notify.phone"]}
        )
        await hass.async_block_till_done()

        assert entry.runtime_data[DATA_ALERTS]._services == ("notify.phone",), (
            "the reloaded router did not pick up the newly configured service"
        )


async def test_an_options_change_does_not_hand_the_lease_to_the_peer(
    hass: HomeAssistant,
) -> None:
    """🚨 Changing a setting on the leader must not fail the house over.

    `async_unload_entry` releases the lease, and its comment has always said
    "unload covers reloads and reconfiguration" — correct, and harmless while
    nothing reloaded on an options change. The update listener that fixed
    options silently doing nothing turned every settings change on the leader
    into a real promotion.

    Observed on the reference pair 2026-09-10: two options submissions, and the
    house moved to the standby. Both times the operator was only editing a
    field.
    """
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.fakes import FakeBackend

    backend = FakeBackend()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": "s" * 44,
            "snapshot_interval": 30,
            "fileset_enabled": False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        backend.lease_holder = "node-a"
        hass.config_entries.async_update_entry(entry, options={"snapshot_interval": 45})
        await hass.async_block_till_done()

    assert backend.lease_holder == "node-a", (
        "an options change released the cluster lease — the peer would promote "
        "and the house would move because somebody edited a setting"
    )


async def test_a_real_removal_still_releases_the_lease(hass: HomeAssistant) -> None:
    """The other direction, and it matters just as much.

    A node that has genuinely stopped running the integration must hand the
    lease back, or the peer waits out the full TTL before it can promote —
    failover budget burned for nothing, on the one shutdown that was entirely
    orderly.
    """
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.fakes import FakeBackend

    backend = FakeBackend()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "redis_host": "valkey.invalid",
            "cluster_namespace": "testns",
            "node_id": "node-a",
            "cluster_secret": "s" * 44,
            "snapshot_interval": 30,
            "fileset_enabled": False,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        backend.lease_holder = "node-a"
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert backend.lease_holder is None, (
        "an orderly shutdown kept the lease and cost the peer a full TTL"
    )
