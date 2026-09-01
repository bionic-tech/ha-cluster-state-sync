"""A minimal RESP client for talking to Valkey, standard library only.

Split out of `fileset_pull.py`, where this used to live. That module also
imports `crypto.py` for the manifest/blob decryption `restore_fileset` needs,
and `crypto.py` imports the third-party `cryptography` package. The lease
promoter (`cluster_promoter.py`) needs none of that -- it only ever calls
`eval` to take or renew the lease -- but importing `ValkeyClient` from
`fileset_pull` dragged the whole decryption stack in anyway, because the RESP
client happened to live in the same module. On a host without
`python3-cryptography` installed, the promoter's systemd timer failed on
every tick with nothing more than a log line -- AR-0040's shape, discovered
in the code written to stop its descendant:

    File ".../crypto.py", line 47, in <module>
        from cryptography.exceptions import InvalidTag
    ModuleNotFoundError: No module named 'cryptography'

So this module exists to be genuinely stdlib-only end to end, and both
`fileset_pull.py` and `cluster_promoter.py` import it -- never each other.

The scope is deliberately just what those two callers need: connect (plain or
TLS), AUTH, SELECT, GET, MGET, EVAL. No pipelining, no pub/sub, no cluster
redirects, no RESP3, no pooling, no reconnection. Anything more would be a
second Redis client to maintain, in a file that has to keep working
unattended during a promotion.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import socket
import ssl
from typing import Any

#: Environment variable carrying the Valkey password.
#:
#: Deliberately not a `--password` flag. `ps` shows every process's argv to
#: every user on the box, and both callers run from a systemd timer on a host
#: that runs other things, so a flag would hand the cluster's Valkey
#: credential to anyone with a shell. `cluster-fileset-pull.sh` and
#: `cluster-promoter.sh` both source it from a 0600 file and export it;
#: `docker run -e CLUSTER_SYNC_VALKEY_PASSWORD` (the pull) and the promoter's
#: own host-native process (which simply inherits the exported variable) both
#: pass it through by name, so the value never reaches a command line at all.
PASSWORD_ENV = "CLUSTER_SYNC_VALKEY_PASSWORD"

#: The database the fileset publisher writes to: `const.DEFAULT_REDIS_DB`.
#:
#: Repeated rather than imported, because this module runs standalone on the
#: host where only its siblings (`lease.py`, and for the pull, `crypto.py`)
#: sit beside it -- `const.py` pulls in `voluptuous`. `tests/test_fileset_pull.py`
#: asserts the two are equal, so the duplication cannot drift in silence.
DEFAULT_DB = 2

#: Per-`recv` timeout, deliberately far longer than `RedisBackend`'s 2.0s.
#: That one runs on Home Assistant's event loop, where failing fast matters
#: more than finishing; the pull is a batch job on a one-minute timer
#: fetching a whole config directory, where a slow network should delay a
#: pull rather than fail one, and the promoter's own tick is forgiving for
#: the same reason -- a slow renewal beats a lease lost to a timeout that was
#: simply too short.
SOCKET_TIMEOUT = 30.0

_RECV_BYTES = 65536


class ValkeyError(Exception):
    """The Valkey/RESP client could not do what was asked of it.

    The shared failure boundary both callers catch. `fileset_pull.py` aliases
    this as `PullError` (`main()`'s `except PullError` turns any failure into
    an exit code `cluster-fileset-pull.sh` can branch on); `cluster_promoter.py`
    catches it directly, alongside `OSError`, in its own `run()`. Raising
    something outside this boundary would reach the operator as a traceback
    instead of a log line and an exit code either caller can act on.
    """


class RespError(ValkeyError):
    """The server refused a command, or said something this cannot parse.

    A subclass of `ValkeyError` on purpose, for the same reason: a protocol
    failure is exactly as much "the caller could not complete, nothing was
    changed" as a missing manifest or a lost lease is, and it has to cross the
    same boundary both callers already catch.
    """


def _encode_command(args: Sequence[str]) -> bytes:
    """One command, as a RESP array of bulk strings.

    Bulk strings throughout, never the inline form: a length-prefixed argument
    cannot be split by a space or terminated early by a newline, so a key or a
    password containing either is carried literally instead of being reparsed
    into a different command.
    """
    out = bytearray(b"*%d\r\n" % len(args))
    for arg in args:
        raw = arg.encode("utf-8")
        out += b"$%d\r\n" % len(raw)
        out += raw
        out += b"\r\n"
    return bytes(out)


class RespReader:
    """Reads RESP replies from a byte source.

    Exactly the reply types the commands below can produce: simple strings
    (`+`), errors (`-`), integers (`:`), bulk strings (`$`, including the
    `$-1` nil) and arrays (`*`, including nil elements and the `*-1` nil
    array).

    **A missing key comes back as `None`, not an exception.** That distinction
    is load-bearing: `restore_fileset` tells a blob the garbage collector
    removed (stale, install it anyway) apart from one that failed to decrypt
    (corrupt, install nothing), and the swap script branches on the difference.

    Bytes are pulled through `recv` on demand rather than read in one go,
    because a socket delivers whatever happened to arrive. A reply can be split
    anywhere -- including inside a length prefix -- and one read can carry the
    tail of one reply and the head of the next. A parser that assumes one tidy
    chunk per reply passes every hand-written test and then fails on a large
    MGET, under load, during a promotion.
    """

    def __init__(self, recv: Callable[[], bytes]) -> None:
        self._recv = recv
        self._buffer = bytearray()

    def read_reply(self) -> Any:
        """Read one complete reply, blocking through as many reads as it takes."""
        line = self._read_line()
        if not line:
            raise RespError("empty reply from the server")
        kind, payload = line[:1], line[1:]
        if kind == b"+":
            return self._text(payload)
        if kind == b"-":
            # The server's own words, unedited: "WRONGPASS", "NOAUTH",
            # "NOPERM" and "DB index is out of range" each point at a
            # different fix, and paraphrasing them would lose that.
            raise RespError(self._text(payload))
        if kind == b":":
            return self._number(payload)
        if kind == b"$":
            length = self._number(payload)
            if length < 0:
                return None
            body = self._read_exactly(length + 2)
            if not body.endswith(b"\r\n"):
                raise RespError("bulk string was not terminated by CRLF")
            return self._text(body[:-2])
        if kind == b"*":
            count = self._number(payload)
            if count < 0:
                return None
            return [self.read_reply() for _ in range(count)]
        raise RespError(f"unsupported RESP reply type {kind!r}")

    def _read_line(self) -> bytes:
        while True:
            end = self._buffer.find(b"\r\n")
            if end >= 0:
                line = bytes(self._buffer[:end])
                del self._buffer[: end + 2]
                return line
            self._fill()

    def _read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._fill()
        body = bytes(self._buffer[:count])
        del self._buffer[:count]
        return body

    def _fill(self) -> None:
        chunk = self._recv()
        if not chunk:
            # Not a short value. A truncated blob would decrypt to nothing,
            # be reported as a GCM failure and write `verify_failed` -- telling
            # the operator the bytes were tampered with when the real answer
            # is that the network dropped.
            raise RespError("the connection closed part-way through a reply")
        self._buffer += chunk

    @staticmethod
    def _number(raw: bytes) -> int:
        try:
            return int(raw)
        except ValueError as err:
            raise RespError(f"expected a RESP length or integer, got {raw!r}") from err

    @staticmethod
    def _text(raw: bytes) -> str:
        """Decode to `str`, mirroring the clients' `decode_responses=True`.

        Everything the pull reads was base64-encoded by the publisher for
        exactly that reason (see `fileset_pull._decode`), so those values
        really are ASCII; the promoter's own traffic (node ids, TTLs) is
        ASCII by construction too.
        """
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as err:
            raise RespError(f"reply is not valid UTF-8: {err}") from err


def tls_context(ca_file: str | None) -> ssl.SSLContext:
    """`RedisBackend`'s TLS configuration, expressed in stdlib `ssl`.

    The production Valkey is TLS-only -- ADD 24 §4.1 sets `--port 0`, so there
    is no plaintext listener to fall back to.

    `RedisBackend._tls_kwargs` fixes `ssl_cert_reqs="required"` and offers no
    insecure toggle; redis-py 6.4 defaults `ssl_check_hostname` to True and
    builds its context with `ssl.create_default_context()`, which sets both.
    So does this, and there is deliberately no way to turn either off: with
    TLS on but verification off, someone on the path presents any certificate
    and the connection is encrypted to *them*. `tests/test_backend_tls.py`
    already pins that the integration fails closed on an untrusted CA; a
    host-side client that accepted one would reopen the same hole from the
    other end.

    Omitting the CA file falls back to the system trust store, which is still
    verification. Naming one that cannot be read is a hard failure rather than
    a quiet widening back to the system store.
    """
    context = ssl.create_default_context()
    if ca_file:
        try:
            context.load_verify_locations(cafile=ca_file)
        except OSError as err:
            raise RespError(f"cannot load the TLS CA file {ca_file}: {err}") from err
    return context


class ValkeyClient:
    """The small synchronous client both callers are driven with.

    Exposes `get`, `mget`, and `eval`, which are the only commands used by
    `restore_fileset` and the lease promoter respectively, plus the
    `authenticate`/`select` handshake `connect` performs. Tests substitute a
    plain object with the same methods, so nothing here is on the path of
    either caller's own reconstruction/promotion logic.
    """

    def __init__(self, sock: Any) -> None:
        self._sock = sock
        self._reader = RespReader(self._read_some)

    @classmethod
    def connect(
        cls,
        *,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        db: int = DEFAULT_DB,
        use_tls: bool = False,
        ca_file: str | None = None,
        timeout: float = SOCKET_TIMEOUT,
    ) -> ValkeyClient:
        """Open a connection, authenticate, and select the database.

        The TLS context is built before the socket is opened, so a CA file
        that cannot be read fails without a connection attempt.
        """
        context = tls_context(ca_file) if use_tls else None
        try:
            sock: Any = socket.create_connection((host, port), timeout=timeout)
        except OSError as err:
            raise RespError(f"cannot reach Valkey at {host}:{port}: {err}") from err
        if context is not None:
            try:
                # server_hostname is what verification is checked against.
                # Without it, `check_hostname` has nothing to compare and
                # raises -- which is the moment someone reaches for the
                # "just turn verification off" fix.
                sock = context.wrap_socket(sock, server_hostname=host)
            except OSError as err:
                sock.close()
                raise RespError(f"TLS handshake with {host}:{port} failed: {err}") from err
        client = cls(sock)
        try:
            client.authenticate(username=username, password=password)
            client.select(db)
        except BaseException:
            client.close()
            raise
        return client

    def authenticate(self, *, username: str | None, password: str | None) -> None:
        """AUTH, in whichever of its two forms this deployment needs.

        ADD 24 §4.2 provisions a named ACL user, and a named user
        authenticates with the two-argument `AUTH <username> <password>`.
        Sending the password alone silently authenticates as `default`
        instead (ADD 03 §2.3) -- the same defect already found and fixed once
        on `RedisBackend`'s Sentinel legs. A `requirepass`-only server has no
        named user and needs the one-argument form; `RedisBackend` makes the
        same distinction by leaving `username` as None.
        """
        if not password:
            if username:
                raise RespError(
                    f"a Valkey username was given but no password; set ${PASSWORD_ENV}. "
                    "Sending AUTH with a username alone would be read by the server as "
                    "the one-argument form and authenticate as `default`."
                )
            return
        args = ("AUTH", username, password) if username else ("AUTH", password)
        self._command(*args)

    def select(self, db: int) -> None:
        """SELECT, always -- db 0 included.

        The publisher writes to the configured database (2 by default). The
        pull used to build its client without one, land on db 0 where the
        manifest is not, and report "no fileset manifest published" -- an
        alarm pointing at entirely the wrong thing. Naming the database out
        loud makes it an assertion rather than an assumption.
        """
        self._command("SELECT", str(db))

    def get(self, key: str) -> str | None:
        """One key, or None when it is not there.

        The type check is not pedantry. `restore_fileset` hands whatever comes
        back to `base64.b64decode`, and anything but a string or None raises a
        `TypeError` from *outside* `main()`'s `except PullError` -- a traceback
        where the calling shell script needs an exit code it can branch on.
        """
        reply = self._command("GET", key)
        if reply is not None and not isinstance(reply, str):
            raise RespError(f"GET {key} returned {type(reply).__name__}, not a string")
        return reply

    def mget(self, keys: Sequence[str]) -> list[Any]:
        """Absent keys come back as `None` **in place**, never dropped.

        `restore_fileset` zips the refs it asked for against what came back
        with `strict=True`. A client that omitted the misses would misalign
        every blob after the gap and write the wrong bytes to the wrong path
        -- with a manifest that authenticated, so nothing downstream would
        notice.
        """
        keys = list(keys)
        if not keys:
            # MGET with no arguments is a syntax error on the server, and a
            # manifest with no entries is a legitimate empty fileset.
            return []
        reply = self._command("MGET", *keys)
        if not isinstance(reply, list):
            raise RespError(f"MGET did not return an array: {reply!r}")
        if len(reply) != len(keys):
            # `restore_fileset` zips this against `refs` with `strict=True`,
            # and that call sits outside every try/except in the function --
            # so a short or long array escapes `main()`'s `except PullError`
            # as a bare ValueError. Caught here, it stays inside the boundary.
            raise RespError(f"MGET returned {len(reply)} values for {len(keys)} keys")
        return reply

    def eval(self, script: str, keys: Sequence[str], args: Sequence[str]) -> Any:
        """Run a Lua script server-side.

        `EVAL` is positional — script, key count, then the keys, then the
        arguments. The count is what tells Redis where the keys stop; get it
        wrong and it reads an argument as a key name, which for the lease means
        operating on a key nobody meant to touch.
        """
        return self._command("EVAL", script, str(len(keys)), *keys, *args)

    def close(self) -> None:
        """Best effort: a socket that will not close is not worth a failed pull."""
        try:
            self._sock.close()
        except OSError:  # pragma: no cover - nothing sensible to do about it
            pass

    def _command(self, *args: str) -> Any:
        try:
            self._sock.sendall(_encode_command(args))
        except OSError as err:
            raise RespError(f"could not send {args[0]} to Valkey: {err}") from err
        return self._reader.read_reply()

    def _read_some(self) -> bytes:
        try:
            return self._sock.recv(_RECV_BYTES)
        except OSError as err:
            raise RespError(f"lost the connection to Valkey: {err}") from err
