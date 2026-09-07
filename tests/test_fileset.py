"""Scanning and manifest construction (design §3, §4).

The measurements these tests encode, taken on node-a 2026-08-29:
`.storage` is 109 MB of which 81 MB is nine hand-made `*.bak-*` copies; the
live set is 28.1 MB across 99 files; `core.restore_state` churns constantly and
belongs to tier 2, not here.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
import os
import pathlib
from unittest.mock import patch

from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cluster_state_sync.const import (
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_FILESET_ENABLED,
    CONF_FILESET_HOT_INTERVAL,
    CONF_LEADERSHIP_ENTITY,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_REDIS_HOST,
    DATA_FILESET,
    DEFAULT_FILESET_EXCLUSIONS,
    DEFAULT_FILESET_HOT_INTERVAL,
    DEFAULT_FILESET_MAX_BYTES,
    DOMAIN,
    LEADERSHIP_ENTITY,
)
from custom_components.cluster_state_sync.crypto import (
    MANIFEST_AAD,
    blob_aad,
    derive_fileset_key,
    open_sealed,
)
from custom_components.cluster_state_sync.fileset import (
    FilesetPublisher,
    FilesetTooLarge,
    Manifest,
    is_excluded,
    scan,
)
from tests.fakes import FakeBackend

SECRET = "a-test-cluster-secret"


def _tree(root: pathlib.Path) -> pathlib.Path:
    """A miniature of tiger1's config directory."""
    storage = root / ".storage"
    storage.mkdir(parents=True)
    (storage / "auth").write_text('{"refresh_tokens": ["t1"]}')
    (storage / "core.config_entries").write_text('{"entries": []}')
    (storage / "core.restore_state").write_text('{"volatile": true}')
    (storage / "core.entity_registry").write_text('{"entities": []}')
    (storage / "core.entity_registry.bak-zivafix-20260614").write_text("x" * 4096)
    (root / "custom_components" / "meross_lan").mkdir(parents=True)
    (root / "custom_components" / "meross_lan" / "__init__.py").write_text("DOMAIN=1")
    (root / "configuration.yaml").write_text("homeassistant:\n")
    (root / "home-assistant_v2.db").write_text("SQLITE" * 100)
    return root


