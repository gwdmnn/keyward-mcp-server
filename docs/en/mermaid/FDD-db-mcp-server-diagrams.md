# Mermaid Diagrams - db-mcp-server

## Overview

db-mcp-server is a local MCP server (Python, stdio transport) that gives Claude Code routed, credential-isolated access to the PostgreSQL databases. The model sends only SQL and receives only result rows; database credentials and jump-host PEM keys never enter the model's context. Reads are read-only by default (a Postgres `READ ONLY` transaction) and writes require explicit per-call confirmation. These diagrams describe Phase 1: a local passphrase-encrypted vault, SSH tunnels reused per customer, initial import via `bootstrap.yaml`, and an out-of-band credential-write path.

## Identified Elements

### External flows
- The Operator supplies the passphrase at launch, imports initial data with `db-bootstrap`, and manages credentials via the `db-vault` CLI.
- Claude Code (MCP client) invokes the tools `list_databases`, `run_query`, `run_write_query`.
- PostgreSQL databases, reached over an SSH tunnel through jump hosts.

### Internal processes
- `server.py` exposes the tools via FastMCP and orchestrates the flow.
- `catalog.py` resolves `customer`/`environment`/`service` from `registry.yaml`.
- `tunnel_manager.py` keeps a tunnel pool per `(customer, environment, db_host, db_port)` with ephemeral ports.
- `vault.py` decrypts the vault into memory at launch and returns credentials by `secret_ref`.
- `crypto.py` derives the key (scrypt) and seals/decrypts (AES-256-GCM) with an atomic write.
- `executor.py` opens a psycopg connection and runs in read-only or confirmed read-write mode.
- `bootstrap.py` splits `bootstrap.yaml` into `registry.yaml` and `vault.enc`.

### Behavioral variations
- `run_query` always in a `READ ONLY` transaction; a mutation is rejected by the engine.
- `run_write_query` opens a read-write transaction only with `confirm=true`.
- A tunnel is reused if active, or opened on demand on the first query for the `(customer, environment, db_host, db_port)` endpoint.

### Public contracts
- MCP tools: `list_databases`, `run_query`, `run_write_query`.
- Pydantic models: `Catalog`, `QueryResult`, `WriteResult`, `Resolved`, `Credential`, `TunnelProfile`, `ErrorResult`.
- Files: `registry.yaml` (non-secret), `vault.enc` (encrypted), and `bootstrap.yaml` (plaintext, transient and git-ignored).
- Error enum: `CATALOG_NOT_FOUND`, `VAULT_LOCKED`, `TUNNEL_FAILED`, `QUERY_READONLY_VIOLATION`, `WRITE_NOT_CONFIRMED`, `QUERY_TIMEOUT`, `SECRET_NOT_FOUND`, `DB_ERROR`.

## Diagrams

### Architecture and security boundary

This flowchart shows the static component architecture and the system's central security boundary. It makes visible that only SQL flows in and only rows flow out between Claude Code and the server, while credentials, PEM keys, and the encrypted vault stay on the inside. Use it to understand which modules exist, where the local stores live, and why the model never sees secrets. It is the highest-level view before drilling into the execution flows.

```mermaid
flowchart LR
    Op[Operator]
    C[Claude Code]

    subgraph Server["db-mcp-server (local process)"]
        SRV[server.py]
        CAT[catalog.py]
        V[vault.py]
        CRY[crypto.py]
        TM[tunnel_manager.py]
        EXE[executor.py]
    end

    subgraph Stores["Local storage"]
        REG[registry.yaml]
        VENC[vault.enc]
        KEYS[PEM keys]
    end

    PG[(Customer PostgreSQL)]
    JH[SSH jump host]

    Op -->|passphrase at launch| SRV
    Op -->|db-vault CLI| VENC
    C -->|only SQL| SRV
    SRV -->|only rows| C
    SRV --> CAT
    SRV --> V
    SRV --> TM
    SRV --> EXE
    CAT --> REG
    V --> CRY
    CRY --> VENC
    TM --> KEYS
    TM --> JH
    JH --> PG
    EXE --> PG
```

**Notes**:
- The `db-mcp-server` boundary isolates credentials and PEM: only SQL and result rows travel between Claude Code and the server.
- Credential writes do not go through any MCP tool; they are done by the Operator via the `db-vault` CLI directly against `vault.enc`.
- `executor.py` connects to Postgres through the ephemeral local port opened by `tunnel_manager.py`.

---

### Main read flow

This sequence diagram represents the end-to-end execution of a `run_query` call, the system's most important path. It shows the temporal order between Claude Code, the internal modules, and PostgreSQL, including the decision to reuse or open an SSH tunnel. Use it to understand what happens on each read query and where the credentials are obtained in memory. It is the best reference for runtime behavior.

