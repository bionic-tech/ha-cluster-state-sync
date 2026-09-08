#!/usr/bin/env python3
"""Apply the leader's long-term statistics on a follower.

Runs on the HOST, inside the Home Assistant image, for exactly the reason
`fileset_pull.py` does: in the cold model the standby's Home Assistant is
stopped, so there is no integration there to receive anything. Same borrowed
`cryptography`, same hand-rolled RESP client (`redis` is a custom-component
requirement Home Assistant pip-installs into a *running* container, and a
stopped one has never seen it), same two-arm import.

**It writes to `.cluster_sync_statistics.db`, never to the real recorder.** On
a warm standby `home-assistant_v2.db` is open by a live recorder; this program
runs outside Home Assistant entirely and has no idea whether that is the case.
`cluster-fileset-swap.sh` installs the accumulated store under the real name at
promotion, when the container is provably stopped.

**Seeding is automatic but verified.** Years of statistics cannot arrive
through the window -- the operator copies a seed across by hand, once. A
484 MB copy that was still running when this ran would be a corrupt database
that opens perfectly well, so a seed is adopted only after SQLite's own
`quick_check` passes and its schema version reads. That is a real gate, not a
ceremony, and unlike a dashboard button it works on a node with no Home
Assistant running.

**What it reports, and to whom.** A follower in the cold model has no logbook,
no repairs panel and no entities. Everything this program learns -- a schema
mismatch, a gap wider than the window, a missing seed -- would die on a host
nobody reads. So it writes a status line back to Valkey, and the leader raises
the alarm on its behalf. That is the only place in this integration where data
flows standby to leader, and this is why.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Sequence
from datetime import UTC, datetime
import json
import os
import pathlib
import sqlite3
import sys

try:  # imported as part of the integration package, e.g. by the tests
    from ..crypto import (
        STATISTICS_AAD,
        STATISTICS_STATUS_AAD,
        FilesetCryptoError,
        open_sealed,
        seal,
    )
    from ..statistics_sync import (
        SchemaMismatch,
        SyncPayload,
        apply_payload,
        schema_version,
        write_seed,
    )
    from .resp import DEFAULT_DB, PASSWORD_ENV, ValkeyClient, ValkeyError
except ImportError:  # run as a standalone script on the host, with its siblings beside it
    from crypto import (  # type: ignore[no-redef]
        STATISTICS_AAD,
        STATISTICS_STATUS_AAD,
        FilesetCryptoError,
        open_sealed,
        seal,
    )
    from resp import DEFAULT_DB, PASSWORD_ENV, ValkeyClient, ValkeyError  # type: ignore[no-redef]
    from statistics_sync import (  # type: ignore[no-redef]
        SchemaMismatch,
        SyncPayload,
        apply_payload,
        schema_version,
        write_seed,
    )

#: Mirrors `const.STATISTICS_DB_NAME` and `const.STATISTICS_SEED_NAME`. Repeated
#: rather than imported for the same reason every constant in the generated
#: shell is: this runs from a bundle directory with no integration package.
STORE_NAME = ".cluster_sync_statistics.db"
SEED_NAME = ".cluster_sync_statistics_seed.db"

#: Home Assistant's own recorder, when it is SQLite. Read here only to rebuild
#: the store after a fail-back -- never written to.
RECORDER_NAME = "home-assistant_v2.db"

#: How long the follower's status line lives in Valkey. Comfortably longer than
#: the publish interval, short enough that a decommissioned follower's last
#: word expires instead of standing as fact forever.
STATUS_TTL_SECONDS = 86400


def _statistics_key(namespace: str) -> str:
    return f"ha:cluster_state_sync:{namespace}:statistics:window"


def _status_key(namespace: str) -> str:
    return f"ha:cluster_state_sync:{namespace}:statistics:follower"


def adopt_seed(seed: pathlib.Path, store: pathlib.Path) -> bool:
    """Promote a hand-copied seed to the store, if it is whole.

    Returns True if a seed was adopted. Raises `ValueError` if one is present
    but unusable — which is the interesting case, because the overwhelmingly
    likely cause is that the copy had not finished, and the operator needs to
    be told rather than left with a store that quietly holds half a database.
    """
    if not seed.exists():
        return False
    con = sqlite3.connect(f"file:{seed}?mode=ro", uri=True, timeout=30.0)
    try:
        # `quick_check` rather than `integrity_check`: it catches the
        # structural damage a truncated copy produces, and does not spend
        # minutes re-reading every index on a 484 MB file.
        result = con.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"seed at {seed} failed SQLite's integrity check: {result}")
        version = schema_version(con)
        if not version:
            raise ValueError(
                f"seed at {seed} carries no readable recorder schema version; every "
                "window would be refused against it"
            )
        rows = con.execute("SELECT COUNT(*) FROM statistics").fetchone()[0]
    except sqlite3.Error as err:
        raise ValueError(f"seed at {seed} is not a readable database: {err}") from err
    finally:
        con.close()

    # Renamed, not copied: an atomic rename within one filesystem means there
    # is never a moment where the store is a partial file.
    seed.replace(store)
    print(f"statistics seed adopted: {rows} rows, recorder schema {version}", file=sys.stderr)
    return True


def local_watermark(store: pathlib.Path) -> float:
    """The newest statistics row already held, or 0.0 for an empty store."""
    con = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=30.0)
    try:
        row = con.execute("SELECT MAX(start_ts) FROM statistics").fetchone()
    except sqlite3.Error:
        return 0.0
    finally:
        con.close()
    return float(row[0]) if row and row[0] is not None else 0.0


def apply_window(config_dir: pathlib.Path, payload: SyncPayload) -> dict[str, object]:
    """Apply one window to this node's store. Returns the status to report."""
    store = config_dir / STORE_NAME
    seed = config_dir / SEED_NAME

    adopted = adopt_seed(seed, store)
    rebuilt = 0
    if not store.exists():
        # Fail-back. `cluster-fileset-swap.sh` MOVES the store into place as
        # the recorder at promotion, so a node that has been leader and is now
        # standby again has no store -- but it does have every one of those
        # statistics in its own recorder, plus everything it recorded while it
        # was running. Rebuilding from that is strictly better than asking an
        # operator to copy 484 MB by hand for a second time, and it is why the
        # swap consumes the store rather than leaving a copy that would freeze.
        recorder = config_dir / RECORDER_NAME
        if recorder.exists():
            try:
                rebuilt = write_seed(recorder, store)
            except (SchemaMismatch, sqlite3.Error, OSError) as err:
                return {
                    "state": "failed",
                    "detail": f"could not rebuild the statistics store from {recorder}: {err}",
                }
            print(
                f"statistics store rebuilt from this node's own recorder: {rebuilt} rows",
                file=sys.stderr,
            )

    if not store.exists():
        # Not an error, and not something the next run will fix on its own:
        # somebody has to copy a seed across. Reported rather than logged,
        # because on this node a log line has no reader.
        return {
            "state": "not_seeded",
            "detail": (
                f"no statistics store at {store} and no seed at {seed}. Write a seed on "
                "the leader and copy it here to start replicating history."
            ),
        }

    before = local_watermark(store)
    # A gap check, and the reason `covers_from` exists. The window carries
    # every row after its floor; if this node's newest row is OLDER than that
    # floor, the rows in between exist on neither side of the copy and no
    # later window will ever contain them. Applying anyway would leave a hole
    # that looks exactly like a working replica.
    gap = before > 0.0 and payload.covers_from > before

    applied = apply_payload(store, payload)
    status: dict[str, object] = {
        "state": "gap" if gap else "ok",
        "applied": applied,
        "watermark": local_watermark(store),
        "schema_version": payload.schema_version,
    }
    if adopted:
        status["seeded"] = True
    if rebuilt:
        status["rebuilt"] = rebuilt
    if gap:
        status["detail"] = (
            f"this node's newest statistics row is {_stamp(before)} but the leader's "
            f"window only reaches back to {_stamp(payload.covers_from)}. The history "
            "between those is on neither node. Widen the window, or re-seed."
        )
    return status


