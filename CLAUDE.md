# PersonalToolsGateway (tools-gateway)

A standalone, multi-user MCP gateway (extracted from `../Scotty2`, history
kept). An admin creates users; each user connects accounts in a small web UI
and gets `/mcp/<user>` (meta tools) and `/mcp/<user>/<integration>` (plain
tools) behind their own bearer tokens. Two kinds of connection: native
connectors (`mail`: IMAP + Gmail API, `calendar`: CalDAV + Google Calendar API;
all accounts of one kind are one tool set) and remote MCP servers proxied over
streamable HTTP. Python only, no child processes. Full reference: `README.md`.

## Layout

- `gateway/`: `settings.py`, `app.py`, `cli.py`, and the packages `catalog/`,
  `storage/` (SQLite, crypto, secrets, connections, audit), `auth/`, `mcp/`
  (per-user runtimes, routing, meta tools, filters), `connectors/<name>/`,
  `outbound/` (remote MCP proxy, OAuth), `web/` (routes, templates, static)
- `catalog/*.yaml`: what users can connect (format: `catalog/README.md`)
- `deploy/`: example compose file + Caddyfile for the Pi (`tools.pi.home`)
- `tests/`: `uv run pytest`; IMAP tests need GreenMail (README, "Development");
  CalDAV tests run against an in-process Radicale
- `.github/workflows/ci.yml`: tests; a tag `vX.Y.Z` publishes `ghcr.io/lugoll/tools-gateway`

## Rules

- The core stays generic: integration code lives in a native connector, with tests. Remote servers are catalog entries only.
- Least privilege: no tool that sends, forwards or permanently deletes. Remote servers: `forbid` (never) and `deny` (off by default) in the catalog. Native connectors: no such code, plus capabilities with destructive ones (calendar delete) off by default. Every change a tool makes is recorded through `ctx.record_change` (audit log).
- Secrets only in the encrypted store (`storage/secrets.py`, AES-GCM, bound to their row); the master key only from the environment. Never in files, logs, audit entries or rendered pages.
- Tenant isolation: a token opens only its owner's paths (`mcp/routing.py`); keep the tests for it.
- Web UI: plain forms, no JavaScript (CSP `default-src 'none'`); every POST goes through `web.common.form` (CSRF) and passes `web/security.py` (same-origin check).
- The Pi is the target: keep writes to the SD card bounded (batched audit, retention, throttled `last_used`/`last_seen` updates).
- Consumers: Scotty2 will be refactored onto this later; the wire contract (endpoints, tool names) is documented in the README.