```mermaid
sequenceDiagram
    participant C as Claude Code
    participant S as server.py
    participant Cat as catalog.py
    participant T as tunnel_manager.py
    participant V as vault.py
    participant PG as PostgreSQL

    C->>S: run_query(customer, env, service, sql)
    S->>Cat: resolve(customer, env, service)
    Cat-->>S: tunnel_profile, dbname, secret_ref
    S->>T: get(tunnel_key, profile)
    alt tunnel active
        T-->>S: 127.0.0.1:port
    else open new
        T->>PG: SSH -L (PEM key)
        T-->>S: 127.0.0.1:port
    end
    S->>V: get(secret_ref)
    V-->>S: user, password (memory)
    S->>PG: BEGIN READ ONLY
    PG-->>S: rows
    S-->>C: columns, rows, row_count, truncated
```

**Notes**:
- The vault is decrypted in full into memory at server launch; `get(secret_ref)` is just a read of the in-memory map, never a per-call decryption.
- The `READ ONLY` transaction guarantees that any mutation is rejected by the Postgres engine itself.
- `max_rows` caps the result and sets `truncated=true` when the cut occurs.

---

### Write flow

This top-down flowchart describes the logic of `run_write_query`, highlighting the confirmation decision that separates a deliberate write from a call with no effect. It shows that, without `confirm=true`, no read-write transaction is opened, and that errors result in a `ROLLBACK`. Use it to understand the explicit barrier against accidental mutations and the `COMMIT`/`ROLLBACK` outcome. It is central to the system's deliberate-write guarantee.

```mermaid
flowchart TD
    A[run_write_query] --> B{confirm true?}
    B -->|no| C[WRITE_NOT_CONFIRMED]
    B -->|yes| D[Resolve catalog]
    D --> E[Get tunnel and credential]
    E --> F[BEGIN read-write; SQL]
    F --> G{Error?}
    G -->|yes| H[ROLLBACK, DB_ERROR]
    G -->|no| I[COMMIT]
    I --> J[affected_rows, committed]
```

**Notes**:
- Without `confirm=true` the tool returns `WRITE_NOT_CONFIRMED` without opening a transaction or touching the database.
- Catalog, tunnel, and credential resolution reuses the same steps as the read flow.
- On error the transaction is rolled back and the return is a structured error with no credentials.

---

### Tunnel lifecycle

This state diagram represents the lifecycle of an SSH tunnel managed by `tunnel_manager.py` for each `(customer, environment, db_host, db_port)` endpoint. It shows the on-demand opening on the first query, reuse while active, and closing on idle-timeout or shutdown. Use it to understand the at-most-one-tunnel-per-customer-environment-and-DB-host policy and how open failures are handled. It is essential to the efficient-tunnel-reuse goal.

```mermaid
stateDiagram-v2
    [*] --> Closed
    Closed --> Opening: 1st query of the pair
    Opening --> Active: bind 127.0.0.1 OK
    Opening --> Closed: TUNNEL_FAILED
    Active --> Active: query reuses tunnel
    Active --> Closed: idle-timeout / shutdown
```

**Notes**:
- A tunnel is shared by all services of the same `(customer, environment)` that use the same DB host.
- The local port is ephemeral on `127.0.0.1`, obtained with `local_bind_address=('127.0.0.1', 0)`.
- After idle-timeout the tunnel is closed; the next query transparently reopens it.

---

### Opening the vault

This top-down flowchart details the cryptographic process of opening the vault at server launch, combining scrypt key derivation and authenticated AES-256-GCM decryption. It shows that a missing or wrong passphrase makes the GCM tag check fail and the server does not start, returning `VAULT_LOCKED`. Use it to understand why secrets stay encrypted at rest and how the vault's authenticity is validated. It is the explanation of the non-obvious algorithm behind the vault.

```mermaid
flowchart TD
    A[Server launch] --> B[Read DB_MCP_PASSPHRASE]
    B --> C[Read vault.enc envelope]
    C --> D[derive_key scrypt]
    D --> E[AES-256-GCM decrypt]
    E --> F{GCM tag valid?}
    F -->|no| G[VAULT_LOCKED, does not start]
    F -->|yes| H[secret_ref map in memory]
    H --> I[Tools exposed]
```

**Notes**:
- The envelope header stores the `salt`, the scrypt parameters, and the `nonce` in clear; only the credential map is encrypted.
- The passphrase is never persisted; it exists only in memory during execution.
- Without the vault open no tool operates (decrypt-all-at-launch).

---

### Bootstrap — credential split

