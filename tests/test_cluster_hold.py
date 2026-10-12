"""The CLUSTER-WIDE maintenance hold (GOTCHAS 29).

On 2026-09-18 an operator set the maintenance hold from the Home Assistant
dashboard before rebooting the leader. Every part of what followed was correct
behaviour by the code as it stood, and the house moved anyway:

* the hold was a file in the leader's own config directory, so the standby
  never saw it;
* the standby's promoter found a lapsed lease and promoted, exactly as it
  should when nothing tells it otherwise;
* the leader's promoter, still held, never demoted -- so it never ran
  `docker stop`, so `unless-stopped` brought Home Assistant back;
* both instances then ran, for four hours.

🚨 **The instruction that would have prevented it could not be followed.**
`cluster-hold.sh` said "set it on BOTH nodes", but in the cold model only ONE
node runs Home Assistant -- the standby's is deliberately stopped -- so the
dashboard the operator reached was the only one in existence. A per-node flag
cannot express a cluster-wide intent from the only node that can be reached.

So the hold now has two halves: the local file, which keeps working when the
backend does not, and a key in the shared store, which is what reaches the
peer. Either one suspends failover. The tests below are about the second half
and about the seam between them.
"""

from __future__ import annotations

import pathlib

import pytest

from custom_components.cluster_state_sync import _clear_auto_hold_cluster_wide
from custom_components.cluster_state_sync.const import hold_key
from custom_components.cluster_state_sync.hold import (
    AUTO_HOLD_MARKER,
    AUTO_HOLD_SECONDS,
    auto_hold_body,
    hold_body,
    hold_deadline,
    hold_origin,
    reason_of,
    set_hold,
)
from custom_components.cluster_state_sync.leadership import LeadershipMonitor
from custom_components.cluster_state_sync.scripts import cluster_promoter
from tests.fakes import FakeBackend

NODE_A = "node-a-aaaaaa"
NODE_B = "node-b-bbbbbb"


# --------------------------------------------------------------------------
# The wire contract: two implementations that must not drift
# --------------------------------------------------------------------------


def test_the_promoter_and_the_integration_agree_on_the_hold_key() -> None:
    """🚨 The drift guard the other duplicated keys never had.

    `cluster_promoter.py` is emitted as standalone text onto a host with no
    `custom_components` on its path (ADR-005), so it repeats the key format
    rather than importing it. That is a deliberate duplication and it is
    exactly the kind that rots: if these two ever disagree, the integration
    publishes a hold to one key and every promoter reads another, and the
    symptom is a hold that appears to be set and suspends nothing at all --
    indistinguishable, from the dashboard, from the bug this key was added to
    fix.
    """
    for namespace in ("prod", "test", "a-b_c"):
        assert cluster_promoter._hold_key(namespace) == hold_key(namespace)


def test_the_promoter_and_the_integration_agree_on_the_origin_line() -> None:
    """Same duplication, same risk: `_hold_origin` mirrors `hold.hold_origin`."""
    for text in (
        hold_body("planned work", origin=NODE_A),
        auto_hold_body(AUTO_HOLD_SECONDS, NODE_B),
        "no origin at all\n",
        "",
        "origin:\n",
    ):
        assert cluster_promoter._hold_origin(text) == hold_origin(text)


def test_the_two_expiry_parsers_agree() -> None:
    """The promoter enforces the deadline; the integration writes it.

    A hold whose `expires:` line one side reads and the other does not is a
    cluster that resumes failover at a moment nobody chose.
    """
    text = auto_hold_body(AUTO_HOLD_SECONDS, NODE_A)
    expired, deadline = cluster_promoter._text_is_expired(text)
    assert expired is False
    assert deadline == hold_deadline(text)

    stale = hold_body("cover a restart", origin=NODE_A, expires="2000-01-01T00:00:00+00:00")
    expired, deadline = cluster_promoter._text_is_expired(stale)
    assert expired is True
    assert deadline == hold_deadline(stale)


def test_a_malformed_deadline_never_reads_as_expired() -> None:
    """Guessing that a broken hold has lapsed resumes failover mid-maintenance."""
    expired, _ = cluster_promoter._text_is_expired(
        hold_body("planned", origin=NODE_A, expires="not-a-date")
    )
    assert expired is False


# --------------------------------------------------------------------------
# The text format
# --------------------------------------------------------------------------


def test_the_machinery_never_leaks_into_the_reason_a_person_reads() -> None:
    text = hold_body("upgrading the integration", origin=NODE_A)
    assert reason_of(text) == "upgrading the integration"
    assert hold_origin(text) == NODE_A


@pytest.mark.parametrize(
    "legacy",
    [
        "",  # `touch`, the documented way to set one by hand
        "doing maintenance\n",  # any version before the origin line existed
    ],
)
def test_a_hold_from_before_this_existed_still_parses(legacy: str) -> None:
    """Backwards compatibility is not optional here.

    A half-upgraded pair is the expected state during an upgrade, and the node
    still on the old version writes a file with no `origin:` line at all. If
    that failed to parse, upgrading would break the hold during precisely the
    maintenance window the operator raised it for.
    """
    assert hold_origin(legacy) is None
    assert hold_deadline(legacy) is None
    assert reason_of(legacy) == legacy.strip()


# --------------------------------------------------------------------------
# The regression: a hold set on one node binds the other
# --------------------------------------------------------------------------


async def test_a_hold_set_on_the_peer_suspends_this_nodes_leadership(
    hass, tmp_path: pathlib.Path
) -> None:
    """🚨 THE regression test for GOTCHAS 29.

    Node B has no local hold file -- it is the cold standby, its Home Assistant
    is stopped, and nobody could have set one there. Node A raised the hold
    from the only dashboard that exists. Before 0.6.0 node B took the free
    lease and promoted; the house moved during planned maintenance.
    """
    backend = FakeBackend()
    await backend.connect()
    await backend.publish_cluster_hold(hold_body("rebooting node A", origin=NODE_A))

    monitor = LeadershipMonitor(
        hass, backend, node_id=NODE_B, source="lease", config_dir=str(tmp_path)
    )

    # No local hold on this node, and none could exist.
    assert not (tmp_path / ".cluster_sync_hold").exists()
    assert await monitor.async_is_held() is True

    # The lease is free. Before this fix, that was all it took.
    assert backend.lease_holder is None
    assert await monitor._from_lease() is False
    assert backend.lease_holder is None, "node B took a free lease during a cluster-wide hold"