def _stamp(ts: float) -> str:
    """A timestamp an operator can act on, not a float."""
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def _report(client: object, namespace: str, status: dict[str, object], key: bytes) -> None:
    """Leave the status where the leader will find it, sealed (AR-0045).

    Never fatal: failing to report is not failing to apply, and a follower
    that applied the window correctly must not exit non-zero because it could
    not describe having done so.
    """
    status = {**status, "ts": datetime.now(tz=UTC).isoformat()}
    try:
        sealed = seal(
            key, json.dumps(status, sort_keys=True).encode("utf-8"), aad=STATISTICS_STATUS_AAD
        )
        client.set(  # type: ignore[attr-defined]
            _status_key(namespace),
            base64.b64encode(sealed).decode("ascii"),
            ex=STATUS_TTL_SECONDS,
        )
    except ValkeyError as err:
        print(f"statistics: could not report status: {err}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the leader's statistics window.")
    parser.add_argument("--redis", required=True, help="host:port")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--config", required=True, help="the follower's config directory")
    parser.add_argument("--db", type=int, default=DEFAULT_DB)
    parser.add_argument("--username", default=None)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--tls-ca-file", default=None)
    # No --password, for the same reason as its sibling: argv is world
    # readable through `ps`, so the password arrives in the environment.
    args = parser.parse_args(argv)

    host, _, port = args.redis.partition(":")
    key = bytes.fromhex(pathlib.Path(args.key_file).read_text(encoding="utf-8").strip())
    config_dir = pathlib.Path(args.config)

    try:
        client = ValkeyClient.connect(
            host=host,
            port=int(port or 6379),
            username=args.username,
            password=os.environ.get(PASSWORD_ENV) or None,
            db=args.db,
            use_tls=args.tls,
            ca_file=args.tls_ca_file,
        )
    except ValkeyError as err:
        print(f"statistics pull failed: {err}", file=sys.stderr)
        return 1

    try:
        raw = client.get(_statistics_key(args.namespace))
        if raw is None:
            # The leader has statistics replication off, or has not published
            # yet. Nothing to do, and nothing wrong: exit clean and quiet so
            # a once-a-minute timer does not fill the journal.
            return 0
        try:
            body = open_sealed(key, base64.b64decode(raw), aad=STATISTICS_AAD)
        except (FilesetCryptoError, ValueError) as err:
            _report(client, args.namespace, {"state": "unauthenticated", "detail": str(err)}, key)
            print(f"statistics window failed authentication: {err}", file=sys.stderr)
            return 1

        try:
            payload = SyncPayload.from_bytes(body)
        except ValueError as err:
            # These bytes authenticated, so this is the leader having
            # published something malformed -- a bug on that side, not
            # corruption on the wire.
            _report(client, args.namespace, {"state": "malformed", "detail": str(err)}, key)
            print(f"statistics window did not parse: {err}", file=sys.stderr)
            return 1

        try:
            status = apply_window(config_dir, payload)
        except SchemaMismatch as err:
            # The alarm this whole status channel exists for. Only this node
            # can see both schema versions, and only the leader has anywhere
            # to display the finding.
            _report(client, args.namespace, {"state": "schema_mismatch", "detail": str(err)}, key)
            print(f"statistics: {err}", file=sys.stderr)
            return 1
        except (ValueError, sqlite3.Error) as err:
            _report(client, args.namespace, {"state": "failed", "detail": str(err)}, key)
            print(f"statistics apply failed: {err}", file=sys.stderr)
            return 1

        _report(client, args.namespace, status, key)
        if status["state"] == "not_seeded":
            print(f"statistics: {status['detail']}", file=sys.stderr)
            # Exit 0: waiting to be seeded is a configuration state, not a
            # fault, and a non-zero exit here would put the timer's unit into
            # `failed` forever on a node nobody has got round to seeding.
            return 0
        if status["state"] == "gap":
            print(f"statistics: {status['detail']}", file=sys.stderr)
            return 1
        print(
            f"statistics: applied {status['applied']} rows, "
            f"history now reaches {_stamp(float(status['watermark']))}",
            file=sys.stderr,
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
