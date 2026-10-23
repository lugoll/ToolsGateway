# Catalog

One YAML file per entry; the file name is the entry's `id`. Users pick an entry
in the web UI ("Add connection") and fill in its `fields`; the result is a
connection stored in the database. The catalog itself holds no secrets and no
per-user values. It is validated at start (`gateway check`); a broken file stops
the gateway with a message naming it.

There are two kinds of entry.

## `kind: native`: an account of a native connector

```yaml
id: icloud-mail
label: iCloud Mail
description: Read, file and draft mail in an iCloud mailbox.
kind: native
integration: mail          # connector in gateway/connectors/ (mail, calendar)
backend: imap              # protocol the connector speaks for this provider
settings:                  # fixed values, not asked
  host: imap.mail.me.com
  port: 993
fields:                    # asked in the UI
  - {name: email, label: Apple ID, type: email}
  - {name: password, label: App-specific password, type: secret, help: "appleid.apple.com → …"}
capabilities:
  default: [read, mark_read, draft, move]   # what the agent may do; the user can change it
oauth:                     # optional: connected with "Connect with Google"
  provider: google
  scopes: [https://www.googleapis.com/auth/gmail.modify]
```

All of a user's connections with the same `integration` are the accounts of one
tool set (`/mcp/<user>/mail`); tools take an `account` argument. What a
connector reads from `settings` + `fields` is listed in its `config.py`
(mail: `gateway/connectors/mail/config.py`, calendar: `gateway/connectors/calendar/config.py`).
`capabilities` must be a subset of the connector's `CAPABILITIES`.

## `kind: remote`: an MCP server that is proxied

```yaml
id: todoist
label: Todoist
kind: remote
integration: todoist       # default path name; the user may choose another
url: https://ai.todoist.net/mcp   # or a field named `url`
headers:                   # optional; {name} is replaced by a field value
  Authorization: "Bearer {token}"
oauth:                     # optional: MCP authorization (discovery, client registration, PKCE)
  provider: mcp
  scopes: []
tools:
  deny: ["delete*"]        # off by default, the user may switch them on
  forbid: ["send*"]        # never served, whatever the user does
timeout: 60
```

Streamable HTTP only: the gateway starts no processes. A header whose
placeholder has no value (an optional token left empty) is not sent.

## Fields

| key | meaning |
|---|---|
| `name` | value name (`[a-z_][a-z0-9_]*`), unique in the entry |
| `label`, `help` | what the UI shows |
| `type` | `text` (default), `email`, `url`, `number`, `bool`, `secret` |
| `required` | default `true` |
| `default` | prefilled value |

`secret` values are encrypted in the database (AES-GCM under
GATEWAY_MASTER_KEY) and can be replaced in the UI but never shown again.

## Least privilege

Entries are where the write restrictions of remote servers live: anything that
sends, forwards or permanently deletes goes to `forbid`, anything destructive
but sometimes wanted goes to `deny`. Native connectors enforce their own
rules (no send tool in mail; no guests and no recurring-event changes in
calendar) and use `capabilities` for the rest, with destructive ones off by
default.
