"""Tests for the routing catalog (milestone M3 acceptance)."""

from __future__ import annotations

import json
import textwrap

import pytest

from db_mcp_server.catalog import Catalog
from db_mcp_server.errors import DbMcpError, ErrorCode


REGISTRY_YAML = textwrap.dedent(
    """
    defaults:
      max_rows: 500
      statement_timeout_ms: 15000
      tunnel_idle_timeout_s: 120

    customers:
      prefmaringa:
        environments:
          live:
            tunnel:
              jump_host: 34.95.223.41
              jump_port: 22
              jump_user: maringa-tunnel
              pem_key: maida-maringa-bastion-key.pem
              db_host: maringa-db-live.maida.health
              db_port: 5432
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
def catalog(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY_YAML, encoding="utf-8")
    return Catalog.load(path)


def test_resolve_valid_target(catalog):
    r = catalog.resolve("prefmaringa", "live", "pessoa-api")
    assert r.dbname == "pessoa_api"
    assert r.secret_ref == "prefmaringa/live/pessoa-api"
    assert r.tunnel_key == ("prefmaringa", "live")
    assert r.tunnel_profile.db_host == "maringa-db-live.maida.health"
    assert r.tunnel_profile.pem_key == "maida-maringa-bastion-key.pem"
    # defaults flow through
    assert r.max_rows == 500
    assert r.statement_timeout_ms == 15000


def test_resolve_unknown_customer(catalog):
    with pytest.raises(DbMcpError) as ei:
        catalog.resolve("nope", "live", "pessoa-api")
    assert ei.value.code is ErrorCode.CATALOG_NOT_FOUND


def test_resolve_unknown_environment(catalog):
    with pytest.raises(DbMcpError) as ei:
        catalog.resolve("prefmaringa", "staging", "pessoa-api")
    assert ei.value.code is ErrorCode.CATALOG_NOT_FOUND


def test_resolve_unknown_service(catalog):
    with pytest.raises(DbMcpError) as ei:
        catalog.resolve("prefmaringa", "live", "ghost-api")
    assert ei.value.code is ErrorCode.CATALOG_NOT_FOUND


def test_list_databases_shape(catalog):
    view = catalog.list_databases()
    assert view == {
        "customers": [
            {
                "customer": "prefmaringa",
                "environments": [
                    {
                        "environment": "live",
                        "services": ["cadastro-cliente-api", "pessoa-api"],
                    }
                ],
            }
        ]
    }


def test_list_databases_leaks_no_secrets(catalog):
    blob = json.dumps(catalog.list_databases())
    for forbidden in (
        "secret_ref",
        "prefmaringa/live/pessoa-api",  # a secret_ref value
        "34.95.223.41",                 # jump host
        "maringa-tunnel",               # jump user
        "maida-maringa-bastion-key.pem",  # PEM filename
        "maringa-db-live.maida.health",   # db host
    ):
        assert forbidden not in blob


def test_secret_refs_collects_all(catalog):
    assert catalog.secret_refs() == {
        "prefmaringa/live/pessoa-api",
        "prefmaringa/live/cadastro-cliente-api",
    }


def test_defaults_applied_when_absent(tmp_path):
    minimal = textwrap.dedent(
        """
        customers:
          c1:
            environments:
              live:
                tunnel:
                  jump_host: h
                  jump_user: u
                  pem_key: k.pem
                  db_host: db
                services:
                  s1:
                    dbname: d1
                    secret_ref: c1/live/s1
        """
    )
    path = tmp_path / "registry.yaml"
    path.write_text(minimal, encoding="utf-8")
    cat = Catalog.load(path)
    r = cat.resolve("c1", "live", "s1")
    # defaults from the model (no `defaults:` block, jump_port/db_port defaults)
    assert r.max_rows == 1000
    assert r.statement_timeout_ms == 30000
    assert r.tunnel_profile.jump_port == 22
    assert r.tunnel_profile.db_port == 5432
