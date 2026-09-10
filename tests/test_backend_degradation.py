"""The backend's "degrade, never raise" posture, which nothing asserted.

Every read in `RedisBackend` is written to return an empty answer rather than
propagate an exception, and the reason is stated in the code: *an unreachable
backend is a reading to publish, not an error that should blank the entities
and hide the problem.* That posture is load-bearing — it is why a Valkey
outage shows as "unknown" on a dashboard instead of taking Home Assistant's
setup down with it — and it was enforced by comment alone.

🚨 It also underpins v0.4.2's promotion detection. That reads `snapshot_source`
from `read_cluster_view`, and a version of this method that raised, or that
invented a leader out of unreadable metadata, would either break setup or
announce a failover that never happened.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from custom_components.cluster_state_sync.backend import RedisBackend

NAMESPACE = "testns"


class _Client:
    """A client whose every call can be told what to do."""

    def __init__(self, *, mget: Any = None, raises: bool = False) -> None:
        self._mget = mget
        self._raises = raises

    async def mget(self, *_keys: str) -> Any:
        if self._raises:
            raise ConnectionError("valkey went away mid-read")
        return self._mget

    async def eval(self, *_args: Any) -> Any:
        if self._raises:
            raise ConnectionError("valkey went away mid-write")
        return 1

    async def ping(self) -> bool:
        if self._raises:
            raise ConnectionError("valkey went away")
        return True


def _backend(client: Any) -> RedisBackend:
    be = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    be._client = client  # noqa: SLF001 — the same client seam test_backend.py uses
    return be


# -- read_cluster_view, which v0.4.2's promotion detection depends on -------


async def test_an_unreachable_backend_reports_nothing_known(hass) -> None:
    """Not an exception, and not a stale value it can no longer vouch for."""
    backend = _backend(_Client(raises=True))
    assert await backend.read_cluster_view() == (None, {})


async def test_no_client_at_all_reports_nothing_known(hass) -> None:
    """Before `connect()`, or after `close()`."""
    backend = _backend(None)
    assert await backend.read_cluster_view() == (None, {})


async def test_unreadable_meta_does_not_lose_the_leader_read_alongside_it(hass) -> None:
    """Two values, one round trip: corrupting one must not discard the other.

    The leader is what decides whether this node publishes at all. Throwing it
    away because the metadata beside it was malformed would turn a cosmetic
    fault into a replication outage.
    """
    backend = _backend(_Client(mget=["node-a", "{not json"]))
    leader, meta = await backend.read_cluster_view()
    assert leader == "node-a"
    assert meta == {}


async def test_an_empty_leader_key_reads_as_no_leader_not_empty_string(hass) -> None:
    """🚨 `""` is truthy nowhere useful and falsy everywhere dangerous.

    v0.4.2 compares the leader against this node's id. An empty string would
    compare unequal to every node id and make a leaderless cluster look, to the
    promotion check, exactly like a cluster led by somebody else.
    """
    backend = _backend(_Client(mget=["", json.dumps({"source_node": "node-b"})]))
    leader, meta = await backend.read_cluster_view()
    assert leader is None
    assert meta["source_node"] == "node-b"


async def test_a_good_read_returns_both_values(hass) -> None:
    backend = _backend(_Client(mget=["node-a", json.dumps({"source_node": "node-b"})]))
    leader, meta = await backend.read_cluster_view()
    assert leader == "node-a"
    assert meta == {"source_node": "node-b"}


# -- the lease, where degrading has to mean "do not claim it" --------------


async def test_a_failed_renew_reports_failure_rather_than_success(hass) -> None:
    """🚨 The direction of this failure is the whole point.

    A renew that cannot reach Valkey must return False. Returning True on error
    would have a node believe it still holds a lease that has expired, while
    the peer takes it — both nodes leaders, radios claimed twice, which is the
    split brain the lease exists to prevent.
    """
    backend = _backend(_Client(raises=True))
    assert await backend.renew_leadership("node-a") is False


async def test_a_renew_with_no_client_does_not_claim_the_lease(hass) -> None:
    backend = _backend(None)
    assert await backend.renew_leadership("node-a") is False


async def test_health_reports_false_rather_than_raising(hass) -> None:
    backend = _backend(_Client(raises=True))
    assert await backend.health() is False


@pytest.mark.parametrize("returned", [1, "1", True])
async def test_a_successful_renew_is_recognised_in_every_shape_redis_returns(
    hass, returned: Any
) -> None:
    """Redis returns integers, redis-py may hand back bytes or ints."""

    class _R(_Client):
        async def eval(self, *_args: Any) -> Any:
            return returned

    assert await _backend(_R()).renew_leadership("node-a") is True
