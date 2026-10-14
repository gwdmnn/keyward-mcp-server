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
            services:
              pessoa-api:
                dbname: pessoa_api
                db_host: maringa-db-live.maida.health
                db_port: 5432
                secret_ref: prefmaringa/live/pessoa-api
              cadastro-cliente-api:
                dbname: cadastro_cliente_api
                db_host: maringa-db-live-2.maida.health
                db_port: 5433
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
    assert r.tunnel_key == ("prefmaringa", "live", "maringa-db-live.maida.health", 5432)
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
        "maringa-db-live-2.maida.health", # db host (split)
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
                services:
                  s1:
                    dbname: d1
                    db_host: db
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


def test_db_host_is_resolved_per_service(catalog):
    pessoa = catalog.resolve("prefmaringa", "live", "pessoa-api")
    cadastro = catalog.resolve("prefmaringa", "live", "cadastro-cliente-api")
    # same jump host, different DB hosts => separate tunnels
    assert pessoa.tunnel_profile.jump_host == cadastro.tunnel_profile.jump_host
    assert cadastro.tunnel_profile.db_host == "maringa-db-live-2.maida.health"
    assert cadastro.tunnel_profile.db_port == 5433
    assert pessoa.tunnel_key != cadastro.tunnel_key


def test_services_on_same_host_share_tunnel_key(tmp_path):
    shared = textwrap.dedent(
        """
        customers:
          c1:
            environments:
              live:
                tunnel: {jump_host: h, jump_user: u, pem_key: k.pem}
                services:
                  s1: {dbname: d1, db_host: db, secret_ref: c1/live/s1}
                  s2: {dbname: d2, db_host: db, secret_ref: c1/live/s2}
        """
    )
    path = tmp_path / "registry.yaml"
    path.write_text(shared, encoding="utf-8")
    cat = Catalog.load(path)
    assert cat.resolve("c1", "live", "s1").tunnel_key == cat.resolve("c1", "live", "s2").tunnel_key


def test_service_without_db_host_is_rejected(tmp_path):
    from pydantic import ValidationError

    # old format: db_host on the tunnel, not on the service
    old = textwrap.dedent(
        """
        customers:
          c1:
            environments:
              live:
                tunnel: {jump_host: h, jump_user: u, pem_key: k.pem, db_host: db}
                services:
                  s1: {dbname: d1, secret_ref: c1/live/s1}
        """
    )
    path = tmp_path / "registry.yaml"
    path.write_text(old, encoding="utf-8")
    with pytest.raises(ValidationError):
        Catalog.load(path)
