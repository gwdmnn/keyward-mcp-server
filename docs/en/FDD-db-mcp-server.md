### FDD: Secure Database Access MCP Server (db-mcp-server)

Version: 0.1.0 (Phase 1 — local passphrase-encrypted vault)
Date: 2026-08-14
Owner: Guilherme Weidmann

---

### 1. Technical context and motivation

**Technical problem.** Today, access to the PostgreSQL databases is done through the `database-connection` skill, whose flow forces the assistant (Claude Code) to:

1. **Read the service's `.env` file** — exposing `DB_USER` and `DB_USER_PASSWORD` in plaintext in the model's context;
2. **Assemble the command** `docker exec pg-client psql "postgresql://user:pass@ssh-tunnel:5432/db"` — putting the password in the tool call, the shell history, and the process list (`ps`);
3. **Access the jump-host PEM keys**, which live in the skill's directory.

The most serious consequence is **not** the local disk: it is that **every credential flows through the conversation transcript**, which is sent to and stored on Anthropic's servers. On top of that, running `psql` directly has no barrier against destructive statements on production databases (`UPDATE`/`DELETE`/`DROP`).

**Fit in the HLD.** This component is a **local MCP server (stdio transport)**, launched by Claude Code under the operator's OS user. It takes on three responsibilities that are scattered today: (a) hold encrypted credentials, (b) open and reuse the SSH tunnels per customer, (c) run queries and return only result rows. The model now **sends only SQL** and **receives only results** — never credentials, keys, or connection strings.

**Actors.**
- **Operator** — the person driving the Claude Code session (Phase 1: single user).
- **Claude Code (MCP client)** — consumes the tools; writes the SQL; never sees secrets.
- **db-mcp-server** — the local process that routes, decrypts, and connects.
- **Jump hosts / PostgreSQL databases** — the final destinations, reached over an SSH tunnel.

**Boundaries (Phase 1).** Single user, local execution, passphrase-encrypted vault. Persistent auditing and shared team use are deferred to Phase 2 (see Scope). Migrating the secret backing to an external secret manager (e.g. **GCP Secret Manager**) is planned evolution (Phase 2).

---

### 2. Technical goals

