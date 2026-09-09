"""Statistics across the wire: publisher, and the follower that applies it.

The publisher runs inside Home Assistant on the leader; the puller runs on the
standby's HOST, inside a borrowed container, because in the cold model that
node's Home Assistant is stopped. Both halves are tested here because the
interesting failures live in the seam between them.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
import pathlib
import sqlite3
import time

import pytest

from custom_components.cluster_state_sync.crypto import (
    STATISTICS_AAD,
    derive_fileset_key,
    seal,
)
from custom_components.cluster_state_sync.scripts.statistics_pull import (
    STORE_NAME,
    adopt_seed,
    apply_window,
    main,
)
from custom_components.cluster_state_sync.statistics_publisher import (
    RECORDER_DB_NAME,
    StatisticsPublisher,
)
from custom_components.cluster_state_sync.statistics_sync import SyncPayload, export_since
from tests.fakes import FakeBackend


def _open_status(raw: str) -> dict:
    """Open what `statistics_pull.py` wrote to the status key (AR-0045).

    The channel is sealed now, so a test that still read it as plain JSON
    would be asserting against a format the follower no longer writes.
    """
    import base64 as _b64
    import json as _json

    from custom_components.cluster_state_sync.crypto import (
        STATISTICS_STATUS_AAD,
        open_sealed,
    )

    plain = open_sealed(derive_fileset_key(SECRET), _b64.b64decode(raw), aad=STATISTICS_STATUS_AAD)
    return _json.loads(plain.decode("utf-8"))


def _sealed_status(status: dict) -> bytes:
    """Seal a status line the way `statistics_pull.py` does (AR-0045)."""
    from datetime import UTC, datetime
    import json as _json

    from custom_components.cluster_state_sync.crypto import STATISTICS_STATUS_AAD

    body = {"ts": datetime.now(tz=UTC).isoformat(), **status}
    return seal(
        derive_fileset_key(SECRET),
        _json.dumps(body, sort_keys=True).encode("utf-8"),
        aad=STATISTICS_STATUS_AAD,
    )


SECRET = "a-test-cluster-secret"
NS = "test"
SCHEMA = 53
DAY = 86400.0


def _recorder(path: pathlib.Path, version: int = SCHEMA) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE schema_changes (
            change_id INTEGER PRIMARY KEY, schema_version INTEGER, changed TEXT);
        CREATE TABLE statistics_meta (
            id INTEGER PRIMARY KEY, statistic_id TEXT UNIQUE, source TEXT,
            unit_of_measurement TEXT, has_mean INTEGER, has_sum INTEGER, name TEXT);
        CREATE TABLE statistics (
            id INTEGER PRIMARY KEY, metadata_id INTEGER, created_ts REAL,
            start_ts REAL, mean REAL, min REAL, max REAL, last_reset_ts REAL,
            state REAL, sum REAL,
            UNIQUE(metadata_id, start_ts));
        """
    )
    con.execute("INSERT INTO schema_changes (schema_version, changed) VALUES (?, '')", (version,))
    con.commit()
    return con


def _add(con: sqlite3.Connection, start_ts: float, value: float = 1.0) -> None:
    con.execute(
        "INSERT OR IGNORE INTO statistics_meta (statistic_id, source) VALUES "
        "('sensor.energy', 'recorder')"
    )
    mid = con.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id='sensor.energy'"
    ).fetchone()[0]
    con.execute(
        "INSERT OR IGNORE INTO statistics (metadata_id, start_ts, mean) VALUES (?,?,?)",
        (mid, start_ts, value),
    )
    con.commit()


class FakeClient:
    """Just enough Valkey: `get`, and the one `set` a follower performs."""

    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self.values = {k: base64.b64encode(v).decode("ascii") for k, v in (values or {}).items()}
        self.written: dict[str, str] = {}
        self.closed = False

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.written[key] = value

    def close(self) -> None:
        self.closed = True


