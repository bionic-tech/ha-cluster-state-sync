"""Sealing for the replicated fileset (design §5).

Three properties matter here and are easy to conflate.

**The key is derived, not reused.** ADR-002 keys its HMAC with the cluster
secret directly. Using those same bytes as an AES key would mean one key
compromise was two, so this derives a separate key with a distinct HKDF info
string. HKDF is one-way, so holding `cluster-fileset.key` — which is what
actually sits on the follower's disk — does not hand anyone the cluster secret.

An earlier version of this docstring went further and said the two uses "fail
independently". **They do not, and they cannot.** `.storage/core.config_entries`
holds `cluster_secret` in the clear, and that file is itself sealed and
published — so the fileset key plus read access to Valkey yields the cluster
secret, and with it ADR-002's HMAC key. The whole point of this feature is to
replicate the credential store, and this integration's own credentials live in
it. Redacting that one entry before sealing would narrow the exposure without
removing it — `secrets.yaml` and every integration the operator configured with
a shared passphrase are replicated in the clear too — while putting a JSON
rewrite of `core.config_entries` into the publish path, on the leader, on a
sixty-second timer, in a file a promotion depends on being faithful. So this
says what is true instead of claiming a property the design cannot deliver.

**The blob reference is keyed, not a bare hash.** Blobs are content-addressed,
so the reference has to be a function of the plaintext — but a bare
`sha256(plaintext)` would let anyone with read access to Valkey confirm a
file's contents by guessing them and comparing. HMAC-ing the digest under the
cluster secret keeps content-addressing and removes the oracle.

**Every sealed value is bound to the slot it is stored in.** The manifest is
authenticated, so `path -> ref` is trustworthy; without associated data
`ref -> ciphertext` was not. Someone with *write* access to Valkey and no
secret at all could move a blob's ciphertext under a different ref — having the
pull write one file's plaintext to another file's path — or restore a single
old blob under its own ref and roll that one file back. Neither needs the key,
because AES-GCM only authenticates the bytes it was given. Passing the ref as
associated data makes the ciphertext refuse to open under any other reference.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: Distinct from every other use of the cluster secret, and the version marker
#: for the sealed format as a whole.
#:
#: Changing this string changes the derived key, so every previously published
#: blob stops authenticating — which is the point. The v1 -> v2 bump is the
#: introduction of associated data: a v1 ciphertext carries no binding to its
#: ref, and silently accepting one would leave exactly the substitution this
#: version exists to prevent. Failing to open is loud (the pull writes
#: `verify_failed` and the swap refuses to install), where accepting is not.
FILESET_INFO: Final[bytes] = b"cluster_state_sync:fileset:v2"

#: Associated data for the manifest. Refs are lowercase hex, so this can never
#: collide with a `blob_aad` value — a blob ciphertext cannot be moved into the
#: manifest key, nor the manifest into a blob key.
MANIFEST_AAD: Final[bytes] = b"cluster_state_sync:fileset:manifest"

#: The statistics window's own label, distinct from the manifest's for the
#: same reason each blob is sealed under its own ref: a ciphertext moved
#: from one key to another must fail to open rather than be applied as
#: something it is not. Moving a manifest onto the statistics key needs
#: Valkey write access and no secret at all.
STATISTICS_AAD: Final[bytes] = b"cluster_state_sync:statistics:window"

#: The follower's status line, sealed under its own label so the window's
#: ciphertext cannot be replayed onto the status key or the reverse.
#: AR-0045: this channel was plain JSON, on the reasoning that a follower
#: with no cluster key must still be able to say "I could not apply". That
#: does not survive contact with the code -- the puller reads the key file
#: before it connects, so a keyless follower never reaches the point of
#: having anything to report. What the plain channel actually bought was a
#: way for anyone with Valkey write access, and no secret at all, to put
#: chosen text on the leader's repairs panel.
STATISTICS_STATUS_AAD: Final[bytes] = b"cluster_state_sync:statistics:follower"

_KEY_BYTES: Final = 32
_NONCE_BYTES: Final = 12


class FilesetCryptoError(Exception):
    """A blob could not be opened: wrong key, tampered, moved, or truncated."""


def derive_fileset_key(secret: str) -> bytes:
    """Derive the AES-256 key for fileset blobs from the cluster secret."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=FILESET_INFO,
    ).derive(secret.encode("utf-8"))


def blob_ref(secret: str, plaintext: bytes) -> str:
    """Return the content-addressed reference for `plaintext`."""
    return hmac.new(
        secret.encode("utf-8"), hashlib.sha256(plaintext).digest(), hashlib.sha256
    ).hexdigest()


def blob_aad(ref: str) -> bytes:
    """Associated data binding a sealed blob to the ref it is stored under."""
    return b"cluster_state_sync:fileset:blob:" + ref.encode("utf-8")


def seal(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
    """Encrypt `plaintext`, returning `nonce || ciphertext||tag`.

    A fresh nonce per call is not optional. Under AES-GCM a reused
    (key, nonce) pair leaks the XOR of the two plaintexts.

    `aad` is keyword-only and has no default on purpose. A default would let a
    call site forget it and get a ciphertext that opens anywhere, which is the
    hole this parameter closes — and it would do so silently, since a
    ciphertext sealed with no binding decrypts perfectly well.
    """
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, sealed: bytes, *, aad: bytes) -> bytes:
    """Reverse `seal`. Raises `FilesetCryptoError` on any failure.

    Including the failure that matters most here: `sealed` authenticating under
    the key but not under this `aad`, which is a value that was stored — or
    moved — under the wrong reference.
    """
    if len(sealed) <= _NONCE_BYTES:
        raise FilesetCryptoError("sealed blob is shorter than its nonce")
    try:
        return AESGCM(key).decrypt(sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:], aad)
    except InvalidTag as err:
        raise FilesetCryptoError("blob failed authentication") from err
