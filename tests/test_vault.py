"""Tests for the Vault (milestone M2 acceptance)."""

from __future__ import annotations

import pytest

from db_mcp_server.errors import DbMcpError, ErrorCode
from db_mcp_server.models import Credential
from db_mcp_server.vault import Vault


PASSPHRASE = "correct horse battery staple"
REF = "prefmaringa/live/pessoa-api"


def _seed_vault(path, passphrase=PASSPHRASE) -> None:
    v = Vault.create_empty(passphrase)
    v.set(REF, Credential(user="svc_pessoa", password="s3cr3t"))
    v.set("planserv/live/pessoa-api", Credential(user="svc_pv", password="pw2"))
    v.save(path)


def test_set_save_reopen_persists(tmp_path):
    vault_file = tmp_path / "vault.enc"
    _seed_vault(vault_file)

    reopened = Vault.open(vault_file, PASSPHRASE)
    cred = reopened.get(REF)
    assert cred.user == "svc_pessoa"
    assert cred.password.get_secret_value() == "s3cr3t"


def test_open_wrong_passphrase_raises_vault_locked(tmp_path):
    vault_file = tmp_path / "vault.enc"
    _seed_vault(vault_file)

    with pytest.raises(DbMcpError) as ei:
        Vault.open(vault_file, "not the passphrase")
    assert ei.value.code is ErrorCode.VAULT_LOCKED


def test_open_missing_file_raises_vault_locked(tmp_path):
    with pytest.raises(DbMcpError) as ei:
        Vault.open(tmp_path / "does-not-exist.enc", PASSPHRASE)
    assert ei.value.code is ErrorCode.VAULT_LOCKED


def test_get_missing_ref_raises_secret_not_found(tmp_path):
    vault_file = tmp_path / "vault.enc"
    _seed_vault(vault_file)
    vault = Vault.open(vault_file, PASSPHRASE)

    with pytest.raises(DbMcpError) as ei:
        vault.get("prefmaringa/live/nope-api")
    assert ei.value.code is ErrorCode.SECRET_NOT_FOUND


def test_keys_returns_sorted_refs_only(tmp_path):
    vault_file = tmp_path / "vault.enc"
    _seed_vault(vault_file)
    vault = Vault.open(vault_file, PASSPHRASE)

    keys = vault.keys()
    assert keys == sorted(keys)
    assert REF in keys
    # keys() must never surface secret material.
    joined = " ".join(keys)
    assert "svc_pessoa" not in joined
    assert "s3cr3t" not in joined


def test_remove_then_missing(tmp_path):
    vault_file = tmp_path / "vault.enc"
    _seed_vault(vault_file)
    vault = Vault.open(vault_file, PASSPHRASE)

    vault.remove(REF)
    with pytest.raises(DbMcpError) as ei:
        vault.get(REF)
    assert ei.value.code is ErrorCode.SECRET_NOT_FOUND

    with pytest.raises(DbMcpError):
        vault.remove(REF)  # removing again is SECRET_NOT_FOUND


def test_ciphertext_on_disk_has_no_plaintext_secret(tmp_path):
    vault_file = tmp_path / "vault.enc"
    _seed_vault(vault_file)
    blob = vault_file.read_bytes()
    assert b"svc_pessoa" not in blob
    assert b"s3cr3t" not in blob
