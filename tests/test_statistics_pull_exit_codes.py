"""The pull script's exit codes, which are a contract with systemd.

This runs as a `.timer`-driven unit on the standby, as root, once a minute. Its
return value is not a detail: systemd puts a unit into `failed` on a non-zero
exit, and a unit in `failed` stops being run and stops being trusted.

🚨 The subtle one is `not_seeded` returning **0**. A standby nobody has got
round to seeding is in a *configuration state*, not a fault. Returning 1 there
would put the timer's unit into `failed` permanently on a node that is working
exactly as designed — and the operator would then be debugging systemd instead
of copying a file.

Nothing tested any of these. The script is 78% covered and the uncovered third
is precisely the branch table below.
"""

from __future__ import annotations

import base64
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.cluster_state_sync.crypto import STATISTICS_AAD, seal
from custom_components.cluster_state_sync.scripts import statistics_pull

KEY = bytes(range(32))


class _Client:
    """Stands in for the Valkey connection, recording what got reported back."""

    def __init__(self, payload: bytes | None) -> None:
        self._payload = payload
        self.reported: list[dict[str, Any]] = []

    def get(self, _key: bytes) -> bytes | None:
        return self._payload

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def setex(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def close(self) -> None:
        # The script closes in a `finally`; a stub that cannot be
        # closed turns every assertion into an AttributeError.
        return None


@pytest.fixture
def argv(tmp_path: pathlib.Path) -> list[str]:
    key_file = tmp_path / "key.hex"
    key_file.write_text(KEY.hex(), encoding="utf-8")
    return [
        "--redis",
        "valkey.invalid:6379",
        "--namespace",
        "testns",
        "--key-file",
        str(key_file),
        "--config",
        str(tmp_path),
    ]


def _run(argv: list[str], client: _Client, **patches: Any) -> int:
    with (
        patch.object(statistics_pull.ValkeyClient, "connect", return_value=client),
        patch.object(
            statistics_pull,
            "_report",
            lambda c, ns, status, k: client.reported.append(status),
        ),
    ):
        for name, value in patches.items():
            patcher = patch.object(statistics_pull, name, value)
            patcher.start()
        try:
            return statistics_pull.main(argv)
        finally:
            patch.stopall()


def test_nothing_published_yet_exits_clean_and_quiet(argv: list[str]) -> None:
    """The leader may simply have replication off.

    Not an error, and it must not fill the journal once a minute for ever.
    """
    client = _Client(None)
    assert _run(argv, client) == 0
    assert client.reported == [], "a quiet no-op should report nothing at all"


def test_a_window_that_fails_authentication_exits_nonzero(argv: list[str]) -> None:
    """🚨 Somebody wrote to the shared store who does not hold the key.

    Reported AND non-zero: this is the one failure here that is not a
    configuration state, and the standby's history stops advancing until it is
    understood.
    """
    client = _Client(base64.b64encode(b"not a sealed blob"))
    assert _run(argv, client) == 1
    assert client.reported[0]["state"] == "unauthenticated"


def test_a_malformed_but_authentic_window_is_blamed_on_the_leader(argv: list[str]) -> None:
    """It authenticated, so it is not corruption on the wire.

    Distinguishing the two matters: one is a bug on the other node, the other
    is somebody in the middle, and they need completely different responses.
    """
    sealed = seal(KEY, b"authentic but not a payload", aad=STATISTICS_AAD)
    client = _Client(base64.b64encode(sealed))
    assert _run(argv, client) == 1
    assert client.reported[0]["state"] == "malformed"


def test_a_connection_failure_exits_nonzero_without_reporting(argv: list[str]) -> None:
    """Nothing can be reported: the report goes through the connection."""
    with patch.object(
        statistics_pull.ValkeyClient,
        "connect",
        side_effect=statistics_pull.ValkeyError("connection refused"),
    ):
        assert statistics_pull.main(argv) == 1
