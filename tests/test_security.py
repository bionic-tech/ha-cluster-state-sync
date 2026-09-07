"""Security tests for the P0 combination (AR-0003/0004/0005/0008/0010).

The v1 review's single P0 was a *combination*: location and alarm state
crossing a flat LAN unencrypted, into a shared password-only keyspace, applied
verbatim on restore with no integrity check. No one of those is fatal alone.
These tests pin each leg of it.
"""

from __future__ import annotations

import json

import pytest
import voluptuous as vol

from custom_components.cluster_state_sync.backend import SnapshotEntry, _sign
from custom_components.cluster_state_sync.const import (
    DEFAULT_INCLUDE_DOMAINS,
    SENSITIVE_DOMAINS,
    validate_namespace,
)

SECRET = "cluster-secret-under-test"
OTHER_SECRET = "a-different-cluster-secret"


def make_entry(entity_id: str = "alarm_control_panel.house") -> SnapshotEntry:
    return SnapshotEntry(
        entity_id=entity_id,
        state="armed_away",
        attributes={"changed_by": "maurice"},
        last_changed="2026-08-05T10:00:00+00:00",
        last_updated="2026-08-05T10:00:00+00:00",
        source_node="node-b",
    )


# -- AR-0005: snapshot integrity -------------------------------------------


def test_signed_entry_round_trips() -> None:
    """The happy path: an entry we signed verifies and parses back."""
    entry = make_entry()
    restored = SnapshotEntry.from_json(entry.entity_id, entry.to_json(secret=SECRET), secret=SECRET)
    assert restored == entry


def test_ar_0005_tampered_state_is_rejected() -> None:
    """AR-0005 — flipping a stored state must not survive verification.

    Production change that would make this fail: dropping the HMAC check in
    `from_json`.

    This is the physical-security case from the review: anyone able to write
    to the shared hash could disarm the alarm on the standby by editing one
    field, and the restore would apply it verbatim.
    """
    entry = make_entry()
    payload = json.loads(entry.to_json(secret=SECRET))
    payload["s"] = "disarmed"

    with pytest.raises(ValueError, match="signature"):
        SnapshotEntry.from_json(entry.entity_id, json.dumps(payload), secret=SECRET)


def test_ar_0005_tampered_attributes_are_rejected() -> None:
    """Attributes are signed too, not just the state value."""
    entry = make_entry()
    payload = json.loads(entry.to_json(secret=SECRET))
    payload["a"] = {"changed_by": "someone else"}

    with pytest.raises(ValueError, match="signature"):
        SnapshotEntry.from_json(entry.entity_id, json.dumps(payload), secret=SECRET)


def test_ar_0005_forged_source_node_is_rejected() -> None:
    """AR-0005 — `source_node` is inside the signature, so it is not forgeable.

    The restore trusts entries from *the peer* and skips its own. Without the
    signature covering `source_node`, an attacker just claims to be the peer
    and the trust check becomes decoration.
    """
    entry = make_entry()
    payload = json.loads(entry.to_json(secret=SECRET))
    payload["n"] = "some-other-node"

    with pytest.raises(ValueError, match="signature"):
        SnapshotEntry.from_json(entry.entity_id, json.dumps(payload), secret=SECRET)


def test_ar_0005_entry_replayed_onto_a_different_entity_is_rejected() -> None:
    """The entity_id is bound into the signature.

    Without binding, a validly-signed `input_boolean.holiday_mode = on` could be
    copied onto the `alarm_control_panel.house` field of the hash and would
    verify perfectly — a valid signature on the wrong entity.
    """
    entry = make_entry("input_boolean.holiday_mode")
    signed = entry.to_json(secret=SECRET)

    with pytest.raises(ValueError, match="signature"):
        SnapshotEntry.from_json("alarm_control_panel.house", signed, secret=SECRET)


def test_ar_0036_replay_is_not_unlocked_by_shadowing_the_entity_id() -> None:
    """The binding above must not be removable by the attacker.

    Production change that would make this fail: signing
    `{"e": entity_id, **payload}`, where a payload carrying its own `e` wins
    the dict merge and replaces the argument.

    The attack needs no secret. Take any validly-signed entry, re-add its
    original entity_id as an ordinary payload key, and write it onto a
    different field of the hash. Verification then re-derives the signature
    over the attacker's `e` rather than the field it actually arrived on, the
    canonical form is byte-identical to the original, and the HMAC matches.

    `source_node` survives the copy too, so the forged entry still claims to
    come from the peer and passes the restore's peer-trust check as well.
    """
    benign = make_entry("input_boolean.holiday_mode")
    benign.state = "on"
    payload = json.loads(benign.to_json(secret=SECRET))
    payload["e"] = "input_boolean.holiday_mode"

    with pytest.raises(ValueError):
        SnapshotEntry.from_json("alarm_control_panel.house", json.dumps(payload), secret=SECRET)


