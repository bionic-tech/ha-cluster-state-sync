"""READINESS T3, T5, T7 and T8.

Four separate claims that were previously asserted by inspection or by a
handful of hand-picked examples:

* **T3** — exclude always beats include, for *any* entity and config
* **T5** — two writers merge rather than erasing each other (ADR-004)
* **T7** — 5,000 entities survive seed → flush → restore, and the README's
  "sub-10ms" claim becomes a measurement rather than an assertion
* **T8** — a rolling upgrade between schema versions does not lose the fleet
"""

from __future__ import annotations

from datetime import UTC, datetime
import time

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from custom_components.cluster_state_sync.backend import SnapshotEntry
from custom_components.cluster_state_sync.const import (
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    SCHEMA_VERSION,
)

from .fakes import FakeBackend

SECRET = "s"

entity_ids = st.from_regex(r"\A[a-z_]{3,10}\.[a-z0-9_]{3,15}\Z", fullmatch=True)


def _entry(entity_id: str, state: str = "on", node: str = "a") -> SnapshotEntry:
    stamp = datetime.now(tz=UTC).isoformat()
    return SnapshotEntry(
        entity_id=entity_id,
        state=state,
        attributes={},
        last_changed=stamp,
        last_updated=stamp,
        source_node=node,
    )


# ---------------------------------------------------------------------------
# T3 — filter precedence
# ---------------------------------------------------------------------------


@settings(max_examples=400)
@given(entity_id=entity_ids)
def test_exclude_always_beats_include(entity_id: str) -> None:
    """Precedence is exactly where entity filters go quietly wrong.

    An operator who excludes something has said "not this one" in the most
    specific way available. No include rule — domain or explicit — may override
    that, or the exclusion silently does nothing and they never find out.
    """
    from custom_components.cluster_state_sync import _should_track

    domain = entity_id.split(".", 1)[0]
    cfg = {
        CONF_EXCLUDE_ENTITIES: [entity_id],
        CONF_INCLUDE_ENTITIES: [entity_id],  # contradicts the exclude
        CONF_INCLUDE_DOMAINS: [domain],  # and so does the domain
    }

    assert _should_track(entity_id, cfg) is False


@settings(max_examples=300)
@given(entity_id=entity_ids)
def test_an_explicit_include_beats_the_domain_list(entity_id: str) -> None:
    """Naming an entity is more specific than naming its domain."""
    from custom_components.cluster_state_sync import _should_track

    cfg = {
        CONF_INCLUDE_ENTITIES: [entity_id],
        CONF_INCLUDE_DOMAINS: ["a_domain_this_is_not"],
    }

    assert _should_track(entity_id, cfg) is True


@settings(max_examples=300)
@given(entity_id=entity_ids, other=entity_ids)
def test_excluding_one_entity_never_affects_another(
    entity_id: str, other: str
) -> None:
    """A filter that catches more than it names is worse than no filter."""
    from custom_components.cluster_state_sync import _should_track

    if other == entity_id:
        return
    domain = other.split(".", 1)[0]
    cfg = {CONF_EXCLUDE_ENTITIES: [entity_id], CONF_INCLUDE_DOMAINS: [domain]}

    assert _should_track(other, cfg) is True


# ---------------------------------------------------------------------------
# T5 — two writers against one backend
# ---------------------------------------------------------------------------


async def test_two_writers_merge_rather_than_erasing_each_other() -> None:
    """ADR-004's no-DEL decision, asserted directly rather than by inspection.

    This is the interim guard that made a misconfigured warm pair degrade
    instead of corrupt, and it is retained even now leadership gating exists.
    """
    backend = FakeBackend()
    await backend.connect()

    await backend.write_snapshot({"light.a": _entry("light.a", node="a")}, "node-a")
    await backend.write_snapshot({"light.b": _entry("light.b", node="b")}, "node-b")

    stored, _ = await backend.read_snapshot()
    assert set(stored) == {"light.a", "light.b"}


async def test_the_later_writer_wins_a_contested_entity() -> None:
    """Last-writer-wins per field: freshness degrades, data does not vanish."""
    backend = FakeBackend()
    await backend.connect()

    await backend.write_snapshot({"light.a": _entry("light.a", "off", "a")}, "node-a")
    await backend.write_snapshot({"light.a": _entry("light.a", "on", "b")}, "node-b")

    stored, meta = await backend.read_snapshot()
    assert stored["light.a"].state == "on"
    assert meta["source_node"] == "node-b"


