# Model/method ACL (`model_acl`)

Odoo's own access control works per model and per operation (read, write,
create, delete), plus record rules. It cannot say "this user may read
projects but never call `message_subscribe`". It also cannot say "this user
may read anything, but an AI agent acting for them may only see projects".

`model_acl` is an **opt-in, server-side** policy that decides, for every call
the MCP server makes to Odoo, whether that **model + method** is allowed. You
write it as a default plus rules, in the same policy file as the other
policies.

> Defense in depth, not a replacement for Odoo ACLs. The real boundary is a
> least-privilege Odoo user. This policy narrows what an agent can do
> *through this server*. It does not change what the Odoo credential can do
> elsewhere.

## Quick start

Add a `model_acl` key to `odoo_mcp_policy.json` (or the file named by
`ODOO_MCP_POLICY_FILE`). You can also point `ODOO_MCP_MODEL_POLICY_FILE` at a
dedicated file.

**Block everything except projects:**

```json
{
  "model_acl": {
    "default": {
      "default": "deny",
      "rules": [
        { "allow": "project.*" }
      ]
    }
  }
}
```

**Allow everything except one method:**

```json
{
  "model_acl": {
    "default": {
      "default": "allow",
      "rules": [
        { "deny": "project.project:message_subscribe" }
      ]
    }
  }
}
```

**A realistic mix:**

```json
{
  "model_acl": {
    "default": {
      "default": "deny",
      "rules": [
        { "allow": "project.*:@read" },
        { "allow": "project.task:@write" },
        { "allow": ["project.project:message_subscribe", "project.task:message_post"] },
        { "deny":  "project.project:unlink", "reason": "Projects are never deleted by agents." }
      ]
    }
  }
}
```

No `model_acl` key → **no behavior change**.

## Schema

```text
model_acl
└── <instance name>            "default" = the default instance
    ├── default: "allow" | "deny"        required; used when no rule matches
    ├── rules:   [ { "allow" | "deny": <pattern or list of patterns>,
    │               "reason"?, "reviewed_by"?, "date"?, "comment"? } ]
    └── metadata: "exempt" | "enforce"   optional, default "exempt"
```

- **Per instance.** An instance with no entry is unaffected. Rules for one
  instance never apply to another.
- A malformed policy (bad JSON, unknown keys, a rule with both or neither of
  `allow`/`deny`, an unknown group, an invalid pattern) **fails closed**. The
  server refuses to start, and Odoo calls are refused, instead of running
  unprotected.

### Patterns: `model:method`

| Pattern | Matches |
| --- | --- |
| `project.project:message_subscribe` | exactly that method on that model |
| `project.project:*` or `project.project` | every method on `project.project` |
| `project.*` | every method on every model starting with `project.`, including `project.project.stage` |
| `project.task:@read` | the read group on `project.task` |
| `sale.order:action_*` | any method starting with `action_` on `sale.order` |
| `*:unlink` | `unlink` on every model |

The `:` separator is needed because Odoo model names contain dots
themselves. `project.project.stage` is a real model, so a plain
`project.project.*` could not tell a sub-model from a method. Both sides
accept `*` and `?` wildcards.

### Method groups

| Group | Methods |
| --- | --- |
| `@read` | `search`, `search_read`, `search_count`, `read`, `fields_get`, `name_get`, `name_search`, `context_get`, `read_group`, `formatted_read_group`, `web_search_read`, `web_read`, `web_read_group`, `search_fetch`, `fetch`, `default_get`, `check_access_rights`, `check_access`, `has_access` |
| `@write` | `create`, `write`, `unlink` |

Methods outside these groups (`action_confirm`, `message_subscribe`,
`message_post`, `button_validate`, custom methods, …) must be named
explicitly or matched by a glob or `*`. `@read` deliberately does **not**
include `get_*` methods: custom code can do anything, so allow those by name
if you need them.

### Which rule wins

When several rules match a call, the **most specific** one wins:

1. The rule whose **model pattern** has more literal (non-wildcard)
   characters. `project.task` beats `project.*`, which beats `*`.
2. Then the **method part**: an exact method name beats a glob with letters
   (`action_*`), which beats a group (`@read`), which beats `*`.
3. If two rules are equally specific, **deny wins**.
4. If no rule matches, the instance's `default` applies.

Examples (with `"default": "deny"`):

| Rules | Call | Result |
| --- | --- | --- |
| `allow project.*` · `deny project.project:unlink` | `project.project:unlink` | denied (exact method on exact model) |
| same | `project.task:unlink` | allowed (`project.*`) |
| `deny project.*:@write` · `allow project.task:@write` | `project.task:write` | allowed (more specific model) |
| `deny *:message_subscribe` · `allow project.*` | `project.task:message_subscribe` | **allowed**: the model part is compared first |

The last row is the one to watch. A broad method deny does not override a
more specific model allow. Repeat the deny at the same model level
(`deny project.*:message_subscribe`) when you need it there too.

A refusal message names the rule that matched, for example `matched rule
'deny project.project:unlink'`, so the agent can explain it and you can
debug the policy.

