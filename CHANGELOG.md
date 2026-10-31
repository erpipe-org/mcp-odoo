# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Fixed

- `validate_write` accepts the UI-readonly `res_model` / `res_id` pair on
  `ir.attachment` create and write, including batch creates. Both fields must
  explicitly identify a target record and match known metadata types. Upload
  tests use real Odoo readonly flags; trusted live metadata, approval gates,
  other readonly checks, and Odoo access rules remain in effect.
- `validate_write` accepts the UI-readonly `line_ids` and `move_type="entry"`
  fields when creating a draft `account.move` journal entry. New journal items
  require live child-model metadata and create-only commands; unrelated readonly
  fields and existing-record writes retain their checks. Single and batch
  creates preserve the original approval payload and distinct same-account items.
  Odoo remains responsible for balance and accounting constraints at execution.

### Added

- Disposable attachment Compose smoke test for Odoo 16–19 (XML-RPC) and Odoo 19
  (JSON-2), including linked uploads, repair of existing unlinked attachments,
  checksum preservation, batch creates, and HTTP downloads by another user.
- Disposable journal-entry Compose smoke test for Odoo 16-19 over XML-RPC and
  Odoo 19 over JSON-2, covering single and batch MCP approval flows, live parent
  and child metadata, posting, atomic rollback on imbalance, independent
  reconciliation of same-account items, and negative validation/execution gates.

## [1.3.2] - 2026-08-19

### Changed

- PyPI project URL **Hosted (ERPipe)** now points at the product site
  `https://erpipe.com/`. Added **Hosted MCP** `https://mcp.erpipe.com/mcp`.
  Do not retag `v1.3.1` (GitHub tag exists; PyPI never received that build).

## [1.3.1] - 2026-08-14

### Fixed

- A search filter that the server cannot understand is now rejected instead of
  matching every record. Agents that write the usual Odoo tuple form (for
  example looking up records by tag) get the intended subset back, and a
  malformed filter no longer silently searches the whole model. The same rule
  applies when a search is sent as a keyword argument, not only as the first
  positional argument.
- If Odoo cannot complete a search or a single-record read, the agent now
  sees an error instead of an empty result that looks like “no matching
  records.”

## [1.3.0] - 2026-07-29

### Changed

- Upgraded to the stable MCP Python SDK 2.x and MCP `2026-07-28`.
  `MCPServer` now serves the stateless `server/discover` lifecycle and legacy
  `2025-11-25` clients from the same stdio or Streamable HTTP endpoint.
- Write confirmation uses the SDK's era-portable resolver: MRTR on modern
  clients, legacy elicitation on older clients, and the existing approval-token
  fallback when form elicitation is unavailable.
- Streamable HTTP settings now pass through `MCPServer.run()` as required by
  SDK v2; DNS-rebinding host/origin defaults and OAuth introspection remain in
  place. OAuth HTTP calls use the SDK-provided `httpx2` dependency.

## [1.2.3] - 2026-07-22

Metadata-only ownership refresh after the repository transfer to `erpipe-org`.
This republishes the package so the PyPI README carries the canonical
`mcp-name: io.github.erpipe-org/mcp-odoo` marker used by the MCP Registry;
runtime behavior is unchanged.

## [1.2.2] - 2026-07-21

