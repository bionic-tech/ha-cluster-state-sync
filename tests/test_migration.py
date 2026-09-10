"""Config-entry migration — v1 to v2, when `automation` joined the defaults.

The migration's whole job is to tell an *accepted default* apart from a
*decision*, and only touch the first. These tests exist because getting that
wrong is silent in exactly the way this project keeps getting caught by: an
over-eager migration would quietly start replicating a domain an operator had
deliberately excluded, and nothing would say so.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync import async_migrate_entry
from custom_components.cluster_state_sync.config_flow import ClusterStateSyncConfigFlow
from custom_components.cluster_state_sync.const import (
    CONF_INCLUDE_DOMAINS,
    CONFIG_ENTRY_VERSION,
    DEFAULT_INCLUDE_DOMAINS,
    DOMAIN,
    LEGACY_DEFAULT_INCLUDE_DOMAINS,
)


def _v1(options: dict[str, Any] | None = None, data: dict[str, Any] | None = None):
    return MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=data if data is not None else {"redis_host": "valkey.lan"},
        options=options if options is not None else {},
    )


# -- the constants themselves ----------------------------------------------


def test_automation_is_in_the_shipped_default() -> None:
    """The gap this migration exists to close.

    `automation` is the only replicated domain applied by calling a service
    rather than writing state, so it is the only one whose restore is not
    overwritten by the next device poll. It shipped absent from the default
    until v0.4.4.
    """
    assert "automation" in DEFAULT_INCLUDE_DOMAINS


def test_the_legacy_set_is_frozen_history_not_a_view_of_today() -> None:
    """`LEGACY_DEFAULT_INCLUDE_DOMAINS` must never track the current default.

    If someone "tidies" it into `frozenset(DEFAULT_INCLUDE_DOMAINS)`, the
    migration's test for an untouched entry becomes vacuously true for the
    *current* default and the v1 entries it exists to fix stop matching. This
    pins the one difference that must exist.
    """
    assert set(DEFAULT_INCLUDE_DOMAINS) - LEGACY_DEFAULT_INCLUDE_DOMAINS == {"automation"}
    assert LEGACY_DEFAULT_INCLUDE_DOMAINS - set(DEFAULT_INCLUDE_DOMAINS) == set()


def test_the_flow_version_and_the_migration_agree() -> None:
    """Two places that must not drift; a mismatch strands entries un-migrated."""
    assert ClusterStateSyncConfigFlow.VERSION == CONFIG_ENTRY_VERSION


# -- the migration ----------------------------------------------------------


async def test_an_untouched_default_gains_automation(hass: HomeAssistant) -> None:
    entry = _v1(options={CONF_INCLUDE_DOMAINS: sorted(LEGACY_DEFAULT_INCLUDE_DOMAINS)})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert "automation" in entry.options[CONF_INCLUDE_DOMAINS]
    assert set(entry.options[CONF_INCLUDE_DOMAINS]) == set(DEFAULT_INCLUDE_DOMAINS)
    assert entry.version == CONFIG_ENTRY_VERSION


async def test_a_customised_allowlist_is_left_alone(hass: HomeAssistant) -> None:
    """The case that matters: a deliberate choice is not overruled.

    An operator who narrowed replication to two helpers gets those two helpers
    back, not those two plus a domain they never asked for.
    """
    chosen = ["input_boolean", "timer"]
    entry = _v1(options={CONF_INCLUDE_DOMAINS: list(chosen)})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.options[CONF_INCLUDE_DOMAINS] == chosen
    assert entry.version == CONFIG_ENTRY_VERSION


async def test_the_default_minus_one_domain_is_a_decision(hass: HomeAssistant) -> None:
    """Near-miss: the shipped default with `vacuum` unticked is still a choice."""
    trimmed = sorted(LEGACY_DEFAULT_INCLUDE_DOMAINS - {"vacuum"})
    entry = _v1(options={CONF_INCLUDE_DOMAINS: trimmed})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.options[CONF_INCLUDE_DOMAINS] == trimmed
    assert "automation" not in entry.options[CONF_INCLUDE_DOMAINS]


async def test_an_entry_with_no_allowlist_is_not_given_one(hass: HomeAssistant) -> None:
    """It does not need one: `async_setup_entry` falls back to the constant.

    Writing an explicit list here would freeze today's default into the entry
    and stop it tracking future changes — the opposite of what we want.
    """
    entry = _v1(options={})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert CONF_INCLUDE_DOMAINS not in entry.options
    assert entry.version == CONFIG_ENTRY_VERSION


async def test_an_allowlist_in_data_is_migrated_too(hass: HomeAssistant) -> None:
    """Older entries carry it in `data`; `{**data, **options}` reads both."""
    entry = _v1(
        data={
            "redis_host": "valkey.lan",
            CONF_INCLUDE_DOMAINS: sorted(LEGACY_DEFAULT_INCLUDE_DOMAINS),
        },
        options={},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert "automation" in entry.data[CONF_INCLUDE_DOMAINS]


async def test_a_future_entry_is_refused_rather_than_guessed_at(hass: HomeAssistant) -> None:
    """A downgrade must decline, not reinterpret.

    Same stance `backend.py` takes on a future snapshot schema: silently
    misreading config written by a newer build is worse than not starting.
    """
    entry = MockConfigEntry(domain=DOMAIN, version=CONFIG_ENTRY_VERSION + 1, data={})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is False


async def test_migrating_twice_changes_nothing_the_second_time(hass: HomeAssistant) -> None:
    entry = _v1(options={CONF_INCLUDE_DOMAINS: sorted(LEGACY_DEFAULT_INCLUDE_DOMAINS)})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    once = dict(entry.options)

    assert await async_migrate_entry(hass, entry) is True
    assert dict(entry.options) == once