async def test_a_local_hold_still_works_when_the_backend_is_down(
    hass, tmp_path: pathlib.Path
) -> None:
    """The reason the local file survives rather than being replaced.

    A shared-key-only hold would stop working the moment the store did --
    during exactly the backend maintenance a hold is most wanted for.
    """
    backend = FakeBackend()
    backend.hold_raises = True
    set_hold(str(tmp_path), "valkey maintenance", NODE_B)

    monitor = LeadershipMonitor(
        hass, backend, node_id=NODE_B, source="lease", config_dir=str(tmp_path)
    )
    assert await monitor.async_is_held() is True


async def test_an_unreadable_cluster_hold_reads_as_not_held(
    hass, tmp_path: pathlib.Path
) -> None:
    """Fails toward NOT holding, matching hold.py -- and safely.

    A hold that switches itself on because a read failed is a cluster that has
    quietly stopped failing over. That posture is safe here for a reason worth
    stating: a node that cannot reach the store cannot take the lease either,
    so failing this way can never promote anything.
    """
    backend = FakeBackend()
    backend.hold_raises = True

    monitor = LeadershipMonitor(
        hass, backend, node_id=NODE_B, source="lease", config_dir=str(tmp_path)
    )
    with pytest.raises(ConnectionError):
        await backend.read_cluster_hold()
    assert await monitor._from_lease() is False


# --------------------------------------------------------------------------
# Clearing: cluster-wide from either side, but never somebody else's
# --------------------------------------------------------------------------


async def test_an_automatic_hold_is_cleared_cluster_wide_on_the_way_back_up() -> None:
    backend = FakeBackend()
    await backend.connect()
    await backend.publish_cluster_hold(auto_hold_body(AUTO_HOLD_SECONDS, NODE_A))

    assert await _clear_auto_hold_cluster_wide(backend) is True
    assert await backend.read_cluster_hold() is None


async def test_an_operators_cluster_hold_is_never_cleared_by_a_restart() -> None:
    """🚨 The one that would resume failover on a machine someone is working on.

    This matters more cluster-wide than it ever did locally: the key binds the
    PEER, so clearing it on our way up would resume failover on a node the
    operator is still working on -- and they would have no reason to suspect
    it, because they set the hold on one machine and a different machine
    removed it.
    """
    backend = FakeBackend()
    await backend.connect()
    operators = hold_body("replacing the disk in node B", origin=NODE_B)
    await backend.publish_cluster_hold(operators)

    assert await _clear_auto_hold_cluster_wide(backend) is False
    assert await backend.read_cluster_hold() == operators
    assert AUTO_HOLD_MARKER not in operators


async def test_a_backend_that_cannot_answer_clears_nothing() -> None:
    """Errs toward a hold outliving its purpose, never toward one vanishing."""
    backend = FakeBackend()
    backend.hold_raises = True
    assert await _clear_auto_hold_cluster_wide(backend) is False


async def test_an_automatic_cluster_hold_lapses_on_its_own() -> None:
    """The node that set it may never come back to clear it.

    Belt (`expires:`) and braces (the key's TTL). This exercises the TTL --
    the half a dead node cannot enforce for itself.
    """
    backend = FakeBackend()
    await backend.connect()
    await backend.publish_cluster_hold(auto_hold_body(AUTO_HOLD_SECONDS, NODE_A), ttl_seconds=0)
    assert await backend.read_cluster_hold() is None


async def test_an_operators_cluster_hold_has_no_ttl() -> None:
    """Indefinite by design. Only a person clears it."""
    backend = FakeBackend()
    await backend.connect()
    await backend.publish_cluster_hold(hold_body("planned work", origin=NODE_A))
    assert backend.cluster_hold_expires_at is None
    assert await backend.read_cluster_hold() is not None


# --------------------------------------------------------------------------
# The promoter: the only reader that exists on a cold standby
# --------------------------------------------------------------------------


