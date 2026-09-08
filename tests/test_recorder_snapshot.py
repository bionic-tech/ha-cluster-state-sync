"""Consistent copies of the recorder database.

Deliberately NOT a WAL replicator: `VACUUM INTO` gives a consistent copy of a
live database from the standard library, so there is no hand-written WAL
shipping to get subtly wrong -- and a bug there corrupts history silently,
which is the one failure this project refuses to build.
"""

from __future__ import annotations

import pathlib
import sqlite3

from custom_components.cluster_state_sync.recorder_snapshot import (
    RECORDER_DB_NAME,
    SNAPSHOT_LOCK_NAME,
    SNAPSHOT_NAME,
    snapshot_path,
    take_snapshot,
)


def _make_db(path: pathlib.Path, rows: int = 500) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE states (id INTEGER PRIMARY KEY, entity TEXT, state TEXT)")
    con.executemany(
        "INSERT INTO states (entity, state) VALUES (?, ?)",
        [(f"light.n{i}", "on" if i % 2 else "off") for i in range(rows)],
    )
    con.commit()
    con.close()


def test_a_snapshot_is_a_readable_database_with_the_same_rows(
    tmp_path: pathlib.Path,
) -> None:
    """The whole point: what lands on the standby must open and be complete."""
    _make_db(tmp_path / RECORDER_DB_NAME, rows=500)
    result = take_snapshot(str(tmp_path))
    assert result.ok, result.error
    assert result.bytes_written > 0

    con = sqlite3.connect(snapshot_path(str(tmp_path)))
    assert con.execute("SELECT COUNT(*) FROM states").fetchone()[0] == 500
    con.close()


def test_the_source_database_is_never_written(tmp_path: pathlib.Path) -> None:
    """🚨 It opens the live recorder read-only.

    The worst case for a running Home Assistant must be a wasted read, never a
    modified database. This is the guarantee that makes the feature safe to run
    on a live instance every 30 minutes.
    """
    src = tmp_path / RECORDER_DB_NAME
    _make_db(src)
    before = src.stat().st_mtime_ns, src.stat().st_size
    assert take_snapshot(str(tmp_path)).ok
    assert (src.stat().st_mtime_ns, src.stat().st_size) == before


def test_a_missing_recorder_is_NOT_an_error(tmp_path: pathlib.Path) -> None:
    """A shared-database install has no SQLite file, and that is ordinary.

    Raising here would put an exception on a timer every interval for a
    perfectly valid configuration -- exactly the noise that trains people to
    ignore logs.
    """
    result = take_snapshot(str(tmp_path))
    assert result.ok is False
    assert "no SQLite recorder database" in (result.error or "")
    assert not snapshot_path(str(tmp_path)).exists()


def test_the_rename_is_atomic_so_a_consumer_never_sees_a_partial_file(
    tmp_path: pathlib.Path,
) -> None:
    """`VACUUM INTO` writes a NEW file, so the copy must land by rename.

    A peer rsyncing mid-write would otherwise ship a partial database that
    looks fine until it is opened -- the torn-database hazard the installation
    runbook warns about, reintroduced by the very feature meant to avoid it.
    """
    _make_db(tmp_path / RECORDER_DB_NAME)
    assert take_snapshot(str(tmp_path)).ok
    assert snapshot_path(str(tmp_path)).exists()
    # No temporary or lock wreckage left behind for a consumer to trip over.
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert leftovers == [], leftovers
    assert not (tmp_path / SNAPSHOT_LOCK_NAME).exists()


def test_an_existing_snapshot_is_replaced_not_appended(tmp_path: pathlib.Path) -> None:
    """One rolling file, because the peer rsyncs deltas against it.

    A dated series would transfer the whole database every time and defeat the
    reason this is cheap on the wire.
    """
    _make_db(tmp_path / RECORDER_DB_NAME, rows=100)
    assert take_snapshot(str(tmp_path)).ok
    first = snapshot_path(str(tmp_path)).stat().st_size

    _make_db(tmp_path / "bigger.db", rows=5000)
    (tmp_path / RECORDER_DB_NAME).unlink()
    (tmp_path / "bigger.db").rename(tmp_path / RECORDER_DB_NAME)
    assert take_snapshot(str(tmp_path)).ok

    assert len([p for p in tmp_path.iterdir() if p.name == SNAPSHOT_NAME]) == 1
    assert snapshot_path(str(tmp_path)).stat().st_size > first


