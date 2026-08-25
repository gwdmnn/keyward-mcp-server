"""Cryptographic primitives for the credential vault (milestone M1).

Envelope encryption: a passphrase is stretched into a 256-bit key with scrypt,
and the plaintext is sealed with AES-256-GCM (authenticated encryption). Every
seal/encrypt generates a fresh random nonce — reusing a nonce with the same key
breaks GCM catastrophically, so this is never optional.

The on-disk envelope shape matches FDD §5 (Contract 5):

    {
      "kdf": "scrypt",
      "kdf_params": {"n": 16384, "r": 8, "p": 1, "salt_b64": "..."},
      "cipher": "AES-256-GCM",
      "nonce_b64": "...",
      "ciphertext_b64": "..."
    }

This module knows nothing about the vault's contents or the MCP error codes; it
deals only in bytes and envelopes. Mapping a decryption failure to VAULT_LOCKED
happens one layer up, in vault.py.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# --- Parameters (defaults; the actual values used are recorded in each envelope) ---
KEY_LEN = 32       # 256-bit key for AES-256-GCM
SALT_LEN = 16      # scrypt salt
NONCE_LEN = 12     # 96-bit nonce, the standard/optimal size for AES-GCM

SCRYPT_N = 2 ** 14  # 16384 — CPU/memory cost factor
SCRYPT_R = 8
SCRYPT_P = 1


class DecryptionError(Exception):
    """Raised when an envelope cannot be authenticated/decrypted.

    Almost always means the wrong passphrase (bad derived key) or a tampered
    ciphertext — GCM's authentication tag fails identically for both.
    """


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def derive_key(
    passphrase: str,
    salt: bytes,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    """Stretch a passphrase into a 32-byte key with scrypt.

    Deterministic for a given (passphrase, salt, n, r, p). A fresh random salt
    per vault means the same passphrase yields a different key each time.
    """
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(plaintext: bytes, key: bytes) -> dict[str, str]:
    """Seal plaintext with AES-256-GCM under ``key``, using a fresh nonce.

    Returns only the cipher portion of the envelope (cipher/nonce/ciphertext);
    the KDF header is added by :func:`seal`.
    """
    nonce = os.urandom(NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return {
        "cipher": "AES-256-GCM",
        "nonce_b64": _b64e(nonce),
        "ciphertext_b64": _b64e(ciphertext),
    }


def decrypt(envelope: dict[str, Any], key: bytes) -> bytes:
    """Authenticate and decrypt an envelope's cipher portion under ``key``.

    Raises :class:`DecryptionError` if the tag does not verify (wrong key or
    tampered ciphertext).
    """
    nonce = _b64d(envelope["nonce_b64"])
    ciphertext = _b64d(envelope["ciphertext_b64"])
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise DecryptionError(
            "authentication failed: wrong passphrase or corrupted/tampered data"
        ) from exc


def seal(
    plaintext: bytes,
    passphrase: str,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> dict[str, Any]:
    """Produce a full self-describing envelope from plaintext + passphrase.

    Generates a fresh salt and nonce, derives the key, and records the KDF
    parameters so :func:`unseal` can reproduce the key from the passphrase alone.
    """
    salt = os.urandom(SALT_LEN)
    key = derive_key(passphrase, salt, n=n, r=r, p=p)
    envelope: dict[str, Any] = {
        "kdf": "scrypt",
        "kdf_params": {"n": n, "r": r, "p": p, "salt_b64": _b64e(salt)},
    }
    envelope.update(encrypt(plaintext, key))
    return envelope


def unseal(envelope: dict[str, Any], passphrase: str) -> bytes:
    """Reverse :func:`seal`: re-derive the key from the passphrase and decrypt.

    Raises :class:`DecryptionError` on a wrong passphrase or tampered envelope.
    """
    params = envelope["kdf_params"]
    salt = _b64d(params["salt_b64"])
    key = derive_key(
        passphrase, salt, n=params["n"], r=params["r"], p=params["p"]
    )
    return decrypt(envelope, key)


def atomic_write(path: str | Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + fsync + os.replace).

    Guarantees the destination is either the old contents or the fully-written
    new contents — never a truncated mix — so a crash mid-write cannot corrupt
    the only copy of the vault. ``os.replace`` overwrites atomically on Windows
    and POSIX alike.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