class StoreClient:
    """A ValkeyClient that models the plain key/value half of the store.

    Enough to exercise the hold end to end: the promoter writes with `set`,
    reads with `get`, removes with `delete`, and evaluates the lease scripts.
    """

    def __init__(self, holds: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.holds = holds
        self.evals: list[tuple[str, list[str], list[str]]] = []

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    def eval(self, script: str, keys: list[str], args: list[str]) -> int:
        if "promoters:" in (keys[0] if keys else ""):
            return 1
        self.evals.append((script, keys, args))
        return 1 if self.holds else 0

    def close(self) -> None: ...


class ExplodingClient(StoreClient):
    """Every command fails. The store is unreachable, not merely empty."""

    def get(self, key: str) -> str | None:
        raise ConnectionError("valkey is unreachable")

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        raise ConnectionError("valkey is unreachable")

    def delete(self, key: str) -> int:
        raise ConnectionError("valkey is unreachable")


def test_the_promoter_sets_and_reads_back_a_cluster_hold() -> None:
    client = StoreClient()
    assert cluster_promoter.set_cluster_hold(
        client, namespace="prod", node_id=NODE_A, reason="planned work"
    ) == 0

    text = cluster_promoter._read_cluster_hold(client, "prod")
    assert text is not None
    assert cluster_promoter._hold_origin(text) == NODE_A
    assert "planned work" in text


def test_the_promoter_records_who_raised_the_hold_even_with_no_reason() -> None:
    """The origin is the half you cannot do without.

    A hold found on a node that did not set it is useless without "go and
    clear it over there".
    """
    client = StoreClient()
    assert cluster_promoter.set_cluster_hold(
        client, namespace="prod", node_id=NODE_A, reason="   "
    ) == 0
    assert cluster_promoter._hold_origin(client.store[cluster_promoter._hold_key("prod")]) == NODE_A


def test_setting_a_cluster_hold_reports_failure_rather_than_swallowing_it(capsys) -> None:
    """🚨 Exit non-zero, loudly. `cluster-hold.sh` branches on this.

    Every other backend call in the promoter degrades quietly, because a
    missed heartbeat costs a diagnostic. This one is the opposite: an operator
    told the hold is set, who then reboots a box, gets the exact accident the
    hold exists to prevent.
    """
    rc = cluster_promoter.set_cluster_hold(
        ExplodingClient(), namespace="prod", node_id=NODE_A, reason="planned"
    )
    assert rc == 1
    assert "could NOT raise" in capsys.readouterr().err


def test_clearing_a_cluster_hold_reports_failure_rather_than_swallowing_it(capsys) -> None:
    rc = cluster_promoter.clear_cluster_hold(ExplodingClient(), namespace="prod")
    assert rc == 1
    assert "could NOT clear" in capsys.readouterr().err


def test_the_promoter_clears_a_hold_whichever_node_raised_it() -> None:
    """Not identity-checked, unlike the lease's release.

    The hold is a property of the cluster, not a claim by one node, so the
    operator at the one running dashboard must be able to release a hold the
    peer set.
    """
    client = StoreClient()
    cluster_promoter.set_cluster_hold(client, namespace="prod", node_id=NODE_A, reason="x")
    assert cluster_promoter.clear_cluster_hold(client, namespace="prod") == 0
    assert cluster_promoter._read_cluster_hold(client, "prod") is None


def test_show_hold_prints_origin_and_reason(capsys) -> None:
    client = StoreClient()
    cluster_promoter.set_cluster_hold(
        client, namespace="prod", node_id=NODE_A, reason="swapping the disk"
    )
    assert cluster_promoter.show_cluster_hold(client, namespace="prod") == 0
    out = capsys.readouterr().out
    assert NODE_A in out
    assert "swapping the disk" in out


def test_show_hold_prints_NOTHING_when_there_is_none(capsys) -> None:
    """🚨 Nothing, not the word "none".

    `cluster-hold.sh status` tests this output for emptiness. A node that
    printed a word here would read as a held cluster.
    """
    assert cluster_promoter.show_cluster_hold(StoreClient(), namespace="prod") == 0
    assert capsys.readouterr().out == ""


def test_show_hold_stays_silent_and_succeeds_when_the_store_is_unreachable(capsys) -> None:
    """Absence and failure must not share a signal — so neither prints.

    Exit 0 either way: the CALLER decides what a hold means for its own exit
    code, and conflating "no hold" with "could not ask" here would hand
    `cluster-hold.sh` a hold it invented.
    """
    assert cluster_promoter.show_cluster_hold(ExplodingClient(), namespace="prod") == 0
    assert capsys.readouterr().out == ""


def test_a_cluster_hold_read_survives_bytes_from_the_store() -> None:
    """A client configured without `decode_responses` hands back bytes."""

    class BytesClient(StoreClient):
        def get(self, key: str) -> object:
            return b"planned\norigin: node-a-aaaaaa\n"

    text = cluster_promoter._read_cluster_hold(BytesClient(), "prod")
    assert text is not None
    assert cluster_promoter._hold_origin(text) == NODE_A


def test_an_unreachable_store_reads_as_no_cluster_hold() -> None:
    """Safe here for a stated reason, not by luck.

    A promoter that cannot reach Valkey cannot take the lease either, because
    the lease lives in the same place. So failing toward "not held" can never
    promote anything.
    """
    assert cluster_promoter._read_cluster_hold(ExplodingClient(), "prod") is None


# --------------------------------------------------------------------------
# End to end through the promoter: the cold-standby case that failed
# --------------------------------------------------------------------------


def test_a_cold_standby_refuses_a_free_lease_under_a_peers_hold(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 The failure of 2026-09-18, driven through `run()` itself.

    This is the standby, mid-incident: no local hold file (its Home Assistant
    is stopped, so nobody could have set one), a lease that has lapsed because
    the leader is rebooting, and a peer that raised a cluster-wide hold before
    starting. Before 0.6.0 this node promoted and the house moved.

    Two things make this test bite, and it was worthless without either:

    * `hold_path` points at a file that does not exist, so the local half
      cannot be what suspends the promotion;
    * `holds=True`, so **the lease is genuinely takeable**. With a lease that
      cannot be taken, a standby does nothing for reasons entirely unrelated
      to the hold, and this test passes against the very code it exists to
      catch. It did, when first written.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    client = StoreClient(holds=True)
    cluster_promoter.set_cluster_hold(
        client, namespace="prod", node_id=NODE_A, reason="rebooting node A"
    )
    recorded: list[list[str]] = []

    rc = cluster_promoter.run(
        client,
        namespace="prod",
        node_id=NODE_B,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=lambda cmd: recorded.append(cmd) or 0,
        hold_path=tmp_path / ".cluster_sync_hold",  # absent, on purpose
        probe=lambda _u, _t: True,
        probe_grace=0,
    )

    assert rc == 0
    assert not (tmp_path / ".cluster_sync_hold").exists()
    assert recorded == [], "the standby promoted during a cluster-wide hold"
    assert state.read_text().strip() == "BACKUP"


def test_the_promoter_names_the_scope_and_the_origin(
    tmp_path: pathlib.Path, capsys
) -> None:
    """The old message claimed cluster-wide suspension for a node-local fact.

    An operator read "Failover is SUSPENDED", believed the cluster was safe,
    and rebooted the leader. The message now has to say which it is, and who
    to go to.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    client = StoreClient(holds=False)
    cluster_promoter.set_cluster_hold(
        client, namespace="prod", node_id=NODE_A, reason="rebooting node A"
    )

    cluster_promoter.run(
        client,
        namespace="prod",
        node_id=NODE_B,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=lambda _cmd: 0,
        hold_path=tmp_path / ".cluster_sync_hold",
        probe=lambda _u, _t: True,
        probe_grace=0,
    )

    out = capsys.readouterr().out
    assert "CLUSTER-WIDE" in out
    assert NODE_A in out, "the operator cannot tell where to go to clear it"
    assert "EVERY node" in out


def test_a_purely_local_hold_says_the_peer_is_unaffected(
    tmp_path: pathlib.Path, capsys
) -> None:
    """The honest version of the old message, for the case it was true of."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    hold = tmp_path / ".cluster_sync_hold"
    hold.write_text("local only\n")

    cluster_promoter.run(
        StoreClient(holds=False),
        namespace="prod",
        node_id=NODE_B,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=lambda _cmd: 0,
        hold_path=hold,
        probe=lambda _u, _t: True,
        probe_grace=0,
    )

    out = capsys.readouterr().out
    assert "LOCAL maintenance hold" in out
    # Assert the PROPERTY, not the phrasing: the message must say the peer is
    # not held and name the mechanism by which it would promote. The original
    # wording ("the peer is unaffected and may still promote") was accurate and
    # was read as reassurance by an operator at speed — so a test pinned to the
    # words would have passed through exactly the fix that was needed.
    assert "PEER IS NOT" in out, "the message does not say the peer is unheld"
    assert "lapse" in out, "the message does not name the mechanism"
    assert "cluster-hold.sh" in out, "the message does not say what to do instead"


def test_an_expired_cluster_hold_is_ignored(tmp_path: pathlib.Path, capsys) -> None:
    """A deadline that has passed while the key's TTL has not — i.e. clock skew
    between the node that set it and this one."""
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")
    # `holds=True` so the lease is actually takeable. Without it the node
    # cannot promote for a reason that has nothing to do with the hold, and
    # this test would pass whether the expiry worked or not -- proving only
    # that a node with no lease does nothing.
    client = StoreClient(holds=True)
    client.store[cluster_promoter._hold_key("prod")] = hold_body(
        "cover a restart", origin=NODE_A, expires="2000-01-01T00:00:00+00:00"
    )
    recorded: list[list[str]] = []

    cluster_promoter.run(
        client,
        namespace="prod",
        node_id=NODE_B,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=lambda cmd: recorded.append(cmd) or 0,
        hold_path=tmp_path / ".cluster_sync_hold",
        probe=lambda _u, _t: True,
        probe_grace=0,
    )

    assert "expired" in capsys.readouterr().err
    assert recorded != [], "an expired hold must not keep suspending failover"


# --------------------------------------------------------------------------
# RedisBackend, at the client seam
# --------------------------------------------------------------------------


class _AsyncStore:
    """The async half of a redis client, for the three new backend calls."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.px: int | None = None

    async def set(self, key: str, value: str, px: int | None = None) -> None:
        self.store[key] = value
        self.px = px

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


class _AsyncExploding(_AsyncStore):
    async def set(self, key: str, value: str, px: int | None = None) -> None:
        raise ConnectionError("down")

    async def get(self, key: str) -> str | None:
        raise ConnectionError("down")

    async def delete(self, key: str) -> int:
        raise ConnectionError("down")


def _redis_backend(client: object):
    from custom_components.cluster_state_sync.backend import RedisBackend

    be = RedisBackend(namespace="prod", host="valkey.invalid", port=6379)
    be._client = client  # noqa: SLF001 — injecting at the client seam
    return be


async def test_the_backend_publishes_an_operators_hold_without_a_ttl() -> None:
    client = _AsyncStore()
    be = _redis_backend(client)
    assert await be.publish_cluster_hold(hold_body("planned", origin=NODE_A)) is True
    assert client.px is None, "an operator's hold is indefinite by design"
    assert await be.read_cluster_hold() is not None


async def test_the_backend_publishes_an_automatic_hold_with_a_ttl() -> None:
    client = _AsyncStore()
    be = _redis_backend(client)
    assert await be.publish_cluster_hold(auto_hold_body(300, NODE_A), ttl_seconds=300) is True
    assert client.px == 300_000, "PX is milliseconds; seconds would be a 300ms hold"


async def test_the_backend_clears_the_hold() -> None:
    client = _AsyncStore()
    be = _redis_backend(client)
    await be.publish_cluster_hold(hold_body("planned", origin=NODE_A))
    assert await be.clear_cluster_hold() is True
    assert await be.read_cluster_hold() is None


async def test_the_backend_never_raises_on_a_dead_store() -> None:
    """Degrades, never crashes Home Assistant — but reports the write failures.

    `read` answers None (fails toward not-held); `publish` and `clear` answer
    False so the caller can say so out loud.
    """
    be = _redis_backend(_AsyncExploding())
    assert await be.read_cluster_hold() is None
    assert await be.publish_cluster_hold("x") is False
    assert await be.clear_cluster_hold() is False


async def test_a_backend_with_no_client_holds_nothing() -> None:
    be = _redis_backend(None)
    assert await be.read_cluster_hold() is None
    assert await be.publish_cluster_hold("x") is False
    assert await be.clear_cluster_hold() is False


# --------------------------------------------------------------------------
# BACKUP-but-serving: the half of GOTCHAS 29 that leaves two houses live
# --------------------------------------------------------------------------


def _inspect(running: bool):
    """The shape `_docker_inspector` REALLY returns — flat, not nested.

    🚨 This fake previously returned {"State": {"Running": ...}}, the raw
    `docker inspect` JSON shape. The real inspector shells out with a
    `--format` template and returns a flat dict. So `_backup_is_serving` read
    a key that never existed, evaluated False on every tick, and the reconcile
    was dead code — while every test here passed, because the fake agreed with
    the bug instead of with the inspector.

    Found by running it on real hardware (2026-09-22), not by the suite.
    `test_the_fake_inspector_matches_the_real_one` now pins the two together.
    """
    return lambda: {
        "Restarting": False,
        "Running": running,
        "ExitCode": 0,
        "StartedAtEpoch": 0.0,
    }


def test_the_fake_inspector_matches_the_real_one() -> None:
    """🚨 The contract test that would have caught the dead reconcile.

    Drives the REAL `_docker_inspector` against a canned `docker inspect
    --format` line, and feeds its output to the REAL consumer. If the parser's
    keys and the consumer's keys ever diverge again, this fails — where a fake
    written from the same wrong assumption as the code cannot.
    """
    import subprocess
    from unittest.mock import patch

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="false true 0 2026-09-22T11:30:31.0Z\n", stderr=""
    )
    with patch.object(cluster_promoter.subprocess, "run", return_value=completed):
        real = cluster_promoter._docker_inspector("home-assistant-2")()

    assert real is not None
    # The consumer must read this exact object and see a running container.
    assert real.get("Running") is True
    assert set(_inspect(True)().keys()) == set(real.keys()), (
        "the fake and the real inspector disagree on shape; a test using the "
        "fake proves nothing about the code using the real one"
    )
    assert cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=False,
        reconcile=True,
        inspect_container=lambda: real,
        state_path=pathlib.Path("/nonexistent-so-never-recently-touched"),
    ) is True


def _aged(path: pathlib.Path, seconds: float) -> pathlib.Path:
    """Backdate a file so it is outside the reconcile grace."""
    import os
    import time

    then = time.time() - seconds
    os.utime(path, (then, then))
    return path


def _stale_state(tmp_path: pathlib.Path, value: str = "BACKUP") -> pathlib.Path:
    state = tmp_path / "vrrp-state"
    state.write_text(f"{value}\n")
    return _aged(state, cluster_promoter.BACKUP_SERVING_GRACE_SECONDS + 60)


def test_backup_while_still_serving_is_detected(tmp_path: pathlib.Path) -> None:
    """The state nothing could see: BACKUP on paper, serving in fact."""
    assert cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=False,
        reconcile=True,
        inspect_container=_inspect(True),
        state_path=_stale_state(tmp_path),
    )


def test_a_warm_standby_running_home_assistant_is_never_touched(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 The condition that stops this fix becoming an outage.

    A warm standby runs Home Assistant continuously — that is what warm
    *means*. Firing here would stop the standby's house on every tick of every
    warm install. `bundle.py` emits `--reconcile-backup` for cold deployments
    only, and this is the assertion that the promoter honours it.
    """
    assert not cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=False,
        reconcile=False,  # warm
        inspect_container=_inspect(True),
        state_path=_stale_state(tmp_path),
    )


def test_a_demotion_in_flight_is_not_mistaken_for_the_fault(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 `notify_backup.sh` writes BACKUP *before* `docker stop -t 120`.

    So for up to two minutes a perfectly healthy demotion is indistinguishable
    from the fault. Without the grace this would fire in the middle of every
    demotion and race the `docker stop` already running.
    """
    state = tmp_path / "vrrp-state"
    state.write_text("BACKUP\n")  # just written: a demotion is in progress
    assert not cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=False,
        reconcile=True,
        inspect_container=_inspect(True),
        state_path=state,
    )


def test_a_leader_is_never_reconciled(tmp_path: pathlib.Path) -> None:
    """A MASTER serving Home Assistant is the entire point of a MASTER."""
    assert not cluster_promoter._backup_is_serving(
        previous="MASTER",
        holds_lease=True,
        reconcile=True,
        inspect_container=_inspect(True),
        state_path=_stale_state(tmp_path, "MASTER"),
    )


def test_a_node_taking_the_lease_this_tick_is_never_reconciled(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 Checked AFTER the lease call, never before.

    A node that has just taken a free lease is promoting, and its container
    should be running. Reconciling on `previous == BACKUP` alone would stop
    Home Assistant on the node that is in the middle of becoming the leader.
    """
    assert not cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=True,  # we hold it: we are promoting
        reconcile=True,
        inspect_container=_inspect(True),
        state_path=_stale_state(tmp_path),
    )


def test_a_stopped_container_is_the_correct_state(tmp_path: pathlib.Path) -> None:
    assert not cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=False,
        reconcile=True,
        inspect_container=_inspect(False),
        state_path=_stale_state(tmp_path),
    )