- **Zero credentials in the model's context.** No tool returns, accepts, or logs `DB_USER`/`DB_USER_PASSWORD`, a full DSN, or PEM key contents. *Invariant:* the input and output payload of any tool is auditable and contains no secrets.
- **Secrets encrypted at rest.** The vault is only decryptable with the passphrase, supplied out of the model's reach (an environment variable at server launch). *Measure:* key derived via scrypt (KDF) and authenticated encryption (AES-256-GCM); the passphrase is never persisted.
- **Safe reads by default.** `run_query` runs in a Postgres `READ ONLY` transaction — the engine itself rejects any write/DDL. *Invariant:* no mutation is possible through `run_query`, regardless of the SQL sent.
- **Explicit, deliberate writes.** Mutations happen only through `run_write_query` with explicit per-call confirmation. *Invariant:* `confirm != true` ⇒ no read-write transaction is opened.
- **Correct routing per customer.** `customer`/`environment`/`service` are resolved against the catalog; an unknown identifier fails immediately, before any connection. *Measure:* `CATALOG_NOT_FOUND` error with no side effects.
- **Out-of-band credential writes.** Adding/updating a credential in the vault **never** goes through an MCP tool (hence never through the model's context); it is an operator action via a dedicated CLI, with the password read from a hidden prompt. *Invariant:* no path Claude touches ever receives `user`/`password` in clear.
- **Efficient tunnel reuse.** At most one active SSH tunnel per `(customer, environment)`, shared by all of that customer's services. *Measure:* number of open tunnels ≤ number of distinct `(customer, environment)` pairs in use.

---

### 3. Scope and exclusions

**Included (Phase 1)**
- A Python stdio MCP server with the tools `list_databases`, `run_query`, `run_write_query`.
- A declarative **catalog** (`registry.yaml`) with tunnel and connection profiles (non-secret).
- A local encrypted **vault** (AES-256-GCM + scrypt) mapping `secret_ref → {user, password}`.
- A native **TunnelManager** (the `sshtunnel` library), pooled per `(customer, environment)`, with ephemeral ports on `127.0.0.1`, and teardown on idle-timeout and shutdown.
- **Read-only enforcement** via a Postgres transaction; a separate, confirmed write path.
- A **row limit** per query (`max_rows`, configurable default) with a `truncated` flag.
- **Bootstrap importer** (`bootstrap.py`): reads a single `bootstrap.yaml` (same shape as `registry.yaml`, but with plaintext `user`/`password` per service) and splits it into `registry.yaml` (non-secret) and the vault (secrets). `secret_ref` is derived as `customer/env/service`. `bootstrap.yaml` is git-ignored and should be removed/kept offline once the vault is generated.
- **Vault admin CLI** (`vault_admin`): adds/updates/removes a `secret_ref` in `vault.enc`, run by the **operator** (not an MCP tool), with the password from a hidden prompt. Includes a catalog↔vault consistency check.

**Excluded (deferred to Phase 2+)**
- Persistent audit trail (log of who/when/which query per database).
- Multi-user operation / shared team server and the corresponding secret model.
- Secret backing in **GCP Secret Manager** (ADC/service account).
- Schema-introspection tools (`describe_schema`, `list_tables`) — can be added later.
- A Claude Code permission denylist (block reads of the vault/PEM and direct `psql`/`docker` execution). *Note:* without it, the server removes credentials from the transcript (the main goal) but does not prevent a shell on the same host from bypassing the barrier. Kept as optional hardening.
- psycopg connection pooling/keep-alive (Phase 1 opens one connection per query over the reused tunnel).

---

### 4. Detailed flows and diagrams

**Main flow — read (`run_query`)**
1. Claude calls `run_query(customer, environment, service, sql, max_rows?)`.
2. The **catalog** resolves `(customer, environment)` → tunnel profile and `(…, service)` → `{ dbname, secret_ref }`. Failure ⇒ `CATALOG_NOT_FOUND`.
3. The **TunnelManager** obtains the tunnel for `(customer, environment)`: reuses it if active, otherwise opens one over SSH (PEM key) and binds `127.0.0.1:<ephemeral-port>`.
4. The **vault** returns the credential for `secret_ref` — just a **read from the already-decrypted in-memory map**. `vault.enc` is decrypted in full **once, at launch** (scrypt + AES-256-GCM); there is no per-query decryption.
5. Opens a psycopg connection to `127.0.0.1:<port>/dbname`, starts a `READ ONLY` transaction, runs `sql`.
6. Applies `max_rows`; returns `{ columns, rows, row_count, truncated }`. The psycopg connection is closed; the credentials stay in the in-memory map for the server's lifetime (a Phase 1 decision — see note below).

**Main flow — write (`run_write_query`)**
- Same as steps 1–4, but the tool requires `confirm=true`. The transaction is read-write; on error it does `ROLLBACK`. Without `confirm=true` ⇒ `WRITE_NOT_CONFIRMED`, with no transaction opened.

> **Note — in-memory credential residency (Phase 1):** the vault is decrypted in full at launch and the `secret_ref → {user, password}` map stays in memory while the server is up (decrypt-all-at-launch). This keeps the main goal (credentials out of the model's transcript) and simplifies the runtime. Against a local attacker with the same OS user, in-memory residency adds little — that process is already inspectable and Python cannot reliably "zero out" secrets. On-demand per-`secret_ref` decryption is a possible Phase 2 refinement, should the threat model come to include local memory inspection.

**Admin flow — adding a credential (out-of-band, by the operator)**
1. The operator runs `vault_admin set <secret_ref>` in their **own terminal** (not via Claude). The passphrase comes from `DB_MCP_PASSPHRASE` or a prompt.
2. The CLI derives the key (scrypt, using the salt from the header) and **decrypts the whole `vault.enc`** into memory → the `secret_ref → {user, password}` map.
3. Reads `user` (visible) and `password` (**hidden prompt, `getpass`**); neither is echoed, logged, or passed as a command-line argument.
4. Updates the map entry; **re-encrypts with a fresh random nonce** (never reuses the previous nonce — an AES-GCM requirement).
5. **Atomic write**: writes `vault.enc.tmp` and does `os.replace` over `vault.enc`, avoiding corruption of the single copy on failure.
6. The operator adds the **routing** half in `registry.yaml` (`dbname` + `secret_ref`; and, if `(customer, environment)` is new, the `tunnel` block + the PEM in `keys/`) — the non-secret part, editable by hand or by Claude.
7. **Restarts the MCP server** (or uses a future `reload`): the vault is decrypted into memory at launch, so a new `secret_ref` only exists after the re-read.

**Alternative flows and exceptions**
- **Locked vault / missing or wrong passphrase:** at startup the server derives the key and tries to decrypt; an authentication failure (GCM) ⇒ the server does not start and reports `VAULT_LOCKED`. No tool operates without an open vault.
- **Tunnel open failure** (invalid PEM, unreachable jump host, handshake timeout): `TUNNEL_FAILED`; no DB connection is attempted.
- **Read-only violation:** a mutating statement in `run_query` is rejected by the engine in the read-only transaction ⇒ mapped to `QUERY_READONLY_VIOLATION`.
- **SQL / DB error** (syntax, permission, constraint): `ROLLBACK` and `DB_ERROR` with the Postgres message (no credentials).
- **Query timeout:** `statement_timeout` on the server; exceeded ⇒ `QUERY_TIMEOUT` + `ROLLBACK`.
- **Tunnel idle-timeout:** a tunnel unused beyond the limit is closed; the next query transparently reopens it.
- **Unknown customer/service:** `CATALOG_NOT_FOUND`, no side effects.
- **`secret_ref` in the catalog without a vault entry:** at startup the server validates `registry.yaml` against the vault; missing refs raise a warning and a query to the affected service returns `SECRET_NOT_FOUND` (without attempting a tunnel/DB connection).

**Diagram — sequence (read)**

```mermaid
sequenceDiagram
    participant C as Claude Code (MCP client)
    participant S as db-mcp-server
    participant Cat as Catalog
    participant V as Vault
    participant T as TunnelManager
    participant PG as PostgreSQL (via jump host)

    Note over V: vault.enc is decrypted in full at launch<br/>(scrypt + AES-256-GCM). At runtime, get() is just a map read.

    C->>S: run_query(customer, env, service, sql)
    S->>Cat: resolve(customer, env, service)
    Cat-->>S: {tunnel_profile, dbname, secret_ref}
    S->>T: get_tunnel(customer, env)
    alt tunnel already active
        T-->>S: 127.0.0.1:port
    else open a new one
        T->>PG: SSH -L (PEM key)
        T-->>S: 127.0.0.1:port
    end
    S->>V: get(secret_ref)
    V-->>S: {user, password} (read from in-memory map)
    S->>PG: BEGIN READ ONLY; sql
    PG-->>S: rows
    S-->>C: {columns, rows, row_count, truncated}
```

**Diagram — tunnel lifecycle**

```mermaid
stateDiagram-v2
    [*] --> Closed
    Closed --> Opening: 1st query for (customer, env)
    Opening --> Active: bind 127.0.0.1:port OK
    Opening --> Closed: TUNNEL_FAILED
    Active --> Active: query reuses tunnel
    Active --> Closed: idle-timeout / shutdown
```

---

### 5. Public contracts (MCP tools, configuration, and examples)

> Note: because this is an MCP server (stdio transport), the "public contracts" are **tools** (name + input schema + output schema), not HTTP routes. Errors are returned as a structured object with `error_code` (enum) + `message` (no secrets), instead of an HTTP status.

**Contract 1 — `list_databases`**
- Type: tool (MCP)
- Signature: `list_databases() -> Catalog`
- Semantics: returns the navigable catalog (customers → environments → services) **with no secrets** (no user, password, PEM, or jump host). It lets the model discover valid targets.

Output (example):
```json
{
  "customers": [
    {
      "customer": "acme",
      "environments": [
        { "environment": "live", "services": ["orders-api", "inventory-api", "shipping-api"] }
      ]
    },
    {
      "customer": "globex",
      "environments": [ { "environment": "live", "services": ["orders-api"] } ]
    }
  ]
}
```

**Contract 2 — `run_query` (read-only)**
- Type: tool (MCP)
- Signature: `run_query(customer: str, environment: str, service: str, sql: str, max_rows?: int) -> QueryResult`
- Semantics: runs `sql` in a **`READ ONLY`** transaction. Any mutation is rejected by the engine. `max_rows` caps the result (catalog default, e.g. 1000); if truncated, `truncated=true`.

Request example (input):
```json
{
  "customer": "acme",
  "environment": "live",
  "service": "orders-api",
  "sql": "SELECT id, name FROM orders WHERE status = 'OPEN' ORDER BY id LIMIT 50",
  "max_rows": 50
}
```

Response example (output):
```json
{
  "columns": ["id", "name"],
  "rows": [[101, "Alpha"], [102, "Beta"]],
  "row_count": 2,
  "truncated": false
}
```

**Contract 3 — `run_write_query` (write, with confirmation)**
- Type: tool (MCP)
- Signature: `run_write_query(customer: str, environment: str, service: str, sql: str, confirm: bool) -> WriteResult`
- Semantics: opens a read-write transaction only if `confirm=true`; otherwise returns `WRITE_NOT_CONFIRMED` with no effect. On success it `COMMIT`s; on error, `ROLLBACK`. Returns the affected row count.

Request example (input):
```json
{
  "customer": "acme",
  "environment": "live",
  "service": "users-api",
  "sql": "INSERT INTO order_tags (tag) VALUES ('priority')",
  "confirm": true
}
```

Response example (output):
```json
{ "affected_rows": 1, "committed": true }
```

**Error enum (all tools)**

| `error_code` | Meaning |
|---|---|
| `CATALOG_NOT_FOUND` | `customer`/`environment`/`service` does not exist in the catalog. No side effects. |
| `VAULT_LOCKED` | Vault not opened (missing/wrong passphrase at launch). |
| `TUNNEL_FAILED` | Failure to open/validate the SSH tunnel (PEM, jump host, timeout). |
| `QUERY_READONLY_VIOLATION` | Attempted mutation in `run_query`. |
| `WRITE_NOT_CONFIRMED` | `run_write_query` called without `confirm=true`. |
| `QUERY_TIMEOUT` | `statement_timeout` exceeded; transaction rolled back. |
| `SECRET_NOT_FOUND` | `secret_ref` exists in the catalog but not in the vault. No tunnel/DB attempt. |
| `DB_ERROR` | PostgreSQL error (syntax, permission, constraint). Message without secrets. |

Error response example:
```json
{ "error_code": "QUERY_READONLY_VIOLATION",
  "message": "cannot execute INSERT in a read-only transaction" }
```

---

**Contract 4 — `registry.yaml` (catalog, non-secret)**
- Type: configuration file
- Semantics: single source of routing; replaces the markdown mapping table and the scattered `.env` files. Contains no passwords — only a `secret_ref` pointer into the vault.

```yaml
defaults:
  max_rows: 1000
  statement_timeout_ms: 30000
  tunnel_idle_timeout_s: 300

customers:
  acme:
    environments:
      live:
        tunnel:
          jump_host: 203.0.113.10
          jump_port: 22
          jump_user: tunnel
          pem_key: acme-live-bastion.pem   # resolved under keys_dir
          db_host: db.internal.example.com
          db_port: 5432
        services:
          orders-api:
            dbname: orders
            secret_ref: acme/live/orders-api
          users-api:
            dbname: users
            secret_ref: acme/live/users-api
```

**Contract 5 — Vault (`vault.enc`, encrypted secrets)**
- Type: encrypted file (internal format)
- Semantics: AES-256-GCM envelope; key derived from the passphrase via scrypt. The plaintext (after decryption) is a `secret_ref → {user, password}` map.

Header (clear) + ciphertext:
```json
{
  "kdf": "scrypt",
  "kdf_params": { "n": 16384, "r": 8, "p": 1, "salt_b64": "…" },
  "cipher": "AES-256-GCM",
  "nonce_b64": "…",
  "ciphertext_b64": "…"
}
```
Plaintext (never persisted in clear):
```json
{
  "acme/live/orders-api": { "user": "…", "password": "…" }
}
```

**Contract 6 — MCP server registration (launch)**
- Type: MCP client configuration (Claude Code)
- Semantics: the **passphrase** is injected via an environment variable at launch, out of the model's reach. The server derives the key, decrypts the vault into memory, and only then exposes the tools.

```json
{
  "mcpServers": {
    "db": {
      "command": "python",
      "args": ["-m", "db_mcp_server.server"],
      "env": {
        "DB_MCP_PASSPHRASE": "…injected at launch, not stored in the repo…",
        "DB_MCP_REGISTRY": "./registry.yaml",
        "DB_MCP_VAULT": "./vault.enc",
        "DB_MCP_KEYS_DIR": "./keys"
      }
    }
  }
}
```

**Contract 7 — `vault_admin` (operator CLI, not an MCP tool)**
- Type: CLI (run by the operator in their own terminal)
- Semantics: the only **write** path for a credential into the vault. It is not exposed as an MCP tool, so Claude never invokes it and the password never enters the model's context. The passphrase comes from `DB_MCP_PASSPHRASE` or a prompt; the DB password is read from a hidden prompt (`getpass`) — never as a command-line argument. Every write re-encrypts the vault with a fresh nonce and uses an atomic write.

Subcommands:

| Command | Effect |
|---|---|
| `vault_admin set <secret_ref>` | Creates/updates the entry; prompts for `user` (visible) and `password` (hidden). |
| `vault_admin rm <secret_ref>` | Removes the entry from the vault. |
| `vault_admin list` | Lists existing `secret_ref`s (**keys only**, never user/password). |
| `vault_admin verify` | Checks consistency: every `secret_ref` in `registry.yaml` has a vault entry, and vice versa. |

Usage example:
```bash
$ python -m db_mcp_server.vault_admin set acme/live/new-api
DB user: svc_new_api
DB password: ****        # hidden (getpass), not echoed
ok: secret_ref 'acme/live/new-api' saved (vault re-sealed, new nonce)
```

`verify` output (example):
```json
{
  "ok": false,
  "missing_in_vault": ["acme/live/billing-api"],
  "orphan_in_vault": []
}
```

> Full flow for adding a new service in §4 ("Admin flow — adding a credential"): the **secret** half via `vault_admin set`, the **routing** half in `registry.yaml`, followed by a server restart.

---

### Appendix — Phase plan

- **Phase 1 (this FDD):** tools + catalog + passphrase vault + native tunnel + bootstrap importer; removal of the plaintext credentials from the `.env` files.
- **Phase 2:** persistent audit trail; multi-user model; migrating the secret backing to GCP Secret Manager; schema-introspection tools; a Claude Code permission denylist as a hard barrier.
