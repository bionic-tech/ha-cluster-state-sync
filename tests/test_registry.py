"""The cluster registry and the clock-skew reading it makes possible.

Nothing enumerated cluster members before this, and nothing measured whether
the two nodes agreed about the time — which matters more than it sounds,
because skew silently disables the restore in both directions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.cluster_state_sync.const import (
    CLOCK_SKEW_CRITICAL_SECONDS,
    CLOCK_SKEW_TOLERANCE,
    CLOCK_SKEW_WARN_SECONDS,
    HEALTH_POLL_INTERVAL,
    NODE_REGISTRY_TTL,
    node_key,
    node_key_pattern,
)
from custom_components.cluster_state_sync.coordinator import ClusterView


def _members(**offsets: float) -> dict[str, dict]:
    return {
        node: {"node_id": node, "offset_s": off, "at": datetime.now(tz=UTC).isoformat()}
        for node, off in offsets.items()
    }


# -- keys ---------------------------------------------------------------------


def test_member_keys_live_under_the_namespace_and_match_the_scan() -> None:
    """Production change that would make this fail: the writer and the scanner
    disagreeing about the key shape, which would report a cluster of zero
    members while every node was dutifully registering itself."""
    key = node_key("prod", "tiger1-abc")
    assert key == "ha:cluster_state_sync:prod:nodes:tiger1-abc"
    prefix = node_key_pattern("prod").rstrip("*")
    assert key.startswith(prefix)


def test_a_missed_poll_does_not_drop_a_node_from_the_cluster() -> None:
    """The TTL has to outlast more than one refresh, or a node that hits a slow
    backup flickers out of the member list and back — and a flickering member
    count is a count nobody trusts."""
    assert NODE_REGISTRY_TTL > 2 * HEALTH_POLL_INTERVAL


# -- skew ---------------------------------------------------------------------


def test_skew_is_the_spread_between_members_offsets() -> None:
    """Each node measures itself against the store, so the skew between two of
    them is the difference of their offsets."""
    view = ClusterView(members=_members(a=0.5, b=-1.25))
    assert view.clock_skew == 1.75


def test_skew_is_unknown_with_fewer_than_two_members() -> None:
    """Skew is a property of a pair. A single node reporting 0.0 would claim an
    agreement it has not established with anyone."""
    assert ClusterView(members=_members(only=0.0)).clock_skew is None
    assert ClusterView(members={}).clock_skew is None


def test_the_critical_threshold_is_the_restores_own_cliff() -> None:
    """Production change that would make this fail: picking a round number for
    the alarm instead of the number the code actually enforces.

    Above CLOCK_SKEW_TOLERANCE, `_restore_from_snapshot` refuses the peer's
    entries outright as `skipped_future`. A warning at any other figure would
    be describing a cliff that is not there.
    """
    assert CLOCK_SKEW_CRITICAL_SECONDS == CLOCK_SKEW_TOLERANCE


def test_the_warning_fires_well_before_the_restore_breaks() -> None:
    """60s is where the restore is already fully broken, not where it starts to
    be. NTP-disciplined hosts sit in milliseconds, so anything approaching the
    warning is already a fault rather than a fluctuation."""
    assert CLOCK_SKEW_WARN_SECONDS < CLOCK_SKEW_CRITICAL_SECONDS / 2


def test_a_member_with_no_offset_is_ignored_rather_than_fatal() -> None:
    """One node writing something odd must cost one row, not the whole
    reading."""
    members = _members(a=0.0, b=1.0)
    members["c"] = {"node_id": "c"}  # no offset at all
    members["d"] = {"node_id": "d", "offset_s": "not a number"}
    assert ClusterView(members=members).clock_skew == 1.0
