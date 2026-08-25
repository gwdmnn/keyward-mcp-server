"""MCP server (milestone M7).

Wires the catalog, vault, tunnel pool, and executor into three stdio MCP tools:
``list_databases``, ``run_query`` (read-only), and ``run_write_query`` (confirmed
write). Domain failures are returned as a structured ``ErrorResult`` (FDD §5), not
raised, so the model can read the ``error_code`` and adjust.

CRITICAL: stdout is the MCP protocol channel — every diagnostic here goes to
stderr. Never ``print`` to stdout from any path the server reaches.

The tool bodies delegate to plain ``_impl`` functions so they can be unit-tested
directly without depending on the decorator's return type.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from fastmcp import FastMCP  

from . import __version__, config, executor
from .catalog import Catalog
from .errors import DbMcpError, ErrorCode
from .models import ErrorResult, QueryResult, WriteResult
from .tunnel_manager import TunnelManager
from .vault import Vault

mcp = FastMCP("maida-pg-mcp-server", on_duplicate="warn")


@dataclass
class _State:
    config: config.Config
    vault: Vault
    catalog: Catalog
    tunnels: TunnelManager


_state: _State | None = None


def _require_state() -> _State:
    if _state is None:
        raise RuntimeError("db-mcp-server is not initialized")
    return _state


def _error(exc: DbMcpError) -> ErrorResult:
    return ErrorResult(error_code=exc.code.value, message=exc.message)


# --- tool implementations (plain, testable) ---
def _list_databases() -> dict:
    return _require_state().catalog.list_databases()


def _run_query(
    customer: str, environment: str, service: str, sql: str, max_rows: int | None
) -> QueryResult | ErrorResult:
    st = _require_state()
    st.tunnels.close_idle()
    try:
        resolved = st.catalog.resolve(customer, environment, service)
        cred = st.vault.get(resolved.secret_ref)
        local = st.tunnels.get(resolved.tunnel_key, resolved.tunnel_profile)
        limit = max_rows if max_rows is not None else resolved.max_rows
        return executor.run_read(resolved, cred, local, sql, limit)
    except DbMcpError as exc:
        return _error(exc)


def _run_write_query(
    customer: str, environment: str, service: str, sql: str, confirm: bool
) -> WriteResult | ErrorResult:
    st = _require_state()
    if not confirm:
        # gate before touching the vault or tunnel
        return ErrorResult(
            error_code=ErrorCode.WRITE_NOT_CONFIRMED.value,
            message="run_write_query requires confirm=true",
        )
    st.tunnels.close_idle()
    try:
        resolved = st.catalog.resolve(customer, environment, service)
        cred = st.vault.get(resolved.secret_ref)
        local = st.tunnels.get(resolved.tunnel_key, resolved.tunnel_profile)
        return executor.run_write(resolved, cred, local, sql, confirm)
    except DbMcpError as exc:
        return _error(exc)


# --- MCP tools (thin wrappers carrying the schema + description) ---
@mcp.tool()
def list_databases() -> dict:
    """List the databases you can query, as customers -> environments -> services.

    Returns only routing identifiers — never credentials, hosts, or keys. Use the
    customer/environment/service names from here as arguments to run_query.
    """
    return _list_databases()


@mcp.tool()
def run_query(
    customer: str,
    environment: str,
    service: str,
    sql: str,
    max_rows: int | None = None,
) -> QueryResult | ErrorResult:
    """Run a read-only SQL query against one service's database.

    Executes in a Postgres READ ONLY transaction: any INSERT/UPDATE/DELETE/DDL is
    rejected by the engine (returned as error_code QUERY_READONLY_VIOLATION). Rows
    are capped at max_rows (service default if omitted); `truncated` signals more.
    On failure, returns {error_code, message} instead of the result.
    """
    return _run_query(customer, environment, service, sql, max_rows)


@mcp.tool()
def run_write_query(
    customer: str,
    environment: str,
    service: str,
    sql: str,
    confirm: bool = False,
) -> WriteResult | ErrorResult:
    """Run a writing SQL statement (INSERT/UPDATE/DELETE/DDL) — requires confirm=true.

    Without confirm=true, nothing runs (error_code WRITE_NOT_CONFIRMED). With it,
    the statement runs in a read-write transaction that commits on success and
    rolls back on error. Returns affected_rows, or {error_code, message}.
    """
    return _run_write_query(customer, environment, service, sql, confirm)


def _startup() -> _State:
    """Load config, open the vault, load the catalog, build the tunnel pool.

    Raises on any launch failure (missing passphrase, locked vault) so the server
    refuses to start rather than serving without credentials.
    """
    cfg = config.load_config()

    passphrase = config.passphrase_from_env()
    if not passphrase:
        raise DbMcpError(
            ErrorCode.VAULT_LOCKED,
            f"{config.ENV_PASSPHRASE} is not set; cannot open the vault",
        )
    vault = Vault.open(cfg.vault_path, passphrase)  # raises VAULT_LOCKED on bad passphrase
    catalog = Catalog.load(cfg.registry_path)

    missing = sorted(catalog.secret_refs() - set(vault.keys()))
    if missing:
        print(
            f"db-mcp-server: warning: {len(missing)} secret_ref(s) in the registry "
            f"have no vault entry (will fail as SECRET_NOT_FOUND):",
            file=sys.stderr,
        )
        for ref in missing:
            print(f"  - {ref}", file=sys.stderr)

    tunnels = TunnelManager(cfg.keys_dir, catalog.defaults.tunnel_idle_timeout_s)
    return _State(cfg, vault, catalog, tunnels)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="db-mcp-server",
        description="Secure MCP server for routed PostgreSQL access via SSH tunnels.",
    )
    parser.add_argument("--version", action="version", version=f"db-mcp-server {__version__}")
    parser.parse_args(argv)

    global _state
    try:
        _state = _startup()
    except DbMcpError as exc:
        print(f"db-mcp-server: {exc.message} ({exc.code.value})", file=sys.stderr)
        return 1

    try:
        mcp.run()  # stdio; blocks until the client disconnects
    finally:
        _state.tunnels.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