@pytest.mark.parametrize(
    "inspect",
    [
        None,  # no --ha-container: bare metal, Core, no CLI
        lambda: None,  # docker cannot answer
        lambda: {},  # malformed
    ],
)
def test_an_unanswerable_question_is_not_a_fault(tmp_path: pathlib.Path, inspect) -> None:
    """Absence of an answer is not evidence of a fault.

    ADR-006 makes the same call about `inspect` returning None. Inventing a
    demotion from a question we could not ask is how a fix becomes an outage.
    """
    assert not cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=False,
        reconcile=True,
        inspect_container=inspect,
        state_path=_stale_state(tmp_path),
    )


def test_an_exploding_inspector_is_not_a_fault(tmp_path: pathlib.Path) -> None:
    def boom():
        raise OSError("docker is not there")

    assert not cluster_promoter._backup_is_serving(
        previous="BACKUP",
        holds_lease=False,
        reconcile=True,
        inspect_container=boom,
        state_path=_stale_state(tmp_path),
    )


def test_the_promoter_runs_the_demotion_that_never_happened(
    tmp_path: pathlib.Path, capsys
) -> None:
    """🚨 End to end: the second half of 2026-09-18, through `run()`.

    This is node-a at 10:53:41 — back from a reboot, state file reset to
    BACKUP on tmpfs, container restored by `unless-stopped`, lease held by the
    peer. `decide()` sees no change and, before this, returned 0 and left two
    live instances running indefinitely.
    """
    state = _stale_state(tmp_path)
    recorded: list[list[str]] = []

    def runner(cmd: list[str]) -> int:
        recorded.append(cmd)
        state.write_text("BACKUP\n")  # what notify_backup.sh does
        return 0

    rc = cluster_promoter.run(
        StoreClient(holds=False),
        namespace="prod",
        node_id=NODE_B,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=runner,
        hold_path=tmp_path / ".cluster_sync_hold",
        probe=lambda _u, _t: True,
        probe_grace=0,
        inspect_container=_inspect(True),
        reconcile_backup=True,
    )

    assert rc == 0
    assert [pathlib.Path(c[0]).name for c in recorded] == ["notify_backup.sh"], (
        "the demotion that never ran must run now"
    )
    err = capsys.readouterr().err
    assert "still" in err and "SERVING" in err


