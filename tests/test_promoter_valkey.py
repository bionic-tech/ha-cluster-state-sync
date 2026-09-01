"""The shared lease script, through the *host-side* client, on a real Valkey.

test_backend_valkey.py exercises the same Lua through the redis library.
test_fileset_pull.py exercises the same client against a fake socket. The
promoter is the combination, and until this module nothing executed it.

`socket_enabled` is function-scoped and is how this suite sanctions a real
socket for one test. Never call `enable_socket()`, which holes the guard for
the whole session.
"""

from __future__ import annotations

import uuid

import pytest

from custom_components.cluster_state_sync.lease import (
    FORCE_SCRIPT,
    LEASE_SCRIPT,
    RELEASE_SCRIPT,
    lease_ttl_ms,
)
from custom_components.cluster_state_sync.scripts.resp import ValkeyClient


@pytest.fixture
def promoter_client(valkey_server: tuple[str, int], socket_enabled: None) -> ValkeyClient:
    host, port = valkey_server
    client = ValkeyClient.connect(host=host, port=port, db=0)
    try:
        yield client
    finally:
        client.close()


def test_the_host_side_client_can_take_renew_and_be_refused_the_lease(
    promoter_client: ValkeyClient,
) -> None:
    """Take, renew, refuse — the three branches the whole design rests on,
    executed by a real Valkey rather than asserted about as text."""
    key = f"t{uuid.uuid4().hex[:12]}:leader"
    ttl = lease_ttl_ms(30)

    assert promoter_client.eval(LEASE_SCRIPT, [key], ["node-a", ttl]) == 1
    assert promoter_client.eval(LEASE_SCRIPT, [key], ["node-a", ttl]) == 1
    assert promoter_client.eval(LEASE_SCRIPT, [key], ["node-b", ttl]) == 0
    assert promoter_client.get(key) == "node-a"


def test_the_ttl_reaches_valkey_as_milliseconds(
    promoter_client: ValkeyClient,
) -> None:
    """Passing seconds would set a 30ms lease: every node would find it free on
    every pass and every node would believe it leads. A wire test cannot catch
    this — only the server knows what it stored."""
    key = f"t{uuid.uuid4().hex[:12]}:leader"
    promoter_client.eval(LEASE_SCRIPT, [key], ["node-a", lease_ttl_ms(30)])
    pttl = int(promoter_client._command("PTTL", key))
    assert 25_000 < pttl <= 30_000, f"PTTL={pttl}ms"


def test_the_force_script_steals_the_lease_rather_than_refusing(
    promoter_client: ValkeyClient,
) -> None:
    """The property the force-master fix rests on. LEASE_SCRIPT would refuse
    node-b here -- that is its entire job. FORCE_SCRIPT must not: the
    override exists precisely to let a node take leadership another node
    still legitimately holds, once an operator has confirmed that holder is
    dead. Only a real Valkey can confirm the Lua overwrites rather than
    checking identity first."""
    key = f"t{uuid.uuid4().hex[:12]}:leader"
    ttl = lease_ttl_ms(30)

    assert promoter_client.eval(LEASE_SCRIPT, [key], ["node-a", ttl]) == 1
    assert promoter_client.eval(FORCE_SCRIPT, [key], ["node-b", ttl]) == 1
    assert promoter_client.get(key) == "node-b"


def test_the_force_scripts_ttl_also_reaches_valkey_as_milliseconds(
    promoter_client: ValkeyClient,
) -> None:
    """FORCE_SCRIPT is a second `SET ... PX` -- deleting `'PX', ARGV[2]` from
    it would still pass `test_the_force_script_steals_the_lease_rather_than_refusing`
    (that test only checks who holds the key), while leaving the forced lease
    permanent: the peer could never legitimately take over again, even after
    a clean repair. Only the server knows what it actually stored."""
    key = f"t{uuid.uuid4().hex[:12]}:leader"
    promoter_client.eval(FORCE_SCRIPT, [key], ["node-a", lease_ttl_ms(30)])
    pttl = int(promoter_client._command("PTTL", key))
    assert 25_000 < pttl <= 30_000, f"PTTL={pttl}ms"


def test_the_release_script_deletes_only_our_own_claim(
    promoter_client: ValkeyClient,
) -> None:
    """Design D3's release, executed by a real Valkey rather than asserted
    about as text. A release keyed on the wrong identity, or one that
    deletes unconditionally, is exactly what would let a leader's release
    evict a peer that has since legitimately promoted."""
    key = f"t{uuid.uuid4().hex[:12]}:leader"
    ttl = lease_ttl_ms(30)

    assert promoter_client.eval(LEASE_SCRIPT, [key], ["node-a", ttl]) == 1
    # A release under someone else's identity must refuse, not delete.
    promoter_client.eval(RELEASE_SCRIPT, [key], ["node-b"])
    assert promoter_client.get(key) == "node-a", "must not delete another node's claim"

    # The rightful holder's own release actually clears the key, so the
    # standby can take it on its very next tick rather than waiting out the
    # TTL.
    promoter_client.eval(RELEASE_SCRIPT, [key], ["node-a"])
    assert promoter_client.get(key) is None
    assert promoter_client.eval(LEASE_SCRIPT, [key], ["node-b", ttl]) == 1
