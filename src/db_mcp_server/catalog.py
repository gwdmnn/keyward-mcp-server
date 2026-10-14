"""The routing catalog (milestone M3).

Parses ``registry.yaml`` — the non-secret source of truth that replaces the
markdown mapping table and the scattered ``.env`` files (FDD Contract 4). It maps
``(customer, environment)`` to a jump-host profile and ``(customer, environment,
service)`` to a ``dbname`` + DB host/port + ``secret_ref``. The DB host is set per
service because some customers split their databases across several hosts. It
holds NO secrets: only a reference (``secret_ref``) into the vault.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .errors import DbMcpError, ErrorCode
from .models import JumpProfile, Resolved, TunnelProfile


# --- Parsed shape of registry.yaml (internal to this module) ---
class Defaults(BaseModel):
    max_rows: int = 1000
    statement_timeout_ms: int = 30000
    tunnel_idle_timeout_s: int = 300


class ServiceProfile(BaseModel):
    dbname: str
    db_host: str
    db_port: int = 5432
    secret_ref: str


class EnvironmentProfile(BaseModel):
    tunnel: JumpProfile
    services: dict[str, ServiceProfile]


class CustomerProfile(BaseModel):
    environments: dict[str, EnvironmentProfile]


class RegistryModel(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    customers: dict[str, CustomerProfile]


class Catalog:
    def __init__(self, registry: RegistryModel):
        self._registry = registry

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        """Parse and validate ``registry.yaml``.

        A missing or malformed registry is a startup misconfiguration, so this
        raises the underlying ``FileNotFoundError`` / pydantic ``ValidationError``
        rather than a per-call ``DbMcpError`` — the server should fail loudly at
        launch, not hand the model a routing error.
        """
        text = Path(path).read_text("utf-8")
        data = yaml.safe_load(text) or {}
        return cls(RegistryModel.model_validate(data))

    @property
    def defaults(self) -> Defaults:
        return self._registry.defaults

    @staticmethod
    def tunnel_key(
        customer: str, environment: str, db_host: str, db_port: int
    ) -> tuple[str, str, str, int]:
        return (customer, environment, db_host, db_port)

    def resolve(self, customer: str, environment: str, service: str) -> Resolved:
        """Resolve a target to everything needed to run a query.

        Raises ``DbMcpError(CATALOG_NOT_FOUND)`` — with no side effects — if the
        customer, environment, or service is unknown.
        """
        cust = self._registry.customers.get(customer)
        if cust is None:
            raise DbMcpError(
                ErrorCode.CATALOG_NOT_FOUND, f"unknown customer '{customer}'"
            )
        env = cust.environments.get(environment)
        if env is None:
            raise DbMcpError(
                ErrorCode.CATALOG_NOT_FOUND,
                f"unknown environment '{environment}' for customer '{customer}'",
            )
        svc = env.services.get(service)
        if svc is None:
            raise DbMcpError(
                ErrorCode.CATALOG_NOT_FOUND,
                f"unknown service '{service}' for '{customer}/{environment}'",
            )

        d = self._registry.defaults
        return Resolved(
            tunnel_profile=TunnelProfile(
                **env.tunnel.model_dump(), db_host=svc.db_host, db_port=svc.db_port
            ),
            tunnel_key=self.tunnel_key(customer, environment, svc.db_host, svc.db_port),
            dbname=svc.dbname,
            secret_ref=svc.secret_ref,
            max_rows=d.max_rows,
            statement_timeout_ms=d.statement_timeout_ms,
        )

    def list_databases(self) -> dict:
        """A navigable view of the catalog with NO secrets.

        Returns customers → environments → service names only. Never exposes
        ``secret_ref``, jump hosts, jump users, or PEM filenames (FDD Contract 1).
        """
        return {
            "customers": [
                {
                    "customer": cname,
                    "environments": [
                        {"environment": ename, "services": sorted(env.services)}
                        for ename, env in cust.environments.items()
                    ],
                }
                for cname, cust in self._registry.customers.items()
            ]
        }

    def secret_refs(self) -> set[str]:
        """All secret_refs declared in the catalog (used by the vault verify)."""
        return {
            svc.secret_ref
            for cust in self._registry.customers.values()
            for env in cust.environments.values()
            for svc in env.services.values()
        }
