# db-mcp-server

A local **MCP server** that gives an AI coding assistant (e.g. Claude Code)
**credential-isolated** access to your PostgreSQL databases. The assistant sends
only SQL and receives only rows — database usernames, passwords, and SSH keys
never enter the model's context or the conversation transcript.

## Why

Wiring an AI assistant to a database usually means putting connection strings and
passwords somewhere the model (and its transcript) can read them. This server
keeps that boundary: it owns the encrypted credentials and the SSH tunnels,
exposes a small SQL-only tool surface, and defaults to read-only.

## How it works

- **Catalog** (`registry.yaml`) — non-secret routing. Organized as
  `customers → environments → services`; each service maps to a database name and
  a `secret_ref` (a pointer into the vault — never a credential).
- **Vault** (`vault.enc`) — AES-256-GCM with a scrypt-derived key. Holds the DB
  credentials, decrypted into memory once at launch using a passphrase.
- **Tunnel pool** — one SSH tunnel per `(customer, environment, db_host)` via `sshtunnel`,
  bound to an ephemeral `127.0.0.1` port.
- **Executor** — `psycopg`. `run_query` runs in a Postgres **READ ONLY**
  transaction (the engine rejects any write); `run_write_query` requires
  `confirm=true`.

## Install

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1   (bash: source .venv/Scripts/activate)
pip install -e ".[dev]"
```

## Configure

Configuration comes from environment variables; defaults resolve relative to the
project root.

| Variable | Purpose | Default |
|---|---|---|
| `DB_MCP_PASSPHRASE` | Vault passphrase (required to run the server) | — |
| `DB_MCP_REGISTRY` | Path to `registry.yaml` | `./registry.yaml` |
| `DB_MCP_VAULT` | Path to `vault.enc` | `./vault.enc` |
| `DB_MCP_KEYS_DIR` | Directory holding the SSH PEM keys | `./keys` |
| `DB_MCP_BOOTSTRAP` | Path to `bootstrap.yaml` | `./bootstrap.yaml` |

## Provision (first-time setup)

1. Copy the template and fill in real values:
   ```bash
   cp bootstrap.example.yaml bootstrap.yaml
   ```
2. Put your SSH private keys in `keys/` (filenames must match the `pem_key`
   fields in the catalog).
3. Generate the non-secret catalog and the encrypted vault (prompts for the
   passphrase you'll reuse to run the server):
   ```bash
   python -m db_mcp_server.bootstrap --dry-run   # preview, writes nothing
   python -m db_mcp_server.bootstrap             # writes registry.yaml + vault.enc
   python -m db_mcp_server.vault_admin verify     # expect {"ok": true}
   ```

`bootstrap.yaml` holds **plaintext** credentials — it is git-ignored; delete it
or keep it offline once the vault exists.

## Command-line tools

| Command | Purpose |
|---|---|
| `db-mcp-server` | The MCP server (stdio). Launched by the MCP client, not by hand. |
| `db-vault` | Manage credentials in the vault: `set` / `rm` / `list` / `verify`. |
| `db-bootstrap` | Split `bootstrap.yaml` into `registry.yaml` + `vault.enc`. |

(Console commands exist after `pip install -e .`; the `python -m db_mcp_server.<module>`
form always works.)

## Tools exposed to the assistant

- `list_databases()` — the catalog (customers → environments → services); no secrets.
- `run_query(customer, environment, service, sql, max_rows?)` — read-only.
- `run_write_query(customer, environment, service, sql, confirm)` — gated write.

Domain failures come back as a structured `{error_code, message}` rather than an
exception, so the assistant can react.

## Register with an MCP client

Example `.mcp.json` (adjust paths). Use `${DB_MCP_PASSPHRASE}` so the passphrase
is read from the shell instead of being written into the file:

```json
{
  "mcpServers": {
    "db": {
      "command": "/absolute/path/to/db-mcp-server/.venv/Scripts/python.exe",
      "args": ["-m", "db_mcp_server.server"],
      "env": {
        "DB_MCP_PASSPHRASE": "${DB_MCP_PASSPHRASE}"
      }
    }
  }
}
```

## Security notes

- `vault.enc`, `keys/`, `bootstrap.yaml`, `*.env`, and `*.pem` are git-ignored —
  never commit them.
- The vault passphrase is supplied via `DB_MCP_PASSPHRASE` (or a prompt) — never
  stored in `registry.yaml`, argv, or logs.
- `db-vault` reads the DB password via a hidden prompt (`getpass`), never via argv.
- `run_query` is read-only at the Postgres engine level; writes require
  `confirm=true`.

## Tests

```bash
pip install -e ".[dev]" && python -m pytest -q
```

The DB integration test is skipped unless `DB_MCP_TEST_DSN` points at a reachable
PostgreSQL.

## Roadmap (not in this build)

Persistent audit trail, multi-user operation, external secret-manager backing,
schema-introspection tools, and a permission denylist to turn the credential
isolation into a hard boundary.
