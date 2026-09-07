"""Which files replicate, and what a manifest of them looks like.

**Why an explicit set of roots rather than the whole config directory.** The
recorder database sits in `/config`, and an rsync of a live SQLite file
replicates corruption rather than data (ADR-001 tier 3). Walking only the named
roots means the database is never a candidate, rather than being excluded by a
pattern somebody could delete.

**Why a deny-list inside `.storage`, when AR-0041 argued for an allow-list.**
The inversion is deliberate and it is about what over- and under-including
cost. At the `/config` level a deny-list fails open into gigabytes and a live
database. Inside `.storage` every file is small atomic JSON: over-including
costs megabytes, under-including costs a credential that silently needs
re-authenticating — discovered during an outage. Home Assistant keeps
credentials in `core.config_entries`, but roughly thirty HACS integrations are
not bound by that convention, and auditing them goes stale the next time one is
installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fnmatch
import json
import logging
import pathlib
from typing import Any, Final

from . import includes
from .crypto import MANIFEST_AAD, blob_aad, blob_ref, derive_fileset_key, seal

_LOGGER = logging.getLogger(__name__)

#: Directories replicated in full, relative to the config directory.
REPLICATED_DIRS: Final = (".storage", "custom_components", "www", "blueprints")
#: Individual files replicated, relative to the config directory.
REPLICATED_FILES: Final = (
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "secrets.yaml",
    "customize.yaml",
    "groups.yaml",
)


class FilesetTooLarge(Exception):
    """The payload exceeded the configured cap; nothing was published."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One replicated file, addressed by its content.

    `mtime_ns` is not decoration and not diagnostics. It is half of the change
    heuristic, and without it `scan` compares size and mode alone -- which
    misses every same-length rewrite. The concrete case is
    `.storage/auth_provider.homeassistant`: it holds a bcrypt hash, bcrypt
    hashes are fixed length, so changing the owner's password leaves the byte
    count identical. The standby would keep publishing the *old* password
    until the leader restarted and reset `_current`, which was the only thing
    that healed it. `.storage/http` and any fixed-length rotating token behave
    the same way.

    rsync's quick check is size **and** mtime for exactly this reason.
    """

    path: str
    ref: str
    size: int
    mode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """The complete set of files for one generation."""

    generation: int
    node: str
    ts: str
    entries: dict[str, FileEntry] = field(default_factory=dict)

    def to_json(self) -> str:
        """Canonical JSON. `sort_keys` keeps the form stable across nodes, so
        an unchanged fileset serialises identically and does not read as a
        change."""
        return json.dumps(
            {
                "generation": self.generation,
                "node": self.node,
                "ts": self.ts,
                "entries": {
                    p: {
                        "ref": e.ref,
                        "size": e.size,
                        "mode": e.mode,
                        "mtime_ns": e.mtime_ns,
                    }
                    for p, e in self.entries.items()
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        data: dict[str, Any] = json.loads(raw)
        return cls(
            generation=int(data["generation"]),
            node=str(data["node"]),
            ts=str(data["ts"]),
            entries={
                p: FileEntry(
                    path=p,
                    ref=v["ref"],
                    size=int(v["size"]),
                    mode=int(v["mode"]),
                    mtime_ns=int(v["mtime_ns"]),
                )
                for p, v in data["entries"].items()
            },
        )


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What one pass over the config directory found."""

    entries: dict[str, FileEntry]
    #: ref -> plaintext, only for content not present in `previous`.
    bodies: dict[str, bytes]
    total_bytes: int


def is_excluded(rel_path: str, patterns: Sequence[str]) -> bool:
    """Match any component of a relative path against the exclusion globs.

    Components rather than the basename alone. The operator thinks in terms of
    "`*.bak-*`", not "`.storage/*.bak-*`" — and the thing that actually costs
    is a stale *directory*: tiger1 carries
    `custom_components/localtuya.bak-5.2.3-20260601-154152/`, inside the 100 MB
    tree that is the largest payload here. Matching only the basename would
    drop the `.storage` backups and ship that one in full.
    """
    return any(
        fnmatch.fnmatch(part, pattern)
        for part in pathlib.PurePosixPath(rel_path).parts
        for pattern in patterns
    )


