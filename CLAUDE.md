# CLAUDE.md — db-mcp-server

Guidance for Claude Code when working in this repository.

## What this is

A local **MCP server (Python, stdio)** that gives Claude Code routed, **credential-isolated** access to customer PostgreSQL databases. Claude sends only SQL and receives only rows; DB usernames/passwords and SSH PEM keys never enter the model's context. It replaces the old `database-connection` skill (which read `.env` files and embedded passwords into `psql` commands — leaking them into the transcript).

Design docs (read these before changing behavior). Available in English (`docs/en/`) and Portuguese (`docs/pt/`); the Portuguese versions are the originals:
- [docs/en/FDD-db-mcp-server.md](docs/en/FDD-db-mcp-server.md) — Feature Design Document (authoritative; contracts in §5). PT: [docs/pt/FDD-db-mcp-server.md](docs/pt/FDD-db-mcp-server.md).
- [docs/en/IMPLEMENTATION-PLAN-fase1.md](docs/en/IMPLEMENTATION-PLAN-fase1.md) — milestones M0–M9, module signatures, test strategy. PT: [docs/pt/IMPLEMENTATION-PLAN-fase1.md](docs/pt/IMPLEMENTATION-PLAN-fase1.md).
- Diagrams: [docs/en/c4/](docs/en/c4/) (PlantUML C4) and [docs/en/mermaid/](docs/en/mermaid/) (Mermaid); PT under [docs/pt/](docs/pt/).

## Core security invariants — do not violate

1. **No secret ever reaches the model's context.** No MCP tool may accept or return a DB username/password, full DSN, or PEM contents. Credential *writes* are out-of-band via the `db-vault` CLI only — never an MCP tool.
2. **The server's stdout is the MCP protocol channel.** NEVER `print()` (or otherwise write to stdout) from any code path the server reaches — it corrupts the stdio protocol. Use stderr/logging if needed. `print()` is fine only in the operator CLIs (`vault_admin`, `bootstrap`), which run in a terminal.
3. **Passphrase from the environment** (`DB_MCP_PASSPHRASE`) — via the shell, a `.env` file (loaded by `config.py`; real env wins), or a `getpass` prompt — never from argv, the catalog, or a log. DB passwords in `db-vault` are read via `getpass`, never argv. **Caveat:** putting `DB_MCP_PASSPHRASE` in `.env` stores the vault passphrase in plaintext on disk next to `vault.enc`; prefer keeping only the path vars in `.env` and supplying the passphrase via shell/prompt.
4. **Never print secret values** from `bootstrap`/`db-vault` — only counts and non-secret `secret_ref`s (`customer/env/service`).
5. **Fresh AES-GCM nonce on every seal; atomic vault writes** (temp + `os.replace`). Both already enforced in `crypto.py` — keep it that way.
6. `vault.enc`, `keys/`, `*.env`, `*.pem` are git-ignored. Never commit them; never add a secret to `registry.yaml` (it is non-secret routing only).

## Architecture (src/db_mcp_server/)

| Module | Role |
|---|---|
| `crypto.py` | scrypt KDF + AES-256-GCM; `seal`/`unseal` (FDD Contract 5 envelope), `atomic_write`. Knows nothing about vault contents or error codes. |
| `vault.py` | `Vault.open/get/set/remove/keys/save`. **Decrypt-all-at-launch**: the whole vault is decrypted into memory once; runtime `get()` is an in-memory lookup (no per-request crypto). |
| `catalog.py` | Parses `registry.yaml`; `resolve(customer, env, service) -> Resolved`; `list_databases()` (no secrets); `secret_refs()`. |
| `models.py` | Pydantic: `Credential` (password is `SecretStr`), `TunnelProfile`, `Resolved`. |
| `errors.py` | `ErrorCode` enum (8 codes) + `DbMcpError(code, message)`. Messages must never contain secrets. |
| `config.py` | Env-var/path resolution (`DB_MCP_*`) with project-root defaults. |
| `vault_admin.py` | `db-vault` operator CLI: `set`/`rm`/`list`/`verify`. NOT an MCP tool. |
| `bootstrap.py` | `db-bootstrap`: splits `bootstrap.yaml` (registry shape + raw creds) → `registry.yaml` + `vault.enc`. |
| `server.py` | MCP server (FastMCP, stdio): tools `list_databases`/`run_query`/`run_write_query`; startup opens vault + warns on catalog↔vault gaps; `close_all()` on shutdown. Tool bodies delegate to `_impl` fns for testability; domain failures return `ErrorResult`, not raised. |
| `tunnel_manager.py` | SSH tunnel pool per `(customer, env)`, ephemeral ports, `close_idle`/`close_all`, `TUNNEL_FAILED`; forwarder injectable. |
| `executor.py` | psycopg; `run_read` (READ ONLY txn) vs `run_write` (confirm-gated); `_connect` is the test seam. |

