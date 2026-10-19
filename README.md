# scotty-gateway

The MCP gateway in front of each household member's integrations. One container
per member (`mcp-gateway-<member>`), holding only that member's credentials. It
replaces 1MCP and the email-mcp sidecar.

- **Proxied servers** (Todoist, CalDAV, any MCP server): configuration only, as
  stdio (`npx`, `uvx`, any command) or HTTP entries.
- **Native modules** (Python, in this package): integrations without a usable
  upstream server. The first is `mail`: IMAP accounts plus Gmail over the
  Gmail API with OAuth. It can read, mark, move and draft; nothing sends or
  permanently deletes, and every change to a mailbox is logged.
- **Per-domain endpoints** `/mcp/<domain>` (stateless streamable HTTP), a REST
  endpoint for scripts, health endpoints that do real checks, and a bearer
  token on everything except `/health/live`.

Built on [FastMCP](https://gofastmcp.com) (`fastmcp==4.0.10`). The config is
read at start from two read-only mounts: the member file
(`config/gateway/<member>.toml`, `SCOTTY_GATEWAY_CONFIG`) and the recipes
(`connectors/`, `SCOTTY_GATEWAY_CONNECTORS`). No hot reload: restart the
container after a change.

## Endpoints

| Path | Auth | What |
|---|---|---|
| `/mcp/<domain>` | Bearer | MCP endpoint of one domain (`todoist`, `calendar`, `email`, ...). Stateless: no session ids, so restarts never wedge a client |
| `/api/v1/tools[?domain=]` | Bearer | `{"tools": [{name, server, tool, domain, description}], "errors": {server: reason}}` |
| `/api/v1/tool-invocations` | Bearer | `POST {"tool": "<server>/<tool>", "args": {}}` → `{"result": {"content", "isError", "structuredContent"}}` or `{"error"}` (404 unknown, 409 not running, 502 backend failure, 504 timeout) |
| `/health/live` | none | the process is up (Docker healthcheck) |
| `/health/ready` | Bearer | per server: real tool listing (cached 30 s), tool counts, errors, name collisions; 503 if a running server fails |

## Config (member file + recipes)

The member file names recipes; each recipe's `[server]` table becomes one
server named `<recipe name>-<member>`, with the recipe's `domain`. Format of
both, and how accounts are picked: `connectors/README.md`.

```toml
# config/gateway/lukas.toml                  # connectors/todoist/recipe.toml
member = "lukas"                             name   = "todoist"
[[connectors]]                               domain = "todoist"
recipe = "todoist"                           [server]
[[connectors]]                               type    = "stdio"
recipe = "mail"                              command = "npx"
accounts = ["icloud", "gmail"]               args    = ["-y", "@doist/todoist-mcp@13.4.0"]
                                             [server.env]
                                             TODOIST_API_KEY = "${TODOIST_API_KEY}"
```

The auth token defaults to `${GATEWAY_TOKEN}` (`[auth] token` in the member
file overrides it). Every recipe's domain is served, an empty endpoint when the
member uses none of its connectors.

Every `[server]` table takes `type`, and optionally (`disabled` + `note` come from the member's entry):
- `allow` (glob list, null = all) and `deny` (globs; deny wins)
- `namespace` (prefixes the server's tool names on the domain endpoint; needed when two servers of one domain share tool names)
- `timeout` (seconds per call)

Type-specific keys:
- `stdio`: `command`, `args`, `env`, `cwd`, `mode`
- `http`: `url`, `headers`, `mode`
- `native`: `module`, `config`

Notes:
- `${VAR}` is substituted from the container environment. A server with a missing variable is not started and is reported with the reason. The gateway refuses to start without a token of at least 24 characters.
- A stdio child gets only its `env` plus a short baseline (`HOME`, `PATH`, ...), never the gateway's other variables. It is started once, kept alive and restarted on the next call after it exits.
- `mode` pins the backend protocol era: `legacy` (the default for stdio) or `auto` (the default for http).

## Mail module

The `config` table of a `module = "mail"` server:

```json
{"max_body_chars": 20000, "timeout": 20,
 "accounts": {
   "icloud": {"backend": "imap", "label": "iCloud", "email": "a@icloud.com",
              "host": "imap.mail.me.com", "port": 993, "username": "a@icloud.com",
              "password": "${ICLOUD_APP_PASSWORD}"},
   "gmail":  {"backend": "gmail", "label": "Gmail", "email": "a@gmail.com",
              "capabilities": ["read", "mark_read", "draft", "move"], "drafts": "any"}}}
```

- **Tools:** `list_accounts`, `list_mailboxes`, `list_emails`, `search_emails`, `get_email`, `get_thread`, `mark_email`, `move_email`, `save_draft`. Nothing else: there is no code that sends, forwards or permanently deletes, and a test asserts the tool set.
- **capabilities** (default all): `read`, `mark_read` (the `\Seen` flag or `UNREAD` label), `draft`, `move`.
- **move_email:** to any existing folder or label, or `archive`. Trash and Spam are allowed and flagged `destination_kind` in the log. IMAP uses MOVE; servers without it (iCloud) get COPY, then `\Deleted` and `UID EXPUNGE` of that one UID (UIDPLUS required, so other messages are never expunged). Gmail moves by labels; Trash goes through `messages.trash` (30 days recoverable).
- **Action log:** every move, mark and draft is one JSON line in `/data/<server>/actions.jsonl` (account, id, origin, destination, sender, subject; never bodies), rotated at 5 MB × 5, and also in `docker logs`. Trash and Spam moves are logged as warnings.
- **drafts:** `any` (default) or `reply-only`. Reply-only means drafts are only replies, and only to people already in the original mail.
- **IMAP:** one connection per call with 20 s socket timeouts. A provider reset fails one call, never the process. Ids are `<mailbox>|<uidvalidity>|<uid>`. Drafts go to the `\Drafts` special-use folder, or `drafts_folder`.
- **Gmail:** the REST API with scope `gmail.modify`: read, labels and drafts, but no permanent delete and no settings. The scope would allow sending; this code never does. The token lives in `/data/<server>/tokens/<account>.json` (0600).

### Authorising a Gmail account

One-time Google Cloud setup (per household):
1. In console.cloud.google.com, create a project and enable the **Gmail API**.
2. Set up the **OAuth consent screen**: user type External, add the scope `.../auth/gmail.modify`, then **Publish app → In production**. In "Testing", refresh tokens expire after 7 days. The app stays unverified; that is fine for personal use.
3. Under **Credentials**, create an OAuth client ID of type **Desktop app**. Put its id and secret into `.env` as `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.

Per account (on the Pi, run `ssh -L 8765:localhost:8765 <pi>` first from the machine with the browser):

```bash
docker compose run --rm -p 127.0.0.1:8765:8765 mcp-gateway-lukas auth email-lukas gmail
```

Open the printed URL and sign in as that Gmail address. Google warns that the app is unverified: choose "Advanced", then continue. The command saves the token only if Google granted exactly `gmail.modify` and the signed-in mailbox matches the configured address. No restart is needed: the gateway reads the token on the next call.

## CLI

```bash
scotty-gateway check-config                 # what would run, and why not
scotty-gateway tools [--domain email]       # in-process listing (starts the backends)
scotty-gateway call email-lukas/list_emails --args '{"account":"icloud","seen":false}'
scotty-gateway auth email-lukas gmail       # see above
```

Inside the running container: `docker exec mcp-gateway-lukas scotty-gateway tools`.

## Adding things

- **A proxied MCP server:** a recipe in `connectors/` with a `[gateway]` table of type `stdio` or `http`. No code.
- **A native integration:** a package under `src/scotty_gateway/modules/<name>/` exposing `build(config, ctx) -> FastMCP` (and optionally `commands`), registered in `modules/__init__.py`, plus a recipe with `type = "native"`. Secrets reach it through `config` (`${VAR}`).

## Development

```bash
uv sync && uv run pytest                     # unit tests; IMAP tests skip without a server
docker run -d --rm --name scotty-greenmail \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
  -p 127.0.0.1:3025:3025 -p 127.0.0.1:3143:3143 greenmail/standalone:2.1.14
SCOTTY_TEST_IMAP=127.0.0.1:3143 SCOTTY_TEST_SMTP=127.0.0.1:3025 uv run pytest
docker build -t scotty-gateway:dev .         # local test image
```

Releases: push a tag `gateway-vX.Y.Z`. CI runs the tests, builds `linux/amd64` + `linux/arm64` and pushes `ghcr.io/lugoll/scotty-gateway:X.Y.Z`. Then bump the tag in `docker-compose.yml` (`x-gateway`).
