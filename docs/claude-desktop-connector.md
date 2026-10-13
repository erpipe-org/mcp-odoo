# Claude Desktop / claude.ai Custom Connector

## Fastest path: the .mcpb bundle (one-click, local)

Every release ships an `odoo-mcp-<version>.mcpb` asset on the
[GitHub releases page](https://github.com/erpipe-org/mcp-odoo/releases).
Double-click it (or drag it into Claude Desktop → Settings → Extensions),
fill in your Odoo URL, database, username, and API key in the dialog, and
you're done — no JSON editing, no terminal. The bundle launches the server
via [uv](https://docs.astral.sh/uv/), so `uv` must be installed
(`brew install uv` on macOS). Credentials marked sensitive are stored by the
OS keychain, not in a config file.

Prefer manual configuration, or connecting to a *remote* server? Read on.

---

This guide explains how to expose `odoo-mcp` as a remote MCP server and connect
it to Claude Desktop or claude.ai as a custom connector. It uses the Streamable
HTTP transport and the OAuth 2.1 resource-server mode that ship in `odoo-mcp`
v0.7.0+.

**When to use this guide:** You want Claude Desktop or claude.ai to reach an
Odoo instance over the network rather than launching a local process. This is
useful for shared team deployments, cloud-hosted Odoo, or when the Odoo server
is not on the same machine as Claude.

**For single-user local use:** the stdio approach in `docs/client-configs.md` is
simpler and does not require a reverse proxy or OAuth.

---

## Overview

```
Claude Desktop / claude.ai
        |  HTTPS (Streamable HTTP MCP)
        v
  Reverse proxy (nginx / Caddy)  ←— TLS termination
        |  HTTP
        v
  odoo-mcp  (--transport streamable-http)
        |  XML-RPC or JSON-2
        v
  Odoo server
```

OAuth flow (when enabled):

```
Claude → Authorization Server (Keycloak / Auth0 / Authentik)
       → bearer token
       → odoo-mcp validates token via RFC 7662 introspection
```

---

## 1. Start odoo-mcp in HTTP mode

### Environment variables

Set Odoo connection variables as usual:

| Variable | Description | Required |
|---|---|---|
| `ODOO_URL` | Odoo instance URL | Yes |
| `ODOO_DB` | Database name | Yes |
| `ODOO_USERNAME` | Login email | Yes |
| `ODOO_PASSWORD` | Password or API key | Yes (or `ODOO_API_KEY`) |
| `ODOO_TRANSPORT` | `xmlrpc` (default) or `json2` | No |
| `ODOO_API_KEY` | API key for JSON-2 transport | No |

Transport and network variables:

| Variable | CLI equivalent | Default | Description |
|---|---|---|---|
| `MCP_TRANSPORT` | `--transport` | `stdio` | Set to `streamable-http` |
| `MCP_HTTP_HOST` | `--host` | `127.0.0.1` | Bind address |
| `MCP_HTTP_PORT` | `--port` | `8000` | Bind port |
| `MCP_HTTP_PATH` | `--path` | `/mcp` | Endpoint path |
| `MCP_ALLOW_REMOTE_HTTP` | `--allow-remote-http` | unset | Required when binding non-loopback |
| `MCP_ALLOWED_HOSTS` | `--allowed-hosts` | (any) | Comma-separated Host header allowlist |
| `MCP_ALLOWED_ORIGINS` | `--allowed-origins` | (any) | Comma-separated Origin allowlist |

### Start command (bind loopback, reverse proxy in front)

```bash
export ODOO_URL="https://mycompany.odoo.com"
export ODOO_DB="mycompany"
export ODOO_USERNAME="bot@mycompany.com"
export ODOO_PASSWORD="mypassword"

odoo-mcp \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000 \
  --path /mcp
```

The server listens on `http://127.0.0.1:8000/mcp`. The reverse proxy handles
TLS and forwards to this address.

### Docker variant

```bash
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -e ODOO_URL \
  -e ODOO_DB \
  -e ODOO_USERNAME \
  -e ODOO_PASSWORD \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_HTTP_HOST=0.0.0.0 \
  -e MCP_HTTP_PORT=8000 \
  -e MCP_ALLOW_REMOTE_HTTP=1 \
  ghcr.io/erpipe-org/mcp-odoo:latest \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8000 \
  --allow-remote-http
```

Note: inside the container, bind `0.0.0.0`; the `-p 127.0.0.1:8000:8000`
flag on the host keeps the port local.

---

## 2. Reverse proxy + TLS

Claude Desktop and claude.ai require HTTPS for remote connectors. Use Caddy
(auto-HTTPS) or nginx with Let's Encrypt.

### Caddy (recommended)

```caddy
mcp.example.com {
  reverse_proxy 127.0.0.1:8000
}
```

Run: `caddy run --config /etc/caddy/Caddyfile`

Caddy obtains and renews TLS certificates automatically via Let's Encrypt.

### nginx + Certbot

```nginx
server {
    listen 443 ssl;
    server_name mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.example.com/privkey.pem;

    location /mcp {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        # Required for SSE and streaming
        proxy_set_header   Connection "";
        proxy_buffering    off;
        proxy_read_timeout 3600s;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Set `MCP_ALLOWED_HOSTS=mcp.example.com` when starting odoo-mcp to lock down
the Host header allowlist.

---

## 3. DNS-rebinding protection

`odoo-mcp` validates the `Host` header on HTTP transports by default
(via MCPServer's transport security middleware). When a reverse
proxy is in front, the `Host` header forwarded to the server must match an
allowed value.

Add the public hostname to the allowlist:

```bash
odoo-mcp \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000 \
  --allowed-hosts "mcp.example.com,127.0.0.1,localhost"
```

Or via environment variable:

```bash
MCP_ALLOWED_HOSTS="mcp.example.com,127.0.0.1,localhost"
```

If you see `403 Forbidden` or `Invalid host header` errors, the forwarded
`Host` value is not in the allowlist.

---

## 4. OAuth 2.1 resource server (optional but recommended)

When the connector is exposed on the internet, enable the built-in OAuth
resource-server mode so only clients with valid bearer tokens can use it. The
server implements RFC 7662 token introspection and RFC 9728 protected-resource
metadata. Compatible authorization servers include Keycloak, Auth0, Authentik,
and any AS with a standard introspection endpoint.

With OAuth enabled, write approvals are bound to the caller: an approval
returned by `validate_write` (or a `chatter_post` preview) can only be executed
by the same authenticated principal — token `client_id`, `iss`, and `sub` —
in the same MCP session. Other clients of the same server process get a
`different authenticated principal` error, even when they send an identical
payload. The `ODOO_MCP_AUDIT_LOG` lines name that principal; the MCP session
id is stored as a digest.

### Required environment variables

| Variable | Description |
|---|---|
| `ODOO_MCP_AUTH_ISSUER_URL` | Authorization server issuer URL (e.g. `https://auth.example.com/realms/myrealm`) |
| `ODOO_MCP_AUTH_INTROSPECTION_URL` | RFC 7662 introspection endpoint (e.g. `https://auth.example.com/realms/myrealm/protocol/openid-connect/token/introspect`) |
| `ODOO_MCP_AUTH_RESOURCE_URL` | Canonical public URL of this MCP server (e.g. `https://mcp.example.com/mcp`) |

### Optional environment variables

| Variable | Description |
|---|---|
| `ODOO_MCP_AUTH_CLIENT_ID` | Client ID for the introspection call (required by most AS) |
| `ODOO_MCP_AUTH_CLIENT_SECRET` | Client secret for the introspection call |
| `ODOO_MCP_AUTH_REQUIRED_SCOPES` | Comma-separated list of scopes that must be present in the token (e.g. `odoo:read,odoo:write`) |

### Example startup with OAuth

```bash
export ODOO_URL="https://mycompany.odoo.com"
export ODOO_DB="mycompany"
export ODOO_USERNAME="bot@mycompany.com"
export ODOO_PASSWORD="mypassword"

export ODOO_MCP_AUTH_ISSUER_URL="https://auth.example.com/realms/myrealm"
export ODOO_MCP_AUTH_INTROSPECTION_URL="https://auth.example.com/realms/myrealm/protocol/openid-connect/token/introspect"
export ODOO_MCP_AUTH_RESOURCE_URL="https://mcp.example.com/mcp"
export ODOO_MCP_AUTH_CLIENT_ID="odoo-mcp-resource"
export ODOO_MCP_AUTH_CLIENT_SECRET="my-client-secret"
export ODOO_MCP_AUTH_REQUIRED_SCOPES="odoo:read"

odoo-mcp \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8000 \
  --path /mcp \
  --allowed-hosts "mcp.example.com,127.0.0.1"
```

Startup output confirms OAuth is active:
```
OAuth resource server enabled (issuer: https://auth.example.com/realms/myrealm)
```

All three `ODOO_MCP_AUTH_*` variables must be set together — if only some are
present, the server raises an error at startup rather than silently falling back
to unauthenticated mode.

### Protected-resource metadata

The server exposes RFC 9728 metadata at:
```
GET https://mcp.example.com/.well-known/oauth-protected-resource
```

This tells compliant clients (including claude.ai) where to obtain tokens.

---

## 5. Add the connector in Claude settings

### claude.ai (browser)

1. Go to **Settings → Integrations → Add custom integration**.
2. Set the **Server URL** to `https://mcp.example.com/mcp`.
3. If OAuth is enabled, claude.ai will redirect you to the authorization server
   for consent. On success, it stores the token and uses it for all requests.
4. Click **Save** and test with a prompt like:
   > "List the first 5 Odoo partners using the odoo tool."

### Claude Desktop (macOS / Windows)

Claude Desktop supports remote MCP servers in recent versions. Add to
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "odoo-remote": {
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

If the server uses OAuth, Claude Desktop will open a browser window for the
authorization flow on first connect.

Some older Claude Desktop versions use `transport` instead of `type`:

```json
{
  "mcpServers": {
    "odoo-remote": {
      "transport": "streamable-http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

---

## 6. Verify the server health

Before adding the connector, confirm the server is reachable:

```bash
# Streamable HTTP health check (prints non-secret runtime JSON)
curl -s https://mcp.example.com/mcp/../health  # not a built-in endpoint

# Use the built-in health flag instead (run locally before proxy):
odoo-mcp --transport streamable-http --health

# Inspect tools via MCP Inspector
npx --yes @modelcontextprotocol/inspector@2 --cli \
  --method tools/list \
  --transport http \
  --server-url https://mcp.example.com/mcp
```

---

## 7. Troubleshooting

### `403 Forbidden` or `Invalid host header`

The `Host` header forwarded by the proxy is not in the `MCP_ALLOWED_HOSTS`
list. Add the public hostname:
```bash
MCP_ALLOWED_HOSTS="mcp.example.com,127.0.0.1"
```

### `Connection reset` or streaming drops

Nginx requires `proxy_buffering off` and a long `proxy_read_timeout` for
streaming SSE/Streamable HTTP connections. See the nginx config in section 2.

### `OAuth resource server enabled` not shown in logs

All three mandatory variables must be set:
- `ODOO_MCP_AUTH_ISSUER_URL`
- `ODOO_MCP_AUTH_INTROSPECTION_URL`
- `ODOO_MCP_AUTH_RESOURCE_URL`

If only some are set, the server throws at startup. If none are set, OAuth is
silently disabled (unauthenticated mode).

### `token audience does not match resource`

Set `ODOO_MCP_AUTH_RESOURCE_URL` to exactly the canonical URL the authorization
server puts in the `aud` claim of issued tokens. These must match character-for-
character.

### Claude Desktop shows the server but tools are empty

1. Verify the endpoint returns a valid MCP `initialize` response:
   ```bash
   curl -s -X POST https://mcp.example.com/mcp \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
   ```
2. Check that `MCP_HTTP_PATH` matches the path in the URL
   (default `/mcp`; the above URL uses `/mcp`).

### `MCP_ALLOW_REMOTE_HTTP` required error

If you bind a non-loopback address (e.g. `0.0.0.0`) without setting this flag,
the server refuses to start. Set:
```bash
MCP_ALLOW_REMOTE_HTTP=1
# or
odoo-mcp --allow-remote-http ...
```

This flag is intentionally explicit: it exists to prevent accidental exposure.
Always pair it with a reverse proxy that enforces TLS and access controls.

## Multi-client note

Background task results (`list_async_tasks`/`get_async_task`) and local
knowledge indexes are process-global, not per-session. When several clients
share one HTTP server process they can see each other's task results and
indexed snippets. All clients already share the same Odoo credential, so
this widens convenience rather than privilege — but run separate server
processes if you need isolation between operators.
