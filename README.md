# tools-gateway

One MCP gateway for a household. An admin creates users. Each user connects
their own accounts in a small web UI, generates bearer tokens, and points any
MCP client at their endpoints:

- `/mcp/<user>`: four meta tools (`list_integrations`, `search_tools`,
  `describe_tool`, `call_tool`) that reach every tool of every integration
- `/mcp/<user>/<integration>`: the plain tools of one integration (`mail`,
  `calendar`, `todoist`, ...)

Two kinds of connection:

- **Native connectors** (Python, in `gateway/connectors/`), for integrations
  without a usable upstream server. All accounts of one kind form one tool
  set, and the connector speaks each provider's protocol:
  - `mail`: IMAP (iCloud, t-online, any) and the Gmail API
  - `calendar`: CalDAV (iCloud, any) and the Google Calendar API
- **Remote MCP servers** (Todoist's hosted server, anything speaking
  streamable HTTP) are proxied as they are. Static headers or MCP OAuth are
  supported, and each tool can be switched on or off.

Python only: the gateway starts no processes and there is no Node in the
image. Built on [FastMCP](https://gofastmcp.com) (`fastmcp==4.0.10`) and
Starlette. Templates of what can be connected live in the [catalog](catalog/README.md),
and state lives in one SQLite file.

## Endpoints

| Path | Auth | What |
|---|---|---|
| `/mcp/<user>` | Bearer | meta tools; stateless streamable HTTP (no session ids, so restarts never wedge a client) |
| `/mcp/<user>/<integration>` | Bearer | the integration's tools |
| `/api/v1/<user>/tools[?integration=]` | Bearer | `{"tools": [{integration, name, description, input_schema}], "errors": {integration: reason}}` |
| `/api/v1/<user>/tool-invocations` | Bearer | `POST {"tool": "<integration>/<tool>", "args": {}}` → `{"result": {"content", "isError", "structuredContent"}}` or `{"error"}` (404 unknown, 409 not running, 502 upstream failure, 504 timeout) |
| `/api/v1/<user>/status[?fresh=1]` | Bearer | per integration `ok` / `off` (why) / `error`, with tool counts and accounts; 503 if one fails |
| `/health/live`, `/health/ready` | none | process up; database and catalog usable |
| everything else | session | the web UI |

A token opens only its owner's paths: a valid token on another user's path
gets 403. Tokens are `tgw_…`, shown once, stored as SHA-256, revocable, and a
user can have several (one per client).

## Configuration

The environment holds only what is needed before the database opens
(`gateway/settings.py`):

| Variable | |
|---|---|
| `GATEWAY_MASTER_KEY` (or `_FILE`) | 32 random bytes, base64 (`gateway keygen`). Encrypts every stored secret. **Required.** |
| `GATEWAY_PUBLIC_URL` | how browsers reach the UI, e.g. `https://tools.pi.home` (cookies, cross-site check, OAuth redirect) |
| `GATEWAY_ADMIN_USER`, `GATEWAY_ADMIN_PASSWORD` | the first admin, created only while no admin exists |
| `GATEWAY_DATA` | writable directory for `gateway.db` (image: `/data`) |
| `GATEWAY_CATALOG` | catalog directory (image: `/app/catalog`) |
| `GATEWAY_AUDIT_DAYS`, `GATEWAY_AUDIT_MAX_ROWS` | audit retention, default 90 days / 100 000 rows |
| `GATEWAY_MASTER_KEY_OLD` | previous key while rotating |

Everything else (users, connections, secrets, tokens, the Google OAuth client)
is managed in the web UI and stored in `gateway.db`.

### Secrets

Passwords, OAuth tokens and the Google client secret are encrypted with
AES-256-GCM under the master key. Each value is bound to its row, so a
ciphertext copied elsewhere does not decrypt. A key check makes a wrong or
missing key fail at start. The UI can set and replace secrets but never shows
them. Users set their own; an admin may overwrite a user's secret (a forgotten
app password), and that shows up in the user's activity log.

To rotate the key, set the new key as `GATEWAY_MASTER_KEY` and the old one as
`GATEWAY_MASTER_KEY_OLD`, run `gateway rotate-key`, then remove the old key.
Back up `gateway.db` and the key separately: the database is useless without
the key, and losing the key means entering every secret again.

