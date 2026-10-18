# Connector recipes

A connector is one integration a household member can use: Todoist, an iCloud
calendar, a set of IMAP mailboxes. Each lives in its own directory here and is
completely self-contained: `scripts/render.py` knows no connector by name. It
only reads the sections a recipe declares, so adding an integration is a new
directory plus a line in `members.toml`; no Python changes.

```
connectors/<id>/
  recipe.toml            the recipe (required)
  usage.md               how the agent uses it; becomes one bullet of the
                         profile's personal-tools skill (optional)
  service.yml.tmpl       a sidecar container, when the server is not a stdio
                         process inside the gateway (optional, see [service])
  files/…                templates rendered per member (optional, see [[files]])
  skills/<name>/…        profile skills that only exist for members with this
                         connector (optional; same rules as templates/profile/skills)
```

A member uses a recipe with

```toml
[[members.lukas.connectors]]
recipe = "mail"
accounts = ["icloud", "gmail"]    # only for recipes that define [accounts]
disabled = true                   # optional: keep the server in gateway.json, marked disabled
note = "why, and what re-enables it"   # required when disabled; shown to the agent
```

## recipe.toml

Every string in the recipe is a template (see `templates/README.md`): the
member's variables (`{{user}}`, `{{USER}}`, `{{name}}`, `{{his}}`, ...) and
`{{connector.*}}` (the member's instance of this connector, see below) are
available. A value that is exactly one placeholder keeps the variable's type,
so `accounts = "{{connector.account_names}}"` yields a list.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | server name in the gateway's `gateway.json`, suffixed per member: `todoist` → `todoist-lukas` (also the `<server>` of REST calls) |
| `domain` | yes | the gateway endpoint `/mcp/<domain>` and Hermes tool group; the profile's `config.yaml` gets one `mcp_servers` entry per distinct domain. Several recipes may share a domain (both calendars are `calendar`); give one of them a `namespace` when their tool names clash |
| `doc` | yes | sentence fragment naming what it gives the member, e.g. `"{{his}} Todoist tasks and projects"`; used in SOUL.md and the personal-tools description |
| `[[env]]` | no | `stem` + `doc`: a variable the member must set in `.env` as `<STEM>_<USER>`; with `global = true` one household-wide `<STEM>` instead (an OAuth client). Passed to the member's gateway container (and to a sidecar, if any). `render.py --check-env` prints missing ones with `doc` |
| `[gateway]` | no | the server entry of `gateway.json` (format: `services/scotty-gateway/README.md`), rendered and copied as is: `type = "stdio"` (`command`, `args`, `env`, `cwd`), `"http"` (`url`, `headers`) or `"native"` (`module`, `config`), plus optional `allow` / `deny` (tool-name globs; deny wins), `namespace`, `timeout`, `mode`. `render.py` adds `domain` and, for a disabled connector, `disabled` + `note` from the manifest. Pin the version of an `npx`/`uvx` server in `args`. Write restrictions live here (`deny`) or in a native module, never in Hermes |
| `[accounts.<id>]` | no | multi-account connector. `label` (human name) and `env` (list of `{stem, doc, global?}`) are read by the renderer; any other key is yours and reachable as `{{account.<key>}}` in this recipe's templates. `{{connector.account_map}}` is all chosen accounts as `{name: table without env}`, which is what the mail module's `config.accounts` takes. Account `env` goes to the sidecar when there is a `[service]`, otherwise to the gateway |
| `[service]` | no | sidecar container: `template` (a compose service snippet in this directory) plus free keys such as `name` and `image`, reachable as `{{service.<key>}}`. The gateway gets `depends_on: <name>: condition: service_healthy`, so the snippet needs a healthcheck |
| `[[files]]` | no | `template` (path in this directory) and `out` (path relative to the repo root, may contain placeholders), rendered per member |
| `[gate_config]` | no | `defaults`: merged into the member's `scripts/gate_config.json` when it is first created (dicts merge, lists concatenate). `render_owned`: dotted keys rewritten on every render (e.g. `horizon.calendar_servers`); everything else belongs to the setup skills afterwards |

`{{connector.*}}` inside a recipe: `name` (`todoist-lukas`), `recipe`, `domain`,
`disabled`, `note`, `doc`, `usage`, `accounts` (list of the chosen account
tables, each with `name`), `account_names`, `account_names_csv`,
`account_names_quoted`, `account_list` ("iCloud, Gmail and t-online"), `multi`,
`account_map`, `env`, `gateway_env`, `service_env`, `service`.

## Adding an integration

1. Copy the closest existing recipe (`todoist` for a stdio server with one
   token, `mail` for a native module with accounts).
2. Set `name`, `domain`, `doc`, the `[[env]]` stems and the `[gateway]` entry.
   An existing MCP server is config only: `type = "stdio"` with a pinned
   `npx`/`uvx` package, or `type = "http"`. Hide write-capable tools with
   `deny`: the stack is read/draft only. Only when no usable server exists,
   write a native module (`services/scotty-gateway/README.md`, "Adding things").
3. Write `usage.md`: one bullet, the tool names and when to reach for them.
4. Add the recipe to a member in `members.toml`, then
   `python3 scripts/render.py --check-env` (lists the `.env` lines to add),
   `python3 scripts/render.py`, and `docker compose up -d` (a new env var) or
   `docker compose restart mcp-gateway-<user>` (only `gateway.json` changed).
5. Check the gateway: `curl -s localhost:<gateway_port>/health/ready -H "Authorization: Bearer $GATEWAY_TOKEN_<USER>" | jq`
   (every server with its real tool count or error).

Existing recipes: `todoist`, `caldav-icloud`, `caldav-google` (app password,
which Google answers with 401; to be replaced by a Google-calendar module on
the mail module's OAuth client), `mail` (accounts `icloud`, `tonline` over
IMAP and `gmail` over the Gmail API; add a provider as another
`[accounts.<id>]` table).