def test_the_reconcile_is_off_unless_asked_for(tmp_path: pathlib.Path) -> None:
    """Default off. A warm deployment that never passes the flag is untouched,
    and so is any caller that has not thought about it."""
    state = _stale_state(tmp_path)
    recorded: list[list[str]] = []

    rc = cluster_promoter.run(
        StoreClient(holds=False),
        namespace="prod",
        node_id=NODE_B,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=lambda cmd: recorded.append(cmd) or 0,
        hold_path=tmp_path / ".cluster_sync_hold",
        probe=lambda _u, _t: True,
        probe_grace=0,
        inspect_container=_inspect(True),
    )
    assert rc == 0
    assert recorded == []


def test_a_held_node_is_never_reconciled(tmp_path: pathlib.Path) -> None:
    """🚨 The hold is the operator's escape hatch, and it must still work.

    Someone who wants Home Assistant up on the standby deliberately sets the
    hold. The held branch returns long before the reconcile is reached, so
    this is the documented way to inspect a standby without it being stopped
    underneath you.
    """
    state = _stale_state(tmp_path)
    hold = tmp_path / ".cluster_sync_hold"
    hold.write_text("looking at something on the standby\n")
    recorded: list[list[str]] = []

    rc = cluster_promoter.run(
        StoreClient(holds=False),
        namespace="prod",
        node_id=NODE_B,
        ttl=30,
        state_path=state,
        install_dir=tmp_path,
        force_path=tmp_path / "force-master",
        runner=lambda cmd: recorded.append(cmd) or 0,
        hold_path=hold,
        probe=lambda _u, _t: True,
        probe_grace=0,
        inspect_container=_inspect(True),
        reconcile_backup=True,
    )
    assert rc == 0
    assert recorded == [], "a held node must run no notify script, reconcile included"


