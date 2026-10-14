# C4 Diagrams - db-mcp-server

## Generated Diagram Files

The following PlantUML files were created:

**Created**:
- `db-mcp-server-c1.puml` - System Context diagram
- `db-mcp-server-c2.puml` - Container diagram
- `db-mcp-server-c3.puml` - Component diagram (MCP Server)
- `db-mcp-server-c4.puml` - Code diagram (class diagram)

**Skipped**: none. All four levels had sufficient information in the FDD (context, containers, components) complemented by the Implementation Plan (code-level signatures).

To render these diagrams, use any PlantUML-compatible tool or viewer.

## Language

This is the English rendering of the diagram set (the source diagrams under `docs/pt/` are in Portuguese). Technical terms are kept in English throughout (MCP, stdio, PostgreSQL, Vault, TunnelManager, READ ONLY, psycopg, scrypt, AES-256-GCM, PEM, SSH, decrypt-all-at-launch).

## Analysis Summary

### Explicit Elements from FDD
- **Actor**: Operator (§1 "Actors"; single user in Phase 1; supplies the passphrase at launch; administers credentials via the CLI).
- **Central system**: db-mcp-server — local MCP server, stdio transport (§1 "Fit in the HLD").
- **External systems**: Claude Code (MCP client), PostgreSQL databases, SSH jump hosts (§1 "Actors").
- **Security invariant**: only SQL in, only rows out; credentials/PEM never reach Claude Code; read-only by default (§2 "Technical goals").
- **Containers** (§3 "Included"): stdio MCP server (tools `list_databases`/`run_query`/`run_write_query`); `registry.yaml` (non-secret catalog); `vault.enc` (AES-256-GCM + scrypt); `keys/` (PEMs); `db-vault`/`vault_admin` operator CLI; bootstrap importer; `bootstrap.yaml` (plaintext input, git-ignored).
- **Bootstrap importer** (§3 "Included"; §4 "Admin flow"): reads a single `bootstrap.yaml` (same shape as `registry.yaml`, with plaintext `user`/`password` per service) and splits it into `registry.yaml` (non-secret) and `vault.enc` (sealed secrets); `secret_ref` is derived as `customer/env/service`; `bootstrap.yaml` is git-ignored and removed/kept offline once the vault is generated.
- **Decrypt-all-at-launch** (§4, step 4 and "Note — in-memory credential residency"): `vault.enc` is decrypted in full once at launch (scrypt + AES-256-GCM); at runtime `vault.get()` is just a read of the already-decrypted in-memory map; credentials reside in memory for the server's lifetime.
- **Tools & contracts** (§5): signatures and schemas of `list_databases`, `run_query`, `run_write_query`; the `error_code` enum; the `registry.yaml` and `vault.enc` formats; the `mcpServers` launch block.
- **Error enum** (§5): CATALOG_NOT_FOUND, VAULT_LOCKED, TUNNEL_FAILED, QUERY_READONLY_VIOLATION, WRITE_NOT_CONFIRMED, QUERY_TIMEOUT, SECRET_NOT_FOUND, DB_ERROR.
- **Tunnel policy** (§2/§3): at most one tunnel per `(customer, environment, db_host, db_port)`, ephemeral ports on `127.0.0.1`, idle-timeout and teardown on shutdown.

### Inferences Made
- **Internal components of the MCP server** (`server.py`, `catalog.py`, `vault.py`, `crypto.py`, `tunnel_manager.py`, `executor.py`) and their responsibilities: taken from the Implementation Plan (§2 File structure, §3 Milestones). The FDD names these responsibilities functionally in §4 (Catalog, Vault, TunnelManager, read-only execution) but the module split is from the plan. Used for C3/C4.
- **Class/method signatures in C4**: derived from the Implementation Plan §4 "Signature specifications" (models.py, crypto.py, vault.py, catalog.py, tunnel_manager.py, executor.py). Marked in the C4 diagram with a documented-inference note. The `bootstrap.py` entry point `main(dry_run, force)` is modeled from the CLI flags `--dry-run`/`--force` documented in the Implementation Plan §3 (M8).
- **Technology versions/libraries** (Python 3.11, mcp/FastMCP, psycopg, sshtunnel, cryptography, PyYAML, pydantic): from Implementation Plan §1 "Stack and dependencies".