def _candidates(root: pathlib.Path, extra: Sequence[str] = ()) -> list[tuple[str, pathlib.Path]]:
    """Everything to publish: the fixed floor, plus whatever the config needs.

    The fixed lists below are a floor, not the answer. They cannot know how an
    operator has split their configuration up, and on this fleet they did not:
    `configuration.yaml` pulled in `configs/*.yaml`, `themes/`, `packages/` and
    a Google service-account credential under `secure/`, none of which crossed.
    The promoted node could not parse its own configuration and came up in
    recovery mode, behind a promotion that reported success at every step.

    So `includes.scan` follows the includes and adds what it finds. See
    `includes.py` for what it deliberately cannot see.
    """
    found: list[tuple[str, pathlib.Path]] = []
    seen: set[str] = set()

    def add_file(rel: str, path: pathlib.Path) -> None:
        if rel in seen:
            return
        if path.is_file() and not path.is_symlink():
            seen.add(rel)
            found.append((rel, path))

    def add_tree(base: pathlib.Path) -> None:
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink():
                add_file(path.relative_to(root).as_posix(), path)

    for name in REPLICATED_DIRS:
        add_tree(root / name)
    for name in REPLICATED_FILES:
        add_file(name, root / name)

    # Whatever the configuration actually references. Never raises: a
    # configuration too broken to parse must still publish the floor above,
    # because a degraded go-bag beats no go-bag.
    try:
        referenced = includes.scan(str(root))
    except Exception:  # noqa: BLE001 - a scan failure must not stop publishing
        _LOGGER.warning("Could not scan configuration.yaml for includes", exc_info=True)
        return found

    for rel in sorted(referenced.paths):
        target = root / rel
        if target.is_dir():
            add_tree(target)
        else:
            add_file(rel, target)

    # The operator's own list, for what no parser can see.
    for rel in extra:
        target = root / rel
        if target.is_dir():
            add_tree(target)
        elif target.is_file():
            add_file(rel, target)
        else:
            _LOGGER.warning(
                "Extra replicated path %r does not exist in the config "
                "directory; a promoted standby will not have it.",
                rel,
            )

    for source, target in referenced.outside:
        _LOGGER.warning(
            "%s references %s, which is outside the config directory and "
            "CANNOT be replicated. A promoted standby will not have it.",
            source,
            target,
        )
    return found


