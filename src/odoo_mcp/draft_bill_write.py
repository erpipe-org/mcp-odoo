"""Narrow, live-verified UI-readonly exceptions for existing vendor bills."""

from copy import deepcopy
from typing import Any

BILL_FIELDS = frozenset(
    {"partner_id", "invoice_date", "invoice_date_due", "invoice_line_ids"}
)
LINE_FIELDS = frozenset(
    {
        "name",
        "product_id",
        "product_uom_id",
        "quantity",
        "price_unit",
        "discount",
        "account_id",
        "tax_ids",
        "analytic_distribution",
    }
)


def is_draft_bill_write(model: str, operation: str, values: dict) -> bool:
    return (
        model == "account.move"
        and operation.strip().lower() == "write"
        and bool(BILL_FIELDS.intersection(values))
    )


def _positive_id(value: Any) -> bool:
    return type(value) is int and value > 0


def draft_bill_metadata(
    client: Any, record_ids: list, values: dict, context: dict, metadata: dict
) -> dict:
    """Read current records; never infer state from client payload or context.

    Returned metadata is a private copy. Only explicitly supported UI fields
    are relaxed; Odoo remains responsible for ACLs and accounting invariants.
    """
    if not record_ids or not all(_positive_id(i) for i in record_ids):
        raise ValueError("Draft bill writes require positive record IDs")
    if context:
        # Context can bypass accounting checks or alter company/default behavior.
        raise ValueError("Draft bill writes currently require an empty context")
    if {"state", "move_type", "line_ids"}.intersection(values):
        raise ValueError("Draft bill writes cannot change state, move_type or line_ids")
    rows = client.execute_method(
        "account.move",
        "read",
        record_ids,
        fields=["state", "move_type", "invoice_line_ids"],
    )
    if not isinstance(rows, list) or {r["id"] for r in rows} != set(record_ids):
        raise ValueError("Could not read every target bill")
    if any(
        r.get("state") != "draft" or r.get("move_type") != "in_invoice" for r in rows
    ):
        raise ValueError("Every target must be an existing draft vendor bill")
    result = deepcopy(metadata)
    for field in BILL_FIELDS.intersection(values):
        if not isinstance(result.get(field), dict):
            raise ValueError(f"Missing trusted metadata for {field}")
        result[field]["readonly"] = False
    if "invoice_line_ids" not in values:
        return result
    if len(record_ids) != 1:
        raise ValueError("Invoice line edits require exactly one bill")
    if (
        result["invoice_line_ids"].get("type") != "one2many"
        or result["invoice_line_ids"].get("relation") != "account.move.line"
    ):
        raise ValueError("Unexpected invoice_line_ids relationship")
    commands = values["invoice_line_ids"]
    if not isinstance(commands, list) or not commands:
        raise ValueError("Invoice lines require a nonempty command list")
    line_meta = client.get_model_fields("account.move.line")
    if not isinstance(line_meta, dict) or not line_meta or "error" in line_meta:
        raise ValueError("Missing trusted invoice line metadata")
    existing = set(rows[0].get("invoice_line_ids", []))
    touched = set()
    for index, command in enumerate(commands):
        path = f"invoice_line_ids[{index}]"
        if (
            not isinstance(command, (list, tuple))
            or len(command) != 3
            or type(command[0]) is not int
        ):
            raise ValueError(f"{path}: invalid command")
        op, line_id, data = command
        if op not in (0, 1, 2):
            raise ValueError(f"{path}: only create, update and delete are supported")
        if op == 0:
            if type(line_id) is not int or line_id != 0:
                raise ValueError(f"{path}: create requires ID zero")
        else:
            if (
                not _positive_id(line_id)
                or line_id not in existing
                or line_id in touched
            ):
                raise ValueError(
                    f"{path}: line must belong to this bill and occur once"
                )
            touched.add(line_id)
        if op == 2:
            if type(data) is not int or data != 0:
                raise ValueError(f"{path}: delete requires zero payload")
            continue
        if not isinstance(data, dict) or not data:
            raise ValueError(f"{path}: expected nonempty line values")
        for field, value in data.items():
            meta = line_meta.get(field)
            if field not in LINE_FIELDS or not isinstance(meta, dict):
                raise ValueError(f"{path}.{field}: unsupported invoice line field")
            if meta.get("readonly"):
                raise ValueError(f"{path}.{field}: readonly invoice line field")
            if meta.get("type") in {"one2many", "many2many"}:
                if field != "tax_ids" or meta.get("relation") != "account.tax":
                    raise ValueError(
                        f"{path}.{field}: nested mutations are unsupported"
                    )
                if not isinstance(value, list):
                    raise ValueError(f"{path}.tax_ids: expected command list")
                for tax in value:
                    if (
                        not isinstance(tax, (list, tuple))
                        or len(tax) != 3
                        or type(tax[0]) is not int
                        or tax[0] != 6
                        or type(tax[1]) is not int
                        or tax[1] != 0
                        or not isinstance(tax[2], list)
                        or not all(_positive_id(i) for i in tax[2])
                    ):
                        raise ValueError(
                            f"{path}.tax_ids: only replacement with existing tax IDs is supported"
                        )
    return result
