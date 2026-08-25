"""Tests for config resolution (environment variables + defaults)."""

from __future__ import annotations

from pathlib import Path

from db_mcp_server import config

_ALL = [
    config.ENV_REGISTRY,
    config.ENV_VAULT,
    config.ENV_KEYS_DIR,
    config.ENV_BOOTSTRAP,
    config.ENV_PASSPHRASE,
]


def _clear(monkeypatch):
    for key in _ALL:
        monkeypatch.delenv(key, raising=False)


def test_load_config_reads_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(config.ENV_REGISTRY, "/r/registry.yaml")
    monkeypatch.setenv(config.ENV_VAULT, "/v/vault.enc")
    monkeypatch.setenv(config.ENV_KEYS_DIR, "/k")
    monkeypatch.setenv(config.ENV_BOOTSTRAP, "/b/bootstrap.yaml")

    cfg = config.load_config()
    assert cfg.registry_path == Path("/r/registry.yaml")
    assert cfg.vault_path == Path("/v/vault.enc")
    assert cfg.keys_dir == Path("/k")
    assert cfg.bootstrap_path == Path("/b/bootstrap.yaml")


def test_load_config_defaults(monkeypatch):
    _clear(monkeypatch)
    cfg = config.load_config()
    assert cfg.registry_path == config._ROOT / "registry.yaml"
    assert cfg.vault_path == config._ROOT / "vault.enc"
    assert cfg.keys_dir == config._ROOT / "keys"
    assert cfg.bootstrap_path == config._ROOT / "bootstrap.yaml"


def test_passphrase_from_env(monkeypatch):
    _clear(monkeypatch)
    assert config.passphrase_from_env() is None
    monkeypatch.setenv(config.ENV_PASSPHRASE, "pw")
    assert config.passphrase_from_env() == "pw"
