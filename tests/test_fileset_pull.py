"""The follower's pull (design §6).

Driven with a dict-backed double rather than a real server: the pull's
interesting behaviour is what it does to the filesystem on partial and
corrupt input, and none of that needs a socket.
"""

from __future__ import annotations

import base64
import json
import pathlib
import ssl
import stat

import pytest

from custom_components.cluster_state_sync.crypto import (
    MANIFEST_AAD,
    blob_aad,
    blob_ref,
    derive_fileset_key,
    seal,
)
from custom_components.cluster_state_sync.fileset import FileEntry, Manifest
from custom_components.cluster_state_sync.scripts.fileset_pull import (
    DEFAULT_DB,
    PASSWORD_ENV,
    PullError,
    ValkeyClient,
    main,
    restore_fileset,
)
from custom_components.cluster_state_sync.scripts.resp import RespError, RespReader, tls_context

SECRET = "a-test-cluster-secret"
NS = "test"


class FakeClient:
    """Just enough Redis: `get` and `mget`, returning base64 strings."""

    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = {k: base64.b64encode(v).decode("ascii") for k, v in values.items()}
        self.closed = False

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.values.get(k) for k in keys]

    def close(self) -> None:
        self.closed = True


def _published(files: dict[str, bytes], modes: dict[str, int] | None = None) -> FakeClient:
    key = derive_fileset_key(SECRET)
    entries = {}
    values: dict[str, bytes] = {}
    for path, body in files.items():
        ref = blob_ref(SECRET, body)
        entries[path] = {
            "ref": ref,
            "size": len(body),
            "mode": (modes or {}).get(path, 0o100644),
        }
        values[f"ha:cluster_state_sync:{NS}:fileset:blob:{ref}"] = seal(
            key, body, aad=blob_aad(ref)
        )
    manifest = json.dumps(
        {"generation": 3, "node": "tiger1", "ts": "2026-08-29T10:00:00+00:00", "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
    )
    values[f"ha:cluster_state_sync:{NS}:fileset:manifest"] = seal(
        key, manifest.encode(), aad=MANIFEST_AAD
    )
    return FakeClient(values)


def test_a_clean_pull_writes_every_file(tmp_path: pathlib.Path) -> None:
    client = _published(
        {
            ".storage/auth": b'{"refresh_tokens": ["t1"]}',
            "configuration.yaml": b"homeassistant:\n",
        }
    )
    status = restore_fileset(
        client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path
    )
    assert status.generation == 3
    restored = (tmp_path / "storage" / ".storage" / "auth").read_bytes()
    assert restored == b'{"refresh_tokens": ["t1"]}'
    assert (tmp_path / "storage" / "configuration.yaml").exists()


def test_a_status_file_records_the_generation(tmp_path: pathlib.Path) -> None:
    """The swap script reads this to decide fresh / stale / missing."""
    client = _published({".storage/auth": b"x"})
    restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["generation"] == 3
    assert status["ts"]


def test_a_missing_blob_leaves_the_previous_staging_untouched(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: writing files as they
    arrive instead of into .incoming.

    A partial go-bag is worse than a stale one — the swap would install a
    `.storage` missing files Home Assistant needs, and D4 says we promote
    anyway.
    """
    good = _published({".storage/auth": b"good"})
    restore_fileset(good, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)

    broken = _published({".storage/auth": b"new", ".storage/other": b"x"})
    del broken.values[next(k for k in broken.values if "blob:" in k)]
    with pytest.raises(PullError):
        restore_fileset(broken, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)

    assert (tmp_path / "storage" / ".storage" / "auth").read_bytes() == b"good"


def test_a_tampered_blob_aborts_the_pull(tmp_path: pathlib.Path) -> None:
    """The GCM tag is the whole point: a blob that does not authenticate must
    stop the pull, not land on disk."""
    client = _published({".storage/auth": b"secret"})
    bad = next(k for k in client.values if "blob:" in k)
    raw = bytearray(base64.b64decode(client.values[bad]))
    raw[-1] ^= 0x01
    client.values[bad] = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)


def test_a_tampered_blob_raises_the_verify_failed_marker(
    tmp_path: pathlib.Path,
) -> None:
    """Pre-flight Ruling A. The swap script branches on this file to decide
    "corrupt, install nothing" versus "stale, install anyway" — and only the
    pull can tell those apart, because only it sees the GCM tag fail.

    Production change that would make this fail: raising without writing the
    marker, which leaves the swap's verify_failed branch dead and lets a
    corrupt go-bag be installed.
    """
    client = _published({".storage/auth": b"secret"})
    bad = next(k for k in client.values if "blob:" in k)
    raw = bytearray(base64.b64decode(client.values[bad]))
    raw[-1] ^= 0x01
    client.values[bad] = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)
    assert (tmp_path / "verify_failed").exists()


def test_a_missing_blob_does_not_raise_the_verify_failed_marker(
    tmp_path: pathlib.Path,
) -> None:
    """A blob the GC removed is not corruption. Conflating the two would have
    the swap refuse to install a perfectly good older go-bag."""
    client = _published({".storage/auth": b"x", ".storage/other": b"y"})
    del client.values[next(k for k in client.values if "blob:" in k)]
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)
    assert not (tmp_path / "verify_failed").exists()


def test_a_clean_pull_clears_a_previous_verify_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Otherwise one bad publish poisons every promotion that follows it."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "verify_failed").write_text("stale")
    client = _published({".storage/auth": b"good"})
    restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)
    assert not (tmp_path / "verify_failed").exists()


def test_no_manifest_at_all_is_an_error_not_an_empty_staging(
    tmp_path: pathlib.Path,
) -> None:
    """Production change that would make this fail: treating an absent manifest
    as "nothing to do" and writing an empty tree. The swap would then install
    an empty `.storage` and the promoted node would look healthy with no
    integrations and nobody able to log in."""
    with pytest.raises(PullError):
        restore_fileset(
            FakeClient({}), namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path
        )


def test_paths_that_escape_the_staging_root_are_refused(
    tmp_path: pathlib.Path,
) -> None:
    """The manifest comes from Valkey. It is authenticated, so this is defence
    in depth rather than the primary control — but a path traversal here writes
    anywhere the container can reach, and the check costs one comparison."""
    client = _published({"../../etc/passwd": b"root:x:0:0"})
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)


def test_a_manifest_missing_entries_is_a_pull_error_not_a_crash(
    tmp_path: pathlib.Path,
) -> None:
    """Bug found in review: a manifest that decrypts cleanly but lacks the
    `entries` key raised a raw `KeyError` past this function's own error
    boundary. `main()` only catches `PullError`, so a real failover would see
    an undiagnosable traceback instead of a diagnosable exit code."""
    key = derive_fileset_key(SECRET)
    manifest = json.dumps({"generation": 1, "node": "tiger1", "ts": "t"})  # no "entries"
    client = FakeClient(
        {
            f"ha:cluster_state_sync:{NS}:fileset:manifest": seal(
                key, manifest.encode(), aad=MANIFEST_AAD
            )
        }
    )
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=key, staged_dir=tmp_path)
    # Judgement call (review round 2): these bytes authenticated — the marker
    # means "did not authenticate", so a merely malformed-but-genuine manifest
    # must not set it. Setting it here would have the swap refuse an
    # otherwise-good OLDER staged copy over what is really a publisher bug.
    assert not (tmp_path / "verify_failed").exists()


def test_a_manifest_missing_generation_is_a_pull_error_not_a_crash(
    tmp_path: pathlib.Path,
) -> None:
    """Bug found in review round 2: `int(manifest["generation"])` ran outside
    every try/except, so a manifest with valid non-empty `entries` but no
    `generation` key raised a raw `KeyError` past `main()`'s error boundary —
    the identical defect class as the missing-`entries` case above, just one
    field over."""
    key = derive_fileset_key(SECRET)
    ref = blob_ref(SECRET, b"x")
    manifest = json.dumps(
        {
            "node": "tiger1",
            "ts": "t",
            "entries": {".storage/auth": {"ref": ref, "size": 1, "mode": 0o100644}},
        }
    )  # no "generation"
    client = FakeClient(
        {
            f"ha:cluster_state_sync:{NS}:fileset:manifest": seal(
                key, manifest.encode(), aad=MANIFEST_AAD
            ),
            f"ha:cluster_state_sync:{NS}:fileset:blob:{ref}": seal(key, b"x", aad=blob_aad(ref)),
        }
    )
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=key, staged_dir=tmp_path)
    assert not (tmp_path / "verify_failed").exists()


def test_a_manifest_with_a_non_numeric_generation_is_a_pull_error_not_a_crash(
    tmp_path: pathlib.Path,
) -> None:
    """Same defect, the other way a `generation` field can be wrong: present
    but not a number. `int("not-a-number")` raised a raw `ValueError` past
    `main()`'s error boundary, same as the missing-key case above."""
    key = derive_fileset_key(SECRET)
    ref = blob_ref(SECRET, b"x")
    manifest = json.dumps(
        {
            "generation": "not-a-number",
            "node": "tiger1",
            "ts": "t",
            "entries": {".storage/auth": {"ref": ref, "size": 1, "mode": 0o100644}},
        }
    )
    client = FakeClient(
        {
            f"ha:cluster_state_sync:{NS}:fileset:manifest": seal(
                key, manifest.encode(), aad=MANIFEST_AAD
            ),
            f"ha:cluster_state_sync:{NS}:fileset:blob:{ref}": seal(key, b"x", aad=blob_aad(ref)),
        }
    )
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=key, staged_dir=tmp_path)
    assert not (tmp_path / "verify_failed").exists()


def test_a_tampered_manifest_raises_the_verify_failed_marker(
    tmp_path: pathlib.Path,
) -> None:
    """The other half of the judgement call above: a manifest that genuinely
    fails to authenticate — not merely malformed after decryption, but tampered
    in transit — must still set the marker. Nothing in the original nine tests
    exercised manifest-level (as opposed to blob-level) tampering, so this was
    an untested path before review round 2."""
    key = derive_fileset_key(SECRET)
    client = _published({".storage/auth": b"secret"})
    manifest_key = f"ha:cluster_state_sync:{NS}:fileset:manifest"
    raw = bytearray(base64.b64decode(client.values[manifest_key]))
    raw[-1] ^= 0x01
    client.values[manifest_key] = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=key, staged_dir=tmp_path)
    assert (tmp_path / "verify_failed").exists()


