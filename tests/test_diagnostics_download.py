"""What leaves the house when somebody opens an issue.

Named `..._download` rather than `test_diagnostics.py`, which was already taken
by the diagnostic *entity* tests (AR-0019, AR-0032) and is a different subject
entirely. I overwrote that file writing this one, and nothing failed — the
tests simply stopped existing, and only a drop in the collected count gave it
away. A test that vanishes does not go red; it stops protecting.

Home Assistant's **Download diagnostics** button hands the operator a file to
read before they decide to attach it. These tests are about what is in that
file, and they are written pessimistically: the interesting failure is not a
crash, it is a secret that quietly appears in a public issue and cannot be
taken back.
"""

from __future__ import annotations

import json

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cluster_state_sync import const
from custom_components.cluster_state_sync.diagnostics import (
    COUNTED_FIELDS,
    SAFE_FIELDS,
    _alias,
    async_get_config_entry_diagnostics,
)

#: Values a real entry carries that must never be published, with the shapes
#: they actually take on the reference estate.
SECRETS = {
    "cluster_secret": "a-shared-cluster-secret-nobody-should-see",
    "redis_password": "hunter2",
    "redis_tls_ca_certs": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----",
    "redis_host": "valkey-cluster-state.example.com",
    "peer_host": "192.168.1.89",
    "ha_config_path": "/mnt/docker_data/homeassistant/config",
    "ha_container": "homeassistant",
    "ha_container_ip": "172.30.0.4",
    "ingress_url": "https://home.example.com/",
    "cluster_namespace": "prod",
    "notify_services": ["notify.mobile_app_fold6"],
    "exclude_entities": ["input_boolean.private_thing"],
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=const.DOMAIN,
        version=const.CONFIG_ENTRY_VERSION,
        data={
            const.CONF_NODE_ID: "node-a",
            "snapshot_interval": 5,
            "restore_max_age": 1800,
            const.CONF_INCLUDE_DOMAINS: ["automation", "input_boolean"],
            const.CONF_NOTIFY_CONDITIONS: ["promoted", "ingress_unreachable"],
            **SECRETS,
        },
    )


async def _report(hass: HomeAssistant) -> dict:
    entry = _entry()
    entry.add_to_hass(hass)
    return await async_get_config_entry_diagnostics(hass, entry)


# -- the thing that must never happen ---------------------------------------


async def test_no_secret_survives_anywhere_in_the_document(hass: HomeAssistant) -> None:
    """🚨 Searched as raw text, not key by key.

    A key-by-key check passes while a password sits inside a nested structure
    somebody added later. The document is serialised and searched whole,
    because that is how it will be read by whoever it leaks to.
    """
    blob = json.dumps(await _report(hass))

    for name, value in SECRETS.items():
        needle = value[0] if isinstance(value, list) else value
        assert needle not in blob, f"{name} was published verbatim in the diagnostics"


async def test_the_hostname_does_not_survive_in_the_alias(hass: HomeAssistant) -> None:
    """The alias must not be the name with a hat on."""
    blob = json.dumps(await _report(hass))
    assert "node-a" not in blob
    assert "f5d04f" not in blob, "the alias leaked part of the node id it replaced"


# -- the allowlist, and why it is one ---------------------------------------


def test_every_config_key_has_been_classified() -> None:
    """🚨 A setting added later must be withheld by default, not published.

    This is the same argument `scripts/export_public.py` makes about files. A
    denylist protects against the keys somebody remembered; the next one added
    will not be on anybody's list, and by the time it is noticed the file is in
    a public issue.
    """
    all_keys = {
        getattr(const, n)
        for n in dir(const)
        if n.startswith("CONF_") and isinstance(getattr(const, n), str)
    }
    classified = SAFE_FIELDS | COUNTED_FIELDS | {const.CONF_NODE_ID}
    unclassified = sorted(all_keys - classified)

    # Unclassified is not an error — it means REDACTED, which is the safe
    # default. What must hold is that nothing in SAFE_FIELDS is a secret.
    assert not (SAFE_FIELDS & {"cluster_secret", "redis_password", "redis_tls_ca_certs"}), (
        "a secret is on the allowlist"
    )
    assert not (SAFE_FIELDS & {"redis_host", "peer_host", "ingress_url", "ha_config_path"}), (
        "an identifying value is on the allowlist"
    )
    assert unclassified, (
        "every key is classified, which means the allowlist has become a "
        "denylist by accident — new keys would now default to PUBLISHED"
    )


async def test_an_unknown_key_is_withheld_rather_than_published(hass: HomeAssistant) -> None:
    """The property the allowlist exists for, exercised rather than asserted."""
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        version=const.CONFIG_ENTRY_VERSION,
        data={"a_setting_invented_tomorrow": "something-private"},
    )
    entry.add_to_hass(hass)

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["config"]["a_setting_invented_tomorrow"] == REDACTED
    assert "something-private" not in json.dumps(report)


# -- what it must still be useful for ---------------------------------------


async def test_the_tunables_that_issues_turn_out_to_be_about_are_present(
    hass: HomeAssistant,
) -> None:
    """Redacting everything would be safe and useless.

    Nearly every support question so far has turned on one of these numbers.
    """
    report = await _report(hass)
    cfg = report["config"]

    assert cfg["snapshot_interval"] == 5
    assert cfg["restore_max_age"] == 1800
    assert cfg[const.CONF_INCLUDE_DOMAINS] == ["automation", "input_boolean"]


async def test_withheld_lists_still_report_their_size(hass: HomeAssistant) -> None:
    """ "You have 1 excluded entity" answers a question "**REDACTED**" does not."""
    cfg = (await _report(hass))["config"]
    assert "1 item(s)" in cfg["exclude_entities"]
    assert "private_thing" not in cfg["exclude_entities"]


def test_the_alias_is_stable_and_not_reversible() -> None:
    """Relationships survive; names do not."""
    assert _alias("somehost1-f5d04f") == _alias("somehost1-f5d04f")
    assert _alias("somehost1-f5d04f") != _alias("somehost2-4554b2")
    assert "somehost" not in (_alias("somehost1-f5d04f") or "")
    assert _alias(None) is None


async def test_the_document_says_what_it_withheld(hass: HomeAssistant) -> None:
    """Transparency is the point: the operator reads this before attaching it."""
    report = await _report(hass)
    blurb = " ".join(report["_what_this_is"]).lower()

    assert "safe to attach" in blurb
    assert "withheld" in blurb
    assert "allowlist" in blurb, "the file should say WHY a value is missing"
