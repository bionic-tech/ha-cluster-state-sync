"""AES-GCM sealing for the fileset (design §5).

ADR-002 signs snapshot entries with HMAC, which proves nothing was tampered
with and does nothing to stop anyone reading them. That is the right trade for
"the kitchen light is on" and the wrong one for `.storage`, which holds every
credential in the house.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import pytest

from custom_components.cluster_state_sync.crypto import (
    FILESET_INFO,
    MANIFEST_AAD,
    FilesetCryptoError,
    blob_aad,
    blob_ref,
    derive_fileset_key,
    open_sealed,
    seal,
)

SECRET = "a-test-cluster-secret"
OTHER = "a-different-cluster-secret"
REF = blob_ref(SECRET, b"whatever")
AAD = blob_aad(REF)


def test_round_trip_returns_the_original_bytes() -> None:
    key = derive_fileset_key(SECRET)
    body = b'{"version": 1, "data": {"refresh_tokens": []}}'
    assert open_sealed(key, seal(key, body, aad=AAD), aad=AAD) == body


def test_sealing_twice_produces_different_ciphertext() -> None:
    """Production change that would make this fail: a fixed nonce.

    A reused nonce under AES-GCM is not a weakened cipher, it is a broken one —
    two messages under the same key and nonce leak their XOR.
    """
    key = derive_fileset_key(SECRET)
    body = b"identical plaintext"
    assert seal(key, body, aad=AAD) != seal(key, body, aad=AAD)


def test_a_tampered_blob_is_rejected() -> None:
    """Production change that would make this fail: decrypting without
    verifying the GCM tag."""
    key = derive_fileset_key(SECRET)
    sealed = bytearray(seal(key, b"the auth store", aad=AAD))
    sealed[-1] ^= 0x01
    with pytest.raises(FilesetCryptoError):
        open_sealed(key, bytes(sealed), aad=AAD)


def test_the_wrong_secret_cannot_open_a_blob() -> None:
    with pytest.raises(FilesetCryptoError):
        open_sealed(
            derive_fileset_key(OTHER),
            seal(derive_fileset_key(SECRET), b"x", aad=AAD),
            aad=AAD,
        )


def test_truncated_input_raises_rather_than_indexing_off_the_end() -> None:
    """A blob shorter than the nonce must fail as a crypto error, not an
    IndexError leaking out of the parser."""
    with pytest.raises(FilesetCryptoError):
        open_sealed(derive_fileset_key(SECRET), b"\x00\x01\x02", aad=AAD)


def test_the_fileset_key_is_not_the_cluster_secret() -> None:
    """Production change that would make this fail: using the secret directly
    as the AES key instead of deriving it.

    ADR-002 already uses the raw secret as an HMAC key. If the same bytes were
    reused here, a compromise of one would be a compromise of both.

    The comparison this replaced was `derive_fileset_key(SECRET) !=
    SECRET.encode()` -- 32 bytes against 21, which cannot fail whatever the
    implementation does. What it has to rule out is every *plausible* way of
    turning a short secret into 32 bytes without deriving: padding it,
    repeating it, and hashing it bare.
    """
    import hashlib

    key = derive_fileset_key(SECRET)
    raw = SECRET.encode()

    assert len(key) == 32
    assert key != raw.ljust(32, b"\0")
    assert key != (raw * 32)[:32]
    assert key != hashlib.sha256(raw).digest()
    # And the derivation is domain-separated, not just "some hash of the
    # secret": a different info string has to give a different key, or the
    # version marker in FILESET_INFO buys nothing.
    assert key != HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"something-else"
    ).derive(raw)


def test_key_derivation_is_deterministic_across_nodes() -> None:
    assert derive_fileset_key(SECRET) == derive_fileset_key(SECRET)


def test_blob_ref_is_stable_for_identical_content() -> None:
    assert blob_ref(SECRET, b"same bytes") == blob_ref(SECRET, b"same bytes")


def test_blob_ref_differs_for_different_content() -> None:
    assert blob_ref(SECRET, b"a") != blob_ref(SECRET, b"b")


def test_blob_ref_is_not_the_bare_content_hash() -> None:
    """Production change that would make this fail: `ref = sha256(plaintext)`.

    A bare hash lets anyone with read access to Valkey confirm a file's
    contents by guessing them. Keying it with the cluster secret closes that
    for nothing.
    """
    import hashlib

    assert blob_ref(SECRET, b"guessable") != hashlib.sha256(b"guessable").hexdigest()
    assert blob_ref(SECRET, b"guessable") != blob_ref(OTHER, b"guessable")


# -- I6: a sealed value is bound to the slot it is stored in ----------------


def test_a_blob_will_not_open_under_a_different_ref() -> None:
    """Production change that would make this fail: `associated_data=None`.

    This is the whole of I6. The manifest is authenticated, so `path -> ref` is
    trustworthy — but `ref -> ciphertext` was not. Someone with *write* access
    to Valkey and no secret at all could copy a blob's ciphertext under another
    ref and have the next pull write one file's plaintext to another file's
    path: `.storage/http` over `.storage/auth`, say, with a manifest that
    verified and a pull that reported success.
    """
    key = derive_fileset_key(SECRET)
    body = b'{"refresh_tokens": ["t1"]}'
    other_ref = blob_ref(SECRET, b"a different file entirely")
    sealed = seal(key, body, aad=blob_aad(REF))

    assert open_sealed(key, sealed, aad=blob_aad(REF)) == body
    with pytest.raises(FilesetCryptoError):
        open_sealed(key, sealed, aad=blob_aad(other_ref))


def test_a_blob_cannot_be_passed_off_as_the_manifest() -> None:
    """Refs are lowercase hex and `MANIFEST_AAD` is not, so the two spaces
    cannot collide — but that is a property of the constants, and this asserts
    it rather than assuming it."""
    key = derive_fileset_key(SECRET)
    sealed_blob = seal(key, b'{"generation": 1}', aad=blob_aad(REF))

    with pytest.raises(FilesetCryptoError):
        open_sealed(key, sealed_blob, aad=MANIFEST_AAD)


def test_the_sealed_format_is_versioned_so_a_change_is_loud() -> None:
    """Nothing is deployed, so changing the format is free — but a *future*
    change must not silently accept values sealed under the old rules.

    `FILESET_INFO` is the version marker, and it is an HKDF info string, so a
    bump changes the derived key and every previously published blob stops
    authenticating. That is the loud direction: the pull writes `verify_failed`
    and the swap refuses to install. Accepting the old bytes would be the quiet
    one, and quiet is what AR-0040 was.
    """
    key_v2 = derive_fileset_key(SECRET)
    key_v1 = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"cluster_state_sync:fileset:v1",
    ).derive(SECRET.encode())

    assert FILESET_INFO == b"cluster_state_sync:fileset:v2"
    assert key_v2 != key_v1
    with pytest.raises(FilesetCryptoError):
        open_sealed(key_v2, seal(key_v1, b"old", aad=blob_aad(REF)), aad=blob_aad(REF))


def test_seal_refuses_to_be_called_without_a_binding() -> None:
    """Production change that would make this fail: giving `aad` a default.

    A default is how this hole comes back — silently, because a ciphertext
    sealed with no binding decrypts perfectly well and nothing downstream can
    tell.
    """
    with pytest.raises(TypeError):
        seal(derive_fileset_key(SECRET), b"x")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        open_sealed(derive_fileset_key(SECRET), b"x" * 40)  # type: ignore[call-arg]
