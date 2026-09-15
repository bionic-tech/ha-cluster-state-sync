"""The TLS context is built in a worker thread, not on the event loop.

Home Assistant reported this integration three times on every connect:

    Detected blocking call to load_default_certs ... inside the event loop by
    custom integration 'cluster_state_sync' at backend.py:506

`redis` builds its `SSLContext` lazily inside `RedisSSLContext.get()`, on
whichever thread first opens a connection, and that reads the system trust
store from disk. `redis` 6.4's `SSLConnection` takes no pre-built context, so
the only supported seam is `ConnectionPool.connection_class`.

These tests exist because the fix reaches into another library's internals on
the cluster's critical connect path. If `redis` reorganises, the correct
outcome is the warning coming back — never a cluster that cannot reach its
backend — and that is asserted here rather than hoped for.
"""

from __future__ import annotations

import asyncio
import ssl
from unittest.mock import MagicMock

import redis.asyncio as aioredis
from redis.asyncio.connection import SSLConnection

from custom_components.cluster_state_sync.backend import RedisBackend


def _backend(**kw) -> RedisBackend:
    return RedisBackend(
        namespace="testns",
        host="valkey.invalid",
        port=6380,
        username="u",
        password="p",
        secret="s" * 32,
        **kw,
    )


def _tls_client() -> aioredis.Redis:
    """A client object only — constructing one opens no socket."""
    return aioredis.Redis(host="valkey.invalid", port=6380, ssl=True, ssl_cert_reqs="required")


async def test_the_context_is_built_and_shared_by_every_connection() -> None:
    """🚨 The whole point: warming one connection would fix only the first.

    Each `SSLConnection` builds its own `RedisSSLContext`, so without swapping
    the pool's class every later connection blocks the loop again.
    """
    backend = _backend(use_tls=True)
    backend._client = _tls_client()
    pool = backend._client.connection_pool

    assert pool.connection_class is SSLConnection

    await backend._warm_tls_context()

    assert pool.connection_class is not SSLConnection, "the pool's class was not swapped"
    assert issubclass(pool.connection_class, SSLConnection), "the swap broke the class chain"

    first = pool.make_connection().ssl_context.get()
    second = pool.make_connection().ssl_context.get()
    assert isinstance(first, ssl.SSLContext)
    assert first is second, "each connection built its own context again"


async def test_the_build_happens_off_the_event_loop() -> None:
    """The blocking call must run in an executor, which is the entire fix."""
    backend = _backend(use_tls=True)
    backend._client = _tls_client()

    loop = asyncio.get_running_loop()
    threads: list[str] = []
    real = loop.run_in_executor

    def _record(executor, func, *args):
        threads.append(getattr(func, "__qualname__", str(func)))
        return real(executor, func, *args)

    loop.run_in_executor = _record  # type: ignore[method-assign]
    try:
        await backend._warm_tls_context()
    finally:
        loop.run_in_executor = real  # type: ignore[method-assign]

    assert threads, "the context was built on the event loop"


async def test_plaintext_backends_are_left_alone() -> None:
    """No TLS, nothing to warm, and no reason to touch the pool."""
    backend = _backend(use_tls=False)
    backend._client = aioredis.Redis(host="valkey.invalid", port=6379)
    before = backend._client.connection_pool.connection_class

    await backend._warm_tls_context()

    assert backend._client.connection_pool.connection_class is before


async def test_a_pool_we_do_not_understand_is_left_alone() -> None:
    """🚨 The failure mode that matters.

    A `redis` reorganisation must cost a log line, not the connection. Anything
    unexpected here has to leave the client exactly as it was and return
    quietly — `connect()` then proceeds and the house still reaches Valkey.
    """
    backend = _backend(use_tls=True)
    backend._client = MagicMock()  # a pool whose connection_class is not a class

    await backend._warm_tls_context()  # must not raise


async def test_a_raising_pool_does_not_break_connect() -> None:
    """Same contract, but for an outright exception rather than a odd shape."""
    backend = _backend(use_tls=True)
    client = MagicMock()
    type(client).connection_pool = property(
        lambda _self: (_ for _ in ()).throw(RuntimeError("redis changed"))
    )
    backend._client = client

    await backend._warm_tls_context()  # must swallow it