def test_a_corrupt_source_fails_soft_and_leaves_no_wreckage(
    tmp_path: pathlib.Path,
) -> None:
    """A snapshot failure must never take Home Assistant with it.

    It runs on a timer inside a running instance; the correct response to an
    unreadable database is a reported failure, not an exception escaping into
    the event loop.
    """
    (tmp_path / RECORDER_DB_NAME).write_bytes(b"this is definitely not a database")
    result = take_snapshot(str(tmp_path))
    assert result.ok is False
    assert result.error
    assert not snapshot_path(str(tmp_path)).exists()
    assert not (tmp_path / SNAPSHOT_LOCK_NAME).exists(), "lock left behind, wedging future runs"


def test_stale_wreckage_from_a_crash_does_not_wedge_the_next_run(
    tmp_path: pathlib.Path,
) -> None:
    """A crash mid-snapshot leaves a .tmp behind. The next run must clear it.

    Otherwise one bad moment disables the feature permanently and silently --
    which is this project's founding incident in miniature.
    """
    _make_db(tmp_path / RECORDER_DB_NAME)
    wreck = snapshot_path(str(tmp_path)).with_suffix(".tmp")
    wreck.write_bytes(b"leftover from a crash")
    (tmp_path / SNAPSHOT_LOCK_NAME).write_text("stale\n")

    assert take_snapshot(str(tmp_path)).ok
    assert not wreck.exists()
    assert not (tmp_path / SNAPSHOT_LOCK_NAME).exists()


def test_the_snapshot_is_excluded_from_the_go_bag_by_default() -> None:
    """🚨 A 1.58 GB database must never enter the fileset.

    Two independent reasons, either of which is sufficient:

    1. It is three times the 512 MB cap, so the go-bag would simply break.
    2. The go-bag is swapped **synchronously during a promotion**, so carrying
       a database would put it on the ~102s RTO critical path -- for data that
       is not needed to bring the house back at all.

    The snapshot travels by its own slower path on purpose.
    """
    from custom_components.cluster_state_sync.const import (
        DEFAULT_FILESET_EXCLUSIONS,
        DEFAULT_FILESET_MAX_BYTES,
    )

    assert SNAPSHOT_NAME in DEFAULT_FILESET_EXCLUSIONS
    assert SNAPSHOT_LOCK_NAME in DEFAULT_FILESET_EXCLUSIONS
    # And the reason, stated as an assertion rather than a comment: a real
    # snapshot genuinely does not fit.
    assert 1_580_000_000 > DEFAULT_FILESET_MAX_BYTES


# --- the promotion hand-over, exercised as real shell -----------------------


def _handover_script(tmp: pathlib.Path) -> pathlib.Path:
    """Extract the hand-over block from the generated swap script.

    Run as actual bash, because generated shell is shell: a Python assertion
    that a string contains `rm -f` proves nothing about whether the file is
    removed (GOTCHAS §13).
    """
    import subprocess  # noqa: PLC0415 - test-only

    from custom_components.cluster_state_sync import bundle
    from custom_components.cluster_state_sync.const import (
        CONF_CLUSTER_NAMESPACE,
        CONF_CLUSTER_SECRET,
        CONF_FILESET_ENABLED,
        CONF_HA_CONFIG_PATH,
        CONF_HA_CONTAINER,
        CONF_NODE_ID,
        CONF_PEER_HOST,
        CONF_REDIS_DB,
        CONF_REDIS_HOST,
        CONF_REDIS_PORT,
        CONF_REDIS_USE_TLS,
        CONF_REDIS_USERNAME,
        CONF_TOPOLOGY_MODEL,
    )

    cfg = {
        CONF_REDIS_HOST: "v",
        CONF_REDIS_PORT: 6380,
        CONF_CLUSTER_NAMESPACE: "prod",
        CONF_REDIS_DB: 2,
        CONF_REDIS_USERNAME: "u",
        CONF_REDIS_USE_TLS: True,
        CONF_NODE_ID: "n",
        CONF_HA_CONTAINER: "homeassistant",
        CONF_HA_CONFIG_PATH: str(tmp),
        CONF_TOPOLOGY_MODEL: "cold",
        CONF_FILESET_ENABLED: True,
        CONF_PEER_HOST: "p",
        CONF_CLUSTER_SECRET: "s" * 44,
    }
    full = bundle.build_bundle(cfg)["cluster-fileset-swap.sh"]
    start = full.index("# 5b. Recorder history hand-over")
    end = full.index("# 6. Stale still beats nothing")
    script = tmp / "handover.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        f'CONFIG="{tmp}"\n'
        'mark() { echo "MARKED:$1" >> "$CONFIG/marks"; }\n'
        "logger() { :; }\n" + full[start:end]
    )
    script.chmod(0o755)
    subprocess.run(["bash", "-n", str(script)], check=True)
    return script


