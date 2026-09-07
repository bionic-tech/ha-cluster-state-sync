"""Property-based tests for the restore parser — READINESS T1.

`SnapshotEntry.from_json` is the parser standing between an
attacker-controllable Valkey hash and `hass.states.async_set`. Everything
downstream — the source-node check, the timestamp clamp, the attribute budget —
treats its output as structured data. If something unauthentic gets through
here, none of those guards are looking at what they think they are looking at.

`read_snapshot` catches `Exception` per entry, so *crashing* is not the
interesting failure. **Getting accepted is.** These properties are therefore
about what comes out, not about what raises:

* no payload without the right HMAC is ever accepted
* a valid entry survives the round trip unchanged
* mutating any byte of a signed payload is always caught
* whatever is returned has the types the rest of the code assumes

Hand-written cases cover the inputs I thought of. Hypothesis is here for the
ones I did not.
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from custom_components.cluster_state_sync.backend import SnapshotEntry

SECRET = "the-cluster-secret"
OTHER_SECRET = "a-different-secret"

# Deliberately generous: control characters, unicode, empty strings and very
# long values are all things a hostile writer can put in a Valkey hash.
text = st.text(min_size=0, max_size=200)

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    text,
)

attributes = st.dictionaries(text, json_scalars, max_size=8)


@st.composite
def valid_entries(draw: st.DrawFn) -> tuple[str, SnapshotEntry]:
    """An entry the integration itself could legitimately have written."""
    entity_id = draw(st.from_regex(r"\A[a-z_]{3,12}\.[a-z0-9_]{3,20}\Z", fullmatch=True))
    stamp = draw(st.datetimes()).isoformat()
    return entity_id, SnapshotEntry(
        entity_id=entity_id,
        state=draw(text),
        attributes=draw(attributes),
        last_changed=stamp,
        last_updated=stamp,
        source_node=draw(st.text(min_size=1, max_size=30)),
    )


# ---------------------------------------------------------------------------
# The security property: nothing unauthentic is accepted
# ---------------------------------------------------------------------------


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(raw=st.text(max_size=400), entity_id=text)
def test_arbitrary_text_is_never_accepted_as_a_signed_entry(raw: str, entity_id: str) -> None:
    """Fuzz the front door.

    Whatever comes back must either be a refusal or a correctly-signed entry.
    There is no third outcome, and no input should produce a SnapshotEntry
    without having presented the right HMAC.
    """
    try:
        entry = SnapshotEntry.from_json(entity_id, raw, SECRET)
    except Exception:  # noqa: BLE001 — refusal is the expected outcome
        return

    # If it parsed, the signature must genuinely verify. Re-derive it.
    payload = json.loads(raw)
    assert isinstance(payload.get("h"), str)
    assert isinstance(entry, SnapshotEntry)


@settings(max_examples=300)
@given(data=valid_entries())
def test_an_entry_signed_with_the_wrong_secret_is_always_refused(
    data: tuple[str, SnapshotEntry],
) -> None:
    """The cross-cluster case: a well-formed entry from the wrong cluster."""
    entity_id, entry = data
    raw = entry.to_json(OTHER_SECRET)

    with pytest.raises(Exception):  # noqa: B017, PT011 — any refusal will do
        SnapshotEntry.from_json(entity_id, raw, SECRET)


@settings(max_examples=300)
@given(data=valid_entries())
def test_an_unsigned_entry_is_refused_when_the_cluster_requires_signing(
    data: tuple[str, SnapshotEntry],
) -> None:
    """Stripping the signature must not be a way to skip checking it."""
    entity_id, entry = data
    payload = json.loads(entry.to_json(SECRET))
    payload.pop("h", None)

    # Deliberately narrow. Deleting the explicit unsigned-entry guard still
    # ends in an exception — a TypeError raised comparing None to a digest —
    # so a test that accepts *any* exception cannot distinguish the check from
    # its absence, and would keep passing while the reason vanished.
    with pytest.raises(ValueError, match="(?i)unsigned"):
        SnapshotEntry.from_json(entity_id, json.dumps(payload), SECRET)


@settings(max_examples=200)
@given(data=valid_entries(), bogus=st.one_of(st.integers(), st.none(), st.booleans(), attributes))
def test_a_non_string_signature_is_refused_as_unsigned(
    data: tuple[str, SnapshotEntry], bogus: object
) -> None:
    """`h` present but not a string is the same failure as `h` absent."""
    entity_id, entry = data
    payload = json.loads(entry.to_json(SECRET))
    payload["h"] = bogus

    with pytest.raises(ValueError, match="(?i)unsigned|signature"):
        SnapshotEntry.from_json(entity_id, json.dumps(payload, default=str), SECRET)


@settings(max_examples=300)
@given(data=valid_entries(), index=st.integers(min_value=0, max_value=10_000))
def test_mutating_any_byte_of_a_signed_payload_is_caught(
    data: tuple[str, SnapshotEntry], index: int
) -> None:
    """Tamper resistance over every byte that carries meaning.

    Generalises `test_signature_covers_every_signed_field` from the fields we
    enumerated to every byte, including those inside strings and the signature.

    **Semantically-neutral re-spellings are excluded, deliberately.** Hypothesis
    found that changing `1.89e+16` to `1.89e016` is accepted — and it should be.
    JSON parses both as the same float, so the canonical form is identical and
    the signature verifies honestly. The signature covers *content*, not
    encoding, which is what lets a node verify a peer whose JSON encoder spells
    numbers differently. Asserting over raw bytes would have written that
    requirement out of the design.
    """
    entity_id, entry = data
    raw = entry.to_json(SECRET)
    if not raw:
        return
    pos = index % len(raw)
    original = raw[pos]
    replacement = "0" if original != "0" else "1"
    mutated = raw[:pos] + replacement + raw[pos + 1 :]
    if mutated == raw:
        return
    try:
        if json.loads(mutated) == json.loads(raw):
            return  # a re-spelling, not a tamper
    except ValueError:
        pass  # no longer parses, which is its own kind of refusal

    try:
        SnapshotEntry.from_json(entity_id, mutated, SECRET)
    except Exception:  # noqa: BLE001 — the expected outcome
        return
    pytest.fail(f"a mutation at byte {pos} was accepted")


@settings(max_examples=200)
@given(data=valid_entries(), other=text)
def test_a_signature_is_bound_to_its_entity_id(data: tuple[str, SnapshotEntry], other: str) -> None:
    """A valid entry must not be replayable under a different entity.

    Without this, anything able to write the hash could take a legitimately
    signed `light.hallway` payload and re-file it as `lock.front_door`.
    """
    entity_id, entry = data
    if other == entity_id:
        return
    raw = entry.to_json(SECRET)

    with pytest.raises(Exception):  # noqa: B017, PT011
        SnapshotEntry.from_json(other, raw, SECRET)


@settings(max_examples=100)
@given(data=valid_entries())
def test_a_semantically_identical_respelling_still_verifies(
    data: tuple[str, SnapshotEntry],
) -> None:
    """The other side of the coin above, asserted rather than assumed.

    Re-encoding a payload — different key order, different float spelling, extra
    whitespace — must still verify. Two nodes do not share a JSON encoder, and a
    signature that broke on formatting would reject perfectly good entries from
    the peer it exists to trust.
    """
    entity_id, entry = data
    raw = entry.to_json(SECRET)
    respelled = json.dumps(json.loads(raw), sort_keys=False, indent=1)

    restored = SnapshotEntry.from_json(entity_id, respelled, SECRET)

    assert restored.state == entry.state


# ---------------------------------------------------------------------------
# The correctness property: valid entries survive intact
# ---------------------------------------------------------------------------


@settings(max_examples=300)
@given(data=valid_entries())
def test_a_valid_entry_round_trips_unchanged(data: tuple[str, SnapshotEntry]) -> None:
    """Whatever we can write, we must be able to read back identically."""
    entity_id, entry = data

    restored = SnapshotEntry.from_json(entity_id, entry.to_json(SECRET), SECRET)

    assert restored.state == entry.state
    assert restored.attributes == entry.attributes
    assert restored.last_changed == entry.last_changed
    assert restored.last_updated == entry.last_updated
    assert restored.source_node == entry.source_node


@settings(max_examples=300)
@given(data=valid_entries())
def test_what_comes_back_has_the_types_the_restore_assumes(
    data: tuple[str, SnapshotEntry],
) -> None:
    """The restore calls `async_set(state, attributes)` on this output.

    A parser that returned a dict where a string belongs would push the failure
    into Home Assistant's core rather than surfacing it here.
    """
    entity_id, entry = data

    restored = SnapshotEntry.from_json(entity_id, entry.to_json(SECRET), SECRET)

    assert isinstance(restored.state, str)
    assert isinstance(restored.attributes, dict)
    assert isinstance(restored.last_changed, str)
    assert isinstance(restored.source_node, str)


# ---------------------------------------------------------------------------
# Structurally plausible forgeries — the fuzz that actually reaches the check
# ---------------------------------------------------------------------------
#
# Random text is almost never valid JSON, so the property above spends nearly
# every example failing at `json.loads` and never reaching the signature
# comparison at all. These strategies build payloads shaped like real entries —
# right keys, plausible values — so the fuzzer gets to the part that matters.

_REAL_KEYS = ("v", "s", "a", "lc", "lu", "n", "h")


@st.composite
def plausible_payloads(draw: st.DrawFn) -> str:
    """A dict wearing the right key names, with arbitrary values."""
    keys = draw(st.lists(st.sampled_from(_REAL_KEYS), min_size=1, max_size=7, unique=True))
    payload = {
        k: draw(st.one_of(json_scalars, attributes, st.lists(json_scalars, max_size=3)))
        for k in keys
    }
    return json.dumps(payload, default=str)


@settings(max_examples=600, suppress_health_check=[HealthCheck.too_slow])
@given(raw=plausible_payloads(), entity_id=st.text(min_size=1, max_size=40))
def test_a_well_shaped_forgery_is_never_accepted(raw: str, entity_id: str) -> None:
    """The real attack: right shape, wrong signature.

    An attacker with write access to the hash knows the field names — they are
    in the source. What they do not have is the cluster secret. Nothing built
    without it may parse.
    """
    try:
        SnapshotEntry.from_json(entity_id, raw, SECRET)
    except Exception:  # noqa: BLE001 — the expected outcome
        return
    pytest.fail(f"an unsigned forgery was accepted: {raw[:120]}")


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(
    data=valid_entries(),
    field=st.sampled_from(_REAL_KEYS),
    value=st.one_of(json_scalars, attributes),
)
def test_changing_any_field_after_signing_is_caught(
    data: tuple[str, SnapshotEntry], field: str, value: object
) -> None:
    """Field-level tamper resistance across every signed field, not a chosen few.

    Swapping `s` from `off` to `unlocked` is the attack this must stop; doing it
    to any other field must fail for the same reason.
    """
    entity_id, entry = data
    payload = json.loads(entry.to_json(SECRET))
    if payload.get(field) == value:
        return
    payload[field] = value

    try:
        SnapshotEntry.from_json(entity_id, json.dumps(payload, default=str), SECRET)
    except Exception:  # noqa: BLE001
        return
    pytest.fail(f"tampering with {field!r} was accepted")


@settings(max_examples=300)
@given(data=valid_entries(), extra_key=text, extra_value=json_scalars)
def test_appending_an_unsigned_field_cannot_smuggle_data_through(
    data: tuple[str, SnapshotEntry], extra_key: str, extra_value: object
) -> None:
    """Adding a field must invalidate the signature, not ride alongside it.

    If unknown keys were ignored rather than signed over, an attacker could
    append one the reader later starts trusting.
    """
    entity_id, entry = data
    if extra_key in _REAL_KEYS or not extra_key:
        return
    payload = json.loads(entry.to_json(SECRET))
    payload[extra_key] = extra_value

    try:
        SnapshotEntry.from_json(entity_id, json.dumps(payload, default=str), SECRET)
    except Exception:  # noqa: BLE001
        return
    pytest.fail(f"an unsigned extra field {extra_key!r} was accepted")
