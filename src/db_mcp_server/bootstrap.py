"""Bootstrap importer (milestone M8).

Reads a single ``bootstrap.yaml`` and splits it into the two files the server
uses:

  * ``registry.yaml`` — non-secret routing (tunnel profiles + dbname + secret_ref)
  * ``vault.enc``     — the credentials, sealed with the passphrase

``bootstrap.yaml`` has the same shape as ``registry.yaml`` but each service also
carries the raw ``user``/``password``. The ``secret_ref`` is derived as
``customer/env/service`` — it is not written by hand. This keeps the process
easy to read and explain: one input file in, two files out.

``bootstrap.yaml`` contains PLAINTEXT credentials — it is git-ignored and should
be deleted (or kept offline) once the vault is generated. This tool is
operator-run and NEVER prints secret values.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from . import config
from .models import Credential, TunnelProfile
from .vault import Vault


# --- Shape of bootstrap.yaml (registry.yaml + raw credentials) ---
class BootstrapDefaults(BaseModel):
    max_rows: int = 1000
    statement_timeout_ms: int = 30000
    tunnel_idle_timeout_s: int = 300


class BootstrapService(BaseModel):
    dbname: str
    user: str
    password: str


class BootstrapEnvironment(BaseModel):
    tunnel: TunnelProfile
    services: dict[str, BootstrapService]


class BootstrapCustomer(BaseModel):
    environments: dict[str, BootstrapEnvironment]


class BootstrapModel(BaseModel):
    defaults: BootstrapDefaults = Field(default_factory=BootstrapDefaults)
    customers: dict[str, BootstrapCustomer]


@dataclass
class Report:
    secret_refs: list[str] = field(default_factory=list)
    customers: int = 0
    environments: int = 0


def build(bootstrap_data: dict) -> tuple[dict, dict, Report]:
    """Split validated bootstrap data into (registry_dict, secrets_map, report).

    ``registry_dict`` is credential-free; ``secrets_map`` is ``secret_ref ->
    {user, password}``.
    """
    model = BootstrapModel.model_validate(bootstrap_data)
    report = Report()
    customers: dict = {}
    secrets: dict[str, dict[str, str]] = {}

    for cname, cust in model.customers.items():
        report.customers += 1
        env_out: dict = {}
        for ename, env in cust.environments.items():
            report.environments += 1
            services_out: dict = {}
            for sname, svc in env.services.items():
                ref = f"{cname}/{ename}/{sname}"
                services_out[sname] = {"dbname": svc.dbname, "secret_ref": ref}
                secrets[ref] = {"user": svc.user, "password": svc.password}
                report.secret_refs.append(ref)
            env_out[ename] = {"tunnel": env.tunnel.model_dump(), "services": services_out}
        customers[cname] = {"environments": env_out}

    registry = {"defaults": model.defaults.model_dump(), "customers": customers}
    return registry, secrets, report


def _passphrase() -> str:
    return config.passphrase_from_env() or getpass.getpass("Vault passphrase: ")


def _print_report(report: Report, *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}customers    : {report.customers}")
    print(f"{prefix}environments : {report.environments}")
    print(f"{prefix}services     : {len(report.secret_refs)}")
    for ref in sorted(report.secret_refs):
        print(f"  - {ref}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="db-bootstrap",
        description="Split bootstrap.yaml into registry.yaml + vault.enc.",
    )
    parser.add_argument("--bootstrap-file", type=Path, default=None,
                        help="Path to bootstrap.yaml (default: DB_MCP_BOOTSTRAP or <root>/bootstrap.yaml).")
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing vault.enc.")
    args = parser.parse_args(argv)

    cfg = config.load_config()
    bootstrap_file = args.bootstrap_file or cfg.bootstrap_path
    if not bootstrap_file.exists():
        print(f"error: bootstrap file not found: {bootstrap_file}", file=sys.stderr)
        return 1

    data = yaml.safe_load(bootstrap_file.read_text("utf-8")) or {}
    registry, secrets, report = build(data)
    _print_report(report, dry_run=args.dry_run)

    if args.dry_run:
        return 0

    if cfg.vault_path.exists() and not args.force:
        print(f"error: {cfg.vault_path} already exists; pass --force to overwrite",
              file=sys.stderr)
        return 1

    passphrase = _passphrase()
    vault = Vault.create_empty(passphrase)
    for ref, cred in secrets.items():
        vault.set(ref, Credential(user=cred["user"], password=cred["password"]))
    vault.save(cfg.vault_path)

    cfg.registry_path.write_text(
        yaml.safe_dump(registry, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    print(f"wrote {cfg.registry_path} and {cfg.vault_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