def test_an_empty_manifest_stages_an_empty_tree_instead_of_crashing(
    tmp_path: pathlib.Path,
) -> None:
    """Bug found in review, reproduced as a crash:

        FileNotFoundError: [Errno 2] No such file or directory:
        '.../.incoming/storage' -> '.../storage'

    A manifest with a valid, empty `entries: {}` is a legitimate structural
    state — an empty config directory, say — not corruption. `.incoming/storage`
    used to be created only as a side effect of the per-entry loop, so zero
    entries meant zero iterations meant nothing to rename.
    """
    client = _published({})
    status = restore_fileset(
        client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path
    )
    assert status.files == 0
    assert status.fetched == 0
    assert (tmp_path / "storage").is_dir()


def test_the_pull_parses_exactly_what_the_publisher_emits(tmp_path: pathlib.Path) -> None:
    """Closes the manifest contract without importing `fileset.Manifest` into
    the pull program itself: `fileset.py` carries an async publisher that can
    never run on the host, so the ruling was a contract test here rather than
    a shared import. Every other test in this file builds the manifest JSON by
    hand, so none of them would catch the reader and the writer drifting apart
    — this one builds a real `Manifest`, serialises it with the publisher's own
    `to_json()`, and drives the pull with exactly those bytes."""
    key = derive_fileset_key(SECRET)
    body = b'{"refresh_tokens": ["t1"]}'
    ref = blob_ref(SECRET, body)
    manifest = Manifest(
        generation=7,
        node="tiger1",
        ts="2026-08-29T10:00:00+00:00",
        entries={
            ".storage/auth": FileEntry(
                path=".storage/auth", ref=ref, size=len(body), mode=0o100644, mtime_ns=1
            )
        },
    )
    client = FakeClient(
        {
            f"ha:cluster_state_sync:{NS}:fileset:manifest": seal(
                key, manifest.to_json().encode(), aad=MANIFEST_AAD
            ),
            f"ha:cluster_state_sync:{NS}:fileset:blob:{ref}": seal(key, body, aad=blob_aad(ref)),
        }
    )
    status = restore_fileset(client, namespace=NS, key=key, staged_dir=tmp_path)
    assert status.generation == 7
    assert (tmp_path / "storage" / ".storage" / "auth").read_bytes() == body


