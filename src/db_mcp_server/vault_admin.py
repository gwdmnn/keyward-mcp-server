"""Operator CLI for the credential vault (Contract 7 in the FDD).

This is intentionally NOT an MCP tool: it is run by the operator in their own
terminal so that DB credentials never pass through the model's context. The DB
password is read via getpass (hidden prompt), never via argv.

Subcommands: set / rm / list / verify (milestone M4).
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import config
from .catalog import Catalog
from .errors import DbMcpError
from .models import Credential
from .vault import Vault
from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db-vault",
        description="Manage credentials in the encrypted vault (operator-only).",
    )
    parser.add_argument("--version", action="version", version=f"db-vault {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{set,rm,list,verify}")

    p_set = sub.add_parser("set", help="Create/update a credential (prompts for user + password).")
    p_set.add_argument("secret_ref", help="e.g. prefmaringa/live/pessoa-api")

    p_rm = sub.add_parser("rm", help="Remove a credential from the vault.")
    p_rm.add_argument("secret_ref")

    sub.add_parser("list", help="List secret_refs (keys only, never values).")
    sub.add_parser("verify", help="Cross-check registry.yaml against the vault.")

    return parser


def _passphrase() -> str:
    """The vault passphrase from the environment, or a hidden prompt for the user."""
    return config.passphrase_from_env() or getpass.getpass("Vault passphrase: ")


def _open_or_create(vault_path: Path, passphrase: str) -> Vault:
    """Open an existing vault, or start a fresh empty one if the file is absent.

    Checking existence first keeps 'no vault yet' (create) distinct from 'wrong
    passphrase / corrupted' (which Vault.open surfaces as VAULT_LOCKED).
    """
    if Path(vault_path).exists():
        return Vault.open(vault_path, passphrase)
    return Vault.create_empty(passphrase)


def cmd_set(cfg: config.Config, secret_ref: str) -> int:
    vault = _open_or_create(cfg.vault_path, _passphrase())
    user = input("DB user: ").strip()
    pw1 = getpass.getpass("DB password: ")
    pw2 = getpass.getpass("DB password (again): ")
    if pw1 != pw2:
        print("error: passwords do not match", file=sys.stderr)
        return 1
    if not user or not pw1:
        print("error: user and password must be non-empty", file=sys.stderr)
        return 1
    vault.set(secret_ref, Credential(user=user, password=pw1))
    vault.save(cfg.vault_path)
    print(f"ok: secret_ref '{secret_ref}' saved (vault re-sealed with a new nonce)")
    return 0


def cmd_rm(cfg: config.Config, secret_ref: str) -> int:
    vault = Vault.open(cfg.vault_path, _passphrase())
    vault.remove(secret_ref)  # raises SECRET_NOT_FOUND if absent
    vault.save(cfg.vault_path)
    print(f"ok: secret_ref '{secret_ref}' removed")
    return 0


def cmd_list(cfg: config.Config) -> int:
    vault = Vault.open(cfg.vault_path, _passphrase())
    keys = vault.keys()
    if not keys:
        print("(vault is empty)")
    for key in keys:
        print(key)
    return 0


def cmd_verify(cfg: config.Config) -> int:
    vault = Vault.open(cfg.vault_path, _passphrase())
    catalog = Catalog.load(cfg.registry_path)
    catalog_refs = catalog.secret_refs()
    vault_refs = set(vault.keys())
    report = {
        "ok": catalog_refs == vault_refs,
        "missing_in_vault": sorted(catalog_refs - vault_refs),
        "orphan_in_vault": sorted(vault_refs - catalog_refs),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    cfg = config.load_config()
    try:
        if args.command == "set":
            return cmd_set(cfg, args.secret_ref)
        if args.command == "rm":
            return cmd_rm(cfg, args.secret_ref)
        if args.command == "list":
            return cmd_list(cfg)
        if args.command == "verify":
            return cmd_verify(cfg)
    except DbMcpError as exc:
        print(f"error: {exc.message} ({exc.code.value})", file=sys.stderr)
        return 1
    return 2  # unreachable: argparse constrains the subcommands


if __name__ == "__main__":
    raise SystemExit(main())