### Exclusions Confirmed (Phase 2 - kept out of all diagrams)
- Persistent audit trail.
- Multi-user operation / shared server.
- Secret backing in GCP Secret Manager.
- Schema-introspection tools (`describe_schema`, `list_tables`).
- Claude Code permission denylist.
- psycopg connection pooling/keep-alive.
- On-demand per-`secret_ref` decryption (Phase 1 uses decrypt-all-at-launch; per-query decryption is a possible Phase 2 refinement).

### Component Nature
- **db-mcp-server**: independent, out-of-process system (keywords "local MCP server", "local process"). Modeled as its own System/Container boundary.
- **db-vault / vault_admin CLI**: separate operator-run process (out-of-band, not an MCP tool) — modeled as a distinct container, not as part of the MCP server process.
- **Bootstrap importer (db-bootstrap)**: separate operator-run process; reads `bootstrap.yaml` and writes `registry.yaml` + `vault.enc` — modeled as a distinct container.
- **registry.yaml, vault.enc, keys/, bootstrap.yaml**: data stores — modeled as ContainerDb inside the boundary; `bootstrap.yaml` is transient plaintext input (git-ignored).

## Diagram Descriptions

### C1 - System Context
- **Audience**: Stakeholders, Product Managers.
- **Key Elements**: Operator (person); db-mcp-server (system); Claude Code, PostgreSQL databases, SSH jump hosts (external).
- **Business Value**: Removes credentials from the model's transcript — Claude Code only exchanges SQL and rows, never secrets.

### C2 - Container
- **Audience**: Architects, Technical Leads.
- **Key Containers**: MCP Server (Python 3.11, mcp/FastMCP, stdio); db-vault CLI; bootstrap importer; registry.yaml; vault.enc (AES-256-GCM + scrypt); keys/; bootstrap.yaml (plaintext, transient).
- **Deployment Context**: Local execution under the operator's OS user; passphrase injected via an environment variable at launch; the vault is decrypted in full into memory at launch.

### C3 - Component
- **Audience**: Tech Leads, Senior Developers.
- **Key Components**: server.py (tools/orchestration), catalog.py (routing), vault.py (decrypt-all-at-launch + in-memory read), crypto.py (scrypt + AES-GCM), tunnel_manager.py (tunnel pool), executor.py (read-only/read-write).
- **Integration Points**: server.py orchestrates catalog/vault/tunnel/executor; vault.py decrypts the vault at launch and serves credentials via an in-memory read; the executor connects to PostgreSQL over the tunnel; the tunnel uses a PEM from keys/.

### C4 - Code
- **Audience**: Developers.
- **Key Interfaces**: tools `list_databases`/`run_query`/`run_write_query`; `Catalog.resolve/load`; `Vault.open/get/save`; `TunnelManager.get/close_all`; `run_read`/`run_write`; CLI `vault_admin`; importer `bootstrap`.
- **Key Structs**: QueryResult, WriteResult, ErrorResult, Resolved, TunnelProfile, Credential.
- **Implementation Notes**: read-only via `conn.read_only`; a fresh nonce on every vault write; decrypt-all-at-launch (vault decrypted in full at launch, credentials resident in memory for the server's lifetime); errors mapped to the `error_code` enum.

## Validation Results

### Checklist
- [x] All elements traced to FDD or documented as inferences
- [x] No excluded items present in diagrams (auditing, multi-user, GCP Secret Manager, schema introspection, denylist, on-demand decryption)
- [x] Technologies match FDD/Implementation Plan specifications
- [x] Appropriate detail level progression (C1 → C2 → C3 → C4)
- [x] Independent system/CLIs handled as separate containers (not embedded)
- [x] Diagrams use modern PlantUML syntax (!include <C4/...>) for C1-C3
- [x] SHOW_LEGEND() in C1-C3 diagrams; class diagram (no legend) in C4
- [x] Notes are concise and scannable

### Consistency Verification
- Error codes, tool names and the tunnel-reuse invariant are consistent across C1-C4.
- The security invariant (only SQL in, only rows out; no credentials/PEM to Claude Code) is reflected in C1 (note), C2 (protocols), C3 (vault/executor notes) and C4 (executor/vault notes).
- The bootstrap importer is consistently modeled as reading a single `bootstrap.yaml` (plaintext, transient) and splitting it into `registry.yaml` + `vault.enc`, in C2 (container + store) and C4 (Bootstrap package/note).
- Decrypt-all-at-launch is consistently reflected across C2 (server description + note), C3 (vault.py description + note) and C4 (Vault note): whole vault decrypted once at launch, runtime `get()` is an in-memory lookup.
- The operator credential-write path (db-vault / vault_admin) is consistently modeled as out-of-band, never as an MCP tool.
