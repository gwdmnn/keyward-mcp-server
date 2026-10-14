"""Pydantic models shared across the server.

Only the models needed so far are defined here; later milestones add the tool
input/output shapes (QueryResult, WriteResult, Resolved, TunnelProfile, ...).
"""

from __future__ import annotations

from pydantic import BaseModel, SecretStr


class Credential(BaseModel):
    """A database username/password pair held only in memory after decryption.

    ``password`` is a :class:`~pydantic.SecretStr` so it never leaks through a
    repr, log line, or accidental serialization; call ``get_secret_value()``
    only at the exact point it is needed (building a DB connection, re-sealing
    the vault).
    """

    user: str
    password: SecretStr


class JumpProfile(BaseModel):
    """Non-secret SSH jump-host settings for one ``(customer, environment)``.

    This is the ``tunnel:`` block of ``registry.yaml``. The PEM key is referenced
    by filename (resolved under ``keys/``), never by contents — the key material
    stays out of the catalog and out of memory here.
    """

    jump_host: str
    jump_port: int = 22
    jump_user: str
    pem_key: str


class TunnelProfile(JumpProfile):
    """A full tunnel endpoint: the environment's jump host + one service's DB host.

    Assembled by ``Catalog.resolve`` — the DB host lives at the service level so a
    customer's databases can be split across several hosts.
    """

    db_host: str
    db_port: int = 5432


class Resolved(BaseModel):
    """Everything needed to run one query, produced by ``Catalog.resolve``.

    Carries only a ``secret_ref`` (not the credential) — the vault is consulted
    separately, so routing data and secrets never travel together.
    """

    tunnel_profile: TunnelProfile
    tunnel_key: tuple[str, str, str, int]
    dbname: str
    secret_ref: str
    max_rows: int
    statement_timeout_ms: int


class QueryResult(BaseModel):
    """Result of a read query (``run_query``). Values are JSON-safe."""

    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool


class WriteResult(BaseModel):
    """Result of a confirmed write (``run_write_query``)."""

    affected_rows: int
    committed: bool


class ErrorResult(BaseModel):
    """Structured error returned to the model (never contains secrets)."""

    error_code: str
    message: str