# --- publisher ------------------------------------------------------------


async def test_the_published_window_is_sealed_and_opens_to_the_rows(
    tmp_path: pathlib.Path,
) -> None:
    con = _recorder(tmp_path / RECORDER_DB_NAME)
    _add(con, time.time() - 3600, 42.0)
    con.close()

    backend = FakeBackend()
    result = await StatisticsPublisher(
        backend,
        config_dir=str(tmp_path),
        secret=SECRET,
        window_days=30,
        max_bytes=64 * 1024 * 1024,
    ).async_publish()

    assert result.rows == 1
    assert result.skipped_reason is None
    assert backend.statistics is not None

    from custom_components.cluster_state_sync.crypto import open_sealed

    payload = SyncPayload.from_bytes(
        open_sealed(derive_fileset_key(SECRET), backend.statistics, aad=STATISTICS_AAD)
    )
    assert payload.rows[0]["mean"] == 42.0
    assert payload.schema_version == SCHEMA


async def test_the_window_is_a_window_and_leaves_the_old_rows_out(
    tmp_path: pathlib.Path,
) -> None:
    """Otherwise every publish would carry 6.4 million rows, not 200,000."""
    con = _recorder(tmp_path / RECORDER_DB_NAME)
    now = time.time()
    _add(con, now - 400 * DAY, 1.0)  # ancient: the seed's job, not the window's
    _add(con, now - 3600, 2.0)
    con.close()

    backend = FakeBackend()
    result = await StatisticsPublisher(
        backend,
        config_dir=str(tmp_path),
        secret=SECRET,
        window_days=30,
        max_bytes=64 * 1024 * 1024,
    ).async_publish()
    assert result.rows == 1


