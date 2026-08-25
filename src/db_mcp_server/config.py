"""Runtime configuration from environment variables (with sensible defaults).

Both the server and the db-vault CLI resolve their paths and the vault passphrase
here. The passphrase is only ever read from the environment (``DB_MCP_PASSPHRASE``)
— never from a file, argv, or the catalog — matching the FDD's launch contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# src/db_mcp_server/config.py -> parents[2] == the db-mcp-server/ project root
_ROOT = Path(__file__).resolve().parents[2]

ENV_PASSPHRASE = "DB_MCP_PASSPHRASE"
ENV_REGISTRY = "DB_MCP_REGISTRY"
ENV_VAULT = "DB_MCP_VAULT"
ENV_KEYS_DIR = "DB_MCP_KEYS_DIR"
ENV_BOOTSTRAP = "DB_MCP_BOOTSTRAP"


@dataclass(frozen=True)
class Config:
    registry_path: Path
    vault_path: Path
    keys_dir: Path
    bootstrap_path: Path


def load_config() -> Config:
    return Config(
        registry_path=Path(os.environ.get(ENV_REGISTRY, _ROOT / "registry.yaml")),
        vault_path=Path(os.environ.get(ENV_VAULT, _ROOT / "vault.enc")),
        keys_dir=Path(os.environ.get(ENV_KEYS_DIR, _ROOT / "keys")),
        bootstrap_path=Path(os.environ.get(ENV_BOOTSTRAP, _ROOT / "bootstrap.yaml")),
    )


def passphrase_from_env() -> str | None:
    """The vault passphrase if set in the environment, else ``None``."""
    return os.environ.get(ENV_PASSPHRASE)