def test_only_a_cold_bundle_asks_the_promoter_to_reconcile() -> None:
    """🚨 The flag must follow the deployment's topology, not a default.

    Emitting it for a warm standby would stop that standby's Home Assistant on
    every tick. This asserts the generated wrapper, which is what actually
    runs as root, rather than the intent.

    The config is written out here rather than borrowed from the fingerprint
    tooling: that module is private and does not ship, so importing it would
    leave this test unable to run in the published repository — where the
    regression it guards matters just as much.
    """
    from custom_components.cluster_state_sync import bundle as bundle_mod

    base = {
        "ha_container": "homeassistant",
        "ha_config_path": "/config",
        "redis_host": "valkey.lan",
        "redis_port": 6379,
        "redis_db": 2,
        "cluster_namespace": "prod",
        "cluster_secret": "s" * 44,
        "node_id": NODE_A,
        "peer_host": "peer.lan",
        "leadership_source": "lease",
        # The promoter is only emitted alongside the fileset (bundle.py gates
        # them together), so without this there is no wrapper to assert on.
        "fileset_enabled": True,
    }
    cold = bundle_mod.build_bundle({**base, "topology_model": "cold"})["cluster-promoter.sh"]
    warm = bundle_mod.build_bundle({**base, "topology_model": "warm"})["cluster-promoter.sh"]
    assert "--reconcile-backup" in cold
    assert "--reconcile-backup" not in warm


# --------------------------------------------------------------------------
# The cold standby had no voice: members, skew (GOTCHAS 34)
# --------------------------------------------------------------------------


def _view(members=None, promoters=None):
    from custom_components.cluster_state_sync.coordinator import ClusterView

    return ClusterView(members=members or {}, promoters=promoters or {})


def test_a_cold_standby_is_counted_even_though_its_home_assistant_is_off() -> None:
    """🚨 `cluster_members` read 1 on a healthy pair AND on a broken one.

    The registry is written by the integration, and on a cold standby Home
    Assistant is deliberately stopped — so the standby never appears. ADR-009
    recorded this exact defect and named the promoter as "the only possible
    reporter". The promoter has been beating on both nodes since 2026-09-08;
    nothing read it.
    """
    view = _view(
        members={NODE_A: {"offset_s": 0.10}},          # the leader's integration
        promoters={NODE_A: {}, NODE_B: {"offset_s": 0.42}},  # both promoters
    )
    assert view.node_count == 2, "the cold standby is still invisible"


def test_clock_skew_is_knowable_on_a_cold_cluster() -> None:
    """Skew needs two offsets, and only one node runs Home Assistant.

    Before this the second offset could never exist, so skew was permanently
    `unknown` on every cold deployment — reported as if it were a transient.
    """
    view = _view(
        members={NODE_A: {"offset_s": 0.10}},
        promoters={NODE_A: {"offset_s": 0.10}, NODE_B: {"offset_s": 0.42}},
    )
    assert view.clock_skew is not None
    assert abs(view.clock_skew - 0.32) < 1e-6


def test_a_single_node_still_reports_no_skew() -> None:
    """Unchanged, and deliberately: skew is a property of a pair. One node
    reporting 0 would claim an agreement it has not established."""
    assert _view(promoters={NODE_A: {"offset_s": 0.1}}).clock_skew is None


def test_a_node_reporting_by_both_routes_is_counted_once() -> None:
    """Otherwise a healthy leader inflates the cluster to two nodes by itself."""
    view = _view(members={NODE_A: {"offset_s": 0.1}}, promoters={NODE_A: {"offset_s": 0.1}})
    assert view.node_count == 1
    assert view.clock_skew is None