This top-down flowchart describes the initial import done by `bootstrap.py`, which reads a single `bootstrap.yaml` (same shape as `registry.yaml`, but with plaintext `user`/`password` per service) and splits it into two halves. The non-secret routing half goes to `registry.yaml`; the credentials are sealed into `vault.enc` via `crypto.py`, and each service's `secret_ref` is derived as `customer/env/service`, linking the two halves. Use it to understand the cutover from the source configuration and why `bootstrap.yaml` is git-ignored and should be removed or kept offline once the vault is generated. It is the basis of the routing-vs-secret separation.

```mermaid
flowchart TD
    BY[bootstrap.yaml<br/>plaintext, git-ignored] --> BP[bootstrap.py]
    BP --> D{Split per service}
    D -->|routing| REG[registry.yaml non-secret]
    D -->|user and password| CRY[crypto.py seals]
    CRY --> VENC[vault.enc encrypted]
    BP -->|derives| SR[secret_ref customer/env/service]
    SR --> REG
    SR --> VENC
    VENC --> DEL[Remove or keep offline]
```

**Notes**:
- `bootstrap.yaml` contains plaintext credentials; it is git-ignored (template: `bootstrap.example.yaml`) and should be removed or kept offline once the vault is generated.
- The derived `secret_ref` (`customer/env/service`) is the key that links the routing entry in `registry.yaml` to the credential in `vault.enc`.
- `--dry-run` shows the summary without writing; `--force` is required to overwrite an existing `vault.enc`. It never prints credential values.

---

### Credential administration (out of band)

This sequence diagram shows the incremental credential-write path into the vault, run by the Operator via the `db-vault` CLI, without going through any MCP tool. It makes clear that the password is read from a hidden prompt (`getpass`), that the vault is re-encrypted with a fresh nonce, and that it is written atomically. Use it to understand why the password never enters the model's context or command-line arguments. It is the basis of the out-of-band credential-write guarantee after the initial bootstrap.

```mermaid
sequenceDiagram
    participant Op as Operator
    participant CLI as db-vault (CLI)
    participant Cry as crypto.py
    participant F as vault.enc

    Op->>CLI: db-vault set secret_ref
    CLI->>F: read envelope
    CLI->>Cry: derive_key(passphrase, salt)
    CLI->>Cry: decrypt(envelope)
    Cry-->>CLI: map in memory
    Op->>CLI: user (visible)
    Op->>CLI: password (getpass hidden)
    CLI->>Cry: encrypt(map, fresh nonce)
    Cry-->>CLI: new envelope
    CLI->>F: atomic write (tmp + replace)
    CLI-->>Op: ok, vault re-sealed
```

**Notes**:
- The passphrase comes from `DB_MCP_PASSPHRASE` or a prompt; the DB password is never passed via argv.
- Every write uses a fresh random nonce, an AES-GCM requirement, and never reuses the previous nonce.
- After adding a new `secret_ref` the server must be restarted, which decrypts the vault at launch.

---

### Public contracts

This class diagram gathers the server's public contracts: the three MCP tools and the Pydantic output and routing models. It shows that no tool accepts or returns `user`, `password`, DSN, or PEM, and that the credential (`Credential`) is internal, referenced by `secret_ref`. Use it as an integration reference for anyone consuming the tools or extending the models. It is the stable specification of the exposed interfaces.

```mermaid
classDiagram
    class ToolsMCP {
        +list_databases() Catalog
        +run_query(customer, environment, service, sql, max_rows) QueryResult
        +run_write_query(customer, environment, service, sql, confirm) WriteResult
    }
    class QueryResult {
        +columns list
        +rows list
        +row_count int
        +truncated bool
    }
    class WriteResult {
        +affected_rows int
        +committed bool
    }
    class ErrorResult {
        +error_code str
        +message str
    }
    class Resolved {
        +tunnel_profile TunnelProfile
        +dbname str
        +secret_ref str
        +max_rows int
        +statement_timeout_ms int
    }
    class TunnelProfile {
        +jump_host str
        +jump_port int
        +pem_key str
        +db_host str (from service)
        +db_port int (from service)
    }
    class Credential {
        +user str
        +password SecretStr
    }

    ToolsMCP --> QueryResult
    ToolsMCP --> WriteResult
    ToolsMCP --> ErrorResult
    Resolved --> TunnelProfile
```

**Notes**:
- `Credential` is internal to the server and never appears in the tools' input or output.
- Errors are returned as `ErrorResult` with an `error_code` (enum) and a `message` without secrets.
- `Resolved` is the internal result of `catalog.resolve`, linking routing and `secret_ref`.

---

### Error handling

This top-down flowchart maps where each `error_code` arises along a query's pipeline, from vault to database. It shows the sequence of barriers (vault open, catalog resolves, `secret_ref` present, tunnel opens, SQL execution) and the error corresponding to each failure. Use it to diagnose which step produced an error and to confirm that early failures have no side effects. It is the consolidated view of the system's resilience.

