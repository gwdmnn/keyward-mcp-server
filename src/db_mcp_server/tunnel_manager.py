"""SSH tunnel pool (milestone M5).

One tunnel is pooled per ``(customer, environment)`` and shared by all of that
customer's services (they reach the same DB host, only the dbname differs). Each
tunnel binds to an ephemeral ``127.0.0.1`` port chosen by the OS, so many
customers can be active at once without port collisions.

The ``sshtunnel`` library is imported lazily inside the default forwarder factory
so this module (and its tests, which inject a fake factory) can be used without a
live SSH stack.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import DbMcpError, ErrorCode
from .models import TunnelProfile

# A forwarder is any object exposing start(), stop(), local_bind_port, is_active.
ForwarderFactory = Callable[[TunnelProfile], object]

_LOCAL_HOST = "127.0.0.1"


@dataclass
class _Entry:
    forwarder: object
    last_used: float


def _is_active(forwarder: object) -> bool:
    return bool(getattr(forwarder, "is_active", False))


def _safe_stop(forwarder: object) -> None:
    try:
        forwarder.stop()
    except Exception:
        pass  # best-effort teardown; a failing stop must not mask the caller's flow


class TunnelManager:
    def __init__(
        self,
        keys_dir: str | Path,
        idle_timeout_s: int = 300,
        *,
        forwarder_factory: ForwarderFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._keys_dir = Path(keys_dir)
        self._idle_timeout_s = idle_timeout_s
        self._new_forwarder = forwarder_factory or self._default_forwarder
        self._clock = clock
        self._lock = threading.Lock()
        self._pool: dict[tuple[str, str], _Entry] = {}

    def _default_forwarder(self, profile: TunnelProfile) -> object:
        # sshtunnel 0.4 references paramiko.DSSKey, which paramiko 3.x removed
        # (DSA is obsolete). We use RSA/ed25519 keys, so alias DSSKey to RSAKey
        # purely to satisfy sshtunnel's key-type list — no DSA key is ever loaded.
        import paramiko

        if not hasattr(paramiko, "DSSKey"):
            paramiko.DSSKey = paramiko.RSAKey

        from sshtunnel import SSHTunnelForwarder  # lazy: keeps module import-light

        return SSHTunnelForwarder(
            (profile.jump_host, profile.jump_port),
            ssh_username=profile.jump_user,
            ssh_pkey=str(self._keys_dir / profile.pem_key),
            remote_bind_address=(profile.db_host, profile.db_port),
            local_bind_address=(_LOCAL_HOST, 0),  # 0 => OS picks an ephemeral port
        )

    def get(self, tunnel_key: tuple[str, str], profile: TunnelProfile) -> tuple[str, int]:
        """Return ``(host, port)`` of a live tunnel for ``tunnel_key``.

        Reuses an active tunnel; a stale one is dropped and reopened. Raises
        ``DbMcpError(TUNNEL_FAILED)`` if a new tunnel cannot be opened.
        """
        with self._lock:
            entry = self._pool.get(tunnel_key)
            if entry is not None and _is_active(entry.forwarder):
                entry.last_used = self._clock()
                return (_LOCAL_HOST, entry.forwarder.local_bind_port)
            if entry is not None:  # present but no longer active — replace it
                _safe_stop(entry.forwarder)
                del self._pool[tunnel_key]

            forwarder = self._open(profile)
            self._pool[tunnel_key] = _Entry(forwarder, self._clock())
            return (_LOCAL_HOST, forwarder.local_bind_port)

    def _open(self, profile: TunnelProfile) -> object:
        try:
            forwarder = self._new_forwarder(profile)
            forwarder.start()
            return forwarder
        except DbMcpError:
            raise
        except Exception as exc:
            raise DbMcpError(
                ErrorCode.TUNNEL_FAILED,
                f"failed to open SSH tunnel to {profile.db_host} via {profile.jump_host}: {exc}",
            ) from exc

    def close_idle(self) -> None:
        """Close tunnels unused for longer than ``idle_timeout_s``."""
        now = self._clock()
        with self._lock:
            for key in list(self._pool):
                entry = self._pool[key]
                if now - entry.last_used > self._idle_timeout_s:
                    _safe_stop(entry.forwarder)
                    del self._pool[key]

    def close_all(self) -> None:
        """Close every tunnel (call on server shutdown)."""
        with self._lock:
            for entry in self._pool.values():
                _safe_stop(entry.forwarder)
            self._pool.clear()
