"""Tests for the crypto core (milestone M1 acceptance)."""

from __future__ import annotations

import copy

import pytest

from db_mcp_server import crypto


PASSPHRASE = "correct horse battery staple"
PLAINTEXT = b'{"prefmaringa/live/pessoa-api": {"user": "svc", "password": "p@ss"}}'

# Fast scrypt cost for tests — real defaults (N=16384) are slow to run in a loop.
FAST = {"n": 2 ** 10, "r": 8, "p": 1}


def test_seal_unseal_round_trip():
    envelope = crypto.seal(PLAINTEXT, PASSPHRASE, **FAST)
    assert crypto.unseal(envelope, PASSPHRASE) == PLAINTEXT


def test_envelope_shape_matches_contract():
    envelope = crypto.seal(PLAINTEXT, PASSPHRASE, **FAST)
    assert envelope["kdf"] == "scrypt"
    assert envelope["cipher"] == "AES-256-GCM"
    assert set(envelope["kdf_params"]) == {"n", "r", "p", "salt_b64"}
    assert {"nonce_b64", "ciphertext_b64"} <= set(envelope)
    # No plaintext leaks into the envelope in the clear.
    assert b"password" not in crypto._b64d(envelope["ciphertext_b64"])


def test_wrong_passphrase_raises():
    envelope = crypto.seal(PLAINTEXT, PASSPHRASE, **FAST)
    with pytest.raises(crypto.DecryptionError):
        crypto.unseal(envelope, "wrong passphrase")


def test_nonce_and_salt_unique_per_seal():
    a = crypto.seal(PLAINTEXT, PASSPHRASE, **FAST)
    b = crypto.seal(PLAINTEXT, PASSPHRASE, **FAST)
    # Same plaintext + passphrase must still differ everywhere randomised.
    assert a["nonce_b64"] != b["nonce_b64"]
    assert a["kdf_params"]["salt_b64"] != b["kdf_params"]["salt_b64"]
    assert a["ciphertext_b64"] != b["ciphertext_b64"]


def test_tamper_detected():
    envelope = crypto.seal(PLAINTEXT, PASSPHRASE, **FAST)
    tampered = copy.deepcopy(envelope)
    raw = bytearray(crypto._b64d(tampered["ciphertext_b64"]))
    raw[0] ^= 0x01  # flip one bit
    tampered["ciphertext_b64"] = crypto._b64e(bytes(raw))
    with pytest.raises(crypto.DecryptionError):
        crypto.unseal(tampered, PASSPHRASE)


def test_derive_key_deterministic_and_salt_sensitive():
    salt = b"0123456789abcdef"
    k1 = crypto.derive_key(PASSPHRASE, salt, **FAST)
    k2 = crypto.derive_key(PASSPHRASE, salt, **FAST)
    k3 = crypto.derive_key(PASSPHRASE, b"fedcba9876543210", **FAST)
    assert k1 == k2 and len(k1) == crypto.KEY_LEN
    assert k1 != k3  # different salt -> different key


def test_encrypt_decrypt_primitives_round_trip():
    key = crypto.derive_key(PASSPHRASE, b"saltsaltsaltsalt", **FAST)
    env = crypto.encrypt(PLAINTEXT, key)
    assert crypto.decrypt(env, key) == PLAINTEXT


def test_atomic_write_overwrites_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "vault.enc"
    crypto.atomic_write(target, b"first")
    crypto.atomic_write(target, b"second")
    assert target.read_bytes() == b"second"
    assert not (tmp_path / "vault.enc.tmp").exists()