def test_a_promoter_on_the_old_payload_still_counts_as_present() -> None:
    """🚨 Rolling upgrades: one node writes JSON, the other a bare timestamp.

    A reader that understood only the new shape would report the un-upgraded
    node as ABSENT — turning an upgrade into an apparent node loss, during the
    window where an operator is least able to tell the difference.
    """
    view = _view(promoters={NODE_A: {"offset_s": 0.1}, NODE_B: {}})  # B: old format
    assert view.node_count == 2
    # ...but it contributes no offset, so skew stays honest rather than
    # inventing agreement from a node that never reported one.
    assert view.clock_skew is None


# --------------------------------------------------------------------------
# The promoter heartbeat, at the client seam
# --------------------------------------------------------------------------


class _PromoterStore:
    """Async redis-ish client carrying promoter heartbeat keys."""

    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = entries
        self.get_raises: set[str] = set()

    async def scan_iter(self, match=None, count=None):
        for key in list(self.entries):
            yield key

    async def get(self, key):
        if key in self.get_raises:
            raise ConnectionError("unreadable")
        return self.entries.get(key)


def _backend_with(client):
    from custom_components.cluster_state_sync.backend import RedisBackend

    be = RedisBackend(namespace="prod", host="valkey.invalid", port=6379)
    be._client = client  # noqa: SLF001 — the client seam
    return be


async def test_read_promoters_parses_the_new_payload() -> None:
    import json as _json

    key = f"ha:cluster_state_sync:prod:promoters:{NODE_B}"
    be = _backend_with(
        _PromoterStore({key: _json.dumps({"node_id": NODE_B, "offset_s": 0.42})})
    )
    got = await be.read_promoters()
    assert set(got) == {NODE_B}
    assert got[NODE_B]["offset_s"] == 0.42


async def test_read_promoters_tolerates_the_OLD_bare_timestamp() -> None:
    """🚨 Rolling upgrade: one node writes JSON, the other a bare integer.

    A reader that understood only the new shape would report the un-upgraded
    node as ABSENT — an upgrade presenting as a node loss.
    """
    key = f"ha:cluster_state_sync:prod:promoters:{NODE_A}"
    be = _backend_with(_PromoterStore({key: "1758547200"}))
    got = await be.read_promoters()
    assert set(got) == {NODE_A}, "the un-upgraded node vanished"
    assert "offset_s" not in got[NODE_A], "a timestamp must not be read as an offset"


async def test_read_promoters_counts_a_node_whose_value_cannot_be_read() -> None:
    """Liveness and clock agreement are different questions.

    The key's EXISTENCE is the liveness signal; its contents are a bonus. A
    node whose payload cannot be fetched is still beating.
    """
    key = f"ha:cluster_state_sync:prod:promoters:{NODE_A}"
    store = _PromoterStore({key: "{}"})
    store.get_raises.add(key)
    got = await _backend_with(store).read_promoters()
    assert set(got) == {NODE_A}


async def test_read_promoters_is_empty_when_the_store_cannot_answer() -> None:
    class Exploding(_PromoterStore):
        async def scan_iter(self, match=None, count=None):
            raise ConnectionError("down")
            yield  # pragma: no cover

    assert await _backend_with(Exploding({})).read_promoters() == {}
    assert await _backend_with(None).read_promoters() == {}


# --------------------------------------------------------------------------
# resp.time(), and the promoter beat that uses it
# --------------------------------------------------------------------------


def test_resp_time_parses_the_server_clock() -> None:
    from custom_components.cluster_state_sync.scripts import resp

    client = resp.ValkeyClient.__new__(resp.ValkeyClient)
    client._command = lambda *a: ["1758547200", "123456"]  # noqa: SLF001
    assert client.time() == (1758547200, 123456)


@pytest.mark.parametrize("reply", [["1"], "nope", ["a", "b"], None])
def test_resp_time_refuses_a_reply_it_cannot_trust(reply) -> None:
    """A clock read wrongly is worse than one not read: it would be published
    as an offset and quietly skew the whole cluster's reading."""
    from custom_components.cluster_state_sync.scripts import resp

    client = resp.ValkeyClient.__new__(resp.ValkeyClient)
    client._command = lambda *a: reply  # noqa: SLF001
    with pytest.raises(resp.RespError):
        client.time()


def test_the_beat_carries_an_offset_when_the_clock_can_be_read() -> None:
    import json as _json

    written: list[list[str]] = []

    class Client:
        def time(self):
            return (int(__import__("time").time()), 0)

        def eval(self, script, keys, args):
            written.append(args)
            return 1

    cluster_promoter._beat(Client(), "k", 60_000, NODE_A)
    assert written, "no heartbeat was written"
    payload = _json.loads(written[0][0])
    assert payload["node_id"] == NODE_A
    assert isinstance(payload["offset_s"], (int, float))


def test_the_beat_still_happens_when_the_clock_cannot_be_read() -> None:
    """🚨 An unmeasurable clock is not a dead node.

    Liveness is the heartbeat's first job. A store too old to answer `TIME`,
    or any failure measuring it, must still leave evidence the node is alive —
    otherwise adding the offset would have turned a working liveness signal
    into a node that reads as absent.
    """
    import json as _json

    written: list[list[str]] = []

    class Client:
        def time(self):
            raise ConnectionError("no TIME")

        def eval(self, script, keys, args):
            written.append(args)
            return 1

    cluster_promoter._beat(Client(), "k", 60_000, NODE_A)
    assert written, "the heartbeat was lost because the clock could not be read"
    payload = _json.loads(written[0][0])
    assert payload["node_id"] == NODE_A
    assert "offset_s" not in payload, "an unmeasured clock must not report an offset"


# --------------------------------------------------------------------------
# The half-upgraded cluster: HACS updates the integration, not the bundle
# --------------------------------------------------------------------------


