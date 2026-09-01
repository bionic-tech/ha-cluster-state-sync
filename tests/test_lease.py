"""The lease script has exactly one definition (design §3).

Two take-or-renew implementations racing on one key is how a cluster ends up
with two leaders. The host-side promoter and the integration must run the same
Lua, so it lives in one stdlib-only module both can import.
"""

from __future__ import annotations

from custom_components.cluster_state_sync import backend, lease


def test_the_backend_uses_the_shared_script_rather_than_its_own() -> None:
    """Production change that would make this fail: re-inlining the Lua in
    backend.py. A copy drifts, and the drift is invisible until two nodes
    disagree about who holds the lease."""
    assert backend._LEASE_SCRIPT is lease.LEASE_SCRIPT


def test_the_script_is_take_or_renew_keyed_on_identity() -> None:
    """The three behaviours the whole design rests on: take when free, renew
    when mine, refuse when someone else's."""
    script = lease.LEASE_SCRIPT
    assert "redis.call('GET', KEYS[1])" in script
    assert "current == ARGV[1]" in script, "renewal must compare identity"
    assert "return 0" in script, "must refuse when another node holds it"


def test_the_backend_releases_using_the_shared_script_rather_than_its_own() -> None:
    """Production change that would make this fail: re-inlining the release
    Lua in backend.py instead of importing D3's shared definition. A second
    hand-copied identity check is exactly how the take and the release could
    end up disagreeing about what "mine" means."""
    import inspect

    source = inspect.getsource(backend.RedisBackend.release_leadership)
    assert "_RELEASE_SCRIPT" in source
    assert "DEL" not in source, "the DEL belongs in lease.py, not inlined here again"


def test_the_release_script_is_identity_checked() -> None:
    """A release must never evict a lease the peer has since legitimately
    taken -- the compare-and-delete is what makes that safe."""
    script = lease.RELEASE_SCRIPT
    assert "redis.call('GET', KEYS[1])" in script
    assert "ARGV[1]" in script, "must compare identity before deleting"
    assert "DEL" in script
    assert "return 0" in script, "must refuse to delete someone else's claim"


def test_the_module_imports_nothing_outside_the_standard_library() -> None:
    """It is shipped to the host beside fileset_pull.py, where Home Assistant
    does not exist."""
    import ast
    import pathlib

    src = (
        pathlib.Path(__file__).parent.parent / "custom_components/cluster_state_sync/lease.py"
    ).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            assert node.module in {"typing", "__future__"}, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in {"typing"}, alias.name


def test_the_ttl_is_expressed_in_milliseconds() -> None:
    """Redis PX takes milliseconds; passing seconds would give a 30ms lease and
    every node would win every race."""
    assert lease.lease_ttl_ms(30) == "30000"