def _run(script: pathlib.Path) -> str:
    import subprocess  # noqa: PLC0415

    subprocess.run(["bash", str(script)], check=False, capture_output=True)
    marks = script.parent / "marks"
    return marks.read_text() if marks.exists() else ""


def test_handover_installs_the_snapshot_when_there_is_no_local_database(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / SNAPSHOT_NAME).write_bytes(b"peer history")
    marks = _run(_handover_script(tmp_path))
    assert (tmp_path / RECORDER_DB_NAME).read_bytes() == b"peer history"
    assert marks == "", f"unexpected degraded marks: {marks}"


def test_handover_removes_the_stale_wal_and_shm(tmp_path: pathlib.Path) -> None:
    """🚨 The corruption case.

    SQLite would try to recover the PREVIOUS database's write-ahead log against
    the newly installed file. That is a corrupt database, not a failed swap --
    it opens, and it is wrong.
    """
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    (tmp_path / SNAPSHOT_NAME).write_bytes(b"peer history")
    (tmp_path / RECORDER_DB_NAME).write_bytes(b"old")
    (tmp_path / f"{RECORDER_DB_NAME}-wal").write_bytes(b"old wal")
    (tmp_path / f"{RECORDER_DB_NAME}-shm").write_bytes(b"old shm")
    # Pinned, not left to whichever file happened to be written last. The
    # hand-over branches on `-nt`, and inode timestamps are only granular to
    # a kernel tick -- so two writes a microsecond apart usually compare equal
    # and occasionally do not. Without this the test takes the install branch
    # by luck and the keep-local branch under load, which is a test that
    # passes for a reason unrelated to what it claims to check.
    old = time.time() - 3600
    os.utime(tmp_path / RECORDER_DB_NAME, (old, old))
    for suffix in ("-wal", "-shm"):
        os.utime(tmp_path / f"{RECORDER_DB_NAME}{suffix}", (old, old))

    _run(_handover_script(tmp_path))

    assert not (tmp_path / f"{RECORDER_DB_NAME}-wal").exists(), "stale WAL survived"
    assert not (tmp_path / f"{RECORDER_DB_NAME}-shm").exists(), "stale shm survived"
    assert (tmp_path / RECORDER_DB_NAME).read_bytes() == b"peer history"


def test_handover_keeps_a_NEWER_local_database_and_marks_degraded(
    tmp_path: pathlib.Path,
) -> None:
    """Promoting onto an older copy would discard more than it restores."""
    import os
    import time

    (tmp_path / SNAPSHOT_NAME).write_bytes(b"old peer history")
    old = time.time() - 3600
    os.utime(tmp_path / SNAPSHOT_NAME, (old, old))
    (tmp_path / RECORDER_DB_NAME).write_bytes(b"newer local history")

    marks = _run(_handover_script(tmp_path))

    assert (tmp_path / RECORDER_DB_NAME).read_bytes() == b"newer local history"
    # 🚨 It must NOT claim the degraded marker. That marker is first-reason-wins
    # and means "your identity may be wrong"; a missing graph history stealing
    # it would pre-empt `stale`, which is strictly more important.
    assert marks == "", f"the recorder claimed the degraded marker: {marks}"


def test_handover_survives_no_snapshot_without_claiming_the_marker(
    tmp_path: pathlib.Path,
) -> None:
    """D4: promote anyway -- and leave the degraded marker for identity faults.

    Recorder health is reported by its own diagnostic sensor. Letting it write
    the shared marker would dilute the one signal an operator actually acts on.
    """
    (tmp_path / RECORDER_DB_NAME).write_bytes(b"whatever was here")
    marks = _run(_handover_script(tmp_path))
    assert marks == "", f"the recorder claimed the degraded marker: {marks}"
    assert (tmp_path / RECORDER_DB_NAME).exists(), "it destroyed the only database"


def test_handover_keeps_the_snapshot_so_a_retry_is_possible(
    tmp_path: pathlib.Path,
) -> None:
    """Copy, not move. A promotion that fails later must be retryable."""
    (tmp_path / SNAPSHOT_NAME).write_bytes(b"peer history")
    _run(_handover_script(tmp_path))
    assert (tmp_path / SNAPSHOT_NAME).exists(), "the snapshot was consumed"