async def test_an_oversized_window_publishes_nothing_at_all(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A truncated window would look exactly like a working replica."""
    con = _recorder(tmp_path / RECORDER_DB_NAME)
    _add(con, time.time() - 3600, 1.0)
    con.close()

    backend = FakeBackend()
    pub = StatisticsPublisher(
        backend,
        config_dir=str(tmp_path),
        secret=SECRET,
        window_days=30,
        max_bytes=1,
    )
    result = await pub.async_publish()
    assert result.skipped_reason == "too_large"
    assert backend.statistics is None, "a window over the cap was published anyway"
    assert pub.last_success_at is None, "a refusal stamped the freshness gauge"
    assert "REFUSED" in caplog.text

    # Said once per transition, not once per interval: this runs on a timer
    # forever, and a line every half hour is a line nobody reads.
    caplog.clear()
    await pub.async_publish()
    assert "REFUSED" not in caplog.text


async def test_a_shared_database_install_is_not_an_error(tmp_path: pathlib.Path) -> None:
    """Postgres and MariaDB have no SQLite file, and already share history."""
    backend = FakeBackend()
    result = await StatisticsPublisher(
        backend,
        config_dir=str(tmp_path),
        secret=SECRET,
        window_days=30,
        max_bytes=64 * 1024 * 1024,
    ).async_publish()
    assert result.skipped_reason == "no_sqlite_recorder"
    assert backend.statistics is None


# --- follower -------------------------------------------------------------


def test_a_half_copied_seed_is_refused_rather_than_adopted(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 The failure the seed step exists to survive.

    A 300 MB scp that was still running when the timer fired leaves a truncated
    SQLite file. Adopting it produces a store that opens cleanly, answers
    queries, and is missing history -- and it would be installed as the real
    recorder at the next promotion.
    """
    seed = tmp_path / ".cluster_sync_statistics_seed.db"
    con = _recorder(seed)
    for i in range(400):  # enough pages that truncation actually damages it
        _add(con, 1000.0 + i, float(i))
    con.close()
    whole = seed.read_bytes()
    seed.write_bytes(whole[: len(whole) // 2])

    with pytest.raises(ValueError, match="integrity check|not a readable database"):
        adopt_seed(seed, tmp_path / STORE_NAME)
    assert seed.exists(), "the unusable seed was consumed"
    assert not (tmp_path / STORE_NAME).exists(), "a truncated seed became the store"


def test_a_seed_with_no_schema_version_is_refused(tmp_path: pathlib.Path) -> None:
    """It would refuse every window forever, after a large manual copy."""
    seed = tmp_path / ".cluster_sync_statistics_seed.db"
    con = sqlite3.connect(seed)
    con.execute("CREATE TABLE statistics (id INTEGER PRIMARY KEY, start_ts REAL)")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="schema version"):
        adopt_seed(seed, tmp_path / STORE_NAME)


def test_a_whole_seed_is_adopted_and_then_extended(tmp_path: pathlib.Path) -> None:
    """The cold-standby story end to end: seed by hand, then let deltas land."""
    seed = tmp_path / ".cluster_sync_statistics_seed.db"
    con = _recorder(seed)
    _add(con, 1000.0, 1.0)
    con.close()

    leader = tmp_path / "leader.db"
    lc = _recorder(leader)
    _add(lc, 1000.0, 1.0)
    _add(lc, 2000.0, 2.0)
    lc.close()

    status = apply_window(tmp_path, export_since(leader, since_ts=500.0))
    assert status["state"] == "ok"
    assert status["seeded"] is True
    assert status["applied"] == 1  # the row it did not already have
    assert not seed.exists(), "the seed was not consumed"

    store = sqlite3.connect(tmp_path / STORE_NAME)
    assert store.execute("SELECT COUNT(*) FROM statistics").fetchone()[0] == 2
    store.close()


def test_an_unseeded_follower_says_so_instead_of_inventing_a_store(
    tmp_path: pathlib.Path,
) -> None:
    """Creating an empty store would report a healthy replica of nothing."""
    leader = tmp_path / "leader.db"
    lc = _recorder(leader)
    _add(lc, 2000.0, 2.0)
    lc.close()

    status = apply_window(tmp_path, export_since(leader))
    assert status["state"] == "not_seeded"
    assert not (tmp_path / STORE_NAME).exists()


def test_a_window_that_does_not_reach_back_far_enough_reports_a_gap(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 The silent-hole case, and the reason `covers_from` is on the wire.

    A standby off for longer than the window comes back, applies the window,
    and looks entirely healthy -- while the months in between exist on neither
    node and no later window will ever contain them.
    """
    store = tmp_path / STORE_NAME
    con = _recorder(store)
    _add(con, 1000.0, 1.0)  # this node stopped here
    con.close()

    leader = tmp_path / "leader.db"
    lc = _recorder(leader)
    _add(lc, 900_000.0, 2.0)
    lc.close()

    # The leader's window starts long after this node's newest row.
    status = apply_window(tmp_path, export_since(leader, since_ts=800_000.0))
    assert status["state"] == "gap"
    assert "re-seed" in str(status["detail"])


def test_a_schema_mismatch_reaches_the_leader_rather_than_dying_on_the_host(
    tmp_path: pathlib.Path,
) -> None:
    """The follower has no logbook, no repairs panel and no entities.

    If this finding does not get written back to Valkey, nobody learns that
    the standby's history stopped advancing until they need it.
    """
    store = tmp_path / STORE_NAME
    con = _recorder(store, version=52)
    _add(con, 1000.0, 1.0)
    con.close()

    leader = tmp_path / "leader.db"
    lc = _recorder(leader, version=53)
    _add(lc, 2000.0, 2.0)
    lc.close()

    body = export_since(leader, since_ts=500.0).to_bytes()
    sealed = seal(derive_fileset_key(SECRET), body, aad=STATISTICS_AAD)
    client = FakeClient({f"ha:cluster_state_sync:{NS}:statistics:window": sealed})

    key_file = tmp_path / "key"
    key_file.write_text(derive_fileset_key(SECRET).hex())

    import custom_components.cluster_state_sync.scripts.statistics_pull as pull

    rc = main_with_client(pull, client, tmp_path, key_file)
    assert rc == 1

    reported = _open_status(client.written[f"ha:cluster_state_sync:{NS}:statistics:follower"])
    assert reported["state"] == "schema_mismatch"
    assert "52" in reported["detail"] and "53" in reported["detail"]

    rows = sqlite3.connect(store).execute("SELECT COUNT(*) FROM statistics").fetchone()[0]
    assert rows == 1, "rows were applied across a schema mismatch"


def test_a_window_moved_onto_the_statistics_key_from_elsewhere_will_not_open(
    tmp_path: pathlib.Path,
) -> None:
    """Sealed under its own label, so Valkey write access is not enough.

    Moving the fileset manifest's ciphertext onto this key needs no secret at
    all; without a distinct AAD it would be applied as something it is not.
    """
    from custom_components.cluster_state_sync.crypto import MANIFEST_AAD

    body = SyncPayload(schema_version=SCHEMA).to_bytes()
    wrong = seal(derive_fileset_key(SECRET), body, aad=MANIFEST_AAD)
    client = FakeClient({f"ha:cluster_state_sync:{NS}:statistics:window": wrong})
    key_file = tmp_path / "key"
    key_file.write_text(derive_fileset_key(SECRET).hex())

    import custom_components.cluster_state_sync.scripts.statistics_pull as pull

    assert main_with_client(pull, client, tmp_path, key_file) == 1
    reported = _open_status(client.written[f"ha:cluster_state_sync:{NS}:statistics:follower"])
    assert reported["state"] == "unauthenticated"


def test_a_leader_that_has_published_nothing_is_quiet_and_clean(
    tmp_path: pathlib.Path,
) -> None:
    """This runs on a timer. "Not configured yet" must not fill the journal."""
    client = FakeClient({})
    key_file = tmp_path / "key"
    key_file.write_text(derive_fileset_key(SECRET).hex())

    import custom_components.cluster_state_sync.scripts.statistics_pull as pull

    assert main_with_client(pull, client, tmp_path, key_file) == 0
    assert client.written == {}


def main_with_client(
    pull_module, client: FakeClient, config_dir: pathlib.Path, key_file: pathlib.Path
) -> int:
    """Run `main()` against the double, patching only the connect call."""
    original = pull_module.ValkeyClient.connect
    pull_module.ValkeyClient.connect = staticmethod(lambda **_kw: client)
    try:
        return main(
            [
                "--redis",
                "127.0.0.1:6379",
                "--namespace",
                NS,
                "--key-file",
                str(key_file),
                "--config",
                str(config_dir),
            ]
        )
    finally:
        pull_module.ValkeyClient.connect = original


def test_a_demoted_node_rebuilds_its_store_from_its_own_recorder(
    tmp_path: pathlib.Path,
) -> None:
    """Fail-back, with no operator involved.

    The swap MOVES the store into place as the recorder at promotion, so a node
    that has been leader and is standby again has no store -- but its own
    recorder holds every one of those statistics and everything it recorded
    while it was running. Asking someone to copy 300 MB across a second time
    would be an absurd price for a planned failback.
    """
    recorder = tmp_path / "home-assistant_v2.db"
    rc = _recorder(recorder)
    _add(rc, 1000.0, 1.0)
    _add(rc, 2000.0, 2.0)
    rc.close()

    leader = tmp_path / "leader.db"
    lc = _recorder(leader)
    _add(lc, 3000.0, 3.0)
    lc.close()

    status = apply_window(tmp_path, export_since(leader, since_ts=1500.0))
    assert status["state"] == "ok"
    assert status["rebuilt"] == 2, "the store was not rebuilt from the local recorder"

    store = sqlite3.connect(tmp_path / STORE_NAME)
    # Its own two rows, plus the one the leader has recorded since.
    assert store.execute("SELECT COUNT(*) FROM statistics").fetchone()[0] == 3
    store.close()


def test_a_node_with_neither_a_store_nor_a_recorder_asks_to_be_seeded(
    tmp_path: pathlib.Path,
) -> None:
    """A first-ever cold standby. Rebuilding is not an option here."""
    leader = tmp_path / "leader.db"
    lc = _recorder(leader)
    _add(lc, 2000.0, 2.0)
    lc.close()
    assert apply_window(tmp_path, export_since(leader))["state"] == "not_seeded"


# --- the alarm channel ----------------------------------------------------


async def test_the_standbys_findings_become_repairs_on_the_leader(
    hass, tmp_path: pathlib.Path
) -> None:
    """🚨 The standby has no logbook, no repairs panel and no entities.

    In the cold model its Home Assistant is stopped. Everything it discovers --
    a schema mismatch, a gap, a missing seed -- would die on a host nobody
    reads. AR-0040 is this project's founding incident and had exactly that
    shape: a step that silently did not work, whose only symptom was a log line.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    registry = ir.async_get(hass)

    backend.statistics_status = _sealed_status(
        {
            "state": "schema_mismatch",
            "detail": "this node's recorder schema is 52, the peer's is 53",
        }
    )
    await _surface_follower_status(hass, backend, SECRET)
    issue = registry.async_get_issue("cluster_state_sync", "statistics_schema_mismatch")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert "52" in issue.translation_placeholders["detail"]

    # And it clears itself once the standby recovers. The issue registry is
    # storage-backed and survives restarts, so a create without a matching
    # delete would leave a fixed cluster carrying an ERROR forever.
    backend.statistics_status = _sealed_status({"state": "ok", "applied": 12})
    await _surface_follower_status(hass, backend, SECRET)
    assert registry.async_get_issue("cluster_state_sync", "statistics_schema_mismatch") is None


async def test_a_silent_standby_does_not_look_like_a_broken_one(hass) -> None:
    """No status at all is not a schema mismatch.

    Raising an alarm for a standby that has simply never reported would train
    the operator to ignore the one that matters.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    backend.statistics_status = None
    await _surface_follower_status(hass, backend, SECRET)
    registry = ir.async_get(hass)
    for issue_id in ("statistics_schema_mismatch", "statistics_gap", "statistics_not_seeded"):
        assert registry.async_get_issue("cluster_state_sync", issue_id) is None


async def test_every_reportable_state_has_a_repair_and_a_translation(hass) -> None:
    """A repair whose translation key is missing renders as a blank card.

    The states come from `statistics_pull.py`; the strings come from
    `strings.json`. Nothing but a test connects the two files.
    """

    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    strings = json.loads(
        pathlib.Path("custom_components/cluster_state_sync/strings.json").read_text(
            encoding="utf-8"
        )
    )
    backend = FakeBackend()
    registry = ir.async_get(hass)

    for state, issue_id in (
        ("schema_mismatch", "statistics_schema_mismatch"),
        ("gap", "statistics_gap"),
        ("not_seeded", "statistics_not_seeded"),
    ):
        backend.statistics_status = _sealed_status({"state": state, "detail": "because"})
        await _surface_follower_status(hass, backend, SECRET)
        assert registry.async_get_issue("cluster_state_sync", issue_id) is not None
        assert issue_id in strings["issues"], f"{issue_id} would render as a blank card"
        assert "{detail}" in strings["issues"][issue_id]["description"]


def test_the_statistics_files_can_never_ride_along_in_the_go_bag() -> None:
    """A 484 MB seed inside the fileset would stop the go-bag entirely.

    `_candidates` walks fixed lists plus what `configuration.yaml` includes,
    so the config root is not swept today. An operator who adds the config
    directory to the extra paths changes that, and the failure is not a large
    go-bag -- it is `FilesetTooLarge`, which publishes NOTHING, so the standby
    stops ageing forward and only finds out at a promotion. AR-0041.
    """
    from custom_components.cluster_state_sync.const import (
        DEFAULT_FILESET_EXCLUSIONS,
        STATISTICS_DB_NAME,
        STATISTICS_SEED_NAME,
    )
    from custom_components.cluster_state_sync.fileset import is_excluded

    for name in (STATISTICS_DB_NAME, STATISTICS_SEED_NAME):
        assert is_excluded(name, DEFAULT_FILESET_EXCLUSIONS), f"{name} would be replicated"


# --- AR-0045: the status channel is sealed --------------------------------


async def test_a_status_written_without_the_cluster_key_is_ignored(hass) -> None:
    """🚨 AR-0045. This channel used to be plain JSON.

    The reasoning was that a follower with no cluster key must still be able to
    say "I could not apply". It does not survive contact with the code: the
    puller reads the key file *before* it connects, so a keyless follower never
    reaches the point of having anything to report. What the plain channel
    actually bought was a way for anyone with Valkey write access — and no
    secret at all — to put chosen text on the leader's repairs panel, at the
    exact moment an operator is anxious and looking for instructions.
    """
    import json as _json

    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    # Exactly what an attacker with Valkey write access can produce: a
    # well-formed status, unsealed.
    backend.statistics_status = _json.dumps(
        {"state": "schema_mismatch", "detail": "Call 0800-EVIL to fix your cluster"}
    ).encode("utf-8")

    await _surface_follower_status(hass, backend, SECRET)
    assert (
        ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_schema_mismatch")
        is None
    ), "an unauthenticated status reached the operator's repairs panel"


async def test_a_status_sealed_under_the_wrong_label_is_ignored(hass) -> None:
    """The window's ciphertext must not be replayable onto the status key."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status
    from custom_components.cluster_state_sync.crypto import STATISTICS_AAD

    backend = FakeBackend()
    backend.statistics_status = seal(
        derive_fileset_key(SECRET), b'{"state": "schema_mismatch"}', aad=STATISTICS_AAD
    )
    await _surface_follower_status(hass, backend, SECRET)
    assert (
        ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_schema_mismatch")
        is None
    )


# --- AR-0046: silence is measured -----------------------------------------


async def test_replication_that_has_stopped_no_longer_looks_healthy(hass) -> None:
    """🚨 AR-0046, and the reason it stings.

    The status key carries a TTL. When it expires the leader reads nothing,
    deletes every statistics issue, and a standby whose replication has DIED
    becomes indistinguishable from one that is working — because the quiet
    state is the good state.

    That is AR-0040's shape, in a mechanism written after AR-0040 taught this
    project exactly that lesson.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    backend.statistics_status = None
    # The standby's promoter IS beating, so the node is up — this is dead
    # replication, not a switched-off machine.
    backend.promoter_nodes = {"node-a", "node-b"}

    await _surface_follower_status(
        hass,
        backend,
        SECRET,
        node_id="node-a",
        stale_after=90 * 60,
        # AR-0057: silence only means something once we have been publishing
        # for longer than the window. These tests are about an ESTABLISHED
        # replication going quiet, so they say so.
        publishing_since=datetime.now(tz=UTC) - timedelta(hours=6),
    )
    issue = ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_stalled")
    assert issue is not None, "replication stopped and nothing said so"
    assert "node-b" in issue.translation_placeholders["detail"]
    assert "promoter is beating" in issue.translation_placeholders["detail"]


async def test_a_switched_off_standby_reads_differently_from_a_broken_one(hass) -> None:
    """Different causes, opposite fixes, so they must not share a message."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    backend.statistics_status = None
    backend.promoter_nodes = {"node-a"}  # only us

    await _surface_follower_status(
        hass,
        backend,
        SECRET,
        node_id="node-a",
        stale_after=90 * 60,
        # AR-0057: silence only means something once we have been publishing
        # for longer than the window. These tests are about an ESTABLISHED
        # replication going quiet, so they say so.
        publishing_since=datetime.now(tz=UTC) - timedelta(hours=6),
    )
    issue = ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_stalled")
    assert issue is not None
    assert "switched off" in issue.translation_placeholders["detail"]


async def test_a_follower_reporting_on_time_raises_nothing(hass) -> None:
    """The alarm must stay quiet on a healthy cluster or it will be ignored."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    backend.statistics_status = _sealed_status({"state": "ok", "applied": 12})
    backend.promoter_nodes = {"node-a", "node-b"}

    await _surface_follower_status(
        hass,
        backend,
        SECRET,
        node_id="node-a",
        stale_after=90 * 60,
        # AR-0057: silence only means something once we have been publishing
        # for longer than the window. These tests are about an ESTABLISHED
        # replication going quiet, so they say so.
        publishing_since=datetime.now(tz=UTC) - timedelta(hours=6),
    )
    assert ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_stalled") is None


async def test_the_stall_alarm_clears_itself_when_the_follower_returns(hass) -> None:
    """The issue registry survives restarts, so creating without deleting
    would leave a recovered cluster carrying a warning forever."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    backend.statistics_status = None
    backend.promoter_nodes = {"node-a", "node-b"}
    await _surface_follower_status(
        hass,
        backend,
        SECRET,
        node_id="node-a",
        stale_after=90 * 60,
        # AR-0057: silence only means something once we have been publishing
        # for longer than the window. These tests are about an ESTABLISHED
        # replication going quiet, so they say so.
        publishing_since=datetime.now(tz=UTC) - timedelta(hours=6),
    )
    assert ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_stalled")

    backend.statistics_status = _sealed_status({"state": "ok", "applied": 3})
    await _surface_follower_status(
        hass,
        backend,
        SECRET,
        node_id="node-a",
        stale_after=90 * 60,
        # AR-0057: silence only means something once we have been publishing
        # for longer than the window. These tests are about an ESTABLISHED
        # replication going quiet, so they say so.
        publishing_since=datetime.now(tz=UTC) - timedelta(hours=6),
    )
    assert ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_stalled") is None


async def test_switching_replication_on_does_not_accuse_a_correct_setup(hass) -> None:
    """🚨 AR-0057. Silence only means something once there is something to be
    silent about.

    On the pass that first enables this, no follower has had a window to fetch
    yet — the status key does not exist, and a missing status counts as stale.
    Without a guard the leader raises "replication has stalled" against a
    perfectly correct installation, for up to three publish intervals (90
    minutes at the default), on the one day its owner is least able to tell a
    real fault from a new one.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    backend.statistics_status = None  # nothing has reported yet
    backend.promoter_nodes = {"node-a", "node-b"}

    await _surface_follower_status(
        hass,
        backend,
        SECRET,
        node_id="node-a",
        stale_after=90 * 60,
        publishing_since=datetime.now(tz=UTC),  # we started publishing just now
    )
    assert ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_stalled") is None, (
        "a brand-new, correct setup was accused of a stalled replication"
    )


async def test_a_follower_silent_long_after_publishing_began_still_alarms(hass) -> None:
    """The guard must not become a way to never alarm at all.

    Once we have been publishing longer than the staleness window, a follower
    that has still said nothing is a real fault — which is the whole point of
    AR-0046.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.cluster_state_sync import _surface_follower_status

    backend = FakeBackend()
    backend.statistics_status = None
    backend.promoter_nodes = {"node-a", "node-b"}

    await _surface_follower_status(
        hass,
        backend,
        SECRET,
        node_id="node-a",
        stale_after=90 * 60,
        publishing_since=datetime.now(tz=UTC) - timedelta(hours=6),
    )
    assert (
        ir.async_get(hass).async_get_issue("cluster_state_sync", "statistics_stalled") is not None
    ), "a follower silent for six hours of publishing did not alarm"
