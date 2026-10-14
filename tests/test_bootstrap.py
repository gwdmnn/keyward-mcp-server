"""Tests for the bootstrap importer (milestone M8 acceptance).

Bootstrap reads a single bootstrap.yaml (registry shape + raw credentials) and
splits it into a non-secret registry.yaml and an encrypted vault.enc.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from db_mcp_server import bootstrap, config
from db_mcp_server.catalog import Catalog
from db_mcp_server.vault import Vault


BOOTSTRAP_YAML = textwrap.dedent(
    """
    defaults:
      max_rows: 500
      statement_timeout_ms: 15000

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
                dbname: sama_pessoa_api
                db_host: maringa-db-live.maida.health
                db_port: 5432
                user: svc_pessoa
                password: pw_pessoa
              cadastro-cliente-api:
                dbname: sama_cadastro_cliente_api
                db_host: maringa-db-live-2.maida.health
                user: svc_cad
                password: pw_cad
    """
)


def _data():
    return yaml.safe_load(BOOTSTRAP_YAML)


# --- unit: the split ---
def test_build_derives_secret_refs_and_strips_credentials():
    registry, secrets, report = bootstrap.build(_data())

    # secret_ref derived as customer/env/service
    assert set(report.secret_refs) == {
        "prefmaringa/live/pessoa-api",
        "prefmaringa/live/cadastro-cliente-api",
    }
    assert report.customers == 1 and report.environments == 1

    svc = registry["customers"]["prefmaringa"]["environments"]["live"]["services"]["pessoa-api"]
    assert svc == {
        "dbname": "sama_pessoa_api",
        "db_host": "maringa-db-live.maida.health",
        "db_port": 5432,
        "secret_ref": "prefmaringa/live/pessoa-api",
    }
    # registry carries NO credentials anywhere
    assert "user" not in svc and "password" not in svc

    assert secrets["prefmaringa/live/pessoa-api"] == {"user": "svc_pessoa", "password": "pw_pessoa"}
    assert registry["defaults"]["max_rows"] == 500


def test_build_puts_db_host_on_services_not_tunnel():
    registry, _secrets, _report = bootstrap.build(_data())
    env = registry["customers"]["prefmaringa"]["environments"]["live"]
    assert "db_host" not in env["tunnel"] and "db_port" not in env["tunnel"]
    cad = env["services"]["cadastro-cliente-api"]
    assert cad["db_host"] == "maringa-db-live-2.maida.health"
    assert cad["db_port"] == 5432  # default when omitted


def test_registry_has_no_plaintext_credentials():
    registry, _secrets, _report = bootstrap.build(_data())
    blob = yaml.safe_dump(registry)
    assert "pw_pessoa" not in blob and "svc_pessoa" not in blob


def test_build_output_validates_as_catalog(tmp_path):
    registry, _secrets, _report = bootstrap.build(_data())
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")

    cat = Catalog.load(registry_path)
    r = cat.resolve("prefmaringa", "live", "pessoa-api")
    assert r.dbname == "sama_pessoa_api"
    assert r.secret_ref == "prefmaringa/live/pessoa-api"
    assert r.tunnel_profile.jump_user == "maringa-tunnel"
    assert r.max_rows == 500


def test_build_rejects_malformed_input():
    from pydantic import ValidationError

    bad = {"customers": {"c": {"environments": {"live": {"services": {}}}}}}  # missing tunnel
    with pytest.raises(ValidationError):
        bootstrap.build(bad)


# --- integration: the CLI ---
@pytest.fixture
def env(tmp_path, monkeypatch):
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(BOOTSTRAP_YAML, encoding="utf-8")
    vault_path = tmp_path / "vault.enc"
    registry_path = tmp_path / "registry.yaml"
    monkeypatch.setenv(config.ENV_PASSPHRASE, "pass")
    monkeypatch.setenv(config.ENV_BOOTSTRAP, str(bootstrap_file))
    monkeypatch.setenv(config.ENV_VAULT, str(vault_path))
    monkeypatch.setenv(config.ENV_REGISTRY, str(registry_path))
    return {"bootstrap": bootstrap_file, "vault": vault_path, "registry": registry_path}


def test_dry_run_writes_nothing(env):
    assert bootstrap.main(["--dry-run"]) == 0
    assert not env["vault"].exists()
    assert not env["registry"].exists()


def test_full_run_writes_registry_and_vault(env):
    assert bootstrap.main([]) == 0
    assert env["vault"].exists() and env["registry"].exists()

    vault = Vault.open(env["vault"], "pass")
    assert vault.get("prefmaringa/live/pessoa-api").password.get_secret_value() == "pw_pessoa"

    # generated registry is credential-free on disk
    assert "pw_pessoa" not in env["registry"].read_text(encoding="utf-8")

    # refuses to clobber the vault without --force
    assert bootstrap.main([]) == 1
    assert bootstrap.main(["--force"]) == 0


def test_missing_bootstrap_file(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_BOOTSTRAP, str(tmp_path / "nope.yaml"))
    assert bootstrap.main([]) == 1
