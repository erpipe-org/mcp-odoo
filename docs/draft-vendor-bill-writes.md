# Editing draft vendor bills

`validate_write` supports existing `account.move` vendor bills (`in_invoice`)
in `draft` state through the standard approval flow. Live reads use the same
Odoo client and instance as validation and execution.

Odoo's `fields_get.readonly` describes UI behavior, not ORM write permissions.
For verified draft bills the validator relaxes this flag only for `partner_id`,
`invoice_date`, `invoice_date_due`, and `invoice_line_ids`. Unknown fields and
other readonly fields retain their normal validation.

Line edits accept create/update/delete commands (0/1/2). Updates and deletions
must address current invoice lines belonging to the single target bill. Linking,
reparenting, clearing, and replacing entire line collections are unsupported.
Line values are restricted to ordinary invoice inputs and checked against live
`account.move.line` metadata. Taxes may only be replaced using command 6 with
existing tax IDs; nested tax creation or modification is unsupported.

This path requires empty context and rejects changes to `state`, `move_type`,
or `line_ids`. It does not enable invoice creation, customer invoices, refunds,
or posting. Header-only edits may target multiple draft vendor bills.

Execution rechecks current state, line membership and metadata before writing.
These reads and the write are separate RPC calls, not an atomic lock: Odoo's
ACLs, record rules and accounting constraints remain authoritative at write time.
Approval tokens, explicit confirmation and write-enablement gates are unchanged.
