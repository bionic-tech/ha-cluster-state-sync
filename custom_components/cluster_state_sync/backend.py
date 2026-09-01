"""Storage backends for Cluster State Sync.

The backend abstraction exists so the recorder/Postgres backend can be added
later without touching the core integration. v0.1 ships Redis only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import base64
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
import hashlib
import hmac
import json
import logging
import socket
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.sentinel import Sentinel

from .const import (
    LEASE_TTL_SECONDS,
    SCHEMA_VERSION,
    fileset_blob_key,
    fileset_manifest_key,
    leader_key,
    meta_key,
    states_key,
)
from .lease import LEASE_SCRIPT as _LEASE_SCRIPT
from .lease import RELEASE_SCRIPT as _RELEASE_SCRIPT
from .lease import lease_ttl_ms

# Take-or-renew, evaluated atomically inside Valkey (AR-0017).
#
# A read-then-write in Python would reintroduce exactly the race this exists to
# close: both nodes could read "unheld" in the same instant and both conclude
# they are leader. Redis runs a script atomically, so acquisition and renewal
# are one indivisible decision.
#
# Renewing only when we already hold it is what makes this a lease rather than
# a lock grab: a follower cannot steal leadership from a live leader, and a
# dead leader's lease simply expires.
# Read both keys in one atomic script (AR-0018).
#
# The writer sets states and meta inside a MULTI. A pipelined reader has no
# such guarantee: it can land between the peer's HSET and its SET and come away
# with states from one flush and meta from another. The meta's
# `last_snapshot_at` then understates the age of the states actually held --
# and that is the timestamp an operator reads to decide whether a promotion is
# safe.
_READ_SCRIPT = """
return { redis.call('HGETALL', KEYS[1]), redis.call('GET', KEYS[2]) }
"""

_LOGGER = logging.getLogger(__name__)

# Every key `to_json` emits, excluding the signature itself. Authenticated
# input is parsed against this closed set: an entry carrying anything else was
# not written by any version of this integration (AR-0036).
_SIGNED_FIELDS = frozenset({"v", "s", "a", "lc", "lu", "n"})


@dataclass
class SnapshotEntry:
    """A single entity's state, as stored and restored."""

    entity_id: str
    state: str
    attributes: dict[str, Any]
    last_changed: str  # ISO-8601
    last_updated: str  # ISO-8601
    source_node: str

    def to_json(self, secret: str | None = None) -> str:
        """Serialise this entry, signing it when a cluster secret is configured."""
        payload: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "s": self.state,
            "a": self.attributes,
            "lc": self.last_changed,
            "lu": self.last_updated,
            "n": self.source_node,
        }
        if secret:
            payload["h"] = _sign(self.entity_id, payload, secret)
        return json.dumps(payload, default=str)

    @classmethod
    def from_json(cls, entity_id: str, raw: str, secret: str | None = None) -> SnapshotEntry:
        """Parse one stored entry, verifying its signature when required.

        Raises on malformed, unreadable or unauthentic input; callers are
        expected to skip the offending entry rather than abandon the whole
        restore (AR-0013).

        AR-0005: without this check, everything downstream is decoration. The
        restore only applies entries whose `source_node` is the peer — but
        `source_node` is just a field in the record, so anything able to write
        to the shared hash could claim to be the peer and have arbitrary state
        applied verbatim by `async_set`. The signature is what makes that field
        mean anything.
        """
        data = json.loads(raw)

        # Authenticate before interpreting. Everything below this point treats
        # the payload as structured data; none of it should run against bytes
        # we have not established came from a cluster node.
        if secret:
            presented = data.get("h")
            if not presented or not isinstance(presented, str):
                raise ValueError(
                    "entry is unsigned but this cluster requires signatures; refusing it"
                )
            signed_fields = {k: v for k, v in data.items() if k != "h"}
            # AR-0036: close the schema *before* deriving a signature over it.
            #
            # Verifying whatever keys happen to be present sounds safe — any
            # extra field changes the canonical form and breaks the HMAC — but
            # it was not, because one particular extra field changed the
            # canonical form back. `_sign` bound the entity_id by merging it
            # into the payload, so a payload carrying its own `e` won the merge
            # and named whichever entity the attacker liked. Re-adding the
            # original entity_id to a validly-signed entry therefore let it be
            # replayed onto any other field of the hash with a signature that
            # verified perfectly — exactly the attack the binding exists to
            # stop, and reachable without the secret.
            #
            # `_sign` no longer lets `e` be shadowed, so this check is the
            # second of two locks. It is the more valuable one: it refuses the
            # whole class rather than the one instance of it, so the next
            # field with a special meaning cannot be smuggled in either.
            unexpected = set(signed_fields) - _SIGNED_FIELDS
            if unexpected:
                raise ValueError(
                    f"entry carries unexpected field(s) {sorted(unexpected)}; refusing it"
                )
            expected = _sign(entity_id, signed_fields, secret)
            # compare_digest, not ==, so a wrong signature cannot be recovered
            # byte by byte from response timing.
            if not hmac.compare_digest(presented, expected):
                raise ValueError("entry failed signature verification; refusing it")

        version = data.get("v", 1)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError(f"entry has a non-integer schema version {version!r}")
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"entry written by a newer node (schema v{version} > "
                f"v{SCHEMA_VERSION}); refusing to guess at its meaning"
            )

        return cls(
            entity_id=entity_id,
            state=data["s"],
            attributes=data.get("a", {}),
            last_changed=data["lc"],
            last_updated=data["lu"],
            source_node=data.get("n", "unknown"),
        )


