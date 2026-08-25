"""Query executor (milestone M6).

Opens a psycopg connection over the SSH tunnel and runs SQL. ``run_read`` uses a
Postgres **READ ONLY** transaction so the engine itself rejects any mutation —
far stronger than string-matching for SELECT. ``run_write`` requires an explicit
``confirm`` and runs a normal read-write transaction.

The connection is opened with keyword args (never a DSN string), so the password
is passed straight to libpq and never assembled into a connection string.
``_connect`` is the single seam the tests patch to avoid needing a live DB for
the fetch/truncation/error-mapping logic; the engine-level read-only guarantee is
covered by the DB-backed integration test.
"""

from __future__ import annotations

import psycopg
from psycopg import errors as pg_errors

from .errors import DbMcpError, ErrorCode
from .models import Credential, QueryResult, Resolved, WriteResult

CONNECT_TIMEOUT_S = 10


def _connect(resolved: Resolved, cred: Credential, local: tuple[str, int]):
    host, port = local
    return psycopg.connect(
        host=host,
        port=port,
        dbname=resolved.dbname,
        user=cred.user,
        password=cred.password.get_secret_value(),
        connect_timeout=CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={int(resolved.statement_timeout_ms)}",
        autocommit=False,
    )


def _pg_message(exc: Exception) -> str:
    """The primary Postgres message, without traceback or credentials."""
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag else None
    return (primary or str(exc)).strip()


def _jsonable(value: object) -> object:
    """Coerce a DB value to something JSON-serializable for the MCP boundary.

    Primitives pass through; everything else (datetime, Decimal, UUID, bytes, …)
    is stringified. Phase 1 returns tabular data for a model to read, so a string
    rendering is acceptable and keeps the result always serializable.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def run_read(
    resolved: Resolved,
    cred: Credential,
    local: tuple[str, int],
    sql: str,
    max_rows: int,
) -> QueryResult:
    """Run ``sql`` in a READ ONLY transaction and return up to ``max_rows`` rows."""
    try:
        with _connect(resolved, cred, local) as conn:
            conn.read_only = True  # engine rejects any mutation in this txn
            cur = conn.execute(sql)
            if cur.description is None:
                return QueryResult(columns=[], rows=[], row_count=0, truncated=False)
            columns = [c.name for c in cur.description]
            fetched = cur.fetchmany(max_rows + 1)  # +1 to detect truncation
            truncated = len(fetched) > max_rows
            rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
            return QueryResult(
                columns=columns, rows=rows, row_count=len(rows), truncated=truncated
            )
    except pg_errors.ReadOnlySqlTransaction as exc:
        raise DbMcpError(ErrorCode.QUERY_READONLY_VIOLATION, _pg_message(exc)) from exc
    except pg_errors.QueryCanceled as exc:
        raise DbMcpError(ErrorCode.QUERY_TIMEOUT, "statement timeout exceeded") from exc
    except psycopg.Error as exc:
        raise DbMcpError(ErrorCode.DB_ERROR, _pg_message(exc)) from exc


def run_write(
    resolved: Resolved,
    cred: Credential,
    local: tuple[str, int],
    sql: str,
    confirm: bool,
) -> WriteResult:
    """Run ``sql`` in a read-write transaction — only if ``confirm`` is true."""
    if not confirm:
        raise DbMcpError(
            ErrorCode.WRITE_NOT_CONFIRMED, "run_write_query requires confirm=true"
        )
    try:
        with _connect(resolved, cred, local) as conn:
            affected = conn.execute(sql).rowcount
        # normal exit commits and closes the connection
        return WriteResult(affected_rows=affected, committed=True)
    except pg_errors.QueryCanceled as exc:
        raise DbMcpError(ErrorCode.QUERY_TIMEOUT, "statement timeout exceeded") from exc
    except psycopg.Error as exc:
        raise DbMcpError(ErrorCode.DB_ERROR, _pg_message(exc)) from exc
