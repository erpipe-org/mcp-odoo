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
account when identifying this operation. Batch creates via `values_list` use
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

Reconciliation requires posted, eligible, unreconciled items on compatible
accounts. Preserve separate items on the account that will actually be
reconciled: separate expense/tax items do not split an aggregated counterpart.
The UI and accounting checks remain Odoo's responsibility.

## Verification

Unit and tool-boundary regression tests:

```bash
uv run python -m pytest tests/test_journal_entry_write.py
```

Live test using the repository's Docker Compose fixture, Python MCP SDK over
stdio, and XML-RPC to a disposable Odoo database:

```bash
uv run --python 3.12 --with-editable . scripts/odoo_journal_entry_smoke.py \
  --versions 16.0 --timeout 360
```

The script creates a uniquely named Compose project, initializes `base,account`,
checks preview/validation/execution, distinct same-account items, posting,
rollback of an unbalanced entry, and reconciliation of one item while the other
remains open. Containers and their volumes are removed on exit. It uses no
existing Odoo database. As with the other Compose smoke tests, the Odoo container
user needs read/traverse access to `tests/fixtures/odoo_addons`.
