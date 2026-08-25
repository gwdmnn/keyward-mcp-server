"""Tests for the db-vault operator CLI (milestone M4 acceptance)."""

from __future__ import annotations

import json
import textwrap

import pytest

from db_mcp_server import config, vault_admin
from db_mcp_server.vault import Vault


PASSPHRASE = "test-pass"

REGISTRY_YAML = textwrap.dedent(
    """
    customers:
      prefmaringa:
        environments:
          live:
            tunnel:
              jump_host: h
              jump_user: u
              pem_key: k.pem
              db_host: db
            services:
              pessoa-api:
                dbname: pessoa_api
                secret_ref: prefmaringa/live/pessoa-api
              cadastro-cliente-api:
                dbname: cadastro_cliente_api
                secret_ref: prefmaringa/live/cadastro-cliente-api
    """
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault_path = tmp_path / "vault.enc"
    registry_path = tmp_path / "registry.yaml"
    monkeypatch.setenv(config.ENV_PASSPHRASE, PASSPHRASE)
    monkeypatch.setenv(config.ENV_VAULT, str(vault_path))
    monkeypatch.setenv(config.ENV_REGISTRY, str(registry_path))
    return {"vault": vault_path, "registry": registry_path}


def _answer_set(monkeypatch, user: str, password: str) -> None:
    """Feed the interactive prompts for `set`: user via input, password via getpass."""
    monkeypatch.setattr("builtins.input", lambda *a, **k: user)
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: password)


def test_set_persists_and_reads_back(env, monkeypatch):
    _answer_set(monkeypatch, "svc_user", "s3cr3t")
    rc = vault_admin.main(["set", "prefmaringa/live/pessoa-api"])
    assert rc == 0

    cred = Vault.open(env["vault"], PASSPHRASE).get("prefmaringa/live/pessoa-api")
    assert cred.user == "svc_user"
    assert cred.password.get_secret_value() == "s3cr3t"


def test_set_password_mismatch_aborts(env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "svc_user")
    answers = iter(["first", "second"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))

    rc = vault_admin.main(["set", "prefmaringa/live/pessoa-api"])
    assert rc == 1
    assert not env["vault"].exists()  # nothing written on mismatch


def test_list_shows_keys_only(env, monkeypatch, capsys):
    _answer_set(monkeypatch, "svc_user", "s3cr3t")
    vault_admin.main(["set", "prefmaringa/live/pessoa-api"])
    capsys.readouterr()  # discard set output

    rc = vault_admin.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "prefmaringa/live/pessoa-api" in out
    assert "svc_user" not in out
    assert "s3cr3t" not in out


def test_rm_removes_then_missing(env, monkeypatch):
    _answer_set(monkeypatch, "u", "p")
    vault_admin.main(["set", "prefmaringa/live/pessoa-api"])

    assert vault_admin.main(["rm", "prefmaringa/live/pessoa-api"]) == 0
    assert Vault.open(env["vault"], PASSPHRASE).keys() == []

    # removing a non-existent ref -> SECRET_NOT_FOUND -> non-zero exit
    assert vault_admin.main(["rm", "prefmaringa/live/pessoa-api"]) == 1


def test_wrong_passphrase_on_existing_vault(env, monkeypatch):
    _answer_set(monkeypatch, "u", "p")
    vault_admin.main(["set", "prefmaringa/live/pessoa-api"])

    monkeypatch.setenv(config.ENV_PASSPHRASE, "wrong-pass")
    assert vault_admin.main(["list"]) == 1  # VAULT_LOCKED -> non-zero


def test_verify_reports_missing_and_orphan(env, monkeypatch, capsys):
    env["registry"].write_text(REGISTRY_YAML, encoding="utf-8")
    _answer_set(monkeypatch, "u", "p")
    vault_admin.main(["set", "prefmaringa/live/pessoa-api"])      # matches catalog
    vault_admin.main(["set", "prefmaringa/live/orphan-api"])      # not in catalog
    capsys.readouterr()

    rc = vault_admin.main(["verify"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["ok"] is False
    assert report["missing_in_vault"] == ["prefmaringa/live/cadastro-cliente-api"]
    assert report["orphan_in_vault"] == ["prefmaringa/live/orphan-api"]


def test_verify_ok_when_aligned(env, monkeypatch, capsys):
    env["registry"].write_text(REGISTRY_YAML, encoding="utf-8")
    _answer_set(monkeypatch, "u", "p")
    vault_admin.main(["set", "prefmaringa/live/pessoa-api"])
    vault_admin.main(["set", "prefmaringa/live/cadastro-cliente-api"])
    capsys.readouterr()

    rc = vault_admin.main(["verify"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report == {"ok": True, "missing_in_vault": [], "orphan_in_vault": []}
