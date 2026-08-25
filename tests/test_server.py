"""Tests for the MCP server wiring (milestone M7 acceptance).

Exercises the tool implementations and startup against real catalog/vault, a
fake tunnel forwarder, and a patched executor connection — no network.
"""

from __future__ import annotations

import textwrap

import pytest

from db_mcp_server import config, executor, server
from db_mcp_server.catalog import Catalog
from db_mcp_server.models import Credential, ErrorResult, QueryResult, WriteResult
from db_mcp_server.tunnel_manager import TunnelManager
from db_mcp_server.vault import Vault


REGISTRY = textwrap.dedent(
    """
    defaults:
      max_rows: 100
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


class FakeForwarder:
    def __init__(self, port=15000):
        self.local_bind_port = port
        self.is_active = False

    def start(self):
        self.is_active = True

    def stop(self):
        self.is_active = False


class _Col:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, columns=None, rows=None, rowcount=None):
        self.description = [_Col(c) for c in columns] if columns is not None else None
        self._rows = list(rows or [])
        self.rowcount = rowcount if rowcount is not None else len(self._rows)

    def fetchmany(self, size):
        return list(self._rows[:size])


class FakeConn:
    def __init__(self, cursor=None):
        self._cursor = cursor
        self.read_only = None

    def execute(self, sql):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_state(tmp_path):
    reg = tmp_path / "registry.yaml"
    reg.write_text(REGISTRY, encoding="utf-8")
    catalog = Catalog.load(reg)
    vault = Vault.create_empty("pass")
    vault.set("prefmaringa/live/pessoa-api", Credential(user="u", password="p"))
    # cadastro-cliente-api is intentionally left out of the vault
    tunnels = TunnelManager(tmp_path / "keys", forwarder_factory=lambda p: FakeForwarder())
    return server._State(config.load_config(), vault, catalog, tunnels)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_state", _make_state(tmp_path))


def test_list_databases_has_no_secrets(wired):
    view = server._list_databases()
    assert view["customers"][0]["customer"] == "prefmaringa"
    assert "secret_ref" not in str(view)
    assert "k.pem" not in str(view)


def test_run_query_success(wired, monkeypatch):
    monkeypatch.setattr(
        executor, "_connect", lambda *a, **k: FakeConn(FakeCursor(["id"], [(1,), (2,)]))
    )
    res = server._run_query("prefmaringa", "live", "pessoa-api", "SELECT id FROM t", None)
    assert isinstance(res, QueryResult)
    assert res.rows == [[1], [2]] and res.row_count == 2


def test_run_query_catalog_not_found(wired):
    res = server._run_query("prefmaringa", "live", "ghost-api", "SELECT 1", None)
    assert isinstance(res, ErrorResult) and res.error_code == "CATALOG_NOT_FOUND"


def test_run_query_secret_not_found(wired):
    # service is in the catalog but has no vault entry
    res = server._run_query("prefmaringa", "live", "cadastro-cliente-api", "SELECT 1", None)
    assert isinstance(res, ErrorResult) and res.error_code == "SECRET_NOT_FOUND"


def test_run_write_requires_confirm_and_does_not_connect(wired, monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "_connect", lambda *a, **k: calls.append(1))
    res = server._run_write_query("prefmaringa", "live", "pessoa-api", "UPDATE t SET x=1", False)
    assert isinstance(res, ErrorResult) and res.error_code == "WRITE_NOT_CONFIRMED"
    assert calls == []


def test_run_write_success(wired, monkeypatch):
    monkeypatch.setattr(executor, "_connect", lambda *a, **k: FakeConn(FakeCursor(rowcount=5)))
    res = server._run_write_query("prefmaringa", "live", "pessoa-api", "UPDATE t SET x=1", True)
    assert isinstance(res, WriteResult) and res.affected_rows == 5 and res.committed is True


def test_main_fails_without_passphrase(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_PASSPHRASE, raising=False)
    monkeypatch.setenv(config.ENV_VAULT, str(tmp_path / "vault.enc"))
    monkeypatch.setenv(config.ENV_REGISTRY, str(tmp_path / "registry.yaml"))
    # startup fails before mcp.run(), so main returns 1 without blocking
    assert server.main([]) == 1


def test_startup_opens_vault_and_builds_state(tmp_path, monkeypatch):
    reg = tmp_path / "registry.yaml"
    reg.write_text(REGISTRY, encoding="utf-8")
    vault_file = tmp_path / "vault.enc"
    v = Vault.create_empty("pass")
    v.set("prefmaringa/live/pessoa-api", Credential(user="u", password="p"))
    v.save(vault_file)

    monkeypatch.setenv(config.ENV_PASSPHRASE, "pass")
    monkeypatch.setenv(config.ENV_VAULT, str(vault_file))
    monkeypatch.setenv(config.ENV_REGISTRY, str(reg))
    monkeypatch.setenv(config.ENV_KEYS_DIR, str(tmp_path / "keys"))

    state = server._startup()
    assert state.catalog.resolve("prefmaringa", "live", "pessoa-api").dbname == "pessoa_api"
    assert "prefmaringa/live/pessoa-api" in state.vault.keys()