## Web UI

Pages are server-rendered with plain HTML forms and no JavaScript.

- **Overview:** your endpoints, integrations and connections with their status.
- **Add connection:** pick a catalog entry, fill in its fields.
- **Connection page:**
  - settings, write-only secrets and "Test connection" (a real round trip to the provider)
  - what the agent may do (native capabilities) or which tools are on (remote servers)
  - "Connect" for OAuth
  - disable or delete
- **Tokens:** create (shown once) and revoke.
- **Activity:** your audit log. Admins see everyone's.
- **Users** (admin): create, disable, reset password, make admin, delete.
- **Settings** (admin): the Google OAuth client.

### Security

- Passwords are hashed with Argon2id.
- Sessions are server-side behind an HttpOnly, SameSite=Strict cookie, named
  `__Host-session` (Secure) when the public URL is https. A session ends
  after 3 idle days, 30 days at most, at logout, and when the password changes
  or the user is disabled.
- Every form carries a per-session CSRF token. Every POST must come from the
  public origin.
- Logins are throttled per address and per name, with backoff after 5 failures.
- Response headers: a strict CSP (`default-src 'none'`; no scripts at all),
  `frame-ancestors 'none'`, nosniff, a referrer policy, and `no-store` on pages.
- TLS and HSTS come from Caddy (`deploy/Caddyfile`).

## Native connectors

### mail

- **Tools:** `list_accounts`, `list_mailboxes`, `list_emails`, `search_emails`,
  `get_email`, `get_thread`, `mark_email`, `move_email`, `save_draft`. There is
  no code that sends, forwards or permanently deletes, and a test asserts the
  tool set.
- **Capabilities** (default all): `read`, `mark_read`, `draft`, `move`.
  `drafts: reply-only` limits drafts to replies to people already in the thread.
- **move_email:** to any folder or label, or `archive`. Trash and Spam are
  allowed and flagged in the log.
  - IMAP uses MOVE. Servers without it (iCloud) get COPY, then `\Deleted` and
    `UID EXPUNGE` of that one UID (UIDPLUS required).
  - Gmail moves by labels; Trash goes through `messages.trash`.
- **IMAP:** one kept connection per account, checked with NOOP before reuse and
  replaced by one fresh login when the provider has reset it; logged out after
  5 idle minutes. Ids are `<mailbox>|<uidvalidity>|<uid>`.
- **Gmail:** the REST API with scope `gmail.modify`, which excludes permanent
  delete and settings.

### calendar

- **Tools:** `list_accounts`, `list_calendars`, `list_events`, `get_event`,
  `search_events`, `create_event`, `update_event`, `delete_event`.
  - Reading can span all accounts at once.
  - Times are `YYYY-MM-DD` (all day) or `YYYY-MM-DDTHH:MM` in the account's
    time zone.
- **Capabilities:** `read`, `write`, `delete` (delete is off by default).
- **Never sends:** guests cannot be added. Events that have guests are not
  changed or deleted, because providers email guests on changes. Recurring
  events are read-only. Google calls pass `sendUpdates=none`.
- **CalDAV:** iCloud with an app-specific password, or any server. **Google:**
  the Calendar API with scopes `calendar.events` + `calendar.calendarlist.readonly`.

Every change made through a tool (moved mail, saved draft, created event, ...)
is an audit entry with what a person needs to find it again.

## OAuth

### Google (Gmail, Google Calendar)

One-time setup per household:

1. In console.cloud.google.com, create a project and enable the **Gmail API**
   and the **Google Calendar API**.
2. On the **OAuth consent screen**, add the scopes from the catalog entries
   (`gmail.modify`, `calendar.events`, `calendar.calendarlist.readonly`), then
   choose **Publish app → In production**. In "Testing", refresh tokens
   expire after 7 days.
3. Under **Credentials**, create an OAuth client of type **Desktop app**.
4. Enter its id and secret in the gateway under **Settings**.

Google allows redirects only to public domains or loopback, and
`tools.pi.home` is neither. The UI therefore uses a paste-back step:

1. "Connect with Google" opens the consent page.
2. Google then sends the browser to `http://localhost/?code=…`, which does not
   load. That is expected.
3. You paste that address into the UI.

The gateway saves the token only if Google granted exactly the catalog's
scopes, returned a refresh token, and the signed-in account is the configured
address.

### Remote MCP servers (Todoist)

"Connect" runs the MCP authorization flow through the MCP SDK: discovery,
dynamic client registration, PKCE. The server sends the browser back to
`<GATEWAY_PUBLIC_URL>/oauth/mcp/callback`. Tokens are stored encrypted per
connection and refreshed on their own. A server that wants a new sign-in fails
calls with a hint to connect again.

## Audit log

Each entry is one of:

- a tool call: who, integration, tool, argument **names** (never values),
  duration, ok
- a change made through a tool
- an account action: login, token, connection or secret changes, admin actions

Entries are buffered, written in batches, and pruned to the retention limits
daily, so the SD card is spared. They are also printed to stdout as JSON
lines; bound Docker's log size (see `deploy/compose.yaml`).

## Deployment (Raspberry Pi behind Caddy)

`deploy/compose.yaml` and `deploy/Caddyfile` are a complete example:

```bash
cd deploy
docker run --rm ghcr.io/lugoll/tools-gateway:0.3.0 keygen    # -> GATEWAY_MASTER_KEY in .env
docker compose up -d
```

Then open `https://tools.pi.home`, log in as the admin, create the users, and
let each of them add connections and a token.

## CLI

```bash
gateway serve                       # the container's default command
gateway check                       # settings, master key, database, catalog, users
gateway keygen                      # a new master key
gateway rotate-key                  # see "Secrets"
gateway reset-password lukas        # prompts; for a locked-out admin
gateway tools lukas [mail]          # a user's tools, in-process
gateway call lukas mail/list_emails --args '{"account":"icloud","seen":false}'
```

In the container: `docker compose exec tools-gateway gateway check`.

## Layout

```
gateway/
  settings.py   app.py   cli.py      environment, wiring, command line
  catalog/                           loads and validates catalog/*.yaml
  storage/                           SQLite + migrations, encryption, secrets, connections, audit log
  auth/                              users + passwords, API tokens, sessions, login throttling
  mcp/                               per-user runtimes, routing, meta tools, tool filters, audit middleware
  connectors/                        native integrations: mail/, calendar/
  outbound/                          leaving the box: remote MCP proxy, oauth/ (Google, MCP)
  web/                               UI routes, templates, static CSS, HTTP hardening
catalog/                             what users can connect (format: catalog/README.md)
deploy/                              example compose file and Caddyfile
tests/
```

Adding things:

- **A remote MCP server:** a catalog entry with `kind: remote`. No code.
- **A provider for an existing integration** (another IMAP host): a catalog
  entry with `kind: native`.
- **A new native integration:** a package in `gateway/connectors/<name>/`
  exposing `INTEGRATION`, `BACKENDS`, `CAPABILITIES`, `CHECK` and
  `build(accounts, ctx) -> FastMCP`, registered in `gateway/connectors/__init__.py`,
  with tests, plus catalog entries.

## Development

```bash
uv sync && uv run pytest              # CalDAV tests run against an in-process Radicale; IMAP tests skip
docker run -d --rm --name gateway-greenmail \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
  -p 127.0.0.1:3025:3025 -p 127.0.0.1:3143:3143 greenmail/standalone:2.1.14
GATEWAY_TEST_IMAP=127.0.0.1:3143 GATEWAY_TEST_SMTP=127.0.0.1:3025 uv run pytest
docker build -t tools-gateway:dev .
```

To run it locally:

```bash
GATEWAY_MASTER_KEY=$(uv run gateway keygen) GATEWAY_ADMIN_USER=admin GATEWAY_ADMIN_PASSWORD=change-me-now \
  uv run gateway serve
```

Then open http://localhost:3050.

Releases: push a tag `vX.Y.Z`. CI runs the tests, builds `linux/amd64` and
`linux/arm64`, and pushes `ghcr.io/lugoll/tools-gateway:X.Y.Z`.