Routing model: **one SSH tunnel pooled per `(customer, environment)`**, shared by all its services; only `dbname` changes per service.

## Conventions

- **Errors:** raise `DbMcpError(ErrorCode.X, "safe message")`; the server maps it to a structured `{error_code, message}` result. Use `CATALOG_NOT_FOUND` only for per-query lookup misses — a malformed/missing `registry.yaml` at load is a startup crash, not a tool error.
- **Secrets in memory:** `password` is `SecretStr`; call `get_secret_value()` only at the exact use point (DB connect, vault re-seal). Never log it.
- **Read-only by default:** `run_query` will run in a Postgres `READ ONLY` transaction (engine-enforced); writes go through a separate `run_write_query(confirm=true)`.
- Match the surrounding code's docstring density and style. Keep new modules dependency-light.

## Dev workflow

Environment uses a venv at `.venv` (created with `pip install -e ".[dev]"`).

Run the tests:
```bash
cd /c/Users/guilherme.weidmann/projects/db-mcp-server && ./.venv/Scripts/python.exe -m pytest -q
```

Every milestone lands with its tests green before the next. Build order is bottom-up; **M1–M4 + M8 are network-free** and fully unit-tested. M5/M6 (tunnels, DB) need mocked-SSH tests + a manual check against a real jump host.

Operator CLIs (run in a terminal; they prompt for the passphrase via getpass):
```bash
./.venv/Scripts/python.exe -m db_mcp_server.bootstrap --dry-run   # preview import, no writes
./.venv/Scripts/python.exe -m db_mcp_server.bootstrap             # write registry.yaml + vault.enc
./.venv/Scripts/python.exe -m db_mcp_server.vault_admin list
./.venv/Scripts/python.exe -m db_mcp_server.vault_admin verify    # catalog <-> vault consistency
```
(Console commands `db-mcp-server`/`db-vault`/`db-bootstrap` exist after `pip install -e .`; a newly added entry point needs a reinstall, or use `python -m` as above.)

## Gotchas

- **Python 3.14** on this machine: `psycopg[binary]` may lack a wheel. If install fails at M5/M6, use a 3.12 venv or plain `psycopg` + local libpq. `cryptography`/`sshtunnel`/`PyYAML` are fine.
- `bootstrap` **overwrites** `registry.yaml` and guards `vault.enc` behind `--force`.
- `bootstrap.yaml` holds **plaintext credentials** — it is git-ignored; delete it or keep it offline once the vault is generated. A committed `bootstrap.example.yaml` is the template.

## Status & scope

- **Done (tested):** M0 scaffold, M1 crypto, M2 vault, M3 catalog, M4 `db-vault` CLI, M8 bootstrap, M5 tunnel_manager, M6 executor, M7 server.
- **Next:** M9 cutover (register the server with Claude Code; retire/redirect the `database-connection` skill; DB-backed manual verification).
- **Phase 2 (out of scope now):** persistent audit trail, multi-user, GCP Secret Manager backing, schema-introspection tools, a Claude Code permission denylist, on-demand per-entry decryption. Do not add these to Phase 1 diagrams/code.
