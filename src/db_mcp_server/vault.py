"""The credential vault (milestone M2).

The on-disk ``vault.enc`` is a JSON envelope (FDD Contract 5). Its decrypted
plaintext is a JSON map ``secret_ref -> {user, password}``. ``Vault.open`` reads
and decrypts it into memory once; the server then resolves credentials by
``secret_ref`` without the passphrase or ciphertext ever reaching the model.

Write operations (``set``/``remove``/``save``) exist for the operator CLI
(``db-vault``); the MCP server only ever calls ``open`` and ``get``.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import crypto
from .errors import DbMcpError, ErrorCode
from .models import Credential


class Vault:
    def __init__(self, credentials: dict[str, Credential], passphrase: str):
        # Passphrase is kept in memory so save() can re-seal with a fresh salt.
        # It is never written to disk or exposed by any accessor.
        self._creds = credentials
        self._passphrase = passphrase

    @classmethod
    def open(cls, path: str | Path, passphrase: str) -> "Vault":
        """Load and decrypt the vault into memory.

        Raises ``DbMcpError(VAULT_LOCKED)`` if the file is missing, unreadable,
        or the passphrase is wrong / the file is corrupted.
        """
        path = Path(path)
        try:
            envelope = json.loads(path.read_text("utf-8"))
        except FileNotFoundError as exc:
            raise DbMcpError(
                ErrorCode.VAULT_LOCKED, f"vault file not found: {path}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise DbMcpError(
                ErrorCode.VAULT_LOCKED, f"vault file unreadable: {path}"
            ) from exc

        try:
            plaintext = crypto.unseal(envelope, passphrase)
        except crypto.DecryptionError as exc:
            raise DbMcpError(
                ErrorCode.VAULT_LOCKED,
                "cannot decrypt vault: wrong passphrase or corrupted file",
            ) from exc

        raw = json.loads(plaintext)
        creds = {ref: Credential(**value) for ref, value in raw.items()}
        return cls(creds, passphrase)

    @classmethod
    def create_empty(cls, passphrase: str) -> "Vault":
        """A new, empty vault held only in memory until ``save`` is called."""
        return cls({}, passphrase)

    def get(self, secret_ref: str) -> Credential:
        try:
            return self._creds[secret_ref]
        except KeyError as exc:
            raise DbMcpError(
                ErrorCode.SECRET_NOT_FOUND, f"no credential for '{secret_ref}'"
            ) from exc

    def set(self, secret_ref: str, credential: Credential) -> None:
        self._creds[secret_ref] = credential

    def remove(self, secret_ref: str) -> None:
        try:
            del self._creds[secret_ref]
        except KeyError as exc:
            raise DbMcpError(
                ErrorCode.SECRET_NOT_FOUND, f"no credential for '{secret_ref}'"
            ) from exc

    def keys(self) -> list[str]:
        """Sorted secret_refs only — never the usernames or passwords."""
        return sorted(self._creds)

    def save(self, path: str | Path) -> None:
        """Re-seal the whole vault with a fresh salt/nonce and write it atomically."""
        raw = {
            ref: {"user": cred.user, "password": cred.password.get_secret_value()}
            for ref, cred in self._creds.items()
        }
        plaintext = json.dumps(raw).encode("utf-8")
        envelope = crypto.seal(plaintext, self._passphrase)
        crypto.atomic_write(path, json.dumps(envelope, indent=2).encode("utf-8"))
