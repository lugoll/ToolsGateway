-- Users and their credentials. Names are URL slugs: /mcp/<name>.
CREATE TABLE users (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    password_hash    TEXT,
    disabled         INTEGER NOT NULL DEFAULT 0,
    created          TEXT NOT NULL
);

-- Bearer tokens for MCP clients. Only the SHA-256 of a token is stored.
CREATE TABLE api_tokens (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    hash       TEXT NOT NULL UNIQUE,
    prefix     TEXT NOT NULL,
    created    TEXT NOT NULL,
    last_used  TEXT,
    revoked    TEXT
);

-- Web UI sessions. Only the SHA-256 of the cookie value is stored.
CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY,
    hash       TEXT NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    created    TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- One catalog entry instantiated by a user. Native connections of one
-- integration are the accounts of one tool set (/mcp/<user>/mail); a remote
-- connection is its own integration (integration = name).
CREATE TABLE connections (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    catalog_id    TEXT NOT NULL,
    integration   TEXT NOT NULL,
    name          TEXT NOT NULL,
    label         TEXT NOT NULL,
    settings      TEXT NOT NULL DEFAULT '{}',   -- non-secret field values (JSON)
    capabilities  TEXT,                         -- JSON list; NULL = catalog default
    tools         TEXT NOT NULL DEFAULT '{}',   -- {"enabled": [...], "disabled": [...]} user toggles
    enabled       INTEGER NOT NULL DEFAULT 1,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    UNIQUE (user_id, integration, name)
);

-- Encrypted values (storage/crypto.py), write-only from the UI.
CREATE TABLE secrets (
    connection_id  INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    value          TEXT NOT NULL,
    updated        TEXT NOT NULL,
    updated_by     TEXT NOT NULL,
    PRIMARY KEY (connection_id, name)
);

-- Encrypted instance-wide values (the Google OAuth client), set by the admin.
CREATE TABLE instance_secrets (
    name        TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated     TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);

-- Tool calls, changes made through tools, and account administration.
-- Argument values are never stored, only their names.
CREATE TABLE audit (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    user         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('call', 'change', 'admin')),
    integration  TEXT,
    connection   TEXT,
    action       TEXT NOT NULL,
    ok           INTEGER NOT NULL DEFAULT 1,
    ms           INTEGER,
    detail       TEXT
);
CREATE INDEX audit_user_id ON audit (user, id);

CREATE TABLE meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
