"""Tests for the query executor (milestone M6 acceptance).

The fetch/truncation/coercion/error-mapping/confirm logic is unit-tested against
a fake connection (patching executor._connect) so no DB is required. The
engine-level read-only guarantee is covered by a DB-backed test that runs only
when DB_MCP_TEST_DSN is set.
"""

from __future__ import annotations

import datetime
import decimal
import os

import psycopg
import pytest
from psycopg import errors as pg_errors

from db_mcp_server import executor
from db_mcp_server.errors import DbMcpError, ErrorCode
from db_mcp_server.models import Credential, Resolved, TunnelProfile


RESOLVED = Resolved(
    tunnel_profile=TunnelProfile(jump_host="h", jump_user="u", pem_key="k.pem", db_host="db"),
    tunnel_key=("c", "live"),
    dbname="d",
    secret_ref="c/live/s",
    max_rows=1000,
    statement_timeout_ms=30000,
)
CRED = Credential(user="u", password="p")
LOCAL = ("127.0.0.1", 15000)


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
    def __init__(self, cursor=None, error=None):
        self._cursor = cursor
        self._error = error
        self.read_only = None

    def execute(self, sql):
        if self._error is not None:
            raise self._error
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch, conn):
    monkeypatch.setattr(executor, "_connect", lambda *a, **k: conn)


# --- read ---
def test_run_read_returns_rows_and_sets_read_only(monkeypatch):
    conn = FakeConn(FakeCursor(columns=["id", "nome"], rows=[(1, "a"), (2, "b")]))
    _patch(monkeypatch, conn)

    res = executor.run_read(RESOLVED, CRED, LOCAL, "SELECT id, nome FROM t", max_rows=10)
    assert res.columns == ["id", "nome"]
    assert res.rows == [[1, "a"], [2, "b"]]
    assert res.row_count == 2 and res.truncated is False
    assert conn.read_only is True  # read-only was applied before executing


def test_run_read_truncates_at_max_rows(monkeypatch):
    _patch(monkeypatch, FakeConn(FakeCursor(columns=["id"], rows=[(1,), (2,), (3,)])))
    res = executor.run_read(RESOLVED, CRED, LOCAL, "SELECT id FROM t", max_rows=2)
    assert res.rows == [[1], [2]]
    assert res.row_count == 2 and res.truncated is True


def test_run_read_coerces_non_json_values(monkeypatch):
    rows = [(datetime.date(2020, 1, 1), decimal.Decimal("1.50"), None)]
    _patch(monkeypatch, FakeConn(FakeCursor(columns=["d", "amt", "n"], rows=rows)))
    res = executor.run_read(RESOLVED, CRED, LOCAL, "SELECT ...", max_rows=10)
    assert res.rows == [["2020-01-01", "1.50", None]]


def test_run_read_no_result_set(monkeypatch):
    _patch(monkeypatch, FakeConn(FakeCursor(columns=None)))
    res = executor.run_read(RESOLVED, CRED, LOCAL, "SET x = 1", max_rows=10)
    assert res.columns == [] and res.rows == [] and res.row_count == 0


def test_run_read_readonly_violation(monkeypatch):
    err = pg_errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only transaction")
    _patch(monkeypatch, FakeConn(error=err))
    with pytest.raises(DbMcpError) as ei:
        executor.run_read(RESOLVED, CRED, LOCAL, "INSERT INTO t VALUES (1)", max_rows=10)
    assert ei.value.code is ErrorCode.QUERY_READONLY_VIOLATION


def test_run_read_timeout(monkeypatch):
    _patch(monkeypatch, FakeConn(error=pg_errors.QueryCanceled("statement timeout")))
    with pytest.raises(DbMcpError) as ei:
        executor.run_read(RESOLVED, CRED, LOCAL, "SELECT pg_sleep(60)", max_rows=10)
    assert ei.value.code is ErrorCode.QUERY_TIMEOUT


def test_run_read_db_error(monkeypatch):
    _patch(monkeypatch, FakeConn(error=pg_errors.UndefinedTable("relation does not exist")))
    with pytest.raises(DbMcpError) as ei:
        executor.run_read(RESOLVED, CRED, LOCAL, "SELECT * FROM nope", max_rows=10)
    assert ei.value.code is ErrorCode.DB_ERROR


# --- write ---
def test_run_write_requires_confirm_and_does_not_connect(monkeypatch):
    calls = []
    monkeypatch.setattr(executor, "_connect", lambda *a, **k: calls.append(1))
    with pytest.raises(DbMcpError) as ei:
        executor.run_write(RESOLVED, CRED, LOCAL, "INSERT INTO t VALUES (1)", confirm=False)
    assert ei.value.code is ErrorCode.WRITE_NOT_CONFIRMED
    assert calls == []  # never opened a connection


def test_run_write_commits(monkeypatch):
    _patch(monkeypatch, FakeConn(FakeCursor(rowcount=3)))
    res = executor.run_write(RESOLVED, CRED, LOCAL, "UPDATE t SET x=1", confirm=True)
    assert res.affected_rows == 3 and res.committed is True


def test_run_write_db_error(monkeypatch):
    _patch(monkeypatch, FakeConn(error=pg_errors.UndefinedTable("no table")))
    with pytest.raises(DbMcpError) as ei:
        executor.run_write(RESOLVED, CRED, LOCAL, "UPDATE nope SET x=1", confirm=True)
    assert ei.value.code is ErrorCode.DB_ERROR


# --- DB-backed integration (opt-in) ---
_DSN = os.environ.get("DB_MCP_TEST_DSN")


@pytest.mark.skipif(not _DSN, reason="set DB_MCP_TEST_DSN to run DB integration tests")
def test_real_readonly_transaction_rejects_ddl(monkeypatch):
    monkeypatch.setattr(
        executor, "_connect", lambda *a, **k: psycopg.connect(_DSN, autocommit=False)
    )
    ok = executor.run_read(RESOLVED, CRED, LOCAL, "SELECT 1 AS one", max_rows=10)
    assert ok.columns == ["one"] and ok.rows == [[1]]

    with pytest.raises(DbMcpError) as ei:
        executor.run_read(RESOLVED, CRED, LOCAL, "CREATE TEMP TABLE t_ro (x int)", max_rows=10)
    assert ei.value.code is ErrorCode.QUERY_READONLY_VIOLATION
