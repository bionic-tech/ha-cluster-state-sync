"""TLS against a real TLS-enabled Valkey — the code half of H5.

`_tls_kwargs` sets `ssl_cert_reqs="required"`, which is the whole point: a
client that negotiates TLS but does not *verify* gives the appearance of
protection while remaining trivially interceptable. Until this module that
setting was asserted by reading the dict, never by a handshake.

Two directions matter, and the second matters more:

* the right CA connects
* **the wrong CA fails closed** — a verification setting that silently degrades
  to "trust anything" is worse than plaintext, because plaintext is at least
  honest about what it is

Certificates are generated per-session with the stdlib and Valkey's own
`--tls-port`. This proves *our client*. It does not prove a TLS listener on the
fleet's Valkey, which needs the instance in READINESS I2 to exist first.
"""

from __future__ import annotations

from collections.abc import Generator
import datetime as dt
from ipaddress import ip_address
from pathlib import Path
import subprocess
import time
import uuid

import pytest

from custom_components.cluster_state_sync.backend import RedisBackend

pytest.importorskip(
    "cryptography",
    reason="TLS tests need `cryptography` to mint a throwaway CA — the handshake "
    "is NOT being verified in this run.",
)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

VALKEY_IMAGE = "valkey/valkey:8.1"


def _mint_ca(directory: Path, common_name: str) -> tuple[Path, Path, Path]:
    """Return (ca_cert, server_cert, server_key) for a self-signed authority."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{common_name}-ca")])
    now = dt.datetime.now(dt.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        # OpenSSL 3 refuses a chain without key identifiers under strict
        # verification ("Missing Authority Key Identifier"), which is exactly
        # the strictness `ssl_cert_reqs="required"` asks for.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            # The IP matters as much as the name: `pytest-socket` permits only
            # 127.0.0.1, so the client connects by address and hostname
            # verification is checked against the IP SAN.
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    paths = {}
    for name, blob in (
        ("ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM)),
        ("server.crt", srv_cert.public_bytes(serialization.Encoding.PEM)),
        (
            "server.key",
            srv_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ),
        ),
    ):
        p = directory / name
        p.write_bytes(blob)
        p.chmod(0o644)
        paths[name] = p
    return paths["ca.crt"], paths["server.crt"], paths["server.key"]


@pytest.fixture(scope="session")
def tls_valkey() -> Generator[tuple[int, Path, Path]]:
    """A TLS-only Valkey, plus the trusted CA and an untrusted one.

    The cert directory is minted with `mkdtemp` and made traversable rather
    than taken from `tmp_path_factory`: pytest's temp roots are mode 700, so
    the container's unprivileged `valkey` user cannot descend into them and the
    server exits before it can bind. World-readable is fine — these keys live
    for the length of one test session and guard nothing.
    """
    import shutil
    import tempfile

    if not shutil.which("docker"):
        pytest.skip(
            "docker unavailable — the TLS handshake is NOT being verified in this run.",
            allow_module_level=True,
        )

    root = Path(tempfile.mkdtemp(prefix="cluster-sync-tls-"))
    root.chmod(0o755)
    certs = root / "trusted"
    certs.mkdir(mode=0o755)
    other = root / "untrusted"
    other.mkdir(mode=0o755)
    ca, crt, key = _mint_ca(certs, "trusted")
    wrong_ca, _, _ = _mint_ca(other, "untrusted")

    started = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "-p", "127.0.0.1::6380",
            "-v", f"{certs}:/certs:ro",
            VALKEY_IMAGE,
            "valkey-server",
            "--port", "0",
            "--tls-port", "6380",
            "--tls-cert-file", "/certs/server.crt",
            "--tls-key-file", "/certs/server.key",
            "--tls-ca-cert-file", "/certs/ca.crt",
            "--tls-auth-clients", "no",
        ],
        capture_output=True, text=True, check=False,
    )
    if started.returncode != 0:
        pytest.skip(
            f"could not start a TLS Valkey: {started.stderr.strip()[:200]}",
            allow_module_level=True,
        )
    container = started.stdout.strip()

    try:
        mapped = subprocess.run(
            ["docker", "port", container, "6380/tcp"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[0]
        port = int(mapped.rpartition(":")[2])

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", container, "valkey-cli", "--tls",
                 "--cacert", "/certs/ca.crt", "-p", "6380", "PING"],
                capture_output=True, text=True, check=False,
            )
            if "PONG" in probe.stdout:
                break
            time.sleep(0.3)
        else:
            pytest.skip("TLS Valkey never answered PING", allow_module_level=True)

        yield port, ca, wrong_ca
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
        shutil.rmtree(root, ignore_errors=True)


def _backend(port: int, ca: Path) -> RedisBackend:
    return RedisBackend(
        namespace=f"t{uuid.uuid4().hex[:12]}",
        host="127.0.0.1",
        port=port,
        db=0,
        use_tls=True,
        tls_ca_certs=str(ca),
        secret="s",
    )


async def test_connects_over_tls_when_the_ca_is_trusted(
    tls_valkey: tuple[int, Path, Path], socket_enabled: None
) -> None:
    port, ca, _ = tls_valkey
    b = _backend(port, ca)

    await b.connect()
    try:
        assert await b.health() is True
    finally:
        await b.close()


async def test_a_snapshot_round_trips_over_tls(
    tls_valkey: tuple[int, Path, Path], socket_enabled: None
) -> None:
    """The encrypted path has to carry real payloads, not just a handshake."""
    from datetime import UTC, datetime

    from custom_components.cluster_state_sync.backend import SnapshotEntry

    port, ca, _ = tls_valkey
    b = _backend(port, ca)
    await b.connect()
    try:
        stamp = datetime.now(tz=UTC).isoformat()
        entry = SnapshotEntry(
            entity_id="light.tls",
            state="on",
            attributes={"friendly_name": "Over TLS"},
            last_changed=stamp,
            last_updated=stamp,
            source_node="node-a",
        )
        assert await b.write_snapshot({"light.tls": entry}, "node-a") is True

        entries, meta = await b.read_snapshot()

        assert entries["light.tls"].state == "on"
        assert meta["source_node"] == "node-a"
    finally:
        await b.close()


async def test_an_untrusted_ca_fails_closed(
    tls_valkey: tuple[int, Path, Path], socket_enabled: None
) -> None:
    """The case that matters.

    `ssl_cert_reqs="required"` has to mean it. A client that shrugs and connects
    anyway would look identical in every other test in this suite.
    """
    port, _, wrong_ca = tls_valkey
    b = _backend(port, wrong_ca)

    with pytest.raises(Exception, match="(?i)certificate|ssl|verify"):
        await b.connect()

    await b.close()


async def test_a_plaintext_client_cannot_talk_to_the_tls_port(
    tls_valkey: tuple[int, Path, Path], socket_enabled: None
) -> None:
    """Confirms the server really is TLS-only, so the tests above mean something."""
    port, _, _ = tls_valkey
    b = RedisBackend(
        namespace=f"t{uuid.uuid4().hex[:12]}",
        host="127.0.0.1",
        port=port,
        db=0,
        use_tls=False,
        secret="s",
    )

    with pytest.raises(Exception):  # noqa: B017 — any failure is the point
        await b.connect()

    await b.close()
