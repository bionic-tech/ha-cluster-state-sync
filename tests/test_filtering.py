"""Entity-filter tests (AR-0030).

`_should_track` decides what leaves this node and what a promoted standby is
allowed to have applied to it. It is three lines with a precedence order, and
precedence is exactly where a filter goes quietly wrong: an operator who adds
an entity to both the include and exclude lists has a clear intent, and the
code has to honour it the same way every time.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.cluster_state_sync import _should_track
from custom_components.cluster_state_sync.const import (
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_LEADERSHIP_ENTITY,
    SENSITIVE_DOMAINS,
)


def cfg(**overrides: Any) -> dict[str, Any]:
    return dict(overrides)


# -- domain allowlist -------------------------------------------------------


def test_entity_in_an_included_domain_is_tracked() -> None:
    assert _should_track("input_boolean.holiday", cfg())


def test_entity_outside_the_allowlist_is_not_tracked() -> None:
    assert not _should_track("sensor.living_room_temperature", cfg())


def test_custom_domain_list_replaces_the_default() -> None:
    """Configuring domains is a replacement, not an addition to the defaults."""
    config = cfg(**{CONF_INCLUDE_DOMAINS: ["counter"]})
    assert _should_track("counter.visits", config)
    assert not _should_track("input_boolean.holiday", config)


@pytest.mark.parametrize("domain", sorted(SENSITIVE_DOMAINS))
def test_sensitive_domains_need_opting_in(domain: str) -> None:
    """AR-0004 at the filter level, not just the constant."""
    assert not _should_track(f"{domain}.someone", cfg())
    assert _should_track(f"{domain}.someone", cfg(**{CONF_INCLUDE_DOMAINS: [domain]}))


# -- precedence -------------------------------------------------------------


def test_explicit_include_overrides_the_domain_allowlist() -> None:
    """One entity can be pulled in without opening its whole domain.

    This is how you mirror a single `sensor.` without mirroring all of them.
    """
    config = cfg(**{CONF_INCLUDE_ENTITIES: ["sensor.boiler_pressure"]})
    assert _should_track("sensor.boiler_pressure", config)
    assert not _should_track("sensor.something_else", config)


def test_exclude_beats_explicit_include() -> None:
    """Exclude is the highest-precedence rule.

    Production change that would make this fail: checking the include list
    first. An operator who lists an entity in both has said "not this one" most
    recently and most specifically; exclusion is the safety valve, so it has to
    be the one that wins.
    """
    config = cfg(
        **{
            CONF_INCLUDE_ENTITIES: ["input_boolean.secret"],
            CONF_EXCLUDE_ENTITIES: ["input_boolean.secret"],
        }
    )
    assert not _should_track("input_boolean.secret", config)


def test_exclude_beats_the_domain_allowlist() -> None:
    config = cfg(**{CONF_EXCLUDE_ENTITIES: ["input_boolean.secret"]})
    assert not _should_track("input_boolean.secret", config)
    assert _should_track("input_boolean.public", config)


# -- robustness -------------------------------------------------------------


def test_empty_lists_fall_through_to_the_defaults() -> None:
    """Empty is not the same as configured-to-nothing.

    An options flow that submits empty lists must not silently stop the node
    mirroring anything at all.
    """
    config = cfg(
        **{
            CONF_INCLUDE_ENTITIES: [],
            CONF_EXCLUDE_ENTITIES: [],
            CONF_INCLUDE_DOMAINS: [],
        }
    )
    assert _should_track("input_boolean.holiday", config)


def test_entity_id_without_a_domain_is_not_tracked() -> None:
    """Malformed IDs must not raise inside the event callback.

    That callback runs on every state change; an exception there would take
    out the mirror for every entity, not just the malformed one.
    """
    assert not _should_track("no_domain_here", cfg())


# -- AR-0038: the leadership signal is node-local, never cluster state -------


def test_ar_0038_the_leadership_entity_is_never_tracked() -> None:
    """The flag that says "I am in charge" must not be replicated.

    Production change that would make this fail: leaving the leadership entity
    to the ordinary include/exclude precedence.

    `input_boolean` is in the default domain list, and the documented way to
    drive `leadership_source: entity` is an `input_boolean` that Keepalived
    toggles. Those two facts together mean the leader publishes its own
    "I am the leader" flag into the shared snapshot, and the standby restores
    it at boot — before the first leadership evaluation — and concludes that
    it is the leader too.

    The integration would be handing the follower the false belief itself. It
    is not a filter default that happens to be wrong; the signal is a
    statement about *this node*, so there is no configuration under which
    replicating it is correct.
    """
    config = cfg(**{CONF_LEADERSHIP_ENTITY: "input_boolean.ha_is_master"})
    assert not _should_track("input_boolean.ha_is_master", config)
    # Nothing else in the domain is affected.
    assert _should_track("input_boolean.holiday", config)


def test_ar_0038_the_leadership_entity_cannot_be_re_enabled_by_configuration() -> None:
    """An explicit include must not be able to switch the guard back off.

    Production change that would make this fail: applying the leadership check
    after the explicit-include branch instead of before it.

    Explicit includes beat the domain allowlist by design, and an operator
    listing their leadership flag is far more likely to be a mistake than an
    intention — the outcome is split brain, so the guard wins.
    """
    config = cfg(
        **{
            CONF_LEADERSHIP_ENTITY: "input_boolean.ha_is_master",
            CONF_INCLUDE_ENTITIES: ["input_boolean.ha_is_master"],
        }
    )
    assert not _should_track("input_boolean.ha_is_master", config)