def _sign(entity_id: str, payload: dict[str, Any], secret: str) -> str:
    """HMAC-SHA256 over the entry's canonical form.

    `entity_id` is bound into the signed material even though it is the hash
    *field* name rather than part of the value. Without it, a validly-signed
    `input_boolean.holiday_mode = "on"` could simply be copied onto the
    `alarm_control_panel.house` field: a perfectly valid signature on entirely
    the wrong entity.

    AR-0036: the binding is only worth anything if the payload cannot undo it.
    This was written as `{"e": entity_id, **payload}`, where a payload with its
    own `e` key wins the merge and the argument is silently discarded — the
    binding removed itself on request. `e` is now assigned last, so it always
    names the field the entry actually arrived on.

    `sort_keys` keeps the canonical form stable regardless of dict ordering, so
    a signature made by one node verifies on the other. Legitimate entries
    never carry an `e`, so signatures are unchanged by this and a mixed-version
    pair still verifies each other's writes.
    """
    canonical_form = dict(payload)
    canonical_form["e"] = entity_id
    canonical = json.dumps(
        canonical_form,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


class ClusterBackend(ABC):
    """Abstract storage backend.

    Implementations must be safe to call from the asyncio event loop and
    must never raise into the caller — failures should be logged and swallowed
    so that backend outages don't break HA itself.

    Deliberate exception: `write_fileset` raises rather than swallowing.
    Every other method's degrade-default (`{}`, `0`, `False`, `None`) means
    genuine absence — there is no honest empty value that could mean "this
    write did not happen" without being indistinguishable from "it happened
    and stored nothing". A caller that treated a swallowed write failure as
    success would report a fileset generation that was never actually
    stored — AR-0040's failure shape, a restore that silently did nothing
    for its entire life while the suite stayed green. Do not "fix" this back
    to return-quietly; see `write_fileset` on `RedisBackend` for the full
    reasoning.
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def write_snapshot(self, entries: dict[str, SnapshotEntry], source_node: str) -> bool:
        """Flush a batch of state entries. Returns True on success."""

    @abstractmethod
    async def read_snapshot(self) -> tuple[dict[str, SnapshotEntry], dict[str, Any]]:
        """Load the full snapshot and metadata. Returns ({}, {}) on miss/error."""

    @abstractmethod
    async def health(self) -> bool:
        """Lightweight liveness check, for the cluster status endpoint."""

    @abstractmethod
    async def acquire_leadership(self, node_id: str) -> bool:
        """Take or renew the cluster lease. True if this node holds it."""

    @abstractmethod
    async def write_fileset(self, manifest: bytes, blobs: Mapping[str, bytes]) -> None:
        """Store `blobs`, then swap the manifest.

        The order is the contract: a reader must see either the previous
        manifest, whose blobs all still exist, or the new one with every blob
        it references already present.
        """

    @abstractmethod
    async def read_fileset_manifest(self) -> bytes | None:
        """Return the current sealed manifest, or None if none has been written."""

    @abstractmethod
    async def read_blobs(self, refs: Sequence[str]) -> dict[str, bytes]:
        """Return whichever of `refs` are present. Absent refs are omitted."""

    @abstractmethod
    async def prune_blobs(self, keep: Collection[str]) -> int:
        """Delete every stored blob whose ref is not in `keep`. Returns the count."""


class RedisBackend(ClusterBackend):
    """Redis / Valkey backend.

    Supports two connection modes:

    * Direct: a single host:port (e.g. a Valkey VIP that already does HA).
    * Sentinel: a list of sentinel hosts and a master service name. Use this
      when pointing at your existing Valkey Sentinel cluster — the client
      will follow leader elections automatically.
    """

    def __init__(
        self,
        *,
        namespace: str,
        host: str | None = None,
        port: int = 6379,
        username: str | None = None,
        password: str | None = None,
        db: int = 2,
        use_sentinel: bool = False,
        sentinel_hosts: list[tuple[str, int]] | None = None,
        sentinel_service: str | None = None,
        use_tls: bool = False,
        tls_ca_certs: str | None = None,
        secret: str | None = None,
    ) -> None:
        self._namespace = namespace
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._db = db
        self._use_sentinel = use_sentinel
        self._sentinel_hosts = sentinel_hosts or []
        self._sentinel_service = sentinel_service
        self._use_tls = use_tls
        self._tls_ca_certs = tls_ca_certs
        self._secret = secret
        self._client: aioredis.Redis | None = None
        self._sentinel: Sentinel | None = None

    @property
    def _tls_kwargs(self) -> dict[str, Any]:
        """TLS parameters for the redis client (AR-0003).

        `ssl_cert_reqs="required"` is not optional: with TLS enabled but
        verification off, an attacker on the path presents any certificate and
        the connection is encrypted to *them*. There is deliberately no
        "insecure TLS" toggle — if the CA path is omitted the system trust
        store is used, which is still verification.
        """
        return {
            "ssl": self._use_tls,
            "ssl_ca_certs": self._tls_ca_certs if self._use_tls else None,
            "ssl_cert_reqs": "required" if self._use_tls else None,
        }

    async def connect(self) -> None:
        if self._use_sentinel:
            if not self._sentinel_hosts or not self._sentinel_service:
                raise ValueError("Sentinel mode requires sentinel_hosts and sentinel_service")
            # Both legs need TLS: the sentinel connections themselves and the
            # master connection they resolve to. Securing one and not the other
            # still leaves credentials and state on the wire in the clear.
            # ---------------------------------------------------------------
            # UNTESTED PATH. No Sentinel runs anywhere on the fleet, so nothing
            # in the suite exercises these two legs — they are the only part of
            # this backend never run against a real server. Deferred by
            # decision, not oversight: ADD 24 §3. If you are enabling
            # `use_sentinel`, you are the first person to run this code.
            #
            # The last two defects here were made of exactly this shape: the
            # missing `username` below, and an ACL recipe that had never met a
            # real server. Treat a green suite as no evidence for this branch.
            # ---------------------------------------------------------------
            # username as well as password: a named ACL user authenticates
            # with AUTH <user> <pass>, and passing the password alone silently
            # authenticates as `default` instead (ADD 03 §2.3).
            self._sentinel = Sentinel(
                self._sentinel_hosts,
                socket_timeout=2.0,
                username=self._username,
                password=self._password,
                **self._tls_kwargs,
            )
            self._client = self._sentinel.master_for(
                self._sentinel_service,
                socket_timeout=2.0,
                username=self._username,
                password=self._password,
                db=self._db,
                decode_responses=True,
                **self._tls_kwargs,
            )
        else:
            self._client = aioredis.Redis(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                db=self._db,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
                decode_responses=True,
                health_check_interval=30,
                **self._tls_kwargs,
            )
        # Force a connection round-trip so we fail fast on bad config.
        await self._client.ping()
        _LOGGER.info(
            "Redis backend connected (sentinel=%s, namespace=%s)",
            self._use_sentinel,
            self._namespace,
        )

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 — best-effort close
                _LOGGER.debug("Error closing Redis client", exc_info=True)
            self._client = None

    async def write_snapshot(self, entries: dict[str, SnapshotEntry], source_node: str) -> bool:
        if not entries or self._client is None:
            return False
        try:
            pipe = self._client.pipeline(transaction=True)
            mapping = {eid: e.to_json(self._secret) for eid, e in entries.items()}
            # AR-0001/AR-0017: deliberately NO `DEL` before the `HSET`.
            #
            # v0.1 wiped the hash and rewrote it from the last interval's change
            # buffer, so each flush destroyed both the rest of the snapshot and
            # anything the peer node had written. `HSET` alone merges, which
            # makes the write last-writer-wins per field and therefore safe
            # while both nodes still run the flush loop. That is the interim
            # guard until the AR-0017 leader lease lands.
            #
            # The caller passes its full authoritative map, so a node's own view
            # is always written whole rather than as a fragment.
            #
            # Consequence: an entity deleted in HA lingers in the hash until
            # overwritten or the namespace is bumped. Tombstones are v0.2; this
            # is the documented known limitation in the README.
            pipe.hset(states_key(self._namespace), mapping=mapping)
            pipe.set(
                meta_key(self._namespace),
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "last_snapshot_at": _utc_now_iso(),
                        "source_node": source_node,
                        "entry_count": len(entries),
                    }
                ),
            )
            await pipe.execute()
            return True
        except Exception:  # noqa: BLE001 — never crash HA on backend failure
            _LOGGER.warning("Failed to write snapshot to Redis", exc_info=True)
            return False

    async def read_snapshot(self) -> tuple[dict[str, SnapshotEntry], dict[str, Any]]:
        if self._client is None:
            return {}, {}
        try:
            # AR-0018: one atomic script rather than a pipeline. The writer
            # sets both keys inside a MULTI; a pipelined reader has no such
            # guarantee and can land between the peer's HSET and its SET,
            # coming away with states from one flush and meta from another.
            raw_states, raw_meta = await self._client.eval(
                _READ_SCRIPT,
                2,
                states_key(self._namespace),
                meta_key(self._namespace),
            )
            # Lua returns a hash as a flat array, not as pairs.
            if isinstance(raw_states, list):
                raw_states = dict(zip(raw_states[::2], raw_states[1::2], strict=False))

            # AR-0013: parse per entry. As a single comprehension, one
            # malformed value raised out of the whole call, `read_snapshot`
            # returned ({}, {}), and the standby came up completely cold — a
            # total failover failure caused by one bad field.
            entries: dict[str, SnapshotEntry] = {}
            skipped = 0
            for eid, raw in (raw_states or {}).items():
                try:
                    entries[eid] = SnapshotEntry.from_json(eid, raw, self._secret)
                except Exception:  # noqa: BLE001 — one bad entry, not a bad snapshot
                    skipped += 1
                    _LOGGER.debug("Skipping unreadable entry %s", eid, exc_info=True)
            if skipped:
                _LOGGER.warning(
                    "Skipped %d unreadable snapshot entries; restored the remaining %d",
                    skipped,
                    len(entries),
                )
            try:
                meta = json.loads(raw_meta) if raw_meta else {}
            except (ValueError, TypeError):
                _LOGGER.debug("Snapshot meta is unreadable", exc_info=True)
                meta = {}
            return entries, meta
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to read snapshot from Redis", exc_info=True)
            return {}, {}

    async def health(self) -> bool:
        if self._client is None:
            return False
        try:
            await asyncio.wait_for(self._client.ping(), timeout=1.0)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def acquire_leadership(self, node_id: str) -> bool:
        """Take or renew this node's cluster lease (AR-0017).

        Returns False on any failure. A backend that cannot answer is not a
        yes: assuming leadership when it cannot be established is how two
        leaders happen, which is the condition the lease exists to prevent.
        """
        if self._client is None:
            return False
        try:
            result = await self._client.eval(
                _LEASE_SCRIPT,
                1,
                leader_key(self._namespace),
                node_id,
                lease_ttl_ms(LEASE_TTL_SECONDS),
            )
            return bool(int(result))
        except Exception:  # noqa: BLE001 — never crash HA on backend failure
            _LOGGER.warning("Could not evaluate the cluster lease", exc_info=True)
            return False

    async def release_leadership(self, node_id: str) -> None:
        """Give up the lease on a clean shutdown so the peer promotes sooner.

        Best-effort: if this fails the lease just expires on its own TTL, which
        costs the standby up to LEASE_TTL_SECONDS of the failover budget.
        """
        if self._client is None:
            return
        try:
            # Only delete a lease we actually hold — never one the peer has
            # since taken. Shared with the host-side promoter's own release
            # under design D3 — see lease.py's docstring for why this is no
            # longer inlined here.
            await self._client.eval(
                _RELEASE_SCRIPT,
                1,
                leader_key(self._namespace),
                node_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not release the cluster lease", exc_info=True)

    async def write_fileset(self, manifest: bytes, blobs: Mapping[str, bytes]) -> None:
        """Blobs first, manifest last — see the base class.

        Base64 because `connect()` builds the client with
        `decode_responses=True` so every value comes back as `str`; sealed
        blobs are AES-GCM ciphertext, arbitrary bytes, and pushing them
        through a UTF-8-decoding round trip would corrupt them silently.

        Deliberately raises rather than following this module's usual
        log-and-return-quietly convention (compare `write_snapshot`): a
        publisher builds a `PublishResult` from this call's outcome, and a
        silent no-op here would let that result report a generation that was
        never actually stored — the same failure shape as AR-0040, where a
        restore did nothing for its entire life while the suite stayed green.
        Task 5's caller wraps this in try/except and logs, so raising is
        handled, not fatal.
        """
        if self._client is None:
            raise RuntimeError("Cannot write fileset: backend is not connected")
        if blobs:
            pipe = self._client.pipeline(transaction=False)
            for ref, sealed in blobs.items():
                pipe.set(
                    fileset_blob_key(self._namespace, ref),
                    base64.b64encode(sealed).decode("ascii"),
                )
            await pipe.execute()
        await self._client.set(
            fileset_manifest_key(self._namespace),
            base64.b64encode(manifest).decode("ascii"),
        )

    async def read_fileset_manifest(self) -> bytes | None:
        """Return the current sealed manifest.

        Returns None on a miss *or* on a backend failure -- the class
        contract (a read must never raise into the caller) applies here the
        same as it does to `read_snapshot`; only `write_fileset` deviates.
        """
        if self._client is None:
            return None
        try:
            raw = await self._client.get(fileset_manifest_key(self._namespace))
            return base64.b64decode(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 — never crash HA on backend failure
            _LOGGER.warning("Failed to read fileset manifest from Redis", exc_info=True)
            return None

    async def read_blobs(self, refs: Sequence[str]) -> dict[str, bytes]:
        """Absent refs are omitted rather than raising: a follower whose blob
        the GC removed needs a short answer it can act on, not an exception
        halfway through a pull. A transient Redis failure is treated the same
        way -- returns {} rather than propagating, per the class contract."""
        refs = list(refs)
        if not refs or self._client is None:
            return {}
        try:
            values = await self._client.mget(
                [fileset_blob_key(self._namespace, ref) for ref in refs]
            )
            return {
                ref: base64.b64decode(value)
                for ref, value in zip(refs, values, strict=True)
                if value is not None
            }
        except Exception:  # noqa: BLE001 — never crash HA on backend failure
            _LOGGER.warning("Failed to read fileset blobs from Redis", exc_info=True)
            return {}

    async def prune_blobs(self, keep: Collection[str]) -> int:
        """Delete unreferenced blobs. Scans rather than tracking a set, so a
        publisher that crashed mid-write still gets cleaned up. A transient
        Redis failure returns 0 rather than propagating -- a GC pass that
        could not run is not an error the caller need react to."""
        if self._client is None:
            return 0
        try:
            prefix = fileset_blob_key(self._namespace, "")
            doomed = [
                key
                async for key in self._client.scan_iter(match=f"{prefix}*", count=500)
                if _ref_of(key, prefix) not in keep
            ]
            if not doomed:
                return 0
            return int(await self._client.delete(*doomed))
        except Exception:  # noqa: BLE001 — never crash HA on backend failure
            _LOGGER.warning("Failed to prune fileset blobs in Redis", exc_info=True)
            return 0


def default_node_id() -> str:
    """Derive a stable node identifier from the container hostname."""
    return socket.gethostname()


def _ref_of(key: bytes | str, prefix: str) -> str:
    """Recover the blob ref from a full Redis key."""
    text = key.decode() if isinstance(key, bytes) else key
    return text[len(prefix) :]


def _utc_now_iso() -> str:
    from datetime import datetime

    return datetime.now(tz=UTC).isoformat()