async def test_a_writer_that_tracks_less_does_not_shrink_the_snapshot() -> None:
    """The documented consequence of no tombstones (ADR-004).

    Asserted so the trade stays visible: a node with a narrower filter leaves
    the peer's extra entries in place rather than deleting them.
    """
    backend = FakeBackend()
    await backend.connect()

    await backend.write_snapshot(
        {"light.a": _entry("light.a"), "light.b": _entry("light.b")}, "node-a"
    )
    await backend.write_snapshot({"light.a": _entry("light.a")}, "node-b")

    stored, _ = await backend.read_snapshot()
    assert set(stored) == {"light.a", "light.b"}


# ---------------------------------------------------------------------------
# T7 — scale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [5000])
async def test_five_thousand_entities_survive_a_round_trip(count: int) -> None:
    """Validates the MAX_RESTORE_ENTRIES bound at the size it guards."""
    backend = FakeBackend()
    await backend.connect()
    entries = {f"light.e{i}": _entry(f"light.e{i}") for i in range(count)}

    assert await backend.write_snapshot(entries, "node-a") is True
    stored, meta = await backend.read_snapshot()

    assert len(stored) == count
    assert meta["entry_count"] == count


def test_signing_five_thousand_entries_is_not_the_bottleneck() -> None:
    """The README claims sub-10ms per entry. Measure it rather than assert it.

    HMAC over a few hundred bytes should be microseconds; the threshold here is
    deliberately loose because CI machines are noisy. It exists to catch an
    order-of-magnitude regression — someone making the signature quadratic, or
    re-deriving a key per entry — not to police a few percent.
    """
    entries = [_entry(f"light.e{i}") for i in range(5000)]

    start = time.perf_counter()
    for entry in entries:
        entry.to_json(SECRET)
    elapsed = time.perf_counter() - start

    per_entry_ms = (elapsed / len(entries)) * 1000
    assert per_entry_ms < 10, f"{per_entry_ms:.3f}ms per entry — the claim was sub-10ms"


# ---------------------------------------------------------------------------
# T8 — rolling upgrade between schema versions
# ---------------------------------------------------------------------------


def test_an_entry_with_no_version_reads_as_v1() -> None:
    """A rolling upgrade must not force a cold start.

    Entries written before versioning existed have no `v`. Refusing them would
    empty the snapshot at exactly the moment an operator was mid-upgrade.
    """
    import json

    from custom_components.cluster_state_sync.backend import _sign

    payload = {
        "s": "on",
        "a": {},
        "lc": "2026-01-01T00:00:00+00:00",
        "lu": "2026-01-01T00:00:00+00:00",
        "n": "old-node",
    }
    payload["h"] = _sign("light.a", payload, SECRET)

    restored = SnapshotEntry.from_json("light.a", json.dumps(payload), SECRET)

    assert restored.state == "on"


def test_an_entry_from_a_newer_schema_is_refused_per_entry() -> None:
    """Forward compatibility has a limit, and it is one entry wide.

    A node that meets a v2 entry it cannot interpret must skip that entry, not
    the snapshot — the older node stays useful for everything it does
    understand.
    """
    import json

    from custom_components.cluster_state_sync.backend import _sign

    payload = {
        "v": SCHEMA_VERSION + 1,
        "s": "on",
        "a": {},
        "lc": "2026-01-01T00:00:00+00:00",
        "lu": "2026-01-01T00:00:00+00:00",
        "n": "newer-node",
    }
    payload["h"] = _sign("light.a", payload, SECRET)

    with pytest.raises(ValueError, match="(?i)newer node|schema v"):
        SnapshotEntry.from_json("light.a", json.dumps(payload), SECRET)


async def test_a_mixed_version_snapshot_restores_what_it_can() -> None:
    """The rolling upgrade in miniature: one old node, one new, one hash."""
    import json

    from custom_components.cluster_state_sync.backend import _sign

    def _raw(entity_id: str, version: int) -> str:
        payload = {
            "v": version,
            "s": "on",
            "a": {},
            "lc": "2026-01-01T00:00:00+00:00",
            "lu": "2026-01-01T00:00:00+00:00",
            "n": "n",
        }
        payload["h"] = _sign(entity_id, payload, SECRET)
        return json.dumps(payload)

    readable = SnapshotEntry.from_json("light.ok", _raw("light.ok", SCHEMA_VERSION), SECRET)
    assert readable.state == "on"

    with pytest.raises(ValueError):
        SnapshotEntry.from_json("light.future", _raw("light.future", SCHEMA_VERSION + 5), SECRET)