```mermaid
flowchart TD
    A[Tool call] --> B{Vault open?}
    B -->|no| E1[VAULT_LOCKED]
    B -->|yes| C{Catalog resolves?}
    C -->|no| E2[CATALOG_NOT_FOUND]
    C -->|yes| D{secret_ref in vault?}
    D -->|no| E3[SECRET_NOT_FOUND]
    D -->|yes| F{Tunnel opens?}
    F -->|no| E4[TUNNEL_FAILED]
    F -->|yes| G[Run SQL]
    G --> H{Outcome}
    H -->|mutation in read-only| E5[QUERY_READONLY_VIOLATION]
    H -->|timeout| E6[QUERY_TIMEOUT]
    H -->|DB error| E7[DB_ERROR]
    H -->|ok| S[Result]
```

**Notes**:
- `CATALOG_NOT_FOUND` and `SECRET_NOT_FOUND` fail before any tunnel or connection, with no side effects.
- `QUERY_READONLY_VIOLATION` occurs when a mutating SQL statement is sent to `run_query`.
- `WRITE_NOT_CONFIRMED` belongs to the write flow and is handled in the write diagram.

---

### Full server process (launch and serving)

This sequence diagram represents the full db-mcp-server process over its lifetime, in two phases within a single timeline. The first phase is launch (once): the Operator starts the server with the passphrase in `DB_MCP_PASSPHRASE`, which loads config, runs `Catalog.load`, opens the vault by decrypting `vault.enc` in full into memory via scrypt and AES-256-GCM, verifies the `secret_ref`s against the vault, and initializes the `TunnelManager`. The second phase is serving (repeated, over stdio): it briefly shows `list_databases` and, in detail, a `run_query`, making clear that the vault is decrypted only at launch and that `vault.get` is just an in-memory read, never a per-call decryption. Use it as the highest-level view of runtime behavior, complementing the focused read flow.

```mermaid
sequenceDiagram
    participant Op as Operator
    participant C as Claude Code
    participant S as server.py
    participant Cat as catalog.py
    participant V as vault.py
    participant Cry as crypto.py
    participant T as tunnel_manager.py
    participant E as executor.py
    participant PG as "PostgreSQL (jump host)"

    Note over Op,PG: Phase 1 - Launch (once)
    Op->>S: start server (DB_MCP_PASSPHRASE)
    S->>S: load config
    S->>Cat: load(registry.yaml)
    Cat-->>S: catalog in memory
    S->>V: open(vault.enc, passphrase)
    V->>Cry: scrypt + AES-256-GCM decrypt
    Cry-->>V: secret_ref map (memory)
    Note over V,Cry: whole vault decrypted once - failure here = VAULT_LOCKED, server does not start
    V-->>S: vault open
    S->>Cat: verify_against(vault)
    Cat-->>S: secret_refs match
    S->>T: initialize TunnelManager
    S-->>C: tools available (stdio)

    Note over C,PG: Phase 2 - Serving (repeated, stdio)
    Note over C,S: only SQL in, only rows out - credentials and PEM never return to Claude Code
    loop on each tool call
        C->>S: list_databases()
        S->>Cat: list_databases()
        Cat-->>S: catalog without secrets
        S-->>C: customers, environments, services

        C->>S: run_query(customer, env, service, sql)
        S->>Cat: resolve(customer, env, service)
        Cat-->>S: tunnel_profile, dbname, secret_ref
        S->>T: get(tunnel_key, profile)
        alt tunnel active
            T-->>S: 127.0.0.1:port
        else open new
            T->>PG: SSH -L (PEM key)
            T-->>S: 127.0.0.1:port
        end
        S->>V: get(secret_ref)
        V-->>S: user, password (in-memory read)
        S->>E: run_read(resolved, cred, sql)
        E->>PG: BEGIN READ ONLY sql
        PG-->>E: rows
        E-->>S: columns, rows, row_count, truncated
        S-->>C: only rows
    end
    Note over C,E: run_write_query requires confirm=true - without confirmation: WRITE_NOT_CONFIRMED, no transaction
```

**Notes**:
- The vault is decrypted in full once at launch (decrypt-all-at-launch); at runtime `vault.get(secret_ref)` is just a read of the in-memory map, never a per-call decryption.
- A failure to open the vault (missing or wrong passphrase) makes the server not start and report `VAULT_LOCKED`; no tool operates without the vault open.
- `list_databases` responds only with the navigable catalog, without `secret_ref`, jump host, or PEM.
- The tunnel is reused when active or opened on demand with the PEM key; the ephemeral port on `127.0.0.1` is used by `executor.py` to open the psycopg connection.
- The security boundary holds in both phases: only SQL and rows travel between Claude Code and the server; credentials and PEM never return to the model.
- `run_write_query` opens a read-write transaction only with `confirm=true`; without confirmation it returns `WRITE_NOT_CONFIRMED` without touching the database.