Metadata and packaging release: publish a clearer PyPI/registry short
description that points at the live free hosted product
[ERPipe](https://mcp.erpipe.com/), plus typed discovery contracts that
landed on `main` after 1.2.1.

### Added
- **Typed output schemas for domain tools** — knowledge, accounting, async,
  and cross-instance tools advertise Pydantic-backed `outputSchema` contracts
  through `tools/list` (additive; runtime envelopes unchanged). Thanks
  @barry166. (#50)
- **Typed `build_domain` success/error contracts** for agent consumers.

### Changed
- **Package / catalog short descriptions** promote live ERPipe hosted free v1
  (`mcp.erpipe.com`) while keeping this package the local/self-host 41-tool
  server. README, site, MCP Desktop Extension manifest, and comparison docs
  aligned.
- PyPI `summary` keywords include `erpipe`, `chatgpt`, `claude`, and
  `model-context-protocol`; project URL `Hosted (ERPipe)` added.

## [1.2.1] - 2026-07-14

Community patch: the write-approval gate no longer drops valid tokens on
int/float transport drift, the 10 highest-traffic tools describe their
parameters to MCP clients, and the suite is hermetic on developer machines
that carry a real Odoo config. All three code changes came from community
PRs. 909 tests.

### Added
- **Per-parameter input descriptions (first 10 tools)** — the highest-traffic
  tools (`search_records`, `read_record`, `execute_method`, the gated-write
  trio, accounting/diagnostics/cross-instance entry points) now wrap every
  non-context parameter in `Annotated[..., Field(description=...)]`, so MCP
  clients and inspectors render real `inputSchema` property descriptions.
  Coverage is pinned by `DESCRIBED_INPUT_TOOLS` in `tests/test_schemas.py`;
  remaining tool groups tracked in #35. (#41)

### Changed
- Field ranking / smart field selection helpers moved from `agent_tools.py`
  into a dedicated `field_ranking.py` core module (compatibility re-exports
  kept, import-linter contract updated). No behavior change. (#42)

### Fixed
- **Write-approval token instability across int/float JSON transports** —
  `canonical_json` (`agent_tools.py`) now normalizes integral floats (`1.0`)
  to `int` (`1`) before hashing, recursively across dicts/lists. Previously,
  a numeric value that round-tripped through a JSON transport layer where
  `1` and `1.0` are the same value (common in JS/TS clients and gateways)
  could change the SHA-256 approval token between `preview_write`/
  `validate_write` and `execute_approved_write`, causing intermittent
  `"approval token does not match the canonical payload"` failures on
  otherwise-valid, unchanged payloads — most visible with "round" numeric
  field values (e.g. `unit_amount: 1.0` on `account.analytic.line`).
  Booleans are left untouched. No change to already-issued tokens for
  payloads that only ever contained plain ints. (#48)

## [1.2.0] - 2026-07-14

Field-ops release: context-safe attachment uploads, an Agent Skills pack,
typo-tolerant model-history lookups, and kinder failure modes around Odoo's
XML-RPC `None`-marshalling quirks — hardened by a real production
misattributed-freight correction run end-to-end through the write gate.
901 tests.

### Added
- **Local-file attachment uploads** — `validate_write` accepts `<field>_from_path`
  (e.g. `datas_from_path`) as an alternative to inlining base64 for binary
  fields such as `ir.attachment.datas`. The server reads the file, and the
  approval only ever carries a `sha256:<hex>:<size>` fingerprint for that
  field — the real bytes never appear in a tool response, in the stored
  approval token payload, or in anything the caller echoes back to
  `execute_approved_write`, which substitutes the real content server-side at
  execution time. Fixes the case where a large attachment (a resume, an
  invoice PDF, ...) can't be inlined into a single tool call without blowing
  past an agent's context budget. Fails closed: requires
  `ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS` (colon-separated allowed local
  directories, mirrors `ODOO_ADDONS_PATHS`) and enforces
  `ODOO_MCP_MAX_ATTACHMENT_UPLOAD_BYTES` (default 10 MiB, hard cap 16 MiB).
  No new tool — routes through the existing `preview_write` → `validate_write`
  → `execute_approved_write` gate, same live-metadata and confirm requirements
  as every other write.
- **Agent Skills pack** — `skills/` ships 4 business-workflow skills in the
  open Agent Skills format (`odoo-data-quality-gate`,
  `odoo-migration-copilot`, `odoo-month-end-close`,
  `odoo-agency-fleet-review`): judgment playbooks (evidence rules, pacing,
  human checkpoints) on top of the MCP tool layer, for Claude Code and other
  skills-compatible agents. Install by copying into `~/.claude/skills/`.
- **Model-history lookups suggest close names** — `lookup_model_history` now
  offers nearest-match suggestions when a model name misses, and the
  cross-version rename map covers additional Odoo model renames.

### Fixed
- `get_model_fields` now requests a bounded, marshal-safe `attributes` list
  instead of a full `fields_get`. On Odoo 19, a full `fields_get` faults
  **server-side** for models whose `domain` attribute is `None` (e.g.
  `product.pricelist`) — Odoo's own XML-RPC layer refuses to marshal its
  response, which no client flag can fix (complements the client-side
  `allow_none` fix shipped in 1.1.0). The bounded list covers every attribute
  the server consumes (`string`, `help`, `type`, `required`, `readonly`,
  `relation`, `selection`, `store`, `searchable`); an absent attribute reads
  as `None` via `.get()`, exactly as before. Unblocks `validate_write` on such
  models (e.g. creating an IQD `product.pricelist`).
- `execute_method` no longer reports a **phantom failure** when a
  side-effect method returns `None` (`button_draft`, `action_post`,
  `account.move.line.reconcile`, ...). Odoo executes and commits the call,
  then its XML-RPC layer faults while serializing the `None` return value
  ("cannot marshal None unless allow_none is enabled") — the state change is
  already persisted, so surfacing an error invites a dangerous retry. The
  tool now returns `success: true, result: null` plus an explicit warning to
  verify with a read.
- The blocked-side-effect error on `execute_method` now points at the policy
  file (`ODOO_MCP_POLICY_FILE`, default `./odoo_mcp_policy.json`, re-read on
  every request — no server restart) as the primary allowlist mechanism;
  previously it only mentioned the env vars, and the better, reviewable path
  was discoverable only by reading source.
- The test suite is now hermetic on a developer machine that carries a real
  Odoo config: an autouse fixture points `ODOO_CONFIG_FILE` at a missing file
  so instance resolution behaves like a bare CI runner (previously a
  `~/.config/odoo/config.json` flipped resolution semantics and failed four
  instance/knowledge tests locally while CI stayed green).

## [1.1.0] - 2026-07-02

Community-roadmap release: the server becomes a platform (plugins), the
migration wave gets real tooling (data quality + upgrade-log analysis), and
the MCP surface gets typed. Tool count 39 → 41; prompts 10 → 11; 885 tests.

### Added
- **Tool plugins** — third parties can ship odoo-mcp tools as normal pip
  packages exposing an `odoo_mcp.tools` entry point with a `register(api)`
  callable; `odoo_mcp.plugin_api` (v1) provides the stable surface (tool
  decorator, instance resolution, field-ACL redaction, envelopes).
  **Opt-in only**: nothing loads unless the name is listed in
  `ODOO_MCP_PLUGINS`; a raising plugin is isolated and reported under
  `health_check.plugins`. Runnable example in `examples/plugin-example/`;
  authoring guide + threat model in [docs/plugins.md](docs/plugins.md).
- **Per-deployment tool filtering** — `ODOO_MCP_TOOLS_INCLUDE` /
  `ODOO_MCP_TOOLS_EXCLUDE` (CSV fnmatch globs) trim the registered tool
  surface for token-constrained clients; removed names surface in
  `health_check.plugins.tools_filtered`.
- **Data-quality pack (read-only)** — new tool `data_quality_report` runs
  evidence-first checks on one model: `duplicates` (server-side read_group on
  identifier fields), `missing_required`, `orphaned_references` (honest about
  ACL-hidden targets), `format_anomalies` (email/phone/vat heuristics). Field
  ACL is the ceiling (denied fields skipped + listed); every issue carries
  record ids/values; remediation stays behind the gated write workflow.
  Registered on the async allowlist for large models. New core module
  `data_quality.py` + surface `tools_data_quality.py`; docs in
  [docs/data-quality.md](docs/data-quality.md). Tool count 39 → 41.
- **Migration workbench v1** — new tool `analyze_upgrade_log` classifies Odoo
  install/update log failures (xpath breaks, missing fields/models/external
  ids, NOT NULL violations, dependency errors, Odoo 17 attrs removal, ORM
  signature changes) into an OpenUpgrade-style worklist with per-finding
  suggestions; input-driven, never contacts Odoo. `scan_addons_source`
  findings and `upgrade_risk_report` risks now carry the same `action`
  taxonomy (`no_action` / `needs_review` / `needs_script`) plus an `actions`
  summary.
- **Workflow prompt #11** — `pre_migration_data_quality` chains the
  data-quality pack, `diagnose_access` confirmation, and `analyze_upgrade_log`
  into a migration gate with human-approved, write-gated remediation batches
  (prompt count 10 → 11).
- **Typed per-tool output schemas (read surface)** — the 10 dict-returning
  read tools (`get_odoo_profile`, `schema_catalog`, `health_check`,
  `list_instances`, `list_models`, `get_model_fields`, `search_records`,
  `read_record`, `read_attachment`, `aggregate_records`) now declare Pydantic
  response models (new core module `schemas.py`), so `tools/list` advertises
  real `outputSchema` fields instead of a generic object wrapper. Runtime
  behavior is unchanged (same envelope, extra keys allowed); MCP responses now
  include explicit `null`s for unset optional fields. Remaining tool groups
  are tracked as `good first issue`s.
- **OAuth hardening (SEP alignment)** — introspection responses carrying an
  `iss` must match the configured issuer (mix-up attack defense);
  `ODOO_MCP_AUTH_REQUIRE_ISS=1` rejects responses without `iss`;
  `ODOO_MCP_AUTH_REQUIRE_AUD=1` rejects tokens without an `aud` claim;
  introspection verdicts (including rejections) are cached for
  `ODOO_MCP_AUTH_CACHE_TTL` seconds (default 60, `0` disables) so hot agent
  loops no longer hammer the authorization server. `health_check` auth
  posture reports the new flags.
- **Server-level instructions** — `ODOO_MCP_INSTRUCTIONS_FILE` appends a
  deployment-specific plain-text briefing to the MCP `instructions` field
  every client receives (idea: #19, thanks @oadiazp). Unreadable path fails
  at startup; content capped at 16k chars.

### Changed
- The deprecated SSE transport now prints a startup warning suggesting
  `streamable-http`.
- python-sdk 2.0 (MCP spec 2026-07-28) migration spiked against `mcp==2.0.0b1`
  and mapped (FastMCP→MCPServer rename, `run()` kwargs, ctor-level
  `token_verifier`, reshaped elicitation); the dependency pin stays
  `mcp>=1.27,<2` until the port ships as its own release.

### Fixed
- XML-RPC transport: create the `ServerProxy` clients with `allow_none=True` so a
  call whose payload or `fields_get` metadata contains `None` no longer raises
  `TypeError: cannot marshal None unless allow_none is enabled` (seen when creating
  a `product.pricelist` through `validate_write`). Odoo's XML-RPC server already
  emits `<nil/>`, so the client must accept it too.

## [1.0.0] - 2026-06-11

The v1.0 milestone: four roadmap phases close the highest-value gaps no
open-source Odoo MCP server covered. Tool count 36 → 39; 854 tests.

### Added
- **Field-level ACL (read-path)** — opt-in, per-instance, per-model field
  allow/deny enforced at a single choke point on every path that returns
  record data: `search_records`, `read_record`, `aggregate_records` (denied
  groupby/measure rejected), `get_model_fields` (denied fields marked
  `"access": "restricted"`, not hidden), `index_knowledge` (denied fields
  excluded before BM25 indexing), and the `odoo://` resources. Responses gain
  a `redacted_fields` note. Configure via a `field_acl` key in the policy
  file or `ODOO_MCP_FIELD_POLICY_FILE`; malformed policy fails closed at
  startup. No policy → byte-identical v0.9.0 behavior. First open-source Odoo
  MCP server with field ACL. New module `field_policy.py`; see
  [docs/field-acl.md](docs/field-acl.md).
- **Cross-instance fan-out (read-only)** — `search_across_instances`,
  `aggregate_across_instances`, and `accounting_health_across_instances` ask
  one question across many configured instances and return merged,
  `_instance`-attributed results with a partial-failure `errors` map. Instance
  selection by list, `"all"`, or `{"tags": [...]}`; opt out per instance with
  `"cross_instance": false`. Each instance is queried under its own field ACL
  (applied before merge), bounded concurrency
  (`ODOO_MCP_CROSS_INSTANCE_WORKERS`, default 4), and its own rate-limit
  budget; large fleets can run via `submit_async_task`. No warehouse, no sync.
  New modules `cross_instance.py` + `tools_cross_instance.py`; see
  [docs/partner-playbook.md](docs/partner-playbook.md).
- **5 operational workflow prompts** (prompt count 5 → 10) —
  `invoice_approval_chain`, `po_to_receipt`, `customer_onboarding`,
  `expense_claim_review`, `accounting_close_checklist`. Each names its
  required modules, the exact tools per step, and human checkpoints; every
  write-bearing step routes through the gated workflow (no ungated writes).
  New module `prompts_workflows.py`.
- **Head-to-head benchmark** — `scripts/benchmark_head_to_head.py` measures
  mcp-odoo against another Odoo MCP server on the same Odoo stack;
  [docs/benchmarks.md](docs/benchmarks.md) publishes a reproducible
  methodology and measured numbers (mcp-odoo p50 ~10 ms for common reads on a
  local Odoo 19 stack).
- Config: optional per-instance `tags` and `cross_instance` keys;
  `list_instances` now reports them.

### Changed
- `health_check` runtime posture adds a `field_acl` section (active flag +
  instance count, never policy contents).
- `.importlinter` extended to cover the new core (`field_policy`,
  `cross_instance`) and surface (`tools_cross_instance`, `prompts_workflows`)
  modules; contracts still 2 kept.

## [0.9.0] - 2026-06-11

### Added
- Background task tools (tool count 27 → 36 together with the knowledge and accounting tools) — `submit_async_task` runs allowlisted long read operations (`scan_addons_source`, `index_knowledge`, `receivable_payable_aging`) on a bounded thread pool (`ODOO_MCP_ASYNC_MAX_WORKERS`, default 2); poll with `get_async_task`, cancel with `cancel_async_task`, list with `list_async_tasks`. Results are retained in memory with a TTL (`ODOO_MCP_ASYNC_RESULT_TTL`, default 1h) and entry cap (`ODOO_MCP_ASYNC_MAX_TASKS`, default 50). Writes are never accepted on the async path.
- Local-first knowledge search — `index_knowledge` fetches a bounded record slice (smart business-field selection by default) and builds an in-process BM25 index; `search_knowledge` runs accent-insensitive relevance-ranked queries with zero further RPC calls; `knowledge_stats` reports index sizes. Document budget via `ODOO_MCP_KNOWLEDGE_MAX_DOCS` (default 5000). No embeddings service, no new dependencies, no data leaving the machine.
- Accounting domain pack (read-only) — `receivable_payable_aging` buckets open posted items by days overdue (not due / 1-30 / 31-60 / 61-90 / 90+) with per-partner totals and a sign-flip for payables; `accounting_health_summary` reports open AR/AP item counts and the draft invoice backlog. Works on Odoo 16+ via `account_type` selections.
- Opt-in tool rate limiting — `ODOO_MCP_RATE_LIMIT_MODE=warn|block` tracks calls per `instance:tool` in a sliding window (`ODOO_MCP_RATE_LIMIT_WINDOW`, default 60s; `ODOO_MCP_RATE_LIMIT_MAX_CALLS`, default 120). `warn` only surfaces counters in `health_check.rate_limits`; `block` refuses over-budget calls on `search_records`, `read_record`, `aggregate_records`, and `execute_method` with a structured error. Default `off` preserves existing behaviour.
- Benchmark harness — `scripts/benchmark_tools.py` measures per-tool latency (p50/p95/mean) over MCP stdio against any Odoo instance; methodology and reference numbers in `docs/benchmarks.md`.
- `docs/claude-desktop-connector.md` — expose the server as a Claude Desktop / claude.ai Custom Connector using the existing Streamable HTTP transport and OAuth 2.1 resource server.
- Dedicated unit tests for `tool_helpers`, `access_helpers`, `schema_cache`, and `write_policy` (245 tests), plus suites for the new task queue, rate limiter, knowledge index, and accounting builders. Full suite: 802 tests.

### Changed
- Internal refactor: `server.py` (2,553 lines) split into domain modules — `server_core.py` (FastMCP instance, lifespan, shared infra, resources), `tools_read.py`, `tools_write.py`, `tools_diagnostics.py`, `tools_knowledge.py`, `tools_accounting.py`, `tools_async.py`, and `prompts.py`. `odoo_mcp.server` remains the full public re-export surface; no behavior change and all existing imports/monkeypatch targets keep working.
- `.importlinter` now enforces two real contracts for `odoo_mcp` (core helpers must not import the MCP surface; server → tool modules → core layering) instead of referencing a non-existent package.
- `docs/catalog-submission.md` expanded with Official MCP Registry and Docker MCP Catalog submission checklists plus outreach drafts.
- README setup restructured into a single `Setup` section with two explicit paths: **For humans** (interactive wizard, Claude Desktop, pip/Docker/dev installs) and **For AI agents** (a paste-able self-install prompt, `claude mcp add` one-liners, framework SDK examples). The long environment-variable table moved to a new `Configuration reference` section.
- Added `llms-install.md` — a self-contained, machine-readable installation guide (Cline convention) for AI agents installing the server on a user's behalf, including the configuration contract, per-client steps, verification sequence, and agent security rules (never echo credentials, never enable writes without explicit consent).

## [0.8.0] - 2026-06-11

### Added
- Free-text `query` parameter on `search_records` — the server builds an OR `ilike` domain over the model's searchable text fields (identifier columns like `name`, `ref`, `email` first; capped at 5) and ANDs it with any explicit `domain`. Falls back to `name` when field metadata is unavailable. The response reports `query_fields_used`.
- Interactive setup wizard — `odoo-mcp --setup` prompts for connection details, tests the connection, writes an owner-only config file (default `~/.config/odoo/config.json`, auto-discovered by the server), and prints ready-to-paste client snippets (Claude Code, Cursor, Claude Desktop).
- `docs/comparison.md` — an honest feature comparison of Odoo MCP bridges (setup, write safety, transports, multi-instance, diagnostics, testing), linked from the README.

### Changed
- Internal refactor: extracted pure helpers from `server.py` (3,053 → ~2,550 lines) into `tool_helpers.py` (validation, domain normalization, request models, version parsing), `schema_cache.py` (BoundedTTLCache), `access_helpers.py` (ACL/record-rule analysis), and `write_policy.py` (write flags + side-effect policy). All names remain importable from `odoo_mcp.server`; no behavior change.

## [0.7.0] - 2026-06-11

### Added
- OAuth 2.1 resource server for HTTP transports — set `ODOO_MCP_AUTH_ISSUER_URL`, `ODOO_MCP_AUTH_INTROSPECTION_URL`, and `ODOO_MCP_AUTH_RESOURCE_URL` to require bearer tokens on Streamable HTTP. Tokens are validated via RFC 7662 introspection (optional client credentials), with an RFC 8707 audience check when the authorization server binds tokens to resources; RFC 9728 protected-resource metadata is served by the MCP SDK. stdio is unaffected; posture appears in `health_check` as `runtime.oauth`.
- Batch create in the gated write workflow — `preview_write`/`validate_write` accept `values_list` (one dict per record, max 100); execution maps to a single atomic Odoo `create(vals_list)` call and the approval token covers the whole batch. Per-record differing `write` values are deliberately rejected (`values_list_unsupported_operation`) because they would require non-atomic per-record RPC calls.
- Added `read_attachment` tool (tool count 26 → 27) — reads `ir.attachment` metadata plus size-capped base64 content (`ODOO_MCP_MAX_ATTACHMENT_BYTES`, default 1 MiB, hard cap 16 MiB), with a defensive re-check of the actually fetched payload size and URL-type attachment handling.

### Compatibility
- Approval tokens for single-record writes are unchanged; the canonical payload only gains a `values_list` key when batching is used.
- OAuth is opt-in; without `ODOO_MCP_AUTH_*` env vars the HTTP transport behaves exactly as before.

## [0.6.0] - 2026-06-10

### Added
- Framework adapter examples in `examples/` — copy-paste integrations for Cursor (`.cursor/mcp.json` + rules), Claude Code, Codex CLI, OpenAI Agents SDK (local `MCPServerStreamableHttp` + `HostedMCPTool`), LangGraph (`langchain-mcp-adapters>=0.2.2`), CrewAI (native `mcps=[...]`), and an importable n8n workflow using the official MCP Client Tool node. Index with a transport matrix at `examples/README.md`. Python adapters support any OpenAI-compatible provider via `OPENAI_BASE_URL`/`OPENAI_MODEL` and were verified end-to-end against a live Odoo 19 stack (openai-agents 0.17.4, langchain 1.3.6, crewai 1.14.6, n8n 2.25.7 import).
- Audit logging trail — `ODOO_MCP_AUDIT_LOG=<path>` appends one JSONL line per write-path event (`preview`, `validate`, `execute`, `elicit`, `chatter_post`) with model, operation, record IDs, instance, outcome, and a token digest (never the token itself). Fail-open with a warning; posture surfaced in `health_check`.
- Elicitation-based write approval — `ODOO_MCP_ELICIT_WRITES=1` makes `execute_approved_write` ask the human through MCP elicitation (native confirm form showing a diff summary) before executing; clients without elicitation support fall back to the unchanged token flow. Declines are audited.
- Side-effect policy file — reviewed `execute_method` side-effect methods can now live in a version-controllable JSON file (`ODOO_MCP_POLICY_FILE`, default `./odoo_mcp_policy.json` when present; see `odoo_mcp_policy.json.example`) with reviewer metadata, merged with the `ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS` env allowlist. Broken policy files fail closed and surface their error in `health_check`.
- Reliability hardening — read-only Odoo calls retry connection-level failures with exponential backoff (`ODOO_MCP_RETRY_ATTEMPTS`, `ODOO_MCP_RETRY_BACKOFF`; writes never retry); the schema cache is now TTL- and LRU-bounded (`ODOO_MCP_SCHEMA_CACHE_TTL`, `ODOO_MCP_SCHEMA_CACHE_MAX`); `health_check` reports models hit by N+1 `read_record` loops (`runtime.n_plus_one`).
- New guides: `docs/troubleshooting.md` (error-classifier categories → fixes), `docs/multi-instance.md`, `docs/performance.md`.

### Compatibility
- No breaking changes. Tool count stays 26; elicitation, audit logging, and the policy file are all opt-in; retry/caching defaults preserve existing behavior envelopes.

## [0.5.0] - 2026-06-10

### Added
- Added `lookup_model_history` tool — resolves outdated model names against a curated rename catalog (`account.invoice` → `account.move`, `mail.channel` → `discuss.channel`, payment acquirers, analytic tags, chart templates, and more) so agents stop hallucinating pre-rename names. Static catalog shipped at `odoo_mcp/data/odoo_renames.json`; never contacts Odoo.
- Added access-error root-cause classification — `diagnose_access` accepts an `observed_error` argument and `diagnose_odoo_call` reports `error_classification`, mapping Odoo error text to `acl`, `record_rule`, `multi_company`, `authentication`, `db_routing`, or `missing_or_filtered` with a recommended next action.
- Added field-relevance ranking — `get_model_fields` accepts `relevance="top"` and `max_fields` to return only the most business-relevant fields (required/searchable boosted) on wide models like `res.partner`.
- Added `server.json` and a `mcp-registry-publish` release job — the server publishes to the official MCP registry (registry.modelcontextprotocol.io) as `io.github.erpipe-org/mcp-odoo` via GitHub OIDC after each PyPI release.

### Changed
- Updated the XML-RPC/JSON-RPC removal timeline to Odoo 22 (fall 2028) following Odoo's postponement from Odoo 20. `diagnose_odoo_call` with `transport="xmlrpc"` now warns (instead of blocking) for Odoo 19–21 targets and errors only for Odoo 22+; `upgrade_risk_report` marks `json2_required` from Odoo 22. The `ODOO20_RPC_REMOVAL` constant is deprecated in favor of `ODOO_RPC_REMOVAL`.
- README repositioned around version fluency (16 → 22) instead of the obsolete "survives Odoo 20" framing.

### Compatibility
- Tool count surfaced by `health_check` is now 26 (was 25 in v0.4.0).
- `upgrade_risk_report` with `target_version="20.0"`/`"21.0"` now returns `blocked: false` with a `json2_migration` warning instead of a blocking `xmlrpc_jsonrpc_removal` error.

## [0.4.0] - 2026-06-10

### Added
- Multi-instance support — configure several named Odoo instances in one config file via an `instances` map plus a `default` key. Every Odoo-facing tool accepts an optional `instance` parameter (omitted → default instance). Clients connect lazily per instance.
- Added `list_instances` tool — reports configured instance names, URLs, databases, and transports without ever exposing credentials.
- Added `ODOO_CONFIG_FILE` env var — explicit config file path checked before `./odoo_config.json`, `~/.config/odoo/config.json`, and `~/.odoo_config.json`.
- Per-instance `timeout` and `verify_ssl` config keys; global env vars now act as fallback defaults for entries that omit a key.
- `health_check` / `runtime_security_report` now include `odoo_instances` posture (`instance_count`, `default_instance`).
- Added `scripts/odoo_multi_instance_smoke.py` — live Docker Compose smoke for multi-instance: three databases at once, two accounts on one instance, per-instance writes, cross-instance isolation, and token-replay rejection.

### Security
- Write-approval tokens (`preview_write` → `validate_write` → `execute_approved_write`) and `chatter_post` tokens now encode the target instance name. A token validated against one instance can never verify or execute against another. `execute_approved_write` executes on the instance recorded in the approval — there is no instance override at execution time.
- Schema caches (smart-field metadata and `schema_catalog`) are partitioned per instance, so field metadata from one Odoo database is never served for another.
- `execute_approved_write` no longer echoes `expected_token` on a token mismatch — returning the correct token for an arbitrary payload was a token-minting oracle. Re-run `preview_write`/`validate_write` instead.
- Instance config entries never inherit `ODOO_API_KEY` (or any credential) from the environment; a warning is printed when `ODOO_CONFIG_FILE` is set but ignored because all four legacy `ODOO_*` connection env vars are present.

### Compatibility
- No breaking changes. Legacy environment variables (`ODOO_URL`/`ODOO_DB`/`ODOO_USERNAME`/`ODOO_PASSWORD`) and flat `odoo_config.json` files keep working unchanged and still take precedence; they define a single instance named `default`.
- Approval tokens are session-scoped and in-memory, so the token format change requires no migration.
- Tool count surfaced by `health_check` is now 25 (was 24 in v0.3.x).
- MCP resources (`odoo://…`) use the default instance in this release; multi-instance resource URIs are future work.

### Schema Compatibility
- No schema compatibility changes.

## [0.3.1] - 2026-05-21

### Fixed
- Fixed `aggregate_records` on Odoo Online SaaS 19.x by detecting SaaS version metadata such as `saas~19` and routing Odoo 19+ aggregation calls to `formatted_read_group` instead of legacy `read_group`.

## [0.3.0] - 2026-05-04

### Added
- Added `aggregate_records` tool — server-side groupby/aggregation that uses `formatted_read_group` on Odoo 19+ and falls back to `read_group` on earlier versions. Supports `sum`, `avg`, `min`, `max`, `count`, `count_distinct`, `array_agg`, `bool_and`, `bool_or`.
- Added `chatter_post` tool — post messages on `mail.thread` records with two safety modes: default approval-token preview/execute flow, or direct mode via `MCP_CHATTER_DIRECT=1`. Supports `message_type` (`comment`/`notification`), `subtype_xmlid`, `partner_ids`, `attachment_ids`.
- Added smart field selection for `search_records` and `read_record` — when the caller omits `fields`, the server picks a curated subset (business identifiers + state + key relations) and excludes audit, message, activity, binary, and unstored-compute columns. Pass `fields=["*"]` to opt out and fetch every field. Cap configured via `ODOO_MCP_MAX_SMART_FIELDS` (default 15).
- Added `ODOO_LOCALE` plumbing — when set, every Odoo call gets `context.lang` injected automatically. Caller-supplied `context.lang` always wins.
- Added structured logging — `setup_logging()` reads `ODOO_MCP_LOG_LEVEL`, `ODOO_MCP_LOG_JSON`, and `ODOO_MCP_LOG_FILE`. JSON formatter and rotating file handler use the standard library only (no new runtime dependencies).
- Surfaced `chatter_direct_enabled` posture in `runtime_security_report` and `health_check`.
- Extended JSON-2 positional argument mappings for `read_group`, `formatted_read_group`, and `message_post`.

### Changed
- `search_records` and `read_record` responses now include `smart_fields_applied` and `fields_used`.
- Tool count surfaced by `health_check` is now 24 (was 22 in v0.2.0).
- `OdooClient.__init__` accepts an optional `lang` argument that drives the locale-injection pipeline.

### Compatibility
- No breaking changes for existing callers. Default behaviour for `search_records` / `read_record` shifts from "all fields" to "smart subset" — pass `fields=["*"]` to restore the prior behaviour.
- New env vars: `ODOO_LOCALE`, `ODOO_MCP_MAX_SMART_FIELDS`, `ODOO_MCP_LOG_LEVEL`, `ODOO_MCP_LOG_JSON`, `ODOO_MCP_LOG_FILE`, `MCP_CHATTER_DIRECT`.

## [0.2.0] - 2026-04-28

### Added
- Added `diagnose_access` to inspect readable ACL, record-rule, current-user, and count evidence for the current Odoo credential without sudo or impersonation.
- Added exact side-effect method allowlisting with `ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS`.
- Added static addon scanner findings for computed-field `@api.depends` coverage and CRUD override `super()` return contracts.
- Added Docker Compose smoke coverage for a packaged custom addon XML install/update lifecycle and its XML-defined record rule.

### Changed
- Classified common Odoo side-effect methods such as `message_post`, `action_*`, `button_*`, `*_send*`, `*_post*`, and `*_validate*` separately from unknown methods.
- Updated smoke expectations from 21 to 22 MCP tools.

## [0.1.0] - 2026-04-28

### Added
- Added MCP safety annotations and structured output schemas across the tool surface.
- Added Odoo profile and schema catalog tools for bounded live environment discovery.
- Added safe write workflow tools: `preview_write`, `validate_write`, and fail-closed `execute_approved_write`.
- Hardened safe writes so execution requires a same-process `validate_write` approval record, `confirm=true`, and `ODOO_MCP_ENABLE_WRITES=1`.
- Blocked direct `create`/`write`/`unlink` through `execute_method` and blocked unknown side-effect methods unless explicitly trusted with `ODOO_MCP_ALLOW_UNKNOWN_METHODS=1`.
- Restricted explicit addon source scans to configured `ODOO_ADDONS_PATHS` roots.
- Added local addon source scanning, structured domain building, business pack reporting, runtime health, and 5 MCP prompts for agent workflows.

### Changed
- Reworked project documentation for a polished open-source GitHub presentation, including a concise README, client configuration guide, architecture notes, testing guide, contributing guide, security policy, support guide, code of conduct, package metadata URLs, and sdist documentation inclusion.
- Hardened HTTP runtime startup so non-local binds require `--allow-remote-http` or `MCP_ALLOW_REMOTE_HTTP=1`.
- Expanded the Docker Compose smoke harness to require 21 tools, 5 prompts, and live checks for the new agent workflow tools.

## [0.0.4] - 2026-04-28

### Added
- Added a Docker Compose integration smoke harness that boots disposable Odoo 16.0, 17.0, 18.0, and 19.0 stacks and validates live HTTP, XML-RPC, and MCP stdio behavior.
- Added Odoo 19 External JSON-2 transport support behind `ODOO_TRANSPORT=json2`, with bearer API-key authentication through `ODOO_API_KEY`.
- Added Odoo 19 JSON-2 smoke validation that generates a disposable API key and verifies both direct `/json/2` calls and MCP stdio calls.
- Added opt-in MCP Streamable HTTP and SSE runtime support through `--transport`, `--host`, `--port`, and `--path`.
- Added read-only typed tools: `list_models`, `get_model_fields`, `search_records`, and `read_record`.
- Added diagnostic/report tools for agent workflows: `diagnose_odoo_call`, `inspect_model_relationships`, `generate_json2_payload`, `upgrade_risk_report`, and `fit_gap_report`.
- Added `ODOO_JSON2_DATABASE_HEADER` support so JSON-2 calls send `X-Odoo-Database` by default and can opt out for host/dbfilter-routed deployments.
- Added MCP Inspector smoke validation for `tools/list` over stdio and Streamable HTTP.
- Added generic client configuration examples for stdio, Docker stdio, and Streamable HTTP.

### Changed
- Documented the current compatibility posture for the planned 0.0.4 release: XML-RPC remains the default transport, while Odoo's External JSON-2 API is available for Odoo 19.
- Documented JSON-2 named argument mapping, per-call transaction behavior, Odoo-shaped error preservation, and debug redaction defaults.
- Updated README setup guidance for Claude Desktop on macOS, including the expected config file location and the recommendation to use an absolute Python path.
- Updated Docker usage guidance to build and run the local `mcp/odoo:latest` image with the current Odoo environment variables.
- Clarified the current MCP surface as 12 tools and 4 resource URI patterns, matching the implemented server.
- Updated release workflow intent so PyPI publishing stays behind tests, build, and MCP Inspector runtime smoke validation.

### Notes
- Odoo's current 19.0 documentation source says XML-RPC and JSON-RPC endpoints are deprecated and scheduled for removal in Odoo 20 (fall 2026), with External JSON-2 as the replacement.
- JSON-2 is explicit opt-in. XML-RPC carries the database name per request; JSON-2 uses bearer auth and this server sends `X-Odoo-Database` by default for multi-database deployments.

## [0.0.3] - 2025-03-18

### Fixed
- Fixed `OdooClient` class by adding missing methods: `get_models()`, `get_model_info()`, `get_model_fields()`, `search_read()`, and `read_records()`
- Ensured compatibility with different Odoo versions by using only basic fields when retrieving model information

### Added
- Support for retrieving all models from an Odoo instance
- Support for retrieving detailed information about specific models
- Support for searching and reading records with various filtering options

## [0.0.2] - 2025-03-18

### Fixed
- Added missing dependencies in pyproject.toml: `mcp>=0.1.1`, `requests>=2.31.0`, `xmlrpc>=0.4.1`

## [0.0.1] - 2025-03-18

### Added
- Initial release with basic Odoo XML-RPC client support
- MCP Server integration for Odoo
- Command-line interface for quick setup and testing
