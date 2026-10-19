# Connector recipes

A connector is one integration a household member can use: Todoist, an iCloud
calendar, a set of mailboxes. Each lives in its own directory here as one
`recipe.toml`. The member's gateway (scotty-gateway) reads this directory
(mounted read-only) together with the member's connector list
`config/gateway/<member>.toml` at start; nothing is generated. The gateway
knows no connector by name, so adding an integration is a new directory plus a
line in a member file.

```
connectors/<id>/recipe.toml
```

A member uses a recipe with an entry in `config/gateway/<member>.toml`:

```toml
member = "lukas"

[[connectors]]
recipe = "mail"
accounts = ["icloud", "gmail"]    # only for recipes that define [accounts]

[[connectors]]
recipe = "caldav-google"
disabled = true                   # optional: listed as off, never started
note = "why, and what re-enables it"   # required when disabled
```

Optional in the member file: `[auth] token` (default `"${GATEWAY_TOKEN}"`),
`[defaults] timeout`, `log_level`.

## recipe.toml

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | server name, suffixed per member: `todoist` → `todoist-lukas` (the `<server>` of REST calls, the gate scripts and the mail module's data directory) |
| `domain` | yes | the gateway endpoint `/mcp/<domain>` and Hermes tool group. Several recipes may share a domain (both calendars are `calendar`); give one of them a `namespace` when their tool names clash. Every recipe's domain is served by every gateway (empty when the member has no such connector), so `config/hermes/config.yaml` can list the household's domains once; a new domain needs an entry there |
| `[server]` | yes | the server entry (format: `services/scotty-gateway/README.md`): `type = "stdio"` (`command`, `args`, `env`, `cwd`), `"http"` (`url`, `headers`) or `"native"` (`module`, `config`), plus optional `allow` / `deny` (tool-name globs; deny wins), `namespace`, `timeout`, `mode`. Pin the version of an `npx`/`uvx` server in `args`. Write restrictions live here (`deny`) or in a native module, never in Hermes |
| `[accounts.<id>]` | no | multi-account connector (native modules): the member picks ids with `accounts = [...]`, and the chosen tables become `config.accounts` of the server, keyed by id |

`${VAR}` in any string is read from the gateway container's environment. Use
plain names (`${TODOIST_API_KEY}`); `docker-compose.yml` maps them per member
from `.env` (`TODOIST_API_KEY: ${TODOIST_API_KEY_LUKAS}`), so a gateway only
ever holds its own member's values. A server whose variables are missing is
not started and is reported with the missing names (`check-config`,
`/health/ready`). Name the variables a recipe needs in its header comment.

## Adding an integration

1. Copy the closest existing recipe (`todoist` for a stdio server with one
   token, `mail` for a native module with accounts).
2. Set `name`, `domain` and the `[server]` entry. An existing MCP server is
   config only: `type = "stdio"` with a pinned `npx`/`uvx` package, or
   `type = "http"`. Hide write-capable tools with `deny`: the stack is
   read/draft only. Only when no usable server exists, write a native module
   (`services/scotty-gateway/README.md`, "Adding things").
3. Tell the agent how to use it: a bullet in `skills/personal-tools/SKILL.md`
   (the tool names and when to reach for them). A new domain also needs its
   `mcp_servers` entry in `config/hermes/config.yaml`.
4. Add the recipe to a member's `config/gateway/<member>.toml`, map its
   variables in that member's gateway service in `docker-compose.yml`, add the
   values to `.env`, then `docker compose up -d` (new env) or
   `docker compose restart mcp-gateway-<member>` (only the member file changed).
5. Check the gateway: `docker exec mcp-gateway-<member> scotty-gateway check-config`
   (what runs, and why not) and
   `curl -s localhost:<port>/health/ready -H "Authorization: Bearer $GATEWAY_TOKEN_<MEMBER>" | jq`
   (every server with its real tool count or error).

Existing recipes: `todoist`, `caldav-icloud`, `caldav-google` (app password,
which Google answers with 401; to be replaced by a Google-calendar module on
the mail module's OAuth client), `mail` (accounts `icloud`, `tonline` over
IMAP and `gmail` over the Gmail API; add a provider as another
`[accounts.<id>]` table).
