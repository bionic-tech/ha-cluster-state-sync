"""Fileset storage against a **real** Valkey.

Follows `test_backend_valkey.py`: unique namespace per test, and a loud skip
when no server is reachable rather than a quiet pass.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import uuid

import pytest

from custom_components.cluster_state_sync.backend import RedisBackend


@pytest.fixture
async def fs_backend(
    valkey_server: tuple[str, int], socket_enabled: None
) -> AsyncGenerator[RedisBackend]:
    host, port = valkey_server
    backend = RedisBackend(host=host, port=port, db=0, namespace=f"fs{uuid.uuid4().hex[:10]}")
    await backend.connect()
    yield backend
    await backend.close()


async def test_manifest_and_blobs_round_trip(fs_backend: RedisBackend) -> None:
    blobs = {"ref-a": b"\x00binary\xff", "ref-b": b"more bytes"}
    await fs_backend.write_fileset(b"manifest-bytes", blobs)
    assert await fs_backend.read_fileset_manifest() == b"manifest-bytes"
    assert await fs_backend.read_blobs(["ref-a", "ref-b"]) == blobs


async def test_reading_an_absent_manifest_returns_none(fs_backend: RedisBackend) -> None:
    assert await fs_backend.read_fileset_manifest() is None


async def test_missing_refs_are_omitted_rather_than_raising(
    fs_backend: RedisBackend,
) -> None:
    """A follower asking for a blob the GC removed must get a short answer it
    can act on, not an exception mid-pull."""
    await fs_backend.write_fileset(b"m", {"present": b"x"})
    assert await fs_backend.read_blobs(["present", "gone"]) == {"present": b"x"}


async def test_prune_removes_only_unreferenced_blobs(fs_backend: RedisBackend) -> None:
    """Production change that would make this fail: pruning by age instead of
    by reference. GC is load-bearing here — content-addressing means every
    change creates a blob and nothing ever overwrites."""
    await fs_backend.write_fileset(b"m", {"keep": b"1", "drop": b"2", "also": b"3"})
    removed = await fs_backend.prune_blobs({"keep", "also"})
    assert removed == 1
    assert await fs_backend.read_blobs(["keep", "drop", "also"]) == {
        "keep": b"1",
        "also": b"3",
    }


# `test_blobs_land_before_the_manifest_moves` used to sit here. It ran both
# writes to completion and then asserted both were readable, which is true of
# any write order at all -- it could not fail for the reason it named. Deleted
# rather than repaired, because `tests/test_backend.py::
# test_blob_pipeline_executes_before_the_manifest_set` already asserts the
# ordering on the actual sequence of Redis calls and *does* discriminate. This
# was redundancy, not coverage.
