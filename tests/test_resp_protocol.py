"""The hand-rolled RESP reader, on input a well-behaved server never sends.

This parser exists because the host-side scripts run under systemd on a node
whose Home Assistant may be stopped — there is no `redis-py` there, and adding
one would mean a Python dependency on the host, which this project refuses.

So it is ours, it runs as root, and it reads bytes off a socket. Its happy path
was covered; the branches that fire on a truncated reply, a wrong type, or a
half-closed connection were not. Those are exactly the ones that run when
something has already gone wrong, which is when a crash is least affordable.
"""

from __future__ import annotations

import pytest

from custom_components.cluster_state_sync.scripts.resp import (
    RespError,
    RespReader,
    ValkeyClient,
)


def _reader(payload: bytes) -> RespReader:
    """The reader pulls through `recv`, so feed it once then signal EOF.

    An empty return is how a socket reports the peer closed, which is exactly
    the condition several of these tests are about.
    """
    chunks = iter([payload])

    def _recv() -> bytes:
        return next(chunks, b"")

    return RespReader(_recv)


# -- malformed replies ------------------------------------------------------


def test_a_blank_line_is_named_rather_than_an_index_error() -> None:
    """A bare CRLF is a reply with no type byte. Indexing it would IndexError."""
    with pytest.raises(RespError, match="empty reply"):
        _reader(b"\r\n").read_reply()


def test_a_server_that_answers_nothing_at_all_is_named() -> None:
    """EOF before a single byte — a closed connection, not a blank reply."""
    with pytest.raises(RespError, match="closed part-way"):
        _reader(b"").read_reply()


def test_an_error_reply_becomes_an_exception_carrying_its_text() -> None:
    """`-NOAUTH ...` is the one every operator meets first."""
    with pytest.raises(RespError, match="NOAUTH"):
        _reader(b"-NOAUTH Authentication required.\r\n").read_reply()


def test_an_unterminated_bulk_string_is_refused() -> None:
    """🚨 The length said five bytes; the stream ended after four.

    Accepting it would hand the caller a silently truncated value — and for
    this integration that value is a sealed blob or a leader id.
    """
    # Exactly length+2 bytes arrive, so the read succeeds -- and the last two
    # are not CRLF. A stream that has lost sync, rather than one that ended.
    with pytest.raises(RespError, match="not terminated"):
        _reader(b"$3\r\nabcXY").read_reply()


def test_an_unknown_reply_type_names_the_byte_it_did_not_understand() -> None:
    """A future RESP3 server, or a stream that has lost sync."""
    with pytest.raises(RespError, match="unsupported RESP reply type"):
        _reader(b"%1\r\n").read_reply()


def test_a_connection_that_closes_mid_reply_is_named() -> None:
    with pytest.raises(RespError, match="closed part-way"):
        _reader(b"$10\r\nshort").read_reply()


def test_a_non_numeric_length_is_refused() -> None:
    with pytest.raises(RespError, match="expected a RESP length or integer"):
        _reader(b"$notanumber\r\n").read_reply()


# -- the typed accessors, which are where a wrong shape does damage ---------


class _Stub(ValkeyClient):
    """A client whose command layer returns whatever the test wants."""

    def __init__(self, reply: object) -> None:
        self._reply = reply

    def _command(self, *_args: str) -> object:
        return self._reply


def test_get_refuses_a_reply_that_is_not_a_string() -> None:
    """🚨 A key holding a list where a blob was expected.

    Returning it would push a list into `open_sealed`, and the failure would
    surface as a crypto error — sending whoever reads it hunting a key problem
    that does not exist.
    """
    with pytest.raises(RespError, match="not a string"):
        _Stub([b"unexpected"]).get("k")


def test_mget_refuses_a_reply_that_is_not_an_array() -> None:
    with pytest.raises(RespError, match="did not return an array"):
        _Stub("not an array").mget(["a", "b"])


def test_mget_refuses_a_wrong_length_array() -> None:
    """Two keys asked for, one value returned.

    Zipping it would silently pair the wrong value with the wrong key — and
    this is the call that reads the leader and the snapshot meta together.
    """
    with pytest.raises(RespError, match="returned 1 values for 2 keys"):
        _Stub(["only-one"]).mget(["a", "b"])


def test_mget_accepts_a_correctly_sized_array() -> None:
    assert _Stub(["x", None]).mget(["a", "b"]) == ["x", None]
