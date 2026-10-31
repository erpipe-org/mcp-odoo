# Odoo MCP

<!-- mcp-name: io.github.erpipe-org/mcp-odoo -->

ERPipe is the managed Odoo MCP gateway from the maintainer of [`erpipe-org/mcp-odoo`](https://github.com/erpipe-org/mcp-odoo), formerly [`tuanle96/mcp-odoo`](https://github.com/tuanle96/mcp-odoo); the Python project remains the self-hosted server.

<p align="center">
  <strong>The free AI layer for Odoo — any edition, any version.</strong><br>
  Odoo's built-in AI is Enterprise-only. Odoo MCP gives Community and Enterprise 16+ the same power for $0 with the LLM you already use (Claude, GPT, Gemini, DeepSeek, Ollama).<br>
  Five-minute install. Zero Odoo-side setup. Safe writes, real diagnostics, JSON-2 ready years before the Odoo 22 XML-RPC removal.
</p>

<p align="center">
  <a href="https://pypi.org/project/odoo-mcp/"><img alt="PyPI" src="https://img.shields.io/pypi/v/odoo-mcp.svg"></a>
  <a href="https://pypi.org/project/odoo-mcp/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/odoo-mcp.svg"></a>
  <a href="https://pypi.org/project/odoo-mcp/"><img alt="Downloads" src="https://img.shields.io/pypi/dm/odoo-mcp.svg"></a>
  <a href="./LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-black.svg"></a>
  <a href="https://github.com/erpipe-org/mcp-odoo/actions/workflows/publish.yml"><img alt="CI" src="https://github.com/erpipe-org/mcp-odoo/actions/workflows/publish.yml/badge.svg"></a>
  <a href="https://github.com/erpipe-org/mcp-odoo/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/erpipe-org/mcp-odoo?style=flat"></a>
  <a href="https://github.com/erpipe-org/mcp-odoo/network/members"><img alt="Forks" src="https://img.shields.io/github/forks/erpipe-org/mcp-odoo?style=flat"></a>
  <a href="https://skills.sh/erpipe-org/mcp-odoo"><img alt="Agent Skills" src="https://skills.sh/b/erpipe-org/mcp-odoo"></a>
</p>

<p align="center">
  <a href="https://erpipe.com/"><strong>🚀 ERPipe hosted</strong> — free v1 · live at erpipe.com</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/erpipe-org/erpipe"><strong>TypeScript open core</strong></a>
  &nbsp;·&nbsp;
  <a href="https://erpipe-org.github.io/mcp-odoo/services.html"><strong>🔍 Fixed-price audits</strong></a>
</p>

> **Want ChatGPT / Claude on a stable remote URL without running a process?**  
> **[ERPipe](https://erpipe.com/)** is the hosted product from the same author — free v1 public beta, live in production.  
> Sign up → add HTTPS Odoo instance(s) → connect once to `https://mcp.erpipe.com/mcp` (workspace OAuth, multi-instance, gated writes, audit dashboard).  
> This repo stays the **local / self-host Python server** (full 41-tool surface, stdio, Docker). TypeScript building blocks: [`erpipe`](https://github.com/erpipe-org/erpipe).

| | **This repo (`odoo-mcp`)** | **[ERPipe hosted](https://erpipe.com/)** |
|--|--|--|
| Run where | Your laptop / Docker / CI | Cloudflare (managed) |
| Install | `uvx odoo-mcp --setup` | Sign up at [erpipe.com](https://erpipe.com/) |
| MCP URL | stdio or local HTTP | `https://mcp.erpipe.com/mcp` |
| Clients | Claude Code, Cursor, local agents | **ChatGPT** (primary), Claude, Cursor, any remote MCP client |
| Tool surface | **41 tools** + 11 prompts (full local pack) | **43 tools** + 7 prompts (workspace multi-instance + governance; [catalog](https://erpipe.com/tools)) |
| Multi-instance | Config file / env on your machine | Dashboard + explicit `instance` key per tool |
| Writes | Env gate + approval tokens (+ optional MCP elicitation) | Default OFF · HITL inbox · journal · field policy |
| Audit | Optional JSONL file | Dashboard + D1 audit trail |
| Cost | Free forever (MIT) | Free v1 beta (fair-use caps) |

Odoo MCP turns any Odoo 16+ database into a Model Context Protocol server — using only your existing credentials. **No App Store module, no permission setup, no admin access required.** Built for local agents, IDEs, and automation tools that need real Odoo context without hand-rolled scripts or unsafe direct write access.

It speaks XML-RPC for Odoo 16-18 and External JSON-2 for Odoo 19+. It exposes a compact MCP surface with read tools, diagnostics, schema discovery, migration helpers, local addon scanning, and a gated write workflow. One server can serve [multiple named Odoo instances](#multiple-odoo-instances) at once.

## Try it in 30 seconds

Once configured (see [Setup](#setup)), ask your agent things like:

> "Show me all customers from Spain with unpaid invoices."
>
> "Find products with stock below 10 units in the main warehouse."
>
> "Audit the `custom_billing` addon for upgrade risks before we move to Odoo 19."

## Highlights

| Capability | What it gives you |
| --- | --- |
| 41 MCP tools | Read records and attachments, aggregate server-side, post chatter, inspect schema, build domains, scan addons, diagnose calls and upgrade logs, check data quality, access rules, resolve model renames, validate writes, and fan out across instances. |
| Field-level ACL | Opt-in per-instance, per-model field allow/deny enforced on every read path (records, aggregates, knowledge index, resources). First open-source Odoo MCP with it. See [docs/field-acl.md](docs/field-acl.md). |
| Cross-instance queries | Read-only fan-out across many client DBs with merged, attributed, partial-failure-tolerant results — no warehouse, no sync. See [docs/partner-playbook.md](docs/partner-playbook.md). |
| Workflow prompts | 11 prompts including 6 end-to-end business workflows (invoice approval, PO match, onboarding, expense review, month-end close, pre-migration data quality) that route writes through the gate. |
| Background tasks | `submit_async_task` runs long read operations (addon scans, knowledge indexing, AR/AP aging) on a bounded worker pool; poll with `get_async_task` while the agent keeps reasoning. |
| Local-first knowledge search | `index_knowledge` + `search_knowledge` give BM25 relevance ranking over a bounded record slice — accent-insensitive, in-process, no embeddings service, no data leaving the machine. |
| Accounting pack | `receivable_payable_aging` and `accounting_health_summary` answer the most common finance questions in one call instead of hand-built domains. |
| Agent Skills pack | 4 business-workflow skills (data-quality gate, migration copilot, month-end close, agency fleet review) — `npx skills add erpipe-org/mcp-odoo`. Developing on Odoo with shell access? Add the 21-skill companion dev suite [odoo-ai-skills](https://github.com/tuanle96/odoo-ai-skills). See [skills/](./skills/). |
| Tool plugins | Ship your own tools as pip packages (`odoo_mcp.tools` entry points) — opt-in via `ODOO_MCP_PLUGINS`, fail-isolated, no fork needed. Trim the surface per deployment with `ODOO_MCP_TOOLS_INCLUDE/EXCLUDE`. See [docs/plugins.md](docs/plugins.md). |
| Rate limiting | Opt-in sliding-window budget per instance and tool (`ODOO_MCP_RATE_LIMIT_MODE=warn\|block`), surfaced in `health_check`. |
| Multi-instance | One server, several named Odoo instances — optional `instance` parameter on every tool, `list_instances` discovery, instance-bound approval tokens, per-instance schema caches. |
| 5 agent prompts | Reusable workflows for failed calls, fit/gap workshops, JSON-2 migration, safe writes, and module audits. |
| Odoo 16-19 coverage | XML-RPC by default, JSON-2 opt-in for Odoo 19. |
| MCP 2026-07-28 | Stateless modern protocol with `server/discover`, plus automatic compatibility with legacy `2025-11-25` clients on the same endpoint. |
| Streamable HTTP | Local HTTP/SSE support for clients that do not use stdio. |
| Smart field selection | `search_records` and `read_record` curate business-relevant fields when no `fields` argument is supplied — drops audit, message, binary, and unstored compute noise. Pass `fields=["*"]` to opt out. |
| Server-side aggregation | `aggregate_records` pushes groupby/sum/count/avg into Postgres via `formatted_read_group` (Odoo 19+) or `read_group` (16-18). |
| Chatter integration | `chatter_post` adds messages to any `mail.thread` record under the same approval-token gate as writes — or directly via `MCP_CHATTER_DIRECT=1`. |
| Locale plumbing | `ODOO_LOCALE` injects `context.lang` automatically on every Odoo call (caller can override). |
| Structured logging | JSON formatter and rotating file handler via `ODOO_MCP_LOG_LEVEL`, `ODOO_MCP_LOG_JSON`, `ODOO_MCP_LOG_FILE`. |
| Safe writes | Direct `create`, `write`, and `unlink` are blocked; approved writes require live metadata, a same-session token, explicit confirmation, and an env gate. |
| Human-in-the-loop approval | `ODOO_MCP_ELICIT_WRITES=1` shows a native MCP confirmation form (with a diff summary) before any approved write executes — token flow stays as fallback. |
| Audit trail | `ODOO_MCP_AUDIT_LOG` appends one JSONL line per write-path event (preview, validate, execute, chatter) with instance and token digest. |
| Resilience | Read-only calls retry connection errors with exponential backoff; schema caches are TTL- and LRU-bounded; `health_check` flags N+1 read loops. |
| Real smoke tests | Docker Compose validation boots disposable Odoo 16.0, 17.0, 18.0, and 19.0 stacks, including restricted users, custom record rules, and packaged addon XML install/update. |

## Why Odoo MCP

| Trait | Odoo MCP | Other MCP-Odoo bridges |
| --- | --- | --- |
| Setup steps on Odoo side | **0** — works with any Odoo 16+ instance using credentials you already have. | Often require installing an App Store module, configuring enabled models, and granting per-tool permissions. |
| Safe write workflow | Approval token + live `fields_get` validation + explicit confirm + env gate. | Often expose direct `create`/`write`/`unlink` or a "yolo" bypass. |
| Diagnostics | `diagnose_odoo_call`, `diagnose_access`, `inspect_model_relationships`, `upgrade_risk_report`, `fit_gap_report`, `business_pack_report`, `scan_addons_source`. | Usually CRUD only. |
| Transport | XML-RPC (16+) **and** External JSON-2 (Odoo 19+). Ready for the Odoo 22 XML-RPC removal years early. | Usually XML-RPC only — deprecated since Odoo 19, removed in Odoo 22. |
| Migration helpers | `generate_json2_payload` previews the JSON-2 body for any XML-RPC call before you migrate. | None. |
| Multi-instance | Named instances in one config file, per-tool routing, tokens and caches isolated per instance. | Usually one global connection per server process. |
| Agent prompts | 5 ready-made prompts for diagnose / fit-gap / JSON-2 migration / safe-write / module-audit. | Usually none. |
| HTTP transport security | DNS-rebinding protection, host/origin allowlists, local-bind by default. | Often missing. |
| Real Odoo smoke tests | Docker Compose harness boots disposable Odoo 16/17/18/19 stacks per release. | Often mock-based only. |
| Framework examples | Copy-paste adapters for Cursor, Claude Code, OpenAI Agents, LangGraph, CrewAI, and n8n in [`examples/`](./examples/). | None. |
| Audit & approval UX | JSONL audit trail + native elicitation confirm forms — without installing anything in Odoo. | Audit features usually require an Odoo-side module. |

Comparing specific projects? See the per-project breakdown in [docs/comparison.md](./docs/comparison.md).

## Setup

Two paths to a working server: set it up yourself, or paste one prompt and let your coding agent do it for you.

### For humans

The fastest path is the interactive wizard via `uvx`, which fetches the package on demand:

```bash
uvx odoo-mcp --setup
```

The wizard asks for your Odoo URL, database, and credentials, tests the connection live, writes the config file, and prints ready-to-paste snippets for Claude Code, Cursor, and Claude Desktop. Prefer a quick smoke check instead? `uvx odoo-mcp --health`.

Using Claude Desktop on macOS? It reads MCP configuration from:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

Use an absolute Python path because GUI apps may not inherit your shell `PATH`:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "/opt/homebrew/bin/python3",
      "args": ["-m", "odoo_mcp"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_DB": "your-database",
        "ODOO_USERNAME": "your-user",
        "ODOO_PASSWORD": "your-password-or-api-key",
        "ODOO_TRANSPORT": "xmlrpc"
      }
    }
  }
}
```

More client configs (Windsurf, VS Code, Zed, Continue.dev, Streamable HTTP) are in [docs/client-configs.md](./docs/client-configs.md).

Other ways to install:

```bash
pip install odoo-mcp
# or: pipx install odoo-mcp
```

Prefer a container? See [Docker](#docker). For local development:

```bash
git clone https://github.com/erpipe-org/mcp-odoo.git
cd mcp-odoo
uv sync --extra dev
```

### For AI agents

Paste this into Claude Code, Cursor, Codex, or any coding agent and it will install the server for you:

```text
Install the odoo-mcp MCP server (https://github.com/erpipe-org/mcp-odoo) in this environment:

1. Ask me for my Odoo URL, database name, username, and password or API key.
   Treat them as secrets: never echo, print, or log these values.
2. Register the server as a stdio MCP server:
   - Claude Code: claude mcp add odoo --env ODOO_URL=<url> --env ODOO_DB=<db>
     --env ODOO_USERNAME=<user> --env ODOO_PASSWORD=<secret> -- uvx odoo-mcp
   - Any other client: write the equivalent config with "command": "uvx",
     "args": ["odoo-mcp"], and the same four env vars.
3. Verify the install: run `uvx odoo-mcp --health`, then call the health_check
   MCP tool and confirm the Odoo connection is reachable.
4. Leave writes disabled (do not set ODOO_MCP_ENABLE_WRITES) unless I
   explicitly ask you to enable them.

Full machine-readable instructions: https://github.com/erpipe-org/mcp-odoo/blob/main/llms-install.md
```

Already know your client? One-liners and config snippets:

```bash
claude mcp add odoo --env ODOO_URL=https://mycompany.odoo.com --env ODOO_DB=mycompany \
  --env ODOO_USERNAME=agent@mycompany.com --env ODOO_PASSWORD=your-api-key -- uvx odoo-mcp
```

- Claude Code `.mcp.json` and Codex CLI `config.toml`: [`examples/README.md`](./examples/README.md)
- Cursor `.cursor/mcp.json` + agent rules: [`examples/cursor/`](./examples/cursor/)
- Windsurf, VS Code, Zed, Continue.dev, Cline, Streamable HTTP, Docker: [`docs/client-configs.md`](./docs/client-configs.md)
- Machine-readable install guide for agents (Cline-style): [`llms-install.md`](./llms-install.md)

#### Framework SDKs

Copy-paste-runnable integrations live in [`examples/`](./examples/):

| Client | Example |
| --- | --- |
| Cursor | [`examples/cursor/`](./examples/cursor/) — `.cursor/mcp.json` + agent rules |
| Claude Code / Codex CLI | snippets in [`examples/README.md`](./examples/README.md) |
| OpenAI Agents SDK | [`examples/openai-agents/`](./examples/openai-agents/) — local + hosted variants |
| LangGraph | [`examples/langgraph/`](./examples/langgraph/) — `langchain-mcp-adapters` |
| CrewAI | [`examples/crewai/`](./examples/crewai/) — native `mcps=[...]` agent |
| n8n | [`examples/n8n/`](./examples/n8n/) — importable workflow JSON |

## Configuration reference

Set connection values in the environment:

```bash
export ODOO_URL="https://your-odoo-instance.com"
export ODOO_DB="your-database"
export ODOO_USERNAME="your-user"
export ODOO_PASSWORD="your-password-or-api-key"
export ODOO_TRANSPORT="xmlrpc"
```

For Odoo 19 JSON-2:

```bash
export ODOO_TRANSPORT="json2"
export ODOO_API_KEY="your-odoo-api-key"
export ODOO_JSON2_DATABASE_HEADER="1"
```

`ODOO_JSON2_DATABASE_HEADER=1` sends `X-Odoo-Database` on JSON-2 calls. Set it to `0` only when host or dbfilter routing already selects the intended database.

Optional environment variables:

| Variable | Default | Effect |
| --- | --- | --- |
| `ODOO_CONFIG_FILE` | unset | Explicit path to a config file, checked before the standard locations. |
| `ODOO_LOCALE` | unset | Inject `context.lang` on every Odoo call. Caller-supplied `context.lang` always wins. |
| `ODOO_MCP_MAX_SMART_FIELDS` | `15` | Cap for smart-field selection when caller omits `fields`. |
| `ODOO_MCP_LOG_LEVEL` | `INFO` | Process logger level (DEBUG/INFO/WARNING/ERROR/CRITICAL). |
| `ODOO_MCP_LOG_JSON` | `0` | Truthy → emit JSON-formatted log lines. |
| `ODOO_MCP_LOG_FILE` | unset | Path → enable rotating file handler (10MB × 3 backups). |
| `ODOO_MCP_ENABLE_WRITES` | `0` | Required for `execute_approved_write`. |
| `ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS` | empty | Exact `model.method` allowlist (e.g. `sale.order.action_confirm`). |
| `ODOO_MCP_POLICY_FILE` | `./odoo_mcp_policy.json` if present | Version-controllable side-effect allowlist with review metadata (see `odoo_mcp_policy.json.example`); merged with the env allowlist. |
| `ODOO_MCP_ALLOW_UNKNOWN_METHODS` | `0` | Broad mode for `execute_method`. Prefer the exact allowlist above. |
| `ODOO_MCP_AUDIT_LOG` | unset | Path → append one JSONL line per write-path event (preview/validate/execute/chatter), tokens stored as digests. |
| `ODOO_MCP_ELICIT_WRITES` | `0` | Truthy → `execute_approved_write` asks the human via MCP elicitation (native confirm form with a diff summary) before executing; falls back to the token flow when the client cannot elicit. |
| `ODOO_MCP_RETRY_ATTEMPTS` | `2` | Extra attempts for read-only calls on connection errors (0–5). Writes never retry. |
| `ODOO_MCP_RETRY_BACKOFF` | `0.5` | Base retry backoff seconds; doubles per retry. |
| `ODOO_MCP_SCHEMA_CACHE_TTL` | `600` | Schema cache entry lifetime in seconds. |
| `ODOO_MCP_SCHEMA_CACHE_MAX` | `256` | Max schema cache entries (LRU eviction). |
| `ODOO_MCP_RATE_LIMIT_MODE` | `off` | `warn` tracks per-`instance:tool` call rates in `health_check`; `block` refuses over-budget calls on the hot read tools and `execute_method`. |
| `ODOO_MCP_RATE_LIMIT_WINDOW` | `60` | Sliding window length in seconds for rate tracking. |
| `ODOO_MCP_RATE_LIMIT_MAX_CALLS` | `120` | Calls allowed per window per `instance:tool`. |
| `ODOO_MCP_ASYNC_MAX_WORKERS` | `2` | Worker threads for `submit_async_task`. |
| `ODOO_MCP_ASYNC_MAX_TASKS` | `50` | Max retained background tasks (finished tasks evicted oldest-first). |
| `ODOO_MCP_ASYNC_RESULT_TTL` | `3600` | Seconds a finished background task result stays pollable. |
| `ODOO_MCP_KNOWLEDGE_MAX_DOCS` | `5000` | Total documents allowed across all local BM25 knowledge indexes. |
| `ODOO_MCP_FIELD_POLICY_FILE` | shared policy file | Field ACL policy (a `field_acl` key in the policy file, or a dedicated file here). Denied fields are removed from every read path. See [docs/field-acl.md](docs/field-acl.md). |
| `ODOO_MCP_CROSS_INSTANCE_WORKERS` | `4` | Bounded concurrency for cross-instance fan-out tools. |
| `MCP_CHATTER_DIRECT` | `0` | Truthy → `chatter_post` skips the approval token gate and posts immediately. |
| `MCP_ALLOW_REMOTE_HTTP` | `0` | Truthy → permit non-local HTTP binds (still requires external auth/TLS). |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | local | CSV allowlists for HTTP transports. |
| `ODOO_MCP_MAX_ATTACHMENT_BYTES` | `1048576` | Download cap for `read_attachment` content (hard cap 16 MiB). |
| `ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS` | unset | Colon-separated local directories `validate_write` may read `<field>_from_path` uploads from (mirrors `ODOO_ADDONS_PATHS`). Required — fails closed with no roots configured. |
| `ODOO_MCP_MAX_ATTACHMENT_UPLOAD_BYTES` | `10485760` | Size cap for `<field>_from_path` local-file uploads (hard cap 16 MiB). |
| `ODOO_MCP_AUTH_ISSUER_URL` | unset | OAuth 2.1: authorization server issuer. With the two vars below, the HTTP transport becomes a protected resource server (RFC 9728 metadata + bearer validation). |
| `ODOO_MCP_AUTH_INTROSPECTION_URL` | unset | RFC 7662 token introspection endpoint of the authorization server. |
| `ODOO_MCP_AUTH_RESOURCE_URL` | unset | Canonical URL of this MCP server (RFC 8707 audience check when the AS binds tokens). |
| `ODOO_MCP_AUTH_REQUIRED_SCOPES` | empty | CSV scopes required on every request. |
| `ODOO_MCP_AUTH_CLIENT_ID` / `_CLIENT_SECRET` | unset | Credentials for the introspection call when the AS requires client auth. |
| `ODOO_MCP_AUTH_REQUIRE_AUD` | `0` | Truthy → reject tokens whose introspection response has no `aud` claim (default only checks `aud` when present). |
| `ODOO_MCP_AUTH_REQUIRE_ISS` | `0` | Truthy → reject introspection responses without an `iss` claim. A present `iss` must always match `ODOO_MCP_AUTH_ISSUER_URL` (mix-up attack hardening). |
| `ODOO_MCP_AUTH_CACHE_TTL` | `60` | Seconds to cache introspection verdicts (`0` disables). Bounds both AS load and revocation lag. |
| `ODOO_MCP_PLUGINS` | unset | CSV entry-point names to load as third-party tool plugins (group `odoo_mcp.tools`). Installation alone activates nothing; failures are isolated and reported in `health_check`. See [docs/plugins.md](docs/plugins.md). |
| `ODOO_MCP_TOOLS_INCLUDE` / `_EXCLUDE` | unset | CSV fnmatch globs trimming the registered tool surface per deployment (small agents drown in 41 tools). Removed names listed in `health_check`. |
| `ODOO_MCP_INSTRUCTIONS_FILE` | unset | Plain-text file appended to the server-level MCP `instructions` every client receives — deployment-specific guidance (fiscal-year rules, naming conventions) without touching tool descriptions. |

You can also use `odoo_config.json`:

```json
{
  "url": "https://your-odoo-instance.com",
  "db": "your-database",
  "username": "your-user",
  "password": "your-password-or-api-key"
}
```

### Multiple Odoo instances

One server can talk to several Odoo databases. Add an `instances` map to your config file (auto-detected — a file without `instances` keeps the flat single-instance shape above):

```json
{
  "default": "acme",
  "instances": {
    "acme": {
      "url": "https://acme.odoo.com",
      "db": "acme",
      "username": "bot",
      "api_key": "...",
      "transport": "json2"
    },
    "globex": {
      "url": "https://globex.odoo.com",
      "db": "globex",
      "username": "bot",
      "password": "...",
      "lang": "fr_FR",
      "timeout": 60
    }
  }
}
```

- Every read/write tool accepts an optional `instance` parameter; omitted → the `default` instance. `default` itself is optional when only one instance is defined.
- Each entry supports the same keys as the flat config (`url`, `db`, `username`, `password`, `api_key`, `transport`, `json2_database_header`, `lang`) plus `timeout` and `verify_ssl`. Instance entries are self-contained: credentials and transport never fall back to env vars (so one instance can never inherit another deployment's `ODOO_API_KEY`). Only non-credential knobs (`ODOO_TIMEOUT`, `ODOO_VERIFY_SSL`, `ODOO_LOCALE`) act as fallback defaults for entries that omit them. Env overrides like `ODOO_TRANSPORT`/`ODOO_API_KEY` still apply to legacy flat configs, as before.
- `ODOO_CONFIG_FILE=/path/to/config.json` points at an explicit config file, checked before `./odoo_config.json`, `~/.config/odoo/config.json`, and `~/.odoo_config.json`.
- **Precedence**: when `ODOO_URL`/`ODOO_DB`/`ODOO_USERNAME`/`ODOO_PASSWORD` are all set, the environment wins and defines a single instance named `default` — unset them to use a multi-instance file.
- Instance names must match `[A-Za-z0-9_-]{1,64}`. Clients connect lazily — an instance is only contacted when a tool targets it.
- Discovery: the `list_instances` tool returns configured names, URLs, databases, and transports — never credentials.
- Write-approval tokens encode the instance name, so a token validated against one instance can never execute on another.
- MCP resources (`odoo://…`) always use the default instance in this release; use tools for multi-instance access.

## Run

Start the MCP server over stdio:

```bash
odoo-mcp
```

or:

```bash
python -m odoo_mcp
```

Start Streamable HTTP for local clients:

```bash
odoo-mcp --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
```

Non-local HTTP binds are rejected unless you pass `--allow-remote-http` or set `MCP_ALLOW_REMOTE_HTTP=1`. This server does not include built-in HTTP authentication. Put remote HTTP deployments behind your own authentication, TLS, and network policy.

Check runtime posture without starting the server loop:

```bash
odoo-mcp --health
```

## MCP Tools

41 tools grouped by use case. Each tool name is a single-purpose handle the agent can call. Tools that talk to Odoo accept an optional `instance` parameter when multiple instances are configured (see [Multiple Odoo instances](#multiple-odoo-instances)).

### Read & Discover (11)

| Tool | Purpose |
| --- | --- |
| `list_models` | List Odoo model technical names and labels. |
| `get_model_fields` | Read field metadata for one model. |
| `search_records` | Run bounded read-only `search_read`. Smart-field selection when caller omits `fields`. |
| `read_record` | Read one record by model and ID. Smart-field selection when caller omits `fields`. |
| `aggregate_records` | Server-side groupby/aggregation via `formatted_read_group` (Odoo 19+) or `read_group` (16-18). |
| `search_employee` | Search employees by name. |
| `search_holidays` | Search leave records by date range. |
| `get_odoo_profile` | Read server version, user context, transport, database, and installed module summary. |
| `schema_catalog` | Build a bounded model catalog with optional field metadata. |
| `build_domain` | Build and validate an Odoo domain from structured conditions. |
| `read_attachment` | Read an `ir.attachment`'s metadata and size-capped base64 content (`ODOO_MCP_MAX_ATTACHMENT_BYTES`, default 1 MiB). |

### Write & Operate (5)

| Tool | Purpose |
| --- | --- |
| `preview_write` | Produce a non-executing approval payload for `create`, `write`, or `unlink`. |
| `validate_write` | Validate a write payload against trusted live `fields_get` metadata. |
| `execute_approved_write` | Execute only a same-session, live-validated, confirmed write when `ODOO_MCP_ENABLE_WRITES=1`. |
| `execute_method` | Execute a reviewed model method. Direct `create`, `write`, and `unlink` are blocked. Side-effect methods require an exact allowlist or `ODOO_MCP_ALLOW_UNKNOWN_METHODS=1`. |
| `chatter_post` | Post a chatter message on a `mail.thread` record. Default mode requires the approval-token preview/execute flow. |

### Diagnose (3)

| Tool | Purpose |
| --- | --- |
| `diagnose_odoo_call` | Diagnose a model call without executing it. |
| `diagnose_access` | Diagnose ACL and record-rule visibility for the current Odoo credential. |
| `inspect_model_relationships` | Group relationship fields, required fields, and create/write hints. |

### Migrate (3)

| Tool | Purpose |
| --- | --- |
| `generate_json2_payload` | Convert XML-RPC-shaped input into JSON-2 endpoint, headers, and named body. |
| `upgrade_risk_report` | Surface transport, method, and migration risks across Odoo versions. |
| `lookup_model_history` | Resolve outdated model names (`account.invoice` → `account.move`) against a curated per-version rename catalog. |

### Audit & Plan (3)

| Tool | Purpose |
| --- | --- |
| `scan_addons_source` | Scan local addon source without importing addon code. |
| `fit_gap_report` | Classify requirements into standard, configuration, Studio, custom module, avoid, or unknown. |
| `business_pack_report` | Report expected modules, models, and discovery calls for sales, CRM, inventory, accounting, or HR. |

### Knowledge search — local-first (3)

| Tool | Purpose |
| --- | --- |
| `index_knowledge` | Fetch a bounded record slice once and build a local BM25 index (accent-insensitive; data never leaves the machine). |
| `search_knowledge` | Relevance-ranked free-text search over indexed records with zero further RPC calls. |
| `knowledge_stats` | Report per-model index sizes and the `ODOO_MCP_KNOWLEDGE_MAX_DOCS` budget. |

### Accounting (2)

| Tool | Purpose |
| --- | --- |
| `receivable_payable_aging` | Aged AR/AP report bucketed by days overdue (not due / 1-30 / 31-60 / 61-90 / 90+), with per-partner totals. |
| `accounting_health_summary` | Open receivable/payable item counts plus the draft invoice backlog. |

### Background tasks (4)

| Tool | Purpose |
| --- | --- |
| `submit_async_task` | Run an allowlisted long read operation (`scan_addons_source`, `index_knowledge`, `receivable_payable_aging`) on a bounded worker pool. Writes are never accepted. |
| `get_async_task` | Poll a task's status and result. |
| `cancel_async_task` | Cancel a pending or running task. |
| `list_async_tasks` | List live and recently finished tasks. |

### Cross-instance fan-out — read-only (3)

One question across many configured instances, merged and attributed. See the [partner playbook](docs/partner-playbook.md).

| Tool | Purpose |
| --- | --- |
| `search_across_instances` | Search every opted-in instance (or a list/tag selection); rows tagged with `_instance`, partial results on per-instance failure. |
| `aggregate_across_instances` | Group/aggregate per instance plus additive grand totals across the fleet. |
| `accounting_health_across_instances` | AR/AP aging across every client DB with combined buckets — the partner-network sweep. |

### Utility (2)

| Tool | Purpose |
| --- | --- |
| `health_check` | Report non-secret MCP runtime posture, including rate-limit counters and field-ACL status when enabled. |
| `list_instances` | List configured Odoo instance names, URLs, databases, transports, and cross-instance tags — never credentials. |

## Resources

| URI | Description |
| --- | --- |
| `odoo://models` | List available models. |
| `odoo://model/{model_name}` | Read model metadata and fields. |
| `odoo://record/{model_name}/{record_id}` | Read one record. |
| `odoo://search/{model_name}/{domain}` | Search records with a bounded domain. |

## Prompts

11 prompts: 5 diagnostic, plus 6 operational **workflow** prompts that encode end-to-end business processes and route every write through the approval gate.

| Prompt | Use it for |
| --- | --- |
| `diagnose_failed_odoo_call` | Root-cause a failing Odoo call before retrying. |
| `fit_gap_workshop` | Turn raw requirements into Odoo fit/gap buckets. |
| `json2_migration_plan` | Plan XML-RPC or JSON-RPC migration to External JSON-2. |
| `safe_write_review` | Review a proposed `create`, `write`, or `unlink`. |
| `custom_module_audit` | Audit local addon source with scan, risk, and business evidence. |
| `invoice_approval_chain` | Triage draft invoices and post each through the write gate with human checkpoints. |
| `po_to_receipt` | Three-way match a purchase order against receipt and bill; flags discrepancies (read-only). |
| `customer_onboarding` | Dedup-check, then gated-create a customer with contacts and payment terms. |
| `expense_claim_review` | Policy-check pending expense claims, then gated approve/refuse. |
| `accounting_close_checklist` | Read-only month-end checklist: aging, unreconciled items, draft backlog. |

## Safe Write Model

Writes are intentionally boring.

1. `preview_write` creates a canonical, non-executing payload.
2. `validate_write` checks model metadata, required fields, readonly fields, relation hints, record IDs, and payload shape.
3. `execute_approved_write` runs only when all gates pass:
   - the approval came from `validate_write` in the same server process,
   - validation used trusted, non-empty live Odoo `fields_get` metadata,
   - the token has not expired or been consumed,
   - `confirm=true` is passed,
   - `ODOO_MCP_ENABLE_WRITES=1` is set.

Odoo access rules, record rules, and server-side constraints still decide the final result.

Batch creates go through the same gates: pass `values_list` (one dict per
record, max 100) to `preview_write`/`validate_write` — execution maps to a
single atomic Odoo `create(vals_list)` call. Per-record differing `write`
values are deliberately unsupported (they would need one non-atomic RPC per
record). Optional extras: `ODOO_MCP_ELICIT_WRITES=1` adds a native
human-confirmation form, `ODOO_MCP_AUDIT_LOG` records every write-path event.

Journal entries use the same workflow: `account.move` creation accepts
`move_type="entry"` (or its default) and new `line_ids` commands even when Odoo
marks these fields UI-readonly. Each item is checked against live
`account.move.line` metadata; separate commands on the same account remain
separate items. Odoo still checks balance and accounting constraints at execution.
See [Journal entry writes](docs/journal-entry-writes.md) for the payload,
supported command shapes, posting, and a disposable integration test.

Large binary fields (a resume attached to `ir.attachment.datas`, a product
image, ...) don't have to be inlined as base64 in the tool call — pass
`<field>_from_path` instead (e.g. `datas_from_path: "/local/path/cv.pdf"`) to
`validate_write`. The server reads the file itself; the approval only ever
carries a `sha256:<hex>:<size>` fingerprint for that field, never the real
content, so nothing large has to round-trip through the calling agent's
context. Requires `ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS` (fails closed otherwise)
and respects `ODOO_MCP_MAX_ATTACHMENT_UPLOAD_BYTES`. No new tool — this rides
the same `preview_write` → `validate_write` → `execute_approved_write` gate as
every other write.

Attachment creation and updates accept the UI-readonly `res_model` / `res_id`
pair when both are provided explicitly with a model name and positive integer
record ID. This lets uploads belong to their target document immediately and
lets an existing unlinked attachment be repaired without uploading it again.
Other readonly checks and all approval gates still apply. Linking alone does
not post a chatter message or send an email. See
[Attachment writes](docs/attachment-writes.md) for examples and verification.

Reviewed side-effect methods such as `sale.order.action_confirm` can be enabled
one by one:

```bash
export ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS="sale.order.action_confirm,res.partner.message_post"
```

`ODOO_MCP_ALLOW_UNKNOWN_METHODS=1` is still supported for trusted deployments,
but `health_check` reports it as broad mode. Prefer exact allowlist entries when
you only need a small number of reviewed methods.

## Docker

Use the prebuilt GHCR image:

```bash
docker pull ghcr.io/erpipe-org/mcp-odoo:latest
```

Or build it locally:

```bash
docker build -t mcp/odoo:latest -f Dockerfile .
```

Run over stdio from an MCP client (replace `mcp/odoo:latest` with `ghcr.io/erpipe-org/mcp-odoo:latest` to use the prebuilt image):

```json
{
  "mcpServers": {
    "odoo": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-e", "ODOO_URL",
        "-e", "ODOO_DB",
        "-e", "ODOO_USERNAME",
        "-e", "ODOO_PASSWORD",
        "-e", "ODOO_TRANSPORT",
        "-e", "ODOO_API_KEY",
        "mcp/odoo:latest"
      ]
    }
  }
}
```

Run Streamable HTTP locally:

```bash
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -e ODOO_URL \
  -e ODOO_DB \
  -e ODOO_USERNAME \
  -e ODOO_PASSWORD \
  -e ODOO_TRANSPORT \
  -e ODOO_API_KEY \
  mcp/odoo:latest \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8000 \
  --allow-remote-http
```

## Test

Run the normal quality gates:

```bash
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest
```

Run real Odoo smoke tests:

```bash
uv run --python 3.12 --with-editable . scripts/odoo_compose_smoke.py \
  --versions 16.0 17.0 18.0 19.0 \
  --timeout 360 \
  --inspector-smoke
```

The smoke harness boots disposable Docker Compose stacks, validates direct Odoo access, validates MCP stdio, and for Odoo 19 also validates JSON-2 and Streamable HTTP.

Run the multi-instance smoke (one stack, three databases, two accounts on one instance):

```bash
uv run --python 3.12 --with-editable . scripts/odoo_multi_instance_smoke.py
```

## Compatibility

XML-RPC remains the default transport for broad compatibility. Odoo 19 supports External JSON-2 through `ODOO_TRANSPORT=json2`. XML-RPC and JSON-RPC are deprecated since Odoo 19 and scheduled for removal in Odoo 22 (fall 2028), so new integrations should plan for JSON-2.

## Documentation

| Guide | Covers |
| --- | --- |
| [docs/comparison.md](./docs/comparison.md) | How Odoo MCP compares to other Odoo MCP bridges |
| [docs/architecture.md](./docs/architecture.md) | System shape, transports, safety boundaries |
| [docs/multi-instance.md](./docs/multi-instance.md) | Multi-database config, routing, isolation model |
| [docs/troubleshooting.md](./docs/troubleshooting.md) | From error text to root cause (ACL, record rules, routing) |
| [docs/performance.md](./docs/performance.md) | Cache/retry knobs, batching patterns, N+1 detection |
| [docs/client-configs.md](./docs/client-configs.md) | Claude Desktop, Docker, Streamable HTTP setups |
| [docs/testing.md](./docs/testing.md) | Local gates and the Docker Compose smoke harness |

## Contributing

Issues, pull requests, and compatibility reports are welcome. Start with [CONTRIBUTING.md](./CONTRIBUTING.md), include your Odoo version, transport, client type, and the verification you ran.

## Security

Do not publish logs that contain Odoo credentials, API keys, database names from private environments, or full Odoo debug traces. Report vulnerabilities through [SECURITY.md](./SECURITY.md).

## License

MIT. See [LICENSE](./LICENSE).
