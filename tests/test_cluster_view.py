"""The cluster-wide entities — the ones that read the same on both nodes.

Every diagnostic before these was local: `entities_tracked` is what this node
mirrors, `last_snapshot_age` is this node's own last flush. On a standby, which
by design never flushes, that second one climbs forever while the cluster may be
perfectly healthy. These read from the shared store instead, so a leader and a
standby can be compared — which is the only way to see a split brain without
reading two sets of logs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.cluster_state_sync.coordinator import ClusterView, _parse_iso


def test_an_empty_view_reports_unknown_age_rather_than_zero() -> None:
    """Production change this catches: returning 0 for a never-written snapshot.

    Zero claims perfect freshness, which is the exact opposite of the truth for
    a store nobody has written to — and it is the value a dashboard would render
    in green.
    """
    assert ClusterView().snapshot_age is None


def test_snapshot_age_is_measured_from_the_shared_timestamp() -> None:
    """Not from anything local. A standby that has never flushed still gets a
    truthful answer, because the number comes from whoever wrote last."""
    written = datetime.now(tz=UTC) - timedelta(seconds=90)
    age = ClusterView(snapshot_at=written).snapshot_age
    assert age is not None and 85 < age < 95, age


def test_a_naive_timestamp_is_read_as_utc_not_local() -> None:
    """The peer writes this field. If it ever loses its offset, treating it as
    local time would shift the age by the machine's UTC offset — silently, and
    differently on each node, which is the worst shape for a number two people
    are meant to compare."""
    parsed = _parse_iso("2026-09-02T10:00:00")
    assert parsed is not None and parsed.tzinfo is UTC


def test_unparseable_meta_costs_one_reading_not_the_whole_poll() -> None:
    """Production change this catches: letting fromisoformat raise. The peer
    writes this value, so a malformed one must not take out the leader we read
    successfully alongside it."""
    assert _parse_iso("not a timestamp") is None
    assert _parse_iso(None) is None
    assert _parse_iso(12345) is None