def test_hand_made_backups_are_excluded(tmp_path: pathlib.Path) -> None:
    """81 of tiger1's 109 MB are operator `.bak-*` copies. Tier 1's rsync
    allow-list is `.storage/***` and would ship all of them, forever."""
    result = scan(_tree(tmp_path), secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    assert not any("bak-zivafix" in p for p in result.entries)


def test_core_restore_state_is_excluded(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: dropping it from the
    defaults. It is tier 2's job; replicating the file would have it fighting
    the Valkey restore over the same entities."""
    result = scan(_tree(tmp_path), secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    assert ".storage/core.restore_state" not in result.entries


def test_the_recorder_database_is_never_scanned(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: walking the whole config
    directory instead of the named roots. An rsync of a live SQLite file
    replicates corruption, not data (ADR-001 tier 3)."""
    result = scan(_tree(tmp_path), secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    assert "home-assistant_v2.db" not in result.entries


def test_identity_and_bulk_are_both_collected(tmp_path: pathlib.Path) -> None:
    result = scan(_tree(tmp_path), secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    assert ".storage/auth" in result.entries
    assert ".storage/core.config_entries" in result.entries
    assert "custom_components/meross_lan/__init__.py" in result.entries
    assert "configuration.yaml" in result.entries


def test_unchanged_files_are_not_re_read(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: ignoring `previous` and
    reading every file every pass. 5,703 files in custom_components change
    weekly at most; re-shipping them every 60s is the thing this avoids."""
    root = _tree(tmp_path)
    first = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    second = scan(
        root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS, previous=first.entries
    )
    assert second.entries == first.entries
    assert second.bodies == {}


def test_a_changed_file_is_re_read_and_gets_a_new_ref(tmp_path: pathlib.Path) -> None:
    root = _tree(tmp_path)
    first = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    (root / ".storage" / "auth").write_text('{"refresh_tokens": ["t1", "t2"]}')
    second = scan(
        root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS, previous=first.entries
    )
    assert second.entries[".storage/auth"].ref != first.entries[".storage/auth"].ref
    assert second.entries[".storage/auth"].ref in second.bodies


def test_a_same_length_rewrite_is_still_re_read(tmp_path: pathlib.Path) -> None:
    """C3. Production change that would make this fail: dropping `mtime_ns`
    from the change heuristic, so `scan` compares size and mode alone.

    This is not a hypothetical. `.storage/auth_provider.homeassistant` holds a
    bcrypt hash, and bcrypt hashes are fixed length -- changing the owner's
    password leaves the byte count identical and the mode untouched. On size
    and mode alone the standby went on publishing the *old* password, and the
    only thing that ever healed it was the leader restarting and resetting
    `_current`. `.storage/http` and every fixed-length rotating token are the
    same shape. rsync's quick check is size **and** mtime for this reason.
    """
    root = _tree(tmp_path)
    stored = root / ".storage" / "auth_provider.homeassistant"
    stored.write_text('{"hash": "AAAAAAAAAAAAAAAAAAAAAAAA"}')
    first = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)

    # Same length, same mode, different bytes -- a password change, exactly.
    stored.write_text('{"hash": "BBBBBBBBBBBBBBBBBBBBBBBB"}')
    # Advanced explicitly rather than relying on the second write landing in a
    # later filesystem timestamp tick. The kernel's granularity can be coarser
    # than the gap between two writes in a test, and a flaky assertion about
    # the *filesystem* would say nothing about the heuristic under test. (That
    # granularity is a real, accepted limit of any size+mtime quick check,
    # rsync's included: a same-length rewrite inside one tick is still missed.)
    os.utime(stored, ns=(stored.stat().st_atime_ns, stored.stat().st_mtime_ns + 1_000_000_000))
    rel = ".storage/auth_provider.homeassistant"
    assert stored.stat().st_size == first.entries[rel].size
    second = scan(
        root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS, previous=first.entries
    )

    assert second.entries[rel].ref != first.entries[rel].ref, (
        "the new password was never noticed; the standby keeps the old one"
    )
    # And the body was actually re-read, so there is a blob to publish. A ref
    # that changed with no body behind it would publish a manifest pointing at
    # a blob that does not exist.
    assert second.bodies[second.entries[rel].ref] == stored.read_bytes()


def test_a_file_touched_without_being_changed_keeps_its_ref(tmp_path: pathlib.Path) -> None:
    """The other side of C3: mtime is a re-read trigger, not an identity.

    Content addressing is what decides whether anything is *published*, so a
    `touch` costs one read and produces the same ref -- it must not produce a
    second copy of the same bytes under a new reference, which would defeat the
    blob GC's `keep` set.
    """
    root = _tree(tmp_path)
    first = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    stored = root / ".storage" / "auth"
    os.utime(stored, ns=(stored.stat().st_atime_ns, stored.stat().st_mtime_ns + 1_000_000_000))

    second = scan(
        root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS, previous=first.entries
    )

    assert second.entries[".storage/auth"].ref == first.entries[".storage/auth"].ref
    assert second.entries[".storage/auth"].mtime_ns != first.entries[".storage/auth"].mtime_ns


def test_identical_content_shares_one_blob(tmp_path: pathlib.Path) -> None:
    """Content-addressing: two files with the same bytes are stored once."""
    root = _tree(tmp_path)
    (root / ".storage" / "twin_a").write_text("same")
    (root / ".storage" / "twin_b").write_text("same")
    result = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    assert result.entries[".storage/twin_a"].ref == result.entries[".storage/twin_b"].ref


def test_over_the_size_cap_it_refuses(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: removing the cap.

    AR-0041 shipped 92.5 GB to copy 108 MB. A payload that can silently grow is
    how that happens; refusing loudly is the fix.
    """
    root = _tree(tmp_path)
    (root / ".storage" / "huge").write_bytes(b"x" * 8192)
    with pytest.raises(FilesetTooLarge):
        scan(
            root,
            secret=SECRET,
            exclusions=DEFAULT_FILESET_EXCLUSIONS,
            max_bytes=1024,
        )


def test_exclusion_globs_match_any_path_component() -> None:
    """Pre-flight Ruling C. Matching only the basename would drop
    `.storage/core.entity_registry.bak-*` while happily shipping every file
    inside `custom_components/localtuya.bak-5.2.3-20260601-154152/` — which is
    a real directory on tiger1, inside the largest payload we replicate.

    Production change that would make this fail: `fnmatch(path.name, ...)`.
    """
    assert is_excluded(".storage/core.entity_registry.bak-x-20260614", ("*.bak-*",))
    assert is_excluded(".storage/core.restore_state", ("core.restore_state",))
    assert is_excluded(
        "custom_components/localtuya.bak-5.2.3-20260601-154152/__init__.py",
        ("*.bak-*",),
    )
    assert not is_excluded(".storage/core.entity_registry", ("*.bak-*",))
    assert not is_excluded(".storage/auth", ("*.bak-*", "core.restore_state"))


def test_a_stale_backup_directory_is_skipped_whole(tmp_path: pathlib.Path) -> None:
    """The 100 MB `custom_components` tree is where a forgotten `.bak-` copy
    costs the most, and it is a directory rather than a file."""
    root = _tree(tmp_path)
    stale = root / "custom_components" / "localtuya.bak-5.2.3-20260601-154152"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("DOMAIN='old'")
    result = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    assert not any("localtuya.bak" in p for p in result.entries)


def test_manifest_round_trips_through_json(tmp_path: pathlib.Path) -> None:
    result = scan(_tree(tmp_path), secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    manifest = Manifest(
        generation=7, node="tiger1", ts="2026-08-29T10:00:00+00:00", entries=result.entries
    )
    assert Manifest.from_json(manifest.to_json()) == manifest


def test_manifest_json_is_canonical(tmp_path: pathlib.Path) -> None:
    """Production change that would make this fail: dropping `sort_keys`.

    An unstable serialisation means two nodes computing the same manifest
    produce different bytes, and every publish looks like a change.

    This used to assert `m.to_json() == m.to_json()` on the *same object*,
    which holds for any deterministic serialiser -- the reviewer set
    `sort_keys=False` and 23 tests still passed. What has to be pinned is that
    two manifests with the same *contents* in a different insertion order
    serialise identically, because that is the only difference two nodes can
    actually produce: `scan` walks `sorted(rglob)` per root, and the roots are
    visited in `REPLICATED_DIRS` order, so a node with a different set of roots
    present builds the same dict with different insertion order.
    """
    result = scan(_tree(tmp_path), secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)
    assert len(result.entries) > 1, "one entry cannot have two insertion orders"

    forwards = Manifest(generation=1, node="n", ts="t", entries=dict(result.entries))
    backwards = Manifest(
        generation=1, node="n", ts="t", entries=dict(reversed(list(result.entries.items())))
    )

    assert list(forwards.entries) != list(backwards.entries), "the orders must actually differ"
    assert forwards.to_json() == backwards.to_json()
    assert json.loads(forwards.to_json())["generation"] == 1


def _publisher(root: pathlib.Path, **kw: object) -> FilesetPublisher:
    return FilesetPublisher(
        FakeBackend(),
        config_dir=str(root),
        node_id="tiger1",
        secret=SECRET,
        exclusions=DEFAULT_FILESET_EXCLUSIONS,
        max_bytes=DEFAULT_FILESET_MAX_BYTES,
        **kw,  # type: ignore[arg-type]
    )


async def test_publishing_stores_sealed_bodies_not_plaintext(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: writing bodies unsealed.

    `.storage/auth` holds the refresh tokens. Valkey on node-ops is shared
    infrastructure; it must only ever hold an opaque blob.
    """
    pub = _publisher(_tree(tmp_path))
    await pub.async_publish()
    stored = list(pub.backend.blobs.values())  # type: ignore[attr-defined]
    assert stored
    assert not any(b"refresh_tokens" in blob for blob in stored)


async def test_a_published_blob_opens_with_the_derived_key(
    tmp_path: pathlib.Path,
) -> None:
    root = _tree(tmp_path)
    pub = _publisher(root)
    await pub.async_publish()
    manifest = Manifest.from_json(
        open_sealed(
            derive_fileset_key(SECRET),
            pub.backend.fileset_manifest,  # type: ignore[attr-defined]
            aad=MANIFEST_AAD,
        ).decode()
    )
    ref = manifest.entries[".storage/auth"].ref
    body = open_sealed(
        derive_fileset_key(SECRET),
        pub.backend.blobs[ref],  # type: ignore[attr-defined]
        aad=blob_aad(ref),
    )
    assert body == (root / ".storage" / "auth").read_bytes()


async def test_the_manifest_itself_is_sealed(tmp_path: pathlib.Path) -> None:
    """It is only paths and hashes, but those leak which integrations run."""
    pub = _publisher(_tree(tmp_path))
    await pub.async_publish()
    assert b"core.config_entries" not in pub.backend.fileset_manifest  # type: ignore[attr-defined]


async def test_an_unchanged_tree_writes_no_new_blobs(tmp_path: pathlib.Path) -> None:
    pub = _publisher(_tree(tmp_path))
    await pub.async_publish()
    second = await pub.async_publish()
    assert second.blobs_written == 0


async def test_the_generation_increments_on_every_publish(
    tmp_path: pathlib.Path,
) -> None:
    pub = _publisher(_tree(tmp_path))
    assert (await pub.async_publish()).generation == 1
    assert (await pub.async_publish()).generation == 2


async def test_gc_keeps_the_current_and_previous_generation(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: pruning to the current
    generation only, which removes the rollback copy; or never pruning, which
    lets Valkey grow without bound.

    Both assertions are load-bearing. `second.pruned == 0` is what actually
    tells "keep current + previous" apart from "keep current only": under the
    keep-current-only regression the first generation's now-superseded
    `.storage/auth` blob is pruned a publish early, on the *second* publish,
    not the third. The final `gen2_auth_ref in ...blobs` checks the rollback
    copy directly rather than inferring its survival from a count.
    """
    root = _tree(tmp_path)
    pub = _publisher(root)
    await pub.async_publish()
    (root / ".storage" / "auth").write_text('{"refresh_tokens": ["t1", "t2"]}')
    second = await pub.async_publish()
    assert second.pruned == 0
    manifest_gen2 = Manifest.from_json(
        open_sealed(
            derive_fileset_key(SECRET),
            pub.backend.fileset_manifest,  # type: ignore[attr-defined]
            aad=MANIFEST_AAD,
        ).decode()
    )
    gen2_auth_ref = manifest_gen2.entries[".storage/auth"].ref
    (root / ".storage" / "auth").write_text('{"refresh_tokens": ["t1", "t2", "t3"]}')
    result = await pub.async_publish()
    assert result.pruned == 1
    assert gen2_auth_ref in pub.backend.blobs  # type: ignore[attr-defined]


async def test_over_the_cap_it_publishes_nothing(tmp_path: pathlib.Path) -> None:
    """Refusing must leave the last good generation intact. A partial publish
    would be worse than a stale one."""
    root = _tree(tmp_path)
    pub = _publisher(root)
    await pub.async_publish()
    good = pub.backend.fileset_manifest  # type: ignore[attr-defined]
    pub._max_bytes = 16  # noqa: SLF001
    result = await pub.async_publish()
    assert result.skipped_reason == "too_large"
    assert pub.backend.fileset_manifest == good  # type: ignore[attr-defined]


async def test_the_age_gauge_does_not_advance_on_a_refusal(tmp_path: pathlib.Path) -> None:
    """Test-sweep item: setting `last_success_at` on the `too_large` path left
    the suite green, so the age gauge's honesty on that path was unasserted.

    `FilesetAgeSensor` reads this to answer "how long since the fileset was
    last published". Stamping it on a pass that published nothing reports a
    fresh fileset that does not exist — the reassuring-but-wrong number the
    whole sensor exists to avoid.
    """
    root = _tree(tmp_path)
    pub = _publisher(root)
    await pub.async_publish()
    after_success = pub.last_success_at
    assert after_success is not None

    pub._max_bytes = 16  # noqa: SLF001
    await pub.async_publish()

    assert pub.last_success_at == after_success

    # And a publisher that has *never* succeeded still reports nothing, rather
    # than a zero that would read as "just published".
    fresh = _publisher(root)
    fresh._max_bytes = 16  # noqa: SLF001
    await fresh.async_publish()
    assert fresh.last_success_at is None


async def test_a_refusal_is_logged_at_error_on_the_transition(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """I2. Production change that would make this fail: returning the
    `too_large` result with no log at all, which is what it did.

    Publishing stops dead here and the only downstream signal was a `stale`
    marker at the next promotion — the moment it is least useful. Logged on the
    transition rather than every pass, because this runs once a minute forever
    and a line a minute is a line nobody reads.
    """
    root = _tree(tmp_path)
    pub = _publisher(root)
    await pub.async_publish()
    pub._max_bytes = 16  # noqa: SLF001

    with caplog.at_level(logging.ERROR, logger="custom_components.cluster_state_sync.fileset"):
        await pub.async_publish()
        first = [r for r in caplog.records if r.levelno >= logging.ERROR]
        caplog.clear()
        await pub.async_publish()
        second = [r for r in caplog.records if r.levelno >= logging.ERROR]

    assert len(first) == 1, "the refusal must say so once"
    assert "REFUSED" in first[0].getMessage()
    assert second == [], "and must not repeat it once a minute forever"


async def test_recovering_from_a_refusal_is_reported_and_re_arms_the_alarm(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half: an operator who raises the cap has to be able to tell
    that publishing resumed, and a second excursion has to be loud again rather
    than swallowed by the first one's transition guard."""
    root = _tree(tmp_path)
    pub = _publisher(root)
    pub._max_bytes = 16  # noqa: SLF001
    await pub.async_publish()

    pub._max_bytes = DEFAULT_FILESET_MAX_BYTES  # noqa: SLF001
    with caplog.at_level(logging.INFO, logger="custom_components.cluster_state_sync.fileset"):
        result = await pub.async_publish()
    assert result.skipped_reason is None
    assert "recovered" in caplog.text

    caplog.clear()
    pub._max_bytes = 16  # noqa: SLF001
    with caplog.at_level(logging.ERROR, logger="custom_components.cluster_state_sync.fileset"):
        await pub.async_publish()
    assert "REFUSED" in caplog.text


async def test_a_refusal_shows_on_the_degraded_sensor(tmp_path: pathlib.Path) -> None:
    """I2, the part that reaches a human. A log line is what AR-0040 was.

    The degraded binary sensor already exists for the other end of the same
    wire — a follower that came up on a bad go-bag — so a leader refusing to
    publish belongs on it too, with an attribute saying which of the two it is.
    """
    from custom_components.cluster_state_sync.binary_sensor import FilesetDegradedBinarySensor

    root = _tree(tmp_path)
    pub = _publisher(root)
    sensor = FilesetDegradedBinarySensor.__new__(FilesetDegradedBinarySensor)
    sensor._degraded = False  # noqa: SLF001
    sensor._publisher = pub  # noqa: SLF001

    await pub.async_publish()
    assert sensor.is_on is False
    assert sensor.extra_state_attributes["publish_skipped_reason"] is None

    pub._max_bytes = 16  # noqa: SLF001
    await pub.async_publish()

    assert sensor.is_on is True
    assert sensor.extra_state_attributes["publish_skipped_reason"] == "too_large"
    assert sensor.extra_state_attributes["boot_marker"] is False


def test_a_symlink_inside_a_replicated_root_is_not_published(
    tmp_path: pathlib.Path,
) -> None:
    """Test-sweep item: dropping `not path.is_symlink()` from `_candidates`
    left the suite green.

    `Path.is_file()` follows symlinks, so without the guard a link is scanned
    as though it were the file it points at — its *target's* bytes are sealed
    and published under the link's path, and the pull writes a real file where
    a link used to be. Pointed outside the config directory (`/etc/shadow`,
    a mounted secret) that is an exfiltration path through a feature whose
    entire job is to copy credentials off this host.
    """
    root = _tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours to replicate")
    (root / ".storage" / "linked").symlink_to(outside)
    (root / "custom_components" / "meross_lan" / "link.py").symlink_to(outside)

    result = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)

    assert ".storage/linked" not in result.entries
    assert "custom_components/meross_lan/link.py" not in result.entries
    assert not any(b"not ours to replicate" in body for body in result.bodies.values())


def test_a_symlinked_top_level_file_is_not_published(tmp_path: pathlib.Path) -> None:
    """`REPLICATED_FILES` has its own `is_symlink` guard, on its own loop.
    Losing one and keeping the other is the likelier regression of the two."""
    root = _tree(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret: value")
    (root / "automations.yaml").symlink_to(outside)

    result = scan(root, secret=SECRET, exclusions=DEFAULT_FILESET_EXCLUSIONS)

    assert "automations.yaml" not in result.entries


# ---------------------------------------------------------------------------
# Task 5 — wiring the publisher into `async_setup_entry`
# ---------------------------------------------------------------------------
#
# These drive the *real* config-entry setup rather than a local copy of the
# leader guard. A helper that reimplements "if is_leader: publish" would pass
# whether or not `__init__.py` ever wires the publisher in at all — it is not
# evidence that the production guard exists, still less that it sits on the
# leader's side. Following the pattern in test_gating.py: leadership is
# steered through real config (`always` vs. an `entity` source pointed at an
# entity that does not exist, which fails closed to follower) rather than by
# mocking `LeadershipMonitor` itself.


def _fileset_entry(**overrides: object) -> MockConfigEntry:
    data: dict[str, object] = {
        CONF_REDIS_HOST: "valkey.invalid",
        CONF_CLUSTER_NAMESPACE: "testns",
        CONF_NODE_ID: "tiger1",
        CONF_CLUSTER_SECRET: SECRET,
        CONF_FILESET_ENABLED: True,
        CONF_FILESET_HOT_INTERVAL: 5,
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data)


async def test_a_follower_never_publishes(hass) -> None:
    """Production change that would make this fail: publishing on the same
    unconditional path as the scheduled flush, i.e. dropping the leader guard
    out of `_scheduled_fileset`.

    A follower that published would overwrite the leader's identity with its
    own — the standby's `.storage`, which after F3 is a cleared-down instance
    with no refresh tokens at all.
    """
    backend = FakeBackend()
    entry = _fileset_entry(
        # Fails closed to "follower": the entity does not exist.
        **{
            CONF_LEADERSHIP_SOURCE: LEADERSHIP_ENTITY,
            CONF_LEADERSHIP_ENTITY: "input_boolean.ha_is_master",
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    # `_scheduled_fileset` runs as a background task (HA's time-interval
    # tracker calls `async_run_hass_job(..., background=True)`), and
    # `FilesetPublisher.async_publish` genuinely suspends at a real
    # `run_in_executor` call — so a plain `async_block_till_done()` returns
    # before the publish has actually run.
    await hass.async_block_till_done(wait_background_tasks=True)

    assert backend.fileset_manifest is None


async def test_a_leader_publishes(hass) -> None:
    """The other half: on the leader, the same real timer does publish."""
    backend = FakeBackend()
    entry = _fileset_entry()  # default leadership source is "always"
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert backend.fileset_manifest is not None


async def test_publisher_absent_when_fileset_not_enabled(hass) -> None:
    """`fileset_enabled` defaults to False. An operator who never opted in
    must get neither a publisher instance nor a timer — on a node that is the
    leader throughout, so a stray timer would have every chance to fire.

    Production change that would make this fail: constructing the publisher,
    or registering its interval, unconditionally.
    """
    backend = FakeBackend()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: "tiger1",
            CONF_CLUSTER_SECRET: SECRET,
            # CONF_FILESET_ENABLED intentionally absent — the default.
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime[DATA_FILESET] is None

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_FILESET_HOT_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert backend.fileset_manifest is None


async def test_no_secret_means_no_publisher_and_no_publish(
    hass, caplog: pytest.LogCaptureFixture
) -> None:
    """`fileset_enabled: True` with no cluster secret must not construct a
    publisher.

    Consequence that makes this worth its own test (per review): `crypto.
    derive_fileset_key` runs HKDF over whatever string it is handed,
    including an empty one — it does not validate the secret. An empty
    secret still derives a deterministic, independently-computable AES-256
    key (confirmed against crypto.py directly: `derive_fileset_key("")`
    always returns the same 32 bytes). If the `if not secret:` guard in
    `async_setup_entry` were ever weakened, inverted, or refactored away,
    `FilesetPublisher` would construct successfully and publish every
    credential in `.storage` — refresh tokens included — sealed with a key
    anyone holding no secret at all can derive themselves. That guard is the
    only thing standing between "opted in but never configured a secret" and
    a credential leak; nothing downstream would catch it.

    Production change that would make this fail: `if not secret:` weakened,
    inverted, or removed.
    """
    backend = FakeBackend()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_REDIS_HOST: "valkey.invalid",
            CONF_CLUSTER_NAMESPACE: "testns",
            CONF_NODE_ID: "tiger1",
            CONF_FILESET_ENABLED: True,
            CONF_FILESET_HOT_INTERVAL: 5,
            # CONF_CLUSTER_SECRET intentionally absent.
        },
    )
    entry.add_to_hass(hass)
    with (
        caplog.at_level(logging.ERROR),
        patch("custom_components.cluster_state_sync.RedisBackend", return_value=backend),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert "no cluster secret is set" in caplog.text.lower()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime[DATA_FILESET] is None

    # No timer either: nothing should be quietly publishing on an interval
    # despite the missing secret.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert backend.fileset_manifest is None


def test_the_publisher_carries_files_the_config_includes(tmp_path) -> None:
    """Production change that would make this fail: going back to a fixed list.

    node-b promoted into recovery mode on 2026-09-04 because
    `configs/customize.yaml` was referenced by `configuration.yaml` and was not
    in the replicated set. The promotion, the swap and the identity graft all
    reported success.
    """
    from custom_components.cluster_state_sync.fileset import _candidates

    (tmp_path / "configuration.yaml").write_text(
        "homeassistant:\n  customize: !include configs/customize.yaml\n"
        "themes: !include_dir_merge_named themes\n"
    )
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/customize.yaml").write_text("a: 1\n")
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes/dark.yaml").write_text("b: 2\n")

    names = {rel for rel, _ in _candidates(tmp_path)}
    assert "configs/customize.yaml" in names
    assert "themes/dark.yaml" in names


def test_a_file_is_never_published_twice(tmp_path) -> None:
    """The floor and the include scan overlap — `configuration.yaml` is in both
    — and a duplicate entry would be published twice and counted twice."""
    from custom_components.cluster_state_sync.fileset import _candidates

    (tmp_path / "configuration.yaml").write_text("a: !include automations.yaml\n")
    (tmp_path / "automations.yaml").write_text("[]\n")

    names = [rel for rel, _ in _candidates(tmp_path)]
    assert len(names) == len(set(names)), f"duplicates: {names}"


def test_an_unparseable_config_still_publishes_the_floor(tmp_path) -> None:
    """A degraded go-bag beats no go-bag. A configuration too broken to scan
    must not stop the leader publishing what it can."""
    from custom_components.cluster_state_sync.fileset import _candidates

    (tmp_path / "configuration.yaml").write_text("this: [is: not: valid\n")
    (tmp_path / "automations.yaml").write_text("[]\n")

    names = {rel for rel, _ in _candidates(tmp_path)}
    assert "automations.yaml" in names