class _StubConnect:
    """Stands in for `ValkeyClient.connect` during `main()`'s tests.

    Records exactly the keyword arguments `main()` passes, which is where the
    two smaller Task 12 defects lived: the password and the database number
    were never handed to the client at all, so the pull connected
    unauthenticated to db 0 while the publisher wrote to db 2.

    Note the shape this pins. The predecessor asserted
    `stub.calls == [(host, port, True)]` -- a three-field tuple with no room
    for credentials or a database -- so the test enforced the bug rather than
    catching it. A signature is only worth pinning if the pin can fail when
    the signature is wrong.
    """

    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeClient:
        self.calls.append(kwargs)
        return self._client


def _install_stub(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> _StubConnect:
    stub = _StubConnect(client)
    monkeypatch.setattr(ValkeyClient, "connect", staticmethod(stub))
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    return stub


def _key_file(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "cluster-fileset.key"
    path.write_text(derive_fileset_key(SECRET).hex())
    return path


def test_main_pulls_successfully_and_returns_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers argument parsing, the `--key-file` hex read, and the success
    path's exit code — the sibling script's test, `test_preflight.py`,
    exercises `main()` the same way."""
    stub = _install_stub(monkeypatch, _published({".storage/auth": b"good"}))
    staged = tmp_path / "staged"

    exit_code = main(
        [
            "--redis",
            "10.0.0.5:6380",
            "--namespace",
            NS,
            "--key-file",
            str(_key_file(tmp_path)),
            "--staged",
            str(staged),
        ]
    )

    assert exit_code == 0
    assert stub.calls == [
        {
            "host": "10.0.0.5",
            "port": 6380,
            "username": None,
            "password": None,
            "db": DEFAULT_DB,
            "use_tls": False,
            "ca_file": None,
        }
    ]
    assert (staged / "storage" / ".storage" / "auth").read_bytes() == b"good"
    assert "staged generation 3" in capsys.readouterr().out


def test_main_defaults_the_redis_port_when_none_is_given(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--redis host` with no `:port` must fall back to 6379, not crash on
    `int("")`."""
    stub = _install_stub(monkeypatch, _published({".storage/auth": b"good"}))

    exit_code = main(
        [
            "--redis",
            "10.0.0.5",
            "--namespace",
            NS,
            "--key-file",
            str(_key_file(tmp_path)),
            "--staged",
            str(tmp_path / "staged"),
        ]
    )

    assert exit_code == 0
    assert stub.calls[0]["port"] == 6379


def test_main_defaults_the_database_to_the_one_the_publisher_writes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`const.DEFAULT_REDIS_DB` is 2. `fileset_pull.py` runs standalone on the
    host and cannot import `const`, so the number is repeated here — and this
    is the test that notices if the two ever drift apart."""
    from custom_components.cluster_state_sync.const import DEFAULT_REDIS_DB

    assert DEFAULT_DB == DEFAULT_REDIS_DB

    stub = _install_stub(monkeypatch, _published({".storage/auth": b"good"}))
    main(
        [
            "--redis",
            "10.0.0.5",
            "--namespace",
            NS,
            "--key-file",
            str(_key_file(tmp_path)),
            "--staged",
            str(tmp_path / "staged"),
        ]
    )
    assert stub.calls[0]["db"] == DEFAULT_REDIS_DB


def test_main_passes_the_database_username_and_tls_options_through(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every one of these was silently dropped before Task 12: the client was
    built with host, port and `decode_responses` and nothing else."""
    stub = _install_stub(monkeypatch, _published({".storage/auth": b"good"}))

    exit_code = main(
        [
            "--redis",
            "valkey.lan:6379",
            "--namespace",
            NS,
            "--key-file",
            str(_key_file(tmp_path)),
            "--staged",
            str(tmp_path / "staged"),
            "--db",
            "7",
            "--username",
            "ha-cluster-sync",
            "--tls",
            "--tls-ca-file",
            "/ca",
        ]
    )

    assert exit_code == 0
    assert stub.calls[0]["db"] == 7
    assert stub.calls[0]["username"] == "ha-cluster-sync"
    assert stub.calls[0]["use_tls"] is True
    assert stub.calls[0]["ca_file"] == "/ca"


def test_main_reads_the_password_from_the_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch, _published({".storage/auth": b"good"}))
    monkeypatch.setenv(PASSWORD_ENV, "s3cret-from-the-env")

    main(
        [
            "--redis",
            "valkey.lan",
            "--namespace",
            NS,
            "--key-file",
            str(_key_file(tmp_path)),
            "--staged",
            str(tmp_path / "staged"),
        ]
    )

    assert stub.calls[0]["password"] == "s3cret-from-the-env"


def test_there_is_no_password_flag_to_put_a_secret_in_argv(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ps` shows every process's argv to every user on the host. The pull runs
    from a systemd timer on a machine that also runs other things, so a
    `--password` flag would publish the Valkey credential to anyone with a
    shell. Regression guard: this must stay an argparse error, never a working
    option someone adds back for convenience."""
    _install_stub(monkeypatch, _published({".storage/auth": b"good"}))

    with pytest.raises(SystemExit):
        main(
            [
                "--redis",
                "valkey.lan",
                "--namespace",
                NS,
                "--key-file",
                str(_key_file(tmp_path)),
                "--staged",
                str(tmp_path / "staged"),
                "--password",
                "s3cret",
            ]
        )


def test_main_closes_the_connection_even_when_the_pull_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pull runs once a minute from a timer, forever. A socket leaked on
    every failed run is a slow-motion outage on the node that is standing by
    to take over."""
    client = FakeClient({})  # no manifest published -> PullError
    stub = _install_stub(monkeypatch, client)

    assert (
        main(
            [
                "--redis",
                "valkey.lan",
                "--namespace",
                NS,
                "--key-file",
                str(_key_file(tmp_path)),
                "--staged",
                str(tmp_path / "staged"),
            ]
        )
        == 1
    )
    assert stub.calls, "connect was never reached"
    assert client.closed


def test_main_turns_a_refused_connection_into_a_clean_exit_code(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Valkey that is down, or a wrong password, must reach the calling
    shell script as exit 1 with a stated reason. `cluster-fileset-pull.sh`
    branches on that exit code; a traceback and an exit 1 from an uncaught
    exception look the same to it, but not to the operator reading the log at
    3am."""

    def _refuse(**_kwargs: object) -> FakeClient:
        raise RespError("WRONGPASS invalid username-password pair")

    monkeypatch.setattr(ValkeyClient, "connect", staticmethod(_refuse))
    monkeypatch.delenv(PASSWORD_ENV, raising=False)

    exit_code = main(
        [
            "--redis",
            "valkey.lan",
            "--namespace",
            NS,
            "--key-file",
            str(_key_file(tmp_path)),
            "--staged",
            str(tmp_path / "staged"),
        ]
    )

    assert exit_code == 1
    assert "WRONGPASS" in capsys.readouterr().err


def test_main_reports_a_failed_pull_on_stderr_and_returns_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `except PullError` boundary: this is the exit code Task 7's swap
    script relies on to know the staged copy is unusable, so it must be a
    clean 1 with a stated reason, not a traceback."""
    _install_stub(monkeypatch, FakeClient({}))  # no manifest published

    exit_code = main(
        [
            "--redis",
            "10.0.0.5",
            "--namespace",
            NS,
            "--key-file",
            str(_key_file(tmp_path)),
            "--staged",
            str(tmp_path / "staged"),
        ]
    )

    assert exit_code == 1
    assert "fileset pull failed" in capsys.readouterr().err


def test_a_structurally_broken_entry_is_a_pull_error_not_a_crash(
    tmp_path: pathlib.Path,
) -> None:
    """Categorical fix (review round 1, third pass): the same defect kept
    reappearing one level deeper — first the top-level manifest fields, then
    `generation`, now per-entry fields. `entries` is the only nested structure
    a manifest has, so this is the last level. Two ways one entry can be
    broken while the manifest itself still authenticates: a missing `ref`
    (needed as a dict key and Redis key fragment), and a `mode` that is not a
    number (`entry["mode"] & 0o777` needs an int). Either used to raise a raw
    KeyError/TypeError from inside the fetch-and-write loops, past `main()`'s
    error boundary — and, per the same judgement call as the manifest-level
    cases, neither should set `verify_failed`: the manifest authenticated, so
    this is a publisher bug, not corruption.

    `bad_mode` publishes a real, valid blob for its `ref` — found missing in
    re-review. Without one, the pull raises `PullError` from the *missing-blob*
    branch before `mode` is ever read, so the assertions passed whether or not
    the `isinstance(mode, int)` check existed. A blob has to actually be
    fetchable for this case to reach the code the fix changed.
    """
    key = derive_fileset_key(SECRET)

    def _manifest_with(
        entry: dict[str, object], blobs: dict[str, bytes] | None = None
    ) -> FakeClient:
        body = json.dumps(
            {"generation": 1, "node": "tiger1", "ts": "t", "entries": {".storage/auth": entry}}
        )
        values: dict[str, bytes] = {
            f"ha:cluster_state_sync:{NS}:fileset:manifest": seal(
                key, body.encode(), aad=MANIFEST_AAD
            )
        }
        for ref, blob_body in (blobs or {}).items():
            values[f"ha:cluster_state_sync:{NS}:fileset:blob:{ref}"] = seal(
                key, blob_body, aad=blob_aad(ref)
            )
        return FakeClient(values)

    missing_ref = _manifest_with({"size": 1, "mode": 0o100644})  # no "ref"
    with pytest.raises(PullError):
        restore_fileset(missing_ref, namespace=NS, key=key, staged_dir=tmp_path / "a")
    assert not (tmp_path / "a" / "verify_failed").exists()

    good_ref = blob_ref(SECRET, b"body")
    bad_mode = _manifest_with(
        {"ref": good_ref, "size": 1, "mode": "not-a-number"}, blobs={good_ref: b"body"}
    )
    with pytest.raises(PullError):
        restore_fileset(bad_mode, namespace=NS, key=key, staged_dir=tmp_path / "b")
    assert not (tmp_path / "b" / "verify_failed").exists()


def test_entries_that_are_not_an_object_is_a_pull_error_not_a_crash(
    tmp_path: pathlib.Path,
) -> None:
    """Bug found in re-review, reproduced by the reviewer: a manifest with
    `entries` present but not a JSON object (a list, here) raised a bare
    `AttributeError` from `.values()`/`.items()` — outside the caught
    `(ValueError, KeyError, TypeError)` tuple, so it sailed past `main()`'s
    error boundary as a raw traceback. The categorical ruling said every read
    derived from manifest content must be protected; it enumerated fields but
    never named the type of `entries` itself. This closes that gap."""
    key = derive_fileset_key(SECRET)
    manifest = json.dumps(
        {"generation": 1, "node": "tiger1", "ts": "t", "entries": ["not", "a", "dict"]}
    )
    client = FakeClient(
        {
            f"ha:cluster_state_sync:{NS}:fileset:manifest": seal(
                key, manifest.encode(), aad=MANIFEST_AAD
            )
        }
    )
    with pytest.raises(PullError):
        restore_fileset(client, namespace=NS, key=key, staged_dir=tmp_path)
    assert not (tmp_path / "verify_failed").exists()


# -- The hand-rolled RESP client (Task 12) ----------------------------------
#
# `main()` used to `import redis`. The Home Assistant image does not have it:
# `redis` is a *custom-component requirement*, which Home Assistant pip-installs
# into the RUNNING container at setup time, so a cold standby's fresh
# `docker run` has never seen it. Verified directly against the image:
#
#     docker run --rm --entrypoint python3 homeassistant/home-assistant:2026.6.4 \
#         -c "import cryptography, redis"
#     cryptography   PRESENT
#     redis          MISSING
#
# So the pull speaks RESP itself, out of the standard library. These tests
# drive the parser against byte strings and the connection against a fake
# socket object -- never a real server. The repo blocks sockets in tests on
# purpose and that rule is not relaxed here.


def _reader(*chunks: bytes) -> RespReader:
    """A reader fed exactly these chunks, in order, then EOF.

    Chunking is the point of several tests below: a real socket does not
    deliver one tidy reply per `recv`, and a parser that assumes it does works
    on a developer machine and fails under load.
    """
    remaining = list(chunks)

    def recv() -> bytes:
        return remaining.pop(0) if remaining else b""

    return RespReader(recv)


def test_a_simple_string_reply_is_decoded() -> None:
    assert _reader(b"+OK\r\n").read_reply() == "OK"


def test_an_integer_reply_is_decoded() -> None:
    assert _reader(b":42\r\n").read_reply() == 42


def test_a_bulk_string_reply_is_decoded() -> None:
    assert _reader(b"$5\r\nhello\r\n").read_reply() == "hello"


def test_an_empty_bulk_string_is_the_empty_string_not_none() -> None:
    """`$0` and `$-1` are different answers and the pull acts on the
    difference: empty means the key exists and holds nothing, nil means it is
    not there at all."""
    assert _reader(b"$0\r\n\r\n").read_reply() == ""


def test_a_nil_bulk_string_is_none_rather_than_an_exception() -> None:
    """Load-bearing. `restore_fileset` distinguishes a missing blob (a
    `PullError` that leaves the staged copy alone and does NOT write
    `verify_failed`) from one that failed to decrypt. A client that raised on
    a missing key would collapse those two into one, and the swap would refuse
    to install a perfectly good older go-bag over a blob the GC removed."""
    assert _reader(b"$-1\r\n").read_reply() is None


def test_an_array_reply_is_decoded_elementwise() -> None:
    assert _reader(b"*2\r\n$3\r\nfoo\r\n$3\r\nbar\r\n").read_reply() == ["foo", "bar"]


def test_an_array_carries_nil_elements_through_as_none() -> None:
    """Exactly what MGET returns for keys that are not there, which is how the
    pull learns a blob is missing."""
    reply = _reader(b"*3\r\n$1\r\na\r\n$-1\r\n$1\r\nc\r\n").read_reply()
    assert reply == ["a", None, "c"]


def test_an_empty_array_is_an_empty_list() -> None:
    assert _reader(b"*0\r\n").read_reply() == []


def test_a_nil_array_is_none() -> None:
    assert _reader(b"*-1\r\n").read_reply() is None


def test_an_error_reply_raises_through_the_pull_error_boundary() -> None:
    """`main()` only catches `PullError`. A server error that escaped it would
    reach the operator as a traceback, and the calling shell script would get
    an exit code it cannot interpret -- the whole reason that boundary exists.
    """
    with pytest.raises(RespError) as caught:
        _reader(b"-WRONGPASS invalid username-password pair\r\n").read_reply()
    assert isinstance(caught.value, PullError)
    assert "WRONGPASS" in str(caught.value)


def test_a_reply_split_across_several_socket_reads_is_reassembled() -> None:
    """A real socket hands over whatever happened to arrive. This one is
    delivered a byte at a time -- the pathological case of the same thing --
    because a parser that assumes one reply per `recv` passes every
    single-chunk test and then fails under load, on a big MGET, during a
    promotion."""
    whole = b"*2\r\n$5\r\nhello\r\n$-1\r\n"
    reader = _reader(*[whole[i : i + 1] for i in range(len(whole))])
    assert reader.read_reply() == ["hello", None]


def test_a_reply_split_mid_header_is_reassembled() -> None:
    """The split that a naive `recv`-then-parse gets wrong most often: the
    length prefix itself straddles two reads."""
    reader = _reader(b"$1", b"1\r\nhello", b" world\r\n")
    assert reader.read_reply() == "hello world"


def test_two_replies_arriving_in_one_read_are_read_in_turn() -> None:
    """AUTH's `+OK` and SELECT's `+OK` can land in the same packet. A reader
    that threw away the remainder of the buffer after each reply would lose
    the second and then hang waiting for it."""
    reader = _reader(b"+OK\r\n+OK\r\n")
    assert reader.read_reply() == "OK"
    assert reader.read_reply() == "OK"


def test_a_truncated_reply_is_an_error_rather_than_a_short_value() -> None:
    """The connection dying halfway through a blob must not come back as a
    shorter blob: that would decrypt to nothing, be reported as a GCM failure,
    and write `verify_failed` -- telling the operator the bytes were tampered
    with when the real answer is that the network dropped."""
    with pytest.raises(RespError):
        _reader(b"$10\r\nhalf").read_reply()


def test_a_reply_truncated_before_its_terminator_is_an_error() -> None:
    with pytest.raises(RespError):
        _reader(b"+OK").read_reply()


def test_an_unknown_type_byte_is_an_error_not_a_silent_none() -> None:
    """RESP3 push frames (`>`), maps (`%`) and the rest are deliberately out
    of scope. Meeting one means an assumption broke, so say so."""
    with pytest.raises(RespError):
        _reader(b"%1\r\n$1\r\na\r\n$1\r\nb\r\n").read_reply()


def test_a_non_numeric_length_is_an_error_not_a_crash() -> None:
    with pytest.raises(RespError):
        _reader(b"$notanumber\r\n").read_reply()


class FakeSocket:
    """A socket-shaped object. No sockets are opened by these tests.

    `sent` records every byte written, so the tests can assert on the exact
    RESP the client puts on the wire -- which is where the AUTH form and the
    SELECT actually live.
    """

    def __init__(self, *replies: bytes) -> None:
        self.sent = bytearray()
        self._replies = list(replies)
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        return self._replies.pop(0) if self._replies else b""

    def close(self) -> None:
        self.closed = True


def test_a_command_goes_out_as_a_resp_array_of_bulk_strings() -> None:
    sock = FakeSocket(b"$3\r\nabc\r\n")
    client = ValkeyClient(sock)
    assert client.get("k") == "abc"
    assert bytes(sock.sent) == b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n"


def test_get_returns_none_for_a_missing_key() -> None:
    client = ValkeyClient(FakeSocket(b"$-1\r\n"))
    assert client.get("nope") is None


def test_mget_keeps_missing_keys_in_place_as_none() -> None:
    """`restore_fileset` zips the refs it asked for against what came back
    with `strict=True`. A client that dropped absent keys would misalign every
    blob after the gap and hand the wrong bytes to the wrong path."""
    client = ValkeyClient(FakeSocket(b"*3\r\n$1\r\na\r\n$-1\r\n$1\r\nc\r\n"))
    assert client.mget(["k1", "k2", "k3"]) == ["a", None, "c"]


def test_mget_refuses_a_reply_of_the_wrong_length() -> None:
    """Found in self-review, and it matters because of where the failure lands.

    `restore_fileset` does `zip(refs, raw, strict=True)`, and that call sits
    outside every try/except in the function. A short or long MGET array would
    therefore raise a bare `ValueError` straight past `main()`'s
    `except PullError` -- a traceback and an uninterpretable exit code for
    `cluster-fileset-pull.sh`, which is precisely the failure mode the
    boundary exists to prevent. Refusing here keeps it inside.
    """
    client = ValkeyClient(FakeSocket(b"*2\r\n$1\r\na\r\n$1\r\nb\r\n"))
    with pytest.raises(PullError):
        client.mget(["k1", "k2", "k3"])


def test_mget_refuses_a_reply_that_is_not_an_array() -> None:
    client = ValkeyClient(FakeSocket(b"+OK\r\n"))
    with pytest.raises(PullError):
        client.mget(["k1"])


def test_get_refuses_a_reply_that_is_not_a_string_or_nil() -> None:
    """Same boundary argument one function over: `_decode` hands whatever it
    gets to `base64.b64decode`, and an integer there is a `TypeError` outside
    the `PullError` boundary rather than a diagnosable exit code."""
    client = ValkeyClient(FakeSocket(b":42\r\n"))
    with pytest.raises(PullError):
        client.get("k")


def test_mget_with_no_keys_asks_the_server_nothing() -> None:
    """MGET with zero arguments is a syntax error on the server. An empty
    fileset manifest is legitimate, so this has to be answered locally."""
    sock = FakeSocket()
    assert ValkeyClient(sock).mget([]) == []
    assert bytes(sock.sent) == b""


def test_authentication_uses_the_two_argument_acl_form_when_a_username_is_set() -> None:
    """ADD 24 §4.2 provisions a named ACL user. `AUTH <password>` alone
    silently authenticates as `default` instead -- ADD 03 §2.3, and the exact
    defect already fixed once on the Sentinel legs of `RedisBackend`."""
    sock = FakeSocket(b"+OK\r\n")
    ValkeyClient(sock).authenticate(username="ha-cluster-sync", password="s3cret")
    assert bytes(sock.sent) == (b"*3\r\n$4\r\nAUTH\r\n$15\r\nha-cluster-sync\r\n$6\r\ns3cret\r\n")


def test_authentication_uses_the_one_argument_form_without_a_username() -> None:
    """`requirepass` deployments (the rehearsal's Valkey is one) have no named
    user, and `AUTH default <pass>` is not the same command."""
    sock = FakeSocket(b"+OK\r\n")
    ValkeyClient(sock).authenticate(username=None, password="s3cret")
    assert bytes(sock.sent) == b"*2\r\n$4\r\nAUTH\r\n$6\r\ns3cret\r\n"


def test_no_password_means_no_auth_command_at_all() -> None:
    sock = FakeSocket()
    ValkeyClient(sock).authenticate(username=None, password=None)
    assert bytes(sock.sent) == b""


def test_a_username_without_a_password_is_refused_before_anything_is_sent() -> None:
    """Sending `AUTH <user>` would be read by the server as `AUTH <password>`
    -- authenticating as `default` with the username as the password. Fail
    loudly on the misconfiguration instead."""
    sock = FakeSocket()
    with pytest.raises(PullError):
        ValkeyClient(sock).authenticate(username="ha-cluster-sync", password=None)
    assert bytes(sock.sent) == b""


def test_a_rejected_password_surfaces_as_a_pull_error() -> None:
    sock = FakeSocket(b"-WRONGPASS invalid username-password pair\r\n")
    with pytest.raises(PullError) as caught:
        ValkeyClient(sock).authenticate(username="u", password="p")
    assert "WRONGPASS" in str(caught.value)


def test_select_names_the_database_explicitly() -> None:
    """The publisher writes to the configured db (2 by default, `const.py`
    DEFAULT_REDIS_DB). Connecting without a SELECT lands on db 0, where the
    manifest simply is not -- which the pull would report as "no fileset
    manifest published", an alarm pointing at the wrong thing entirely."""
    sock = FakeSocket(b"+OK\r\n")
    ValkeyClient(sock).select(2)
    assert bytes(sock.sent) == b"*2\r\n$6\r\nSELECT\r\n$1\r\n2\r\n"


def test_selecting_a_database_the_server_rejects_is_a_pull_error() -> None:
    sock = FakeSocket(b"-ERR DB index is out of range\r\n")
    with pytest.raises(PullError):
        ValkeyClient(sock).select(99)


def test_closing_the_client_closes_the_socket() -> None:
    sock = FakeSocket()
    ValkeyClient(sock).close()
    assert sock.closed


def test_a_server_that_hangs_up_is_an_error_not_a_silent_empty_answer() -> None:
    client = ValkeyClient(FakeSocket())  # EOF immediately
    with pytest.raises(PullError):
        client.get("k")


# -- TLS ---------------------------------------------------------------------
#
# The production Valkey is TLS-only (D3 / ADD 24 §4.1: `--port 0`). These
# mirror `RedisBackend._tls_kwargs`, which fixes `ssl_cert_reqs="required"` and
# has no insecure toggle, and redis-py 6.4's `ssl_check_hostname=True` default.
# `tests/test_backend_tls.py` already asserts the integration side fails closed
# on an untrusted CA; a host-side client that accepted one would reopen exactly
# that hole from the other end.


def test_the_tls_context_verifies_the_certificate_and_the_hostname() -> None:
    ctx = tls_context(None)
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def _throwaway_ca(path: pathlib.Path) -> None:
    """Write a self-signed CA certificate, generated here rather than pasted in.

    A PEM blob committed to a test file reads as a credential to every scanner
    and every reviewer who meets it, and it expires one day and fails on a
    date nobody chose. This one lives for the length of the test.
    """
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cluster-state-sync-test-ca")])
    now = datetime.now(tz=UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_a_ca_file_is_actually_loaded_into_the_trust_store(tmp_path: pathlib.Path) -> None:
    """A `--tls-ca-file` that was accepted and then ignored would verify
    against the system store instead, which for a step-ca-issued certificate
    means every connection fails -- or, worse on a host that happens to trust
    a public CA, succeeds against the wrong server."""
    ca = tmp_path / "ca.pem"
    _throwaway_ca(ca)
    ctx = tls_context(str(ca))
    subjects = [c["subject"] for c in ctx.get_ca_certs()]
    assert any("cluster-state-sync-test-ca" in str(s) for s in subjects), subjects


def test_a_ca_file_that_does_not_exist_fails_closed(tmp_path: pathlib.Path) -> None:
    """Not "carry on with the system store". An operator who mistyped the path
    must find out now, not by having verification quietly widened."""
    with pytest.raises(PullError):
        tls_context(str(tmp_path / "absent.pem"))


class _RecordingContext:
    """Stands in for the real `SSLContext` to capture `server_hostname`."""

    def __init__(self) -> None:
        self.server_hostname: str | None = None

    def wrap_socket(self, sock: object, *, server_hostname: str) -> object:
        self.server_hostname = server_hostname
        return sock


def test_the_handshake_names_the_host_so_verification_has_something_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`wrap_socket` without `server_hostname` cannot check the certificate
    against anything, and with `check_hostname` on it raises instead --
    silently pushing an operator towards turning verification off."""
    # `ValkeyClient.connect` lives in `resp.py` now (moved out so the lease
    # promoter can use it without dragging in `crypto.py`'s `cryptography`
    # dependency), so its own module globals -- not fileset_pull's re-export
    # of the same names -- are what it actually looks `tls_context` and
    # `socket` up in at call time. Patching `fileset_pull.tls_context` would
    # rebind a name nothing reads.
    import custom_components.cluster_state_sync.scripts.resp as resp_mod

    recorded = _RecordingContext()
    monkeypatch.setattr(resp_mod, "tls_context", lambda _ca: recorded)
    monkeypatch.setattr(
        resp_mod.socket, "create_connection", lambda *_a, **_k: FakeSocket(b"+OK\r\n", b"+OK\r\n")
    )

    ValkeyClient.connect(host="valkey.lan", port=6379, db=2, use_tls=True, ca_file="/ca")

    assert recorded.server_hostname == "valkey.lan"


def test_connect_authenticates_then_selects_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters: SELECT before AUTH is refused with NOAUTH on an
    authenticated server."""
    # See the comment in the test above: `ValkeyClient.connect` resolves
    # `socket` against `resp.py`'s own globals, not fileset_pull's.
    import custom_components.cluster_state_sync.scripts.resp as resp_mod

    sock = FakeSocket(b"+OK\r\n+OK\r\n")
    monkeypatch.setattr(resp_mod.socket, "create_connection", lambda *_a, **_k: sock)

    ValkeyClient.connect(
        host="valkey.lan", port=6379, username="ha-cluster-sync", password="p", db=2
    )

    wire = bytes(sock.sent)
    assert wire.index(b"AUTH") < wire.index(b"SELECT")


# -- I5: the staged go-bag is a full credential tree -------------------------


def test_the_staging_tree_is_owner_only(tmp_path: pathlib.Path) -> None:
    """I5. Production change that would make this fail: `mkdir` with no mode.

    Under root's umask that is 0755, and what is staged is the leader's entire
    credential tree. File modes are preserved, so `.storage/auth` arrives 0600
    — but `core.config_entries` is 0644 as Home Assistant writes it, and it
    carries `cluster_secret` and `redis_password` in the clear. On a host that
    runs other things, 0755 hands both to every local account.
    """
    staged = tmp_path / "staged"
    client = _published(
        {
            ".storage/auth": b'{"refresh_tokens": ["t1"]}',
            ".storage/core.config_entries": b'{"data": {"entries": []}}',
        }
    )

    restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=staged)

    for path in (staged, staged / "storage", staged / "storage" / ".storage"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_a_staging_directory_that_already_exists_is_tightened(
    tmp_path: pathlib.Path,
) -> None:
    """`mkdir(exist_ok=True)` does not touch a directory that is already there,
    and this runs once a minute forever — so the very first pull after an
    upgrade has to fix what an earlier one left at 0755."""
    staged = tmp_path / "staged"
    staged.mkdir(mode=0o755)
    client = _published({".storage/auth": b"x"})

    restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=staged)

    assert stat.S_IMODE(staged.stat().st_mode) == 0o700


def test_the_auth_store_arrives_with_the_mode_it_was_published_with(
    tmp_path: pathlib.Path,
) -> None:
    """Test-sweep item: deleting `target.chmod(entry["mode"] & 0o777)` left 64
    tests passing, because nothing asserted a single file mode.

    `.storage/auth` is 0600 on the leader and holds the refresh tokens this
    whole feature exists to carry across a promotion. `cp -a` in the swap
    preserves what is here, so a go-bag that staged it 0644 would install it
    0644 — world-readable refresh tokens on a host that runs other things.
    """
    client = _published(
        {".storage/auth": b'{"refresh_tokens": ["t1"]}', "configuration.yaml": b"x"},
        modes={".storage/auth": 0o100600},
    )

    restore_fileset(client, namespace=NS, key=derive_fileset_key(SECRET), staged_dir=tmp_path)

    assert stat.S_IMODE((tmp_path / "storage" / ".storage" / "auth").stat().st_mode) == 0o600
    # And a mode that is not 0600 is not being forced to 0600 either: the
    # manifest is the source of truth, not a hardcoded value.
    assert stat.S_IMODE((tmp_path / "storage" / "configuration.yaml").stat().st_mode) == 0o644


# -- the backstop against a misaligned MGET ---------------------------------


class MisalignedClient(FakeClient):
    """A client whose `mget` does not answer position-for-position.

    `ValkeyClient.mget` guards against this itself, but `restore_fileset`
    accepts *any* client object — the rehearsal substitutes `redis.Redis`, and
    a future one might substitute something else. `strict=True` is the backstop
    that turns a misalignment into an exception instead of writing every blob
    after the gap to the wrong path, under a manifest that authenticated.
    """

    def __init__(self, values: dict[str, bytes], *, drop: bool) -> None:
        super().__init__(values)
        self._drop = drop

    def mget(self, keys: list[str]) -> list[str | None]:
        answer = super().mget(keys)
        return answer[1:] if self._drop else [*answer, None]


def _misaligned(drop: bool) -> MisalignedClient:
    files = {
        ".storage/auth": b'{"refresh_tokens": ["t1"]}',
        ".storage/http": b'{"server_id": "aaa"}',
        "configuration.yaml": b"homeassistant:\n",
    }
    client = MisalignedClient({}, drop=drop)
    client.values = _published(files).values
    return client


def test_a_long_mget_reply_is_refused_rather_than_quietly_truncated(
    tmp_path: pathlib.Path,
) -> None:
    """Test-sweep item: relaxing `zip(refs, raw, strict=True)` to
    `strict=False` passed the whole suite. This is the case that proves it
    must not.

    A reply with more values than keys is accepted outright by a relaxed zip —
    it stops at the shorter side, every blob pairs correctly, and the pull
    reports success against a client that is not answering the question it was
    asked. `strict=True` turns that into a `ValueError`; nothing else does.
    """
    with pytest.raises(ValueError):
        restore_fileset(
            _misaligned(drop=False),
            namespace=NS,
            key=derive_fileset_key(SECRET),
            staged_dir=tmp_path,
        )

    assert not (tmp_path / "storage").exists()


def test_a_short_mget_reply_never_writes_one_files_bytes_to_another_path(
    tmp_path: pathlib.Path,
) -> None:
    """The half of the misalignment the `mget` docstring actually describes: a
    client that omits its misses instead of returning `None` in place.

    Two independent things now refuse it, and that is deliberate. The ref is
    associated data (I6), so the first shifted pair fails to authenticate
    before `zip` ever runs out — and `strict=True` catches whatever the AAD
    binding would not. Either way staging is untouched, which is the property
    that matters: a shifted pairing would write `.storage/http`'s bytes to
    `.storage/auth` under a manifest that verified.
    """
    with pytest.raises((ValueError, PullError)):
        restore_fileset(
            _misaligned(drop=True),
            namespace=NS,
            key=derive_fileset_key(SECRET),
            staged_dir=tmp_path,
        )

    assert not (tmp_path / "storage").exists()


def test_eval_puts_the_script_key_count_keys_and_args_on_the_wire_in_order() -> None:
    """RESP EVAL is positional: script, numkeys, then KEYS, then ARGV. Get the
    count or the order wrong and the server reads an argument as a key name --
    which for the lease means taking a lock on the wrong key entirely, so both
    nodes would believe they hold it.

    Asserted as exact bytes, the way every other wire test in this file is: a
    substring check would pass on a reordered or miscounted command.
    """
    sock = FakeSocket(b":1\r\n")
    assert ValkeyClient(sock).eval("return 1", ["k"], ["a", "b"]) == 1
    assert bytes(sock.sent) == (
        b"*6\r\n$4\r\nEVAL\r\n$8\r\nreturn 1\r\n$1\r\n1\r\n$1\r\nk\r\n$1\r\na\r\n$1\r\nb\r\n"
    )


def test_eval_counts_keys_not_arguments() -> None:
    """The numkeys field is the length of `keys`. Passing len(args), or a
    hardcoded 1, is the mistake that makes ARGV[1] land in KEYS."""
    sock = FakeSocket(b":0\r\n")
    ValkeyClient(sock).eval("x", ["one", "two"], ["only-arg"])
    assert b"$1\r\n2\r\n" in bytes(sock.sent), bytes(sock.sent)


def test_eval_surfaces_a_server_error_as_a_pull_error() -> None:
    """A Lua error must reach the caller through the same boundary everything
    else uses, so the shell gets an exit code rather than a traceback.

    This is what lets the promoter fail closed with one `except PullError`
    instead of a bare `except Exception`.
    """
    with pytest.raises(PullError):
        ValkeyClient(FakeSocket(b"-ERR unknown command\r\n")).eval("return 1", [], [])
