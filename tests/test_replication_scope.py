"""Device exclusion, and the scope report that makes any of it legible.

Before this, "what actually crosses?" was answerable only by reading
`_should_track` and evaluating a four-step precedence chain by hand — and one
of its four inputs, `exclude_entities`, was reachable from no screen at all.

The device unit exists because a domain is the wrong one for "not this
thermostat". `climate` is replicated because a promoted node needs the
setpoints; one climate device might still be a guest annexe, a test unit, or
the piece of hardware wired to the node that does not fail over.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync import _should_track
from custom_components.cluster_state_sync.const import (
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_LEADERSHIP_ENTITY,
    DOMAIN,
)
from custom_components.cluster_state_sync.scope import (
    replication_scope,
    resolve_device_entities,
)


def _device_with_entities(hass: HomeAssistant, *entity_ids: str) -> str:
    """A real registry device owning real registry entities."""
    entry = MockConfigEntry(domain="demo")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("demo", "-".join(entity_ids))},
        name="Guest annexe thermostat",
    )
    entities = er.async_get(hass)
    for entity_id in entity_ids:
        domain, object_id = entity_id.split(".", 1)
        entities.async_get_or_create(
            domain,
            "demo",
            object_id,
            device_id=device.id,
            suggested_object_id=object_id,
        )
    return device.id


# -- resolution -------------------------------------------------------------


async def test_a_device_resolves_to_every_entity_it_owns(hass: HomeAssistant) -> None:
    device_id = _device_with_entities(hass, "climate.annexe", "sensor.annexe_battery")
    resolved = resolve_device_entities(hass, [device_id])
    assert resolved == {"climate.annexe", "sensor.annexe_battery"}


async def test_a_disabled_entity_is_still_resolved(hass: HomeAssistant) -> None:
    """The instruction was about the DEVICE.

    A disabled entity has no state to publish today and can be re-enabled at
    any moment. Narrowing "not this device" to "the entities it happens to
    expose right now" is the kind of helpfulness that produces a replicated
    entity nobody asked for.
    """
    device_id = _device_with_entities(hass, "climate.annexe")
    entities = er.async_get(hass)
    entities.async_update_entity("climate.annexe", disabled_by=er.RegistryEntryDisabler.USER)

    assert resolve_device_entities(hass, [device_id]) == {"climate.annexe"}


async def test_a_device_that_no_longer_exists_is_skipped_not_raised(
    hass: HomeAssistant,
) -> None:
    """Integrations remove devices without warning.

    A stale exclusion is an exclusion that has stopped mattering, not a broken
    configuration — and certainly not a reason to fail a flush.
    """
    assert resolve_device_entities(hass, ["a-device-that-never-existed"]) == frozenset()


async def test_no_devices_resolves_to_nothing_without_touching_the_registry(
    hass: HomeAssistant,
) -> None:
    assert resolve_device_entities(hass, []) == frozenset()


# -- the filter -------------------------------------------------------------


def test_an_excluded_device_beats_the_domain_allowlist() -> None:
    cfg = {CONF_INCLUDE_DOMAINS: ["climate"]}
    excluded = frozenset({"climate.annexe"})
    assert _should_track("climate.annexe", cfg, excluded) is False
    assert _should_track("climate.hall", cfg, excluded) is True


def test_an_excluded_device_beats_an_explicit_include() -> None:
    """🚨 Precedence, and it must go this way round.

    An operator who ticks a device in "never cross" has said something more
    specific and more recent than an entity they listed at install. Letting the
    include win would make the exclusion silently ineffective — and the only
    symptom would be data crossing that they explicitly asked to keep local.
    """
    cfg = {
        CONF_INCLUDE_DOMAINS: [],
        CONF_INCLUDE_ENTITIES: ["climate.annexe"],
    }
    assert _should_track("climate.annexe", cfg, frozenset({"climate.annexe"})) is False


def test_the_leadership_entity_is_still_refused_first(hass: HomeAssistant) -> None:
    """AR-0038 sits above everything, including this."""
    cfg = {
        CONF_INCLUDE_DOMAINS: ["input_boolean"],
        CONF_LEADERSHIP_ENTITY: "input_boolean.is_leader",
    }
    assert _should_track("input_boolean.is_leader", cfg, frozenset()) is False


def test_no_excluded_set_behaves_exactly_as_before() -> None:
    """Every existing call site and test passes nothing. That must be a no-op."""
    cfg = {CONF_INCLUDE_DOMAINS: ["climate"]}
    assert _should_track("climate.hall", cfg) is True
    assert _should_track("climate.hall", cfg, None) is True
    assert _should_track("climate.hall", cfg, frozenset()) is True


# -- the scope report -------------------------------------------------------


async def test_the_scope_reports_only_domains_this_instance_actually_has(
    hass: HomeAssistant,
) -> None:
    """A list of every domain in Home Assistant would be noise.

    The useful question is "what do I run that is not crossing?".
    """
    hass.states.async_set("sensor.outside", "12")
    hass.states.async_set("climate.hall", "heat")
    hass.states.async_set("light.kitchen", "on")

    scope = replication_scope(hass, {CONF_INCLUDE_DOMAINS: ["climate"]})

    assert scope["replicated_domains"] == ["climate"]
    assert "sensor" in scope["not_replicated_domains"]
    assert "light" in scope["not_replicated_domains"]
    assert "climate" not in scope["not_replicated_domains"]
    # Nothing this instance does not run.
    assert "vacuum" not in scope["not_replicated_domains"]


async def test_the_scope_names_excluded_devices_for_a_human(hass: HomeAssistant) -> None:
    """A raw device id is a 32-character hex string and tells nobody anything."""
    device_id = _device_with_entities(hass, "climate.annexe")
    scope = replication_scope(hass, {CONF_EXCLUDE_DEVICES: [device_id]})

    assert scope["excluded_devices"] == [
        {"id": device_id, "name": "Guest annexe thermostat", "present": True}
    ]
    assert scope["entities_excluded_by_device"] == ["climate.annexe"]


async def test_a_renamed_device_reports_the_operators_name(hass: HomeAssistant) -> None:
    """`name_by_user` is what they typed, so it is what they will recognise."""
    device_id = _device_with_entities(hass, "climate.annexe")
    dr.async_get(hass).async_update_device(device_id, name_by_user="Annexe (do not sync)")

    scope = replication_scope(hass, {CONF_EXCLUDE_DEVICES: [device_id]})
    assert scope["excluded_devices"][0]["name"] == "Annexe (do not sync)"


async def test_a_vanished_device_is_shown_rather_than_hidden(hass: HomeAssistant) -> None:
    """The exclusion is still in the config and still counts.

    Hiding it would leave an operator unable to see why a line in their config
    exists, or to tidy it away.
    """
    scope = replication_scope(hass, {CONF_EXCLUDE_DEVICES: ["gone"]})
    assert scope["excluded_devices"] == [{"id": "gone", "name": None, "present": False}]


async def test_the_refused_leadership_entity_is_reported_apart(hass: HomeAssistant) -> None:
    """It is refused, but the operator did not choose it.

    Folding it in with their own exclusions would misattribute it; omitting it
    entirely would make the scope report lie by omission.
    """
    scope = replication_scope(
        hass, {CONF_LEADERSHIP_ENTITY: "input_boolean.is_leader", CONF_EXCLUDE_ENTITIES: ["a.b"]}
    )
    assert scope["leadership_entity_refused"] == "input_boolean.is_leader"
    assert scope["excluded_entities"] == ["a.b"]


async def test_the_default_scope_names_the_eleven_defaults(hass: HomeAssistant) -> None:
    """An empty config must report the documented default, not an empty list."""
    from custom_components.cluster_state_sync.const import DEFAULT_INCLUDE_DOMAINS

    scope = replication_scope(hass, {})
    assert scope["replicated_domains"] == sorted(DEFAULT_INCLUDE_DOMAINS)
    assert DOMAIN  # the import is load-bearing for the registry fixtures above


# -- the cache, which is the part that can go quietly wrong -----------------


async def _setup(hass: HomeAssistant, **cfg: object) -> MockConfigEntry:
    from unittest.mock import patch

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
            **cfg,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=FakeBackend()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_setup_resolves_the_exclusions_and_hands_them_to_the_mirror(
    hass: HomeAssistant,
) -> None:
    from custom_components.cluster_state_sync.const import DATA_EXCLUDED_IDS, DATA_MIRROR

    device_id = _device_with_entities(hass, "climate.annexe")
    entry = await _setup(hass, exclude_devices=[device_id])

    assert entry.runtime_data[DATA_EXCLUDED_IDS] == {"climate.annexe"}
    assert entry.runtime_data[DATA_MIRROR].excluded_ids == {"climate.annexe"}


async def test_an_entity_added_to_an_excluded_device_later_is_caught(
    hass: HomeAssistant,
) -> None:
    """🚨 The failure this listener exists for.

    Resolve once at setup and never again, and a firmware update that adds a
    ninth entity to an excluded device starts replicating it. The exclusion
    still looks correct in the UI, every health check stays green, and the only
    symptom is data crossing that the operator asked to keep local.
    """
    from custom_components.cluster_state_sync.const import DATA_EXCLUDED_IDS, DATA_MIRROR

    device_id = _device_with_entities(hass, "climate.annexe")
    entry = await _setup(hass, exclude_devices=[device_id])
    assert entry.runtime_data[DATA_EXCLUDED_IDS] == {"climate.annexe"}

    added = er.async_get(hass).async_get_or_create(
        "sensor", "demo", "annexe_humidity", device_id=device_id
    )
    await hass.async_block_till_done()

    assert added.entity_id in entry.runtime_data[DATA_EXCLUDED_IDS], (
        "an entity added to an excluded device after setup was not picked up"
    )
    assert entry.runtime_data[DATA_MIRROR].excluded_ids == entry.runtime_data[DATA_EXCLUDED_IDS]


async def test_an_unrelated_registry_change_does_not_churn_the_cache(
    hass: HomeAssistant,
) -> None:
    """The listener fires on every registry write. Recomputing is cheap; the
    identity check keeps it from replacing an equal set on every entity Home
    Assistant creates during startup."""
    from custom_components.cluster_state_sync.const import DATA_EXCLUDED_IDS

    device_id = _device_with_entities(hass, "climate.annexe")
    entry = await _setup(hass, exclude_devices=[device_id])
    before = entry.runtime_data[DATA_EXCLUDED_IDS]

    er.async_get(hass).async_get_or_create("sensor", "demo", "unrelated")
    await hass.async_block_till_done()

    assert entry.runtime_data[DATA_EXCLUDED_IDS] is before, "the cache was replaced needlessly"