## What it enforces

The check sits in the Odoo client (`OdooClient._execute`), which **every**
call to Odoo goes through. That covers all tools, `odoo://` resources,
plugins, chatter posting, attachments, cross-instance fan-out, background
tasks, and the final step of the approved-write workflow.

Several tools also refuse early with a clearer error (`"acl": {...}` in
the response): `search_records`, `read_record`, `aggregate_records`,
`execute_method`, `chatter_post`, `preview_write`, `validate_write`. A
write the policy denies never gets an approval token.

| Surface | Behavior |
| --- | --- |
| read/search/aggregate tools | Refused if the model's method is denied. |
| `preview_write` / `validate_write` / `execute_approved_write` | Refused for a denied `create`/`write`/`unlink`. |
| `execute_method` | Model ACL is checked first, then the side-effect review (see below). An exact allow rule counts as that review. |
| `list_models`, `schema_catalog`, `odoo://models` | Models where **every** method is denied are hidden. |
| `get_model_fields`, `inspect_model_relationships`, `odoo://model/...` | Refused for hidden models. |
| `health_check` | `runtime.model_acl` reports active state, per-instance default, rule count and metadata mode. It never shows the rules. |

### The server's own metadata lookups

The server needs some Odoo calls for itself: the model list (`ir.model`),
field definitions (`fields_get`), the user context, installed modules, the
Odoo version, and the ACL/record-rule reads behind `diagnose_access`. These
are **exempt by default**, so discovery and health keep working under
`"default": "deny"`. They are exempt only when the server makes them for
itself. An agent querying `ir.model` directly through `search_records` is
still subject to the policy.

Set `"metadata": "enforce"` on an instance to remove the exemption. You then
have to allow the lookups yourself, for example `ir.model:@read` and
`*:fields_get`.

## How it fits with the other gates

`model_acl` is an **extra outer gate**. Every existing gate still applies:

| Layer | Scope | Default |
| --- | --- | --- |
| Odoo user groups, `ir.model.access`, record rules | model × CRUD, per record | whatever the API user may do |
| **`model_acl`** (this page) | instance × model × method | off |
| `ODOO_MCP_ENABLE_WRITES` | global switch for executing writes | off |
| Approval workflow (`preview_write` → `validate_write` → `execute_approved_write`) | every create/write/unlink | always on |
| `execute_method` side-effect review (`allowed_side_effect_methods`, `ODOO_MCP_ALLOW_UNKNOWN_METHODS`) | `action_*`, `button_*`, `*send*`, `*post*`, `*validate*`, unknown methods | blocked unless reviewed |
| `MCP_CHATTER_DIRECT` | chatter posting without approval | off |
| [Field ACL](field-acl.md) (`field_acl`) | instance × model × field, reads only | off |

Allowing `project.task:@write` does not skip the approval workflow or the
write switch.

### Side-effect methods and `execute_method`

`execute_method` can call any Odoo method, but the server only sees the
method's name. Names that look like they change something (`action_*`,
`button_*`, `*_send`, `*_post`, `*_validate`) and names it cannot classify
are **blocked until someone decides they are fine for agents to call**.
`action_confirm` on a quotation, for example, can reserve stock and email
the customer.

That decision is made in one of two ways:

1. **An exact `model_acl` allow rule**, with both the model and the method
   written out: `{ "allow": "sale.order:action_confirm", "reviewed_by": "…",
   "reason": "…" }`. This works under either default.
2. **The `allowed_side_effect_methods` list** (or
   `ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS`), as before. It still works;
   prefer `model_acl` for new policies.

Being allowed in any other way **does not count** as that decision, so the
method stays blocked:

| How the method is allowed | Counts as reviewed? |
| --- | --- |
| `allow sale.order:action_confirm` | yes |
| `allow sale.*:action_confirm` / `allow *:action_confirm` | no: also covers models nobody looked at |
| `allow sale.order:action_*` / `allow sale.order` / a group | no: also covers methods nobody looked at |
| `"default": "allow"` with no matching rule | no: nobody decided anything |

So with `"default": "allow"` the review still protects you. With
`"default": "deny"` you usually name methods exactly anyway, so there is
nothing extra to configure. `ODOO_MCP_ALLOW_UNKNOWN_METHODS=1` still turns
the review off entirely, for trusted deployments only.

## Limits

- **Effects inside Odoo are not separate calls.** Once a call is allowed,
  whatever Odoo does internally is not checked again. `message_post` creates
  `mail.message` records, writes with one2many/many2many commands create or
  link records in other models, and automated actions run server code. Only
  the call the server makes (`project.task:message_post`) is checked.
- **Related and computed fields** can expose data from other models (e.g.
  `partner_id` names on a task). Use [field ACL](field-acl.md) to hide them.
- **Server-side only.** Anyone using the same Odoo credential directly is
  unaffected.
- The policy is loaded once per process. Restart the server after changing it.

## Verifying

```bash
uvx odoo-mcp --health    # runtime.model_acl.active == true
```

Then, from your agent: `list_models` shows only the allowed models, and a
denied read returns an error that names the matching rule.
