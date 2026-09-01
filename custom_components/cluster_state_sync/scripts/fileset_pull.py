#!/usr/bin/env python3
"""Pull the replicated fileset onto a follower.

Runs on the HOST, inside the Home Assistant image, because in the cold model
the standby's Home Assistant is stopped and there is no integration there to
receive.

**The image is borrowed for `cryptography`, and for nothing else.** The
original design assumed it could also be borrowed for `redis`. Half of that
was wrong, and the end-to-end rehearsal found it: `redis` is a *custom
component requirement*, which Home Assistant pip-installs into the **running**
container when it sets the integration up. A cold standby's container is
stopped, so a fresh `docker run` of the same image has never seen it.
Confirmed against the image the fleet runs::

    docker run --rm --entrypoint python3 \
        homeassistant/home-assistant:2026.6.4 -c "import cryptography, redis"
    cryptography   PRESENT
    redis          MISSING

`/config/deps` does not carry it either. So this program speaks RESP itself,
out of the standard library — see `resp.ValkeyClient` — exactly as its
sibling `ha_device_preflight.py` uses only the standard library so it can run
on a host with nothing installed. No third-party dependency, no operator
install step, nothing to go stale between promotions.

**The RESP client itself lives in `resp.py`, not here — moved out so the lease
promoter (`cluster_promoter.py`) can use it without dragging in `crypto.py`'s
`cryptography` dependency, which it needs none of.** A promoter that imported
this module for `ValkeyClient` would drag the whole decryption stack in
transitively, and fail on every tick on a host without `python3-cryptography`
installed — nothing more than a log line the only symptom. See `resp.py`'s
own docstring for the incident that found it.

**Everything lands in `.incoming` and is renamed into place only once the whole
manifest has been reconstructed.** A partial go-bag is worse than a stale one:
decision D4 promotes regardless, so a half-written `.storage` would be
installed and Home Assistant would come up looking healthy with pieces missing.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
import pathlib
import shutil
import sys
from typing import Any

try:  # imported as part of the integration package, e.g. by the tests
    from ..crypto import MANIFEST_AAD, FilesetCryptoError, blob_aad, open_sealed
    from .resp import DEFAULT_DB, PASSWORD_ENV, ValkeyClient, ValkeyError
except ImportError:  # run as a standalone script on the host, with its siblings beside it
    from crypto import (  # type: ignore[no-redef]
        MANIFEST_AAD,
        FilesetCryptoError,
        blob_aad,
        open_sealed,
    )
    from resp import DEFAULT_DB, PASSWORD_ENV, ValkeyClient, ValkeyError  # type: ignore[no-redef]

#: This module's own name for `resp.ValkeyError`, kept for every existing
#: `raise PullError` / `except PullError` below and for every caller that
#: already catches it under this name (both Task 2 and Task 3 depend on it).
#: An alias, not a new class -- `RespError` (defined in `resp.py`) is a
#: `ValkeyError` subclass, so it remains a `PullError` too, and `issubclass`
#: proves it rather than this comment merely asserting it.
PullError = ValkeyError


@dataclass(frozen=True, slots=True)
class PullStatus:
    generation: int
    files: int
    fetched: int
    ts: str


def _manifest_key(namespace: str) -> str:
    return f"ha:cluster_state_sync:{namespace}:fileset:manifest"


def _blob_key(namespace: str, ref: str) -> str:
    return f"ha:cluster_state_sync:{namespace}:fileset:blob:{ref}"


def _decode(raw: str | bytes | None) -> bytes | None:
    """Undo Task 3's base64 encoding.

    The Redis clients here are built with `decode_responses=True`, so raw
    AES-GCM ciphertext would be pushed through a UTF-8 decoder and silently
    corrupted on the way in. Task 3 stores everything base64-encoded to avoid
    that, so every read on this side has to reverse it.
    """
    if raw is None:
        return None
    return base64.b64decode(raw)


def _verify_marker(staged_dir: pathlib.Path) -> pathlib.Path:
    """The swap script branches on this file.

    It distinguishes "the bytes did not authenticate" from "we could not reach
    Valkey" — and only this program can tell those apart, because only it sees
    the GCM tag fail. A corrupt go-bag must not be installed; a merely stale one
    must (decision D4).
    """
    return staged_dir / "verify_failed"


def restore_fileset(
    client: Any, *, namespace: str, key: bytes, staged_dir: pathlib.Path
) -> PullStatus:
    """Reconstruct the published fileset under `staged_dir`."""
    # 0700 throughout the staging tree, and chmod'd rather than left to
    # `mkdir`'s mode argument alone: `exist_ok=True` does not touch a directory
    # that is already there, and this runs once a minute forever.
    #
    # What is staged is the leader's entire credential tree. File modes are
    # preserved from the manifest, so `.storage/auth` arrives 0600 -- but
    # `core.config_entries` is 0644 as Home Assistant writes it, and it carries
    # `cluster_secret` and `redis_password` in the clear. Under root's umask a
    # bare `mkdir` gives 0755, so every local account on a host that runs other
    # things could read them.
    staged_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged_dir.chmod(0o700)
    sealed_manifest = _decode(client.get(_manifest_key(namespace)))
    if sealed_manifest is None:
        # Not "nothing to do". An empty staging would be installed by the swap
        # and the promoted node would come up with no integrations and no
        # accounts, reporting success the whole way. Not a verification
        # failure either — there is nothing to verify.
        raise PullError("no fileset manifest published; refusing to stage nothing")
    try:
        manifest_plaintext = open_sealed(key, sealed_manifest, aad=MANIFEST_AAD)
    except FilesetCryptoError as err:
        # Only a genuine authentication failure belongs on the marker. It is
        # what the marker means: "these bytes did not authenticate", not
        # "something about this pull was unusable" — and the swap script
        # treats its presence as "corrupt, install nothing" (decision D4's
        # counterpart: a merely stale go-bag still installs).
        _verify_marker(staged_dir).write_text(f"manifest: {err}\n", encoding="utf-8")
        raise PullError(f"manifest failed authentication: {err}") from err

    try:
        manifest = json.loads(manifest_plaintext.decode("utf-8"))
        entries: dict[str, dict[str, Any]] = manifest["entries"]
        if not isinstance(entries, dict):
            # Caught by review, not by the "categorical" ruling that preceded
            # it: that ruling enumerated fields (generation, ref, mode) but
            # never named the type of `entries` itself. Without this, a
            # manifest with `entries` present but not a JSON object (a list,
            # say) reaches `.values()`/`.items()` below and raises a bare
            # AttributeError — outside the tuple this except clause catches.
            raise TypeError(f"manifest entries is not an object: {entries!r}")
        generation = int(manifest["generation"])
        # Validated now, not lazily. Entries are the only nested structure a
        # manifest has, and every field read from one below (`ref` as a dict
        # key and Redis key fragment, `mode` in a bitwise `&`) assumes it is
        # present and the right type. Deferring either check to first use
        # would put it outside this region — the same defect this review
        # round found at the top-level manifest, then at `generation`; this is
        # the last level there is.
        for entry in entries.values():
            ref, mode = entry["ref"], entry["mode"]
            if not isinstance(ref, str) or not isinstance(mode, int):
                raise TypeError(f"entry has a non-string ref or non-integer mode: {entry!r}")
    except (ValueError, KeyError, TypeError) as err:
        # Deliberately does NOT write the marker. These bytes authenticated —
        # they really were sealed with the cluster key — so this is not
        # corruption in the sense the marker exists to flag. It is the
        # publisher having emitted something that is not a valid manifest
        # (a bug in the leader's own publish path), and writing the marker
        # here would have the swap refuse to install a perfectly good OLDER
        # staged copy over a problem that has nothing to do with the bytes on
        # the wire. main()'s `except PullError` still reports it loudly.
        raise PullError(f"manifest did not parse: {err}") from err

    incoming = staged_dir / ".incoming"
    if incoming.exists():
        shutil.rmtree(incoming)
    incoming.mkdir(mode=0o700)

    refs = sorted({e["ref"] for e in entries.values()})
    raw = client.mget([_blob_key(namespace, ref) for ref in refs])
    bodies: dict[str, bytes] = {}
    for ref, value in zip(refs, raw, strict=True):
        decoded = _decode(value)
        if decoded is None:
            raise PullError(f"blob {ref} is missing; staging left unchanged")
        try:
            # Opened under the ref it was fetched by, so a ciphertext moved
            # to another key -- which needs Valkey write access and no secret
            # -- fails to authenticate here rather than being written to the
            # wrong path with a manifest that verified.
            bodies[ref] = open_sealed(key, decoded, aad=blob_aad(ref))
        except FilesetCryptoError as err:
            _verify_marker(staged_dir).write_text(f"blob {ref}: {err}\n", encoding="utf-8")
            raise PullError(f"blob {ref} failed authentication: {err}") from err

    root = (incoming / "storage").resolve()
    # Created unconditionally, not just as a side effect of the loop below: a
    # manifest with zero entries is a legitimate empty fileset, not corruption,
    # and it must still produce a `storage` directory to rename into place.
    # Before this, a zero-entry manifest crashed the rename below with
    # FileNotFoundError — no loop iterations meant no `.incoming/storage` was
    # ever created to move.
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for rel, entry in sorted(entries.items()):
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            # The manifest is authenticated, so this is defence in depth — but
            # a traversal here writes anywhere the container can reach.
            raise PullError(f"manifest path escapes the staging root: {rel}")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(bodies[entry["ref"]])
        target.chmod(entry["mode"] & 0o777)

    final = staged_dir / "storage"
    previous = staged_dir / ".previous"
    if previous.exists():
        shutil.rmtree(previous)
    if final.exists():
        final.rename(previous)
    (incoming / "storage").rename(final)
    shutil.rmtree(incoming, ignore_errors=True)

    # A clean pull clears a previous failure. Otherwise one bad publish would
    # poison every promotion after it.
    _verify_marker(staged_dir).unlink(missing_ok=True)

    status = PullStatus(
        generation=generation,
        files=len(entries),
        fetched=len(bodies),
        ts=datetime.now(tz=UTC).isoformat(),
    )
    (staged_dir / "status.json").write_text(
        json.dumps(
            {
                "generation": status.generation,
                "files": status.files,
                "fetched": status.fetched,
                "ts": status.ts,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis", required=True, help="host:port")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--staged", required=True)
    parser.add_argument(
        "--db",
        type=int,
        default=DEFAULT_DB,
        help="Valkey database the publisher writes to (default: %(default)s)",
    )
    parser.add_argument("--username", default=None, help="Valkey ACL user, if the server has one")
    parser.add_argument("--tls", action="store_true", help="connect over TLS, with verification")
    parser.add_argument(
        "--tls-ca-file",
        default=None,
        help="CA bundle to verify the server against; omit to use the system trust store",
    )
    # There is deliberately no --password. See PASSWORD_ENV: argv is world
    # readable through `ps`, so the password arrives in the environment.
    args = parser.parse_args(argv)

    host, _, port = args.redis.partition(":")
    key = bytes.fromhex(pathlib.Path(args.key_file).read_text(encoding="utf-8").strip())

    try:
        client = ValkeyClient.connect(
            host=host,
            port=int(port or 6379),
            username=args.username,
            # Empty is treated as absent: an exported-but-unset variable is
            # how a missing password file shows up, and "" is not a password.
            password=os.environ.get(PASSWORD_ENV) or None,
            db=args.db,
            use_tls=args.tls,
            ca_file=args.tls_ca_file,
        )
    except PullError as err:
        print(f"fileset pull failed: {err}", file=sys.stderr)
        return 1

    try:
        status = restore_fileset(
            client,
            namespace=args.namespace,
            key=key,
            staged_dir=pathlib.Path(args.staged),
        )
    except PullError as err:
        print(f"fileset pull failed: {err}", file=sys.stderr)
        return 1
    finally:
        # The timer runs this once a minute, forever. A socket leaked on every
        # failed run is a slow-motion outage on the node standing by to take
        # over.
        client.close()
    print(f"staged generation {status.generation}: {status.files} files, {status.fetched} fetched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