def scan(
    root: pathlib.Path,
    *,
    secret: str,
    exclusions: Sequence[str],
    extra_paths: Sequence[str] = (),
    previous: Mapping[str, FileEntry] | None = None,
    max_bytes: int | None = None,
) -> ScanResult:
    """Walk the replicated roots and build a manifest.

    `previous` is the last published manifest's entries. A file whose size,
    mode **and** modification time are unchanged is assumed unchanged and its
    body is not re-read — which is what keeps 5,703 files in
    `custom_components` from being hashed every sixty seconds.

    Size and mode alone are not enough, and the gap is not theoretical.
    `.storage/auth_provider.homeassistant` holds a bcrypt hash, which is fixed
    length: changing the owner's password leaves the byte count identical, so
    the standby went on publishing the *old* password until the leader
    restarted and reset `_current`. rsync's quick check is size and mtime for
    exactly this reason.
    """
    entries: dict[str, FileEntry] = {}
    bodies: dict[str, bytes] = {}
    total = 0

    for rel, path in _candidates(root, extra_paths):
        if is_excluded(rel, exclusions):
            continue
        stat = path.stat()
        total += stat.st_size
        if max_bytes is not None and total > max_bytes:
            raise FilesetTooLarge(f"fileset exceeds {max_bytes} bytes at {rel}; nothing published")
        prior = previous.get(rel) if previous else None
        if (
            prior is not None
            and prior.size == stat.st_size
            and prior.mode == stat.st_mode
            and prior.mtime_ns == stat.st_mtime_ns
        ):
            entries[rel] = prior
            continue
        body = path.read_bytes()
        ref = blob_ref(secret, body)
        entries[rel] = FileEntry(
            path=rel,
            ref=ref,
            size=stat.st_size,
            mode=stat.st_mode,
            mtime_ns=stat.st_mtime_ns,
        )
        bodies[ref] = body

    return ScanResult(entries=entries, bodies=bodies, total_bytes=total)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What one publish pass did — surfaced through diagnostics."""

    generation: int
    files: int
    blobs_written: int
    bytes_written: int
    pruned: int
    skipped_reason: str | None = None


class FilesetPublisher:
    """Publish the config fileset to the cluster backend. Leader only.

    Holds the previous two generations' manifests so garbage collection can
    keep both: the current one, and one generation of rollback. Content
    addressing means every change *creates* a blob and nothing overwrites, so
    without a prune Valkey grows without bound.
    """

    def __init__(
        self,
        backend: Any,
        *,
        config_dir: str,
        node_id: str,
        secret: str,
        exclusions: Sequence[str],
        extra_paths: Sequence[str] = (),
        max_bytes: int,
    ) -> None:
        self.backend = backend
        self._root = pathlib.Path(config_dir)
        self._node_id = node_id
        self._secret = secret
        self._key = derive_fileset_key(secret)
        self._exclusions = tuple(exclusions)
        self._extra_paths = tuple(extra_paths)
        self._max_bytes = max_bytes
        self._generation = 0
        self._current: dict[str, FileEntry] = {}
        self._previous: dict[str, FileEntry] = {}
        self.last_result: PublishResult | None = None
        # Separate from `last_result`, which carries no timestamp: it answers
        # "what did the last pass do", not "how long ago". `FilesetAgeSensor`
        # needs the latter to be a real gauge -- one that keeps climbing if
        # the publish loop silently stops, rather than freezing at whatever
        # `last_result` last said. Mirrors `StateMirror.last_successful_flush`.
        self.last_success_at: datetime | None = None
        # The standing refusal, separate from `last_result.skipped_reason`,
        # which is overwritten by the next pass. This is what makes the error
        # log fire on the transition into the state rather than once a minute
        # for as long as it lasts.
        self._skipped_reason: str | None = None

    async def async_publish(self) -> PublishResult:
        """Scan, seal and publish. Blocking I/O runs in an executor."""
        try:
            scanned = await asyncio.get_running_loop().run_in_executor(None, self._scan)
        except FilesetTooLarge as err:
            # Publish nothing. A partial fileset is worse than a stale one:
            # the follower would swap in a go-bag missing files it needs.
            #
            # Said out loud, because refusing in silence is how this becomes
            # AR-0040 again. Publishing stops dead here: `last_success_at`
            # correctly stops advancing, but the only downstream signal used to
            # be a `stale` marker at the next promotion -- the moment it is
            # least useful and least likely to be acted on. Logged on the
            # *transition* rather than on every pass, because this runs once a
            # minute forever and a line a minute is a line nobody reads.
            # `FilesetDegradedBinarySensor` carries the standing state.
            if self._skipped_reason != "too_large":
                _LOGGER.error(
                    "Fileset publish REFUSED and nothing was published: %s. The standby's "
                    "go-bag stops ageing forward from here and will be stale at the next "
                    "promotion. Raise the fileset size cap or add an exclusion.",
                    err,
                )
            self._skipped_reason = "too_large"
            result = PublishResult(
                generation=self._generation,
                files=len(self._current),
                blobs_written=0,
                bytes_written=0,
                pruned=0,
                skipped_reason="too_large",
            )
            self.last_result = result
            # Deliberately does NOT touch `last_success_at`. Nothing was
            # published, so the age gauge has to keep climbing from the last
            # real success -- stamping it here would report a fresh fileset
            # that does not exist, which is the exact shape of reassurance
            # this project exists to distrust.
            return result

        if self._skipped_reason is not None:
            _LOGGER.info(
                "Fileset publish recovered: the payload is back under the cap and "
                "publishing has resumed"
            )
            self._skipped_reason = None

        self._generation += 1
        manifest = Manifest(
            generation=self._generation,
            node=self._node_id,
            ts=_utc_now_iso(),
            entries=scanned.entries,
        )
        # Each blob is sealed under its own ref, so a ciphertext stored at any
        # other reference will not open. Without that, Valkey *write* access
        # and no secret at all is enough to move one file's bytes onto another
        # file's path at the next pull, or to roll a single file back.
        sealed_bodies = {
            ref: seal(self._key, body, aad=blob_aad(ref)) for ref, body in scanned.bodies.items()
        }
        await self.backend.write_fileset(
            seal(self._key, manifest.to_json().encode("utf-8"), aad=MANIFEST_AAD), sealed_bodies
        )

        self._previous, self._current = self._current, scanned.entries
        keep = {e.ref for e in self._current.values()} | {e.ref for e in self._previous.values()}
        pruned = await self.backend.prune_blobs(keep)

        result = PublishResult(
            generation=self._generation,
            files=len(scanned.entries),
            blobs_written=len(sealed_bodies),
            bytes_written=sum(len(b) for b in sealed_bodies.values()),
            pruned=pruned,
        )
        self.last_result = result
        self.last_success_at = datetime.now(tz=UTC)
        return result

    def _scan(self) -> ScanResult:
        return scan(
            self._root,
            secret=self._secret,
            exclusions=self._exclusions,
            extra_paths=self._extra_paths,
            previous=self._current,
            max_bytes=self._max_bytes,
        )
