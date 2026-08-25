"""Domain error codes and the single exception type they travel in.

The MCP tools translate a :class:`DbMcpError` into the structured
``{error_code, message}`` result described in FDD §5. Keeping one exception with
an enum code (rather than a class hierarchy) makes that mapping trivial and keeps
messages free of secrets by construction — callers pass a human message, never a
credential.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    CATALOG_NOT_FOUND = "CATALOG_NOT_FOUND"
    VAULT_LOCKED = "VAULT_LOCKED"
    SECRET_NOT_FOUND = "SECRET_NOT_FOUND"
    TUNNEL_FAILED = "TUNNEL_FAILED"
    QUERY_READONLY_VIOLATION = "QUERY_READONLY_VIOLATION"
    WRITE_NOT_CONFIRMED = "WRITE_NOT_CONFIRMED"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    DB_ERROR = "DB_ERROR"


class DbMcpError(Exception):
    """A domain error carrying a stable :class:`ErrorCode` and a safe message."""

    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")