def test_ar_0036_signing_binds_an_entity_id_the_payload_cannot_change() -> None:
    """The primitive itself, independent of who calls it.

    Production change that would make this fail: building the canonical form
    as `{"e": entity_id, **payload}`.

    The check in `from_json` means nothing carrying an `e` ever reaches
    `_sign` today, so this invariant is not observable through the public
    path — and that is exactly why it is pinned here. The finding was that the
    signing primitive was unsound; a fix that is only enforced by its current
    caller is one refactor away from being no fix at all.

    Stated without reference to the implementation: two different entities
    must never produce the same signature, whatever the payload claims about
    itself.
    """
    payload = {
        "v": 1,
        "s": "on",
        "a": {},
        "lc": "2026-08-05T10:00:00+00:00",
        "lu": "2026-08-05T10:00:00+00:00",
        "n": "node-b",
    }
    honest = _sign("input_boolean.holiday_mode", payload, SECRET)
    shadowed = _sign(
        "alarm_control_panel.house",
        {**payload, "e": "input_boolean.holiday_mode"},
        SECRET,
    )

    assert shadowed != honest


def test_ar_0036_entry_carrying_an_unknown_field_is_rejected() -> None:
    """Authenticated input is parsed against a closed schema, not an open one.

    Production change that would make this fail: verifying every key present
    instead of checking the key set first.

    `to_json` emits exactly six fields plus the signature. An entry carrying a
    seventh has not been produced by any version of this integration, so the
    only question worth asking is whether to refuse it — and refusing is what
    stops the next field-injection trick as well as the one above.
    """
    payload = json.loads(make_entry().to_json(secret=SECRET))
    payload["extra"] = "unexpected"

    with pytest.raises(ValueError, match="unexpected field"):
        SnapshotEntry.from_json("alarm_control_panel.house", json.dumps(payload), secret=SECRET)


def test_ar_0005_unsigned_entry_is_rejected_when_a_secret_is_configured() -> None:
    """An attacker must not be able to opt out of verification by omitting it."""
    entry = make_entry()
    unsigned = entry.to_json()
    assert "h" not in json.loads(unsigned)

    with pytest.raises(ValueError, match="unsigned"):
        SnapshotEntry.from_json(entry.entity_id, unsigned, secret=SECRET)


def test_ar_0005_entry_signed_with_another_key_is_rejected() -> None:
    """Two clusters sharing a Valkey must not be able to write into each other."""
    entry = make_entry()
    with pytest.raises(ValueError, match="signature"):
        SnapshotEntry.from_json(entry.entity_id, entry.to_json(secret=OTHER_SECRET), secret=SECRET)


def test_signature_covers_every_signed_field() -> None:
    """Guard against a field being added to the payload but not to the signature.

    Iterates the payload rather than naming fields, so a future field added to
    `to_json` without being signed fails here instead of silently becoming
    forgeable.
    """
    entry = make_entry()
    payload = json.loads(entry.to_json(secret=SECRET))

    for field in [k for k in payload if k != "h"]:
        mutated = dict(payload)
        mutated[field] = "tampered" if field != "a" else {"x": "tampered"}
        if mutated[field] == payload[field]:
            continue
        with pytest.raises(ValueError, match="signature"):
            SnapshotEntry.from_json(entry.entity_id, json.dumps(mutated), secret=SECRET)


# -- AR-0004: sensitive domains off by default -----------------------------


@pytest.mark.parametrize("domain", ["person", "device_tracker", "alarm_control_panel"])
def test_ar_0004_sensitive_domains_are_not_mirrored_by_default(domain: str) -> None:
    """AR-0004 — where people are and whether the house is armed is opt-in.

    Production change that would make this fail: putting these back in
    `DEFAULT_INCLUDE_DOMAINS`.

    Mirroring them by default means a plaintext copy of the household's
    movements and alarm state on a shared Valkey, for anyone who ticked the
    defaults without reading them.
    """
    assert domain in SENSITIVE_DOMAINS
    assert domain not in DEFAULT_INCLUDE_DOMAINS


def test_ordinary_domains_remain_on_by_default() -> None:
    """The opt-in change must not gut the useful default."""
    assert "input_boolean" in DEFAULT_INCLUDE_DOMAINS
    assert "counter" in DEFAULT_INCLUDE_DOMAINS


# -- AR-0010: namespace is part of a key, so it must be constrained --------


@pytest.mark.parametrize("value", ["default", "house-1", "site_2", "abc123"])
def test_ar_0010_valid_namespaces_are_accepted(value: str) -> None:
    assert validate_namespace(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "has:colon",  # would forge a key boundary
        "has space",
        "UPPER",
        "trailing-",
        "",
        "x" * 65,
        "unicode-é",
    ],
)
def test_ar_0010_invalid_namespaces_are_rejected(value: str) -> None:
    """AR-0010 — the namespace is interpolated into the Redis key.

    Production change that would make this fail: dropping the charset check.
    A colon lets a crafted namespace address a key outside its own prefix.
    """
    with pytest.raises(vol.Invalid):
        validate_namespace(value)