async def test_a_legacy_promoter_is_identified_from_its_payload() -> None:
    """The only evidence available, and it decides whether the hold binds."""
    old = f"ha:cluster_state_sync:prod:promoters:{NODE_B}"
    new = f"ha:cluster_state_sync:prod:promoters:{NODE_A}"
    import json as _json

    be = _backend_with(
        _PromoterStore({old: "1758547200", new: _json.dumps({"offset_s": 0.0})})
    )
    got = await be.read_promoters()
    assert got[NODE_B].get("legacy_bundle") is True
    assert "legacy_bundle" not in got[NODE_A]


async def test_the_switch_refuses_to_claim_every_node_when_a_bundle_is_stale(
    hass, tmp_path: pathlib.Path, caplog
) -> None:
    """🚨 The accident of GOTCHAS 29, recreated by its own fix.

    HACS upgrades the INTEGRATION and not the host bundle. A 0.6.0 switch
    against 0.5.x promoters publishes the cluster hold, and not one promoter
    reads it — so announcing "failover is suspended on EVERY node" tells the
    operator the cluster is held when it is not, which is exactly what made
    2026-09-18 happen.
    """
    import logging

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.cluster_state_sync.switch import MaintenanceHoldSwitch

    backend = FakeBackend()
    await backend.connect()
    backend.promoter_nodes = {NODE_A, NODE_B}
    backend.promoter_payloads = {NODE_A: {"offset_s": 0.0}}  # B: legacy, no payload

    entry = MockConfigEntry(domain="cluster_state_sync", data={})
    entry.add_to_hass(hass)
    sw = MaintenanceHoldSwitch(entry, str(tmp_path), backend, NODE_A)
    sw.hass = hass
    # Built outside a platform, so it has no entity_id and the state write at
    # the end of async_turn_on would raise. The assertions here are about what
    # the operator is TOLD, not about the state machine.
    sw.entity_id = "switch.maintenance_hold_test"

    with caplog.at_level(logging.ERROR):
        await sw.async_turn_on()

    assert "WILL promote through it" in caplog.text, "the operator was not warned"
    assert NODE_B in caplog.text, "the stale node was not named"
    assert "suspended on EVERY node" not in caplog.text, (
        "claimed a cluster-wide hold that no promoter reads"
    )


async def test_the_switch_does_claim_every_node_when_all_bundles_are_current(
    hass, tmp_path: pathlib.Path, caplog
) -> None:
    """The happy path must still say the strong thing, or the warning above
    becomes noise nobody reads."""
    import logging

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.cluster_state_sync.switch import MaintenanceHoldSwitch

    backend = FakeBackend()
    await backend.connect()
    backend.promoter_nodes = {NODE_A, NODE_B}
    backend.promoter_payloads = {NODE_A: {"offset_s": 0.0}, NODE_B: {"offset_s": 0.0}}

    entry = MockConfigEntry(domain="cluster_state_sync", data={})
    entry.add_to_hass(hass)
    sw = MaintenanceHoldSwitch(entry, str(tmp_path), backend, NODE_A)
    sw.hass = hass
    # Built outside a platform, so it has no entity_id and the state write at
    # the end of async_turn_on would raise. The assertions here are about what
    # the operator is TOLD, not about the state machine.
    sw.entity_id = "switch.maintenance_hold_test"

    with caplog.at_level(logging.WARNING):
        await sw.async_turn_on()

    assert "suspended on EVERY node" in caplog.text
    assert "WILL promote through it" not in caplog.text


async def test_a_bundle_stamped_with_a_different_version_is_stale(
    hass, tmp_path: pathlib.Path, caplog
) -> None:
    """Version stamping beats inferring staleness from payload shape.

    Shape told us only "old". The stamp says WHICH — so a bundle from 0.6.1
    against an 0.6.2 integration is caught too, not just pre-0.6.0 ones.
    """
    import logging

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.cluster_state_sync.const import INTEGRATION_VERSION
    from custom_components.cluster_state_sync.switch import MaintenanceHoldSwitch

    backend = FakeBackend()
    await backend.connect()
    backend.promoter_nodes = {NODE_A, NODE_B}
    backend.promoter_payloads = {
        NODE_A: {"bundle_version": INTEGRATION_VERSION},
        NODE_B: {"bundle_version": "0.5.2"},
    }

    entry = MockConfigEntry(domain="cluster_state_sync", data={})
    entry.add_to_hass(hass)
    sw = MaintenanceHoldSwitch(entry, str(tmp_path), backend, NODE_A)
    sw.hass = hass
    sw.entity_id = "switch.maintenance_hold_test"

    with caplog.at_level(logging.ERROR):
        await sw.async_turn_on()
    assert NODE_B in caplog.text
    assert NODE_A not in caplog.text.split("still running")[-1].split(".")[0]


async def test_a_matching_stamp_is_never_called_stale(
    hass, tmp_path: pathlib.Path, caplog
) -> None:
    """🚨 An unknown version must never accuse a correct install.

    If either side's version cannot be established, the check stays quiet. A
    mismatch warning that fires on a healthy cluster is how the real one gets
    ignored.
    """
    import logging

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.cluster_state_sync.const import INTEGRATION_VERSION
    from custom_components.cluster_state_sync.switch import MaintenanceHoldSwitch

    backend = FakeBackend()
    await backend.connect()
    backend.promoter_nodes = {NODE_A, NODE_B}
    backend.promoter_payloads = {
        NODE_A: {"bundle_version": INTEGRATION_VERSION},
        NODE_B: {"bundle_version": ""},  # a bundle that did not stamp
    }

    entry = MockConfigEntry(domain="cluster_state_sync", data={})
    entry.add_to_hass(hass)
    sw = MaintenanceHoldSwitch(entry, str(tmp_path), backend, NODE_A)
    sw.hass = hass
    sw.entity_id = "switch.maintenance_hold_test"

    with caplog.at_level(logging.WARNING):
        await sw.async_turn_on()
    assert "suspended on EVERY node" in caplog.text
    assert "WILL promote through it" not in caplog.text
