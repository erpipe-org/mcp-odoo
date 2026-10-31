# Journal entry writes

Create a manual journal entry through the standard workflow:

1. `preview_write` with `model="account.move"` and `operation="create"`.
2. `validate_write` with the **same payload**, using live metadata (the default).
3. After review, `execute_approved_write` with the returned approval and
   `confirm=true`. The server must have `ODOO_MCP_ENABLE_WRITES=1`.

Example arguments for both preview and validation (replace the date and IDs
with values appropriate to the selected Odoo instance):

```json
{
  "model": "account.move",
  "operation": "create",
  "values": {
    "move_type": "entry",
    "journal_id": 7,
    "date": "2026-01-15",
    "line_ids": [
      [0, 0, {"name": "Tax item A", "account_id": 11, "debit": 10.0}],
      [0, 0, {"name": "Tax item B", "account_id": 11, "debit": 20.0}],
      [0, 0, {"name": "Counterpart", "account_id": 12, "credit": 30.0}]
    ]
  }
}
```

Omit `state`; Odoo creates the entry in draft. `move_type` may be omitted when
the effective default is `entry`. `context.default_move_type` is taken into
account when identifying this operation, as is `context.default_state`. Explicit
values take precedence over context defaults; explicit `state` still undergoes
the normal readonly check. Batch creates via `values_list` use
the same validation for every entry and one atomic Odoo `create(vals_list)` call.

## Validation contract

Odoo's `readonly` field metadata primarily describes the UI. For example, Odoo
16 marks `account.move.line_ids` readonly but editable in draft, and marks
`move_type` readonly even though the ORM accepts it during creation.

The MCP validator has a narrow exception for these two fields when creating a
draft manual journal entry. It does not generally ignore readonly metadata:

- Parent fields must still exist in live `account.move` metadata. `line_ids`
  must be a one2many relation to `account.move.line`.
- `line_ids` must be a non-empty list of `[0, 0, values]` commands. Each command
  creates a new journal item. Updating, linking, deleting, or moving existing
  items is outside this contract.
- Item fields are checked against live `account.move.line` metadata from the
  **same instance**. Unknown and readonly item fields are rejected, as are
  explicit `move_id` values and nested one2many/many2many fields.
- Missing or failed child metadata prevents executable approval. Supplying
  client metadata or disabling live metadata does not authorize execution.
- `context.default_line_ids` and disabling `check_move_validity` are rejected.
- No commands are merged, reordered, or rewritten. Two commands using the same
  account remain distinct throughout preview, validation, and execution.

This is metadata and command-shape validation, **not an Odoo transaction dry
run**. Account/journal compatibility, permissions, company rules, currency
rounding, balance, and other accounting constraints are enforced by Odoo during
execution. A rejected ORM create rolls back the entry and its items together.
Custom addons and Odoo defaults can affect the resulting records; read back the
created move and lines to verify the result.

## Posting and reconciliation

Creation produces a draft, not a posted entry. Posting is a separate operation
through `execute_method` with `model="account.move"`, `method="action_post"`,
and `args=[[move_id]]`. It requires the existing reviewed side-effect policy,
for example the exact allowlist entry `account.move.action_post`. It does not
reuse the create approval token, and creation plus posting are two transactions.
Read the move back and confirm `state="posted"`.

For JSON-2, use `kwargs={"ids": [move_id]}` instead of positional `args` for
`action_post`. Custom method positional arguments are not automatically mapped
by the existing JSON-2 transport. The same named `ids` convention applies to
`account.move.line.reconcile`.

Reconciliation requires posted, eligible, unreconciled items on compatible
accounts. Preserve separate items on the account that will actually be
reconciled: separate expense/tax items do not split an aggregated counterpart.
The UI and accounting checks remain Odoo's responsibility. Some versions commit
reconciliation but cannot serialize its returned recordsets over XML-RPC. Read
the items to verify the outcome before considering another call; do not
automatically retry a serialization failure. This creation fix does not change
that separate transport limitation.

## Verification

Unit and tool-boundary regression tests:

```bash
uv run python -m pytest tests/test_journal_entry_write.py
```

Live test using the repository's Docker Compose fixture, Python MCP SDK over
stdio, and XML-RPC to disposable Odoo 16-19 databases, plus JSON-2 on Odoo 19:

```bash
uv run --python 3.12 --with-editable . scripts/odoo_journal_entry_smoke.py \
  --versions 16.0 17.0 18.0 19.0 --timeout 360
```

The script creates a uniquely named Compose project, initializes `base,account`,
checks single and batch preview -> validation -> execution with live parent and
child metadata, unchanged canonical payloads and tokens, distinct same-account
items, and posting of every created move. An unbalanced second batch member
must roll back the entire batch and all its items. Reconciliation of one item
must leave the other open; another entry in the batch also remains untouched.
Negative field/command/context checks, missing-validation, confirmation and
tamper gates, and the direct-create prohibition are exercised live.
Containers and their volumes are removed on exit. It uses no
existing Odoo database. As with the other Compose smoke tests, the Odoo container
user needs read/traverse access to `tests/fixtures/odoo_addons`.

The upstream general smoke complements this with read tools, disabled writes,
restricted-user access, record rules, addon install/update, MCP Inspector, and
Odoo 19 JSON-2/Streamable HTTP:

```bash
uv run --python 3.12 --with-editable . scripts/odoo_compose_smoke.py \
  --versions 16.0 17.0 18.0 19.0 --timeout 360 --inspector-smoke
```

Check its fixed Compose project names and ports for collisions before running.

### Verified compatibility

Verified on 2026-10-05 with the official disposable Odoo Community Docker images:

| Odoo | Odoo transport | MCP transport | Single create | Batch create |
| --- | --- | --- | --- | --- |
| 16.0 | XML-RPC | stdio | Passed | Passed |
| 17.0 | XML-RPC | stdio | Passed | Passed |
| 18.0 | XML-RPC | stdio | Passed | Passed |
| 19.0 | XML-RPC | stdio | Passed | Passed |
| 19.0 | JSON-2 | stdio | Passed | Passed |

Every accounting scenario includes posting/readback, rollback on imbalance,
independent reconciliation, and negative validation/execution checks. In these
images, `line_ids` is UI-readonly on Odoo 16 and writable on Odoo 17-19;
`move_type` is UI-readonly on all four versions. Child validation remains required
regardless of the parent's readonly flag.

The general smoke also passed on all four versions, including Odoo 19 JSON-2
over stdio and Streamable HTTP, plus MCP Inspector checks. The accounting flow
itself was tested over stdio. Enterprise modules, custom addons, and additional
localizations are not covered by this matrix. Odoo 16 reconciliation's XML-RPC
response failed serialization, but readback confirmed the committed outcome in
both single and batch scenarios; it was not retried.
