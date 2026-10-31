"""Pure helper builders for agent-facing Odoo MCP tools.

This module avoids network, config, and Odoo client side effects. Server
adapters pass live metadata in when they have it.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import time
from importlib import resources
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from .tool_helpers import MODEL_NAME_RE
from .field_ranking import (
    DEFAULT_MAX_QUERY_FIELDS,  # noqa: F401
    DEFAULT_MAX_RELEVANT_FIELDS,  # noqa: F401
    DEFAULT_MAX_SMART_FIELDS,  # noqa: F401
    build_text_query_domain,  # noqa: F401
    rank_relevant_fields,  # noqa: F401
    select_smart_fields,  # noqa: F401
    select_text_query_fields,  # noqa: F401
)

WRITE_OPERATIONS = {"create", "write", "unlink"}
SAFE_DOMAIN_OPERATORS = {
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "in",
    "not in",
    "like",
    "not like",
    "ilike",
    "not ilike",
    "=like",
    "=ilike",
    "child_of",
    "parent_of",
}

BUSINESS_PACKS: dict[str, dict[str, Any]] = {
    "sales": {
        "modules": ["sale", "sale_management", "crm"],
        "models": ["sale.order", "sale.order.line", "res.partner", "product.product"],
        "safe_reports": ["quotation_pipeline", "order_status", "customer_activity"],
    },
    "crm": {
        "modules": ["crm"],
        "models": ["crm.lead", "crm.stage", "res.partner", "mail.activity"],
        "safe_reports": ["pipeline", "lost_reasons", "activity_backlog"],
    },
    "inventory": {
        "modules": ["stock", "product"],
        "models": ["stock.picking", "stock.move", "stock.quant", "product.product"],
        "safe_reports": ["on_hand", "open_transfers", "reordering_attention"],
    },
    "accounting": {
        "modules": ["account"],
        "models": [
            "account.move",
            "account.move.line",
            "account.journal",
            "res.partner",
        ],
        "safe_reports": ["open_invoices", "journal_health", "partner_balances"],
    },
    "hr": {
        "modules": ["hr", "hr_holidays"],
        "models": ["hr.employee", "hr.leave", "hr.leave.report.calendar"],
        "safe_reports": ["employee_lookup", "leave_calendar", "leave_status"],
    },
}


def _normalize_numbers(value: Any) -> Any:
    """Recursively collapse integral floats (``1.0``) to ``int`` (``1``).

    JSON has one number type; Python's ``json`` module does not. A payload
    that crosses a JS/TS transport layer and back can turn an int into a
    float with the same numeric value (``1`` -> ``1.0``), which changes
    ``canonical_json``'s output and therefore the SHA-256 approval token —
    even though the value is unchanged from a business standpoint. Booleans
    are left untouched despite being an ``int`` subclass in Python.
    """
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_json(value: Any) -> str:
    """Return a stable JSON representation for hashing and comparisons."""
    normalized = _normalize_numbers(value)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)


def build_approval_token(payload: dict[str, Any]) -> str:
    """Build a deterministic approval token for a canonical write preview."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"odoo-write:{digest[:32]}"


MAX_WRITE_BATCH_SIZE = 100


def build_write_preview_report(
    *,
    model: str,
    operation: str,
    values: dict[str, Any] | None = None,
    values_list: list[dict[str, Any]] | None = None,
    record_ids: list[int] | None = None,
    context: dict[str, Any] | None = None,
    instance: str = "default",
) -> dict[str, Any]:
    """Build a non-executing preview for standard ORM write operations.

    Batch create: pass ``values_list`` (one dict per record). It maps to
    Odoo's native ``create(vals_list)`` — a single atomic call. Per-record
    differing values for ``write`` are deliberately unsupported: they would
    need one RPC call per record without transactional atomicity.
    """
    normalized_operation = operation.strip().lower()
    issues: list[dict[str, str]] = []
    if normalized_operation not in WRITE_OPERATIONS:
        issues.append(
            {
                "code": "unsupported_write_operation",
                "severity": "error",
                "message": "operation must be one of create, write, or unlink.",
            }
        )

    normalized_values = dict(values or {})
    normalized_values_list = (
        [dict(entry) if isinstance(entry, dict) else entry for entry in values_list]
        if values_list is not None
        else None
    )
    normalized_ids = [int(record_id) for record_id in record_ids or []]
    if normalized_values_list is not None:
        if normalized_operation != "create":
            issues.append(
                {
                    "code": "values_list_unsupported_operation",
                    "severity": "error",
                    "message": (
                        "values_list is only supported for create; per-record "
                        "write values would require non-atomic per-record calls."
                    ),
                }
            )
        if normalized_values:
            issues.append(
                {
                    "code": "values_and_values_list",
                    "severity": "error",
                    "message": "Pass either values or values_list, not both.",
                }
            )
        if not normalized_values_list:
            issues.append(
                {
                    "code": "empty_values_list",
                    "severity": "error",
                    "message": "values_list must contain at least one record.",
                }
            )
        elif len(normalized_values_list) > MAX_WRITE_BATCH_SIZE:
            issues.append(
                {
                    "code": "values_list_too_large",
                    "severity": "error",
                    "message": (
                        f"values_list holds {len(normalized_values_list)} records; "
                        f"the cap is {MAX_WRITE_BATCH_SIZE} per approval."
                    ),
                }
            )
        for index, entry in enumerate(normalized_values_list):
            if not isinstance(entry, dict) or not entry:
                issues.append(
                    {
                        "code": "invalid_values_list_entry",
                        "severity": "error",
                        "message": f"values_list[{index}] must be a non-empty object.",
                    }
                )
    elif normalized_operation == "create" and not normalized_values:
        issues.append(
            {
                "code": "missing_create_values",
                "severity": "error",
                "message": "create requires non-empty values.",
            }
        )
    if normalized_operation in {"write", "unlink"} and not normalized_ids:
        issues.append(
            {
                "code": "missing_record_ids",
                "severity": "error",
                "message": f"{normalized_operation} requires record_ids.",
            }
        )
    if normalized_operation == "write" and not normalized_values:
        issues.append(
            {
                "code": "missing_write_values",
                "severity": "error",
                "message": "write requires non-empty values.",
            }
        )

    canonical_payload = {
        "model": model,
        "operation": normalized_operation,
        "record_ids": normalized_ids,
        "values": normalized_values,
        "context": dict(context or {}),
        "instance": instance or "default",
    }
    if normalized_values_list is not None:
        # Key only present for batches so single-write tokens stay unchanged.
        canonical_payload["values_list"] = normalized_values_list
    approval_token = build_approval_token(canonical_payload)

    return {
        "success": not any(issue["severity"] == "error" for issue in issues),
        "tool": "preview_write",
        "model": model,
        "operation": normalized_operation,
        "approval": {**canonical_payload, "token": approval_token},
        "execute_method": _write_execute_method_args(canonical_payload),
        "issues": issues,
        "warnings": [
            {
                "code": "destructive_operation",
                "message": (
                    "This preview does not execute. execute_approved_write is "
                    "destructive and requires the matching approval token plus confirm=true."
                ),
            }
        ],
        "metadata_used": {"client_instantiated": False},
    }


def verify_write_approval(approval: dict[str, Any]) -> tuple[bool, str]:
    """Verify a write approval token against the canonical payload."""
    token = str(approval.get("token", ""))
    payload = {
        "model": approval.get("model"),
        "operation": approval.get("operation"),
        "record_ids": approval.get("record_ids") or [],
        "values": approval.get("values") or {},
        "context": approval.get("context") or {},
        "instance": approval.get("instance") or "default",
    }
    if approval.get("values_list") is not None:
        payload["values_list"] = approval.get("values_list")
    expected = build_approval_token(payload)
    return token == expected, expected


def _metadata_issues_for_values(
    values: dict[str, Any],
    fields_metadata: dict[str, Any],
    *,
    label: str = "",
    readonly_allowed_fields: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Check one values dict against fields_get metadata."""
    issues: list[dict[str, str]] = []
    hints: list[dict[str, str]] = []
    prefix = f"{label}: " if label else ""
    for field_name in sorted(values):
        meta = fields_metadata.get(field_name)
        if not isinstance(meta, dict):
            issues.append(
                {
                    "code": "unknown_field",
                    "severity": "error",
                    "message": (
                        f"{prefix}{field_name!r} is not present in fields_get metadata."
                    ),
                }
            )
            continue
        field_type = str(meta.get("type", ""))
        if meta.get("readonly") and field_name not in readonly_allowed_fields:
            issues.append(
                {
                    "code": "readonly_field",
                    "severity": "error",
                    "message": (
                        f"{prefix}{field_name!r} is readonly in fields_get metadata."
                    ),
                }
            )
        elif field_type == "many2one":
            hints.append(
                {
                    "field": field_name,
                    "hint": "many2one values should be record IDs.",
                }
            )
        elif field_type in {"many2many", "one2many"}:
            hints.append(
                {
                    "field": field_name,
                    "hint": "relational values should use Odoo command lists.",
                }
            )
    return issues, hints


def is_journal_entry_create(
    model: str,
    operation: str,
    values: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> bool:
    """Identify the narrow ORM create contract; never infer an existing move's state."""
    context = context or {}
    return (
        model == "account.move"
        and operation.strip().lower() == "create"
        and values.get("move_type", context.get("default_move_type", "entry"))
        == "entry"
        and values.get("state", context.get("default_state", "draft")) == "draft"
    )


def _journal_entry_create_issues(
    values: dict[str, Any],
    fields_metadata: dict[str, Any],
    line_fields_metadata: dict[str, Any] | None,
    context: dict[str, Any] | None,
    *,
    label: str,
) -> tuple[list[dict[str, str]], frozenset[str]]:
    """Validate new journal items, without changing the approved command list.

    Odoo's readonly flag describes UI behavior, not ORM create permissions.
    Only these two known fields have an exception. Existing-item commands and
    nested relational mutations are deliberately outside this create contract.
    Balance, currencies, defaults, ACLs and accounting rules remain Odoo's job.
    """
    issues: list[dict[str, str]] = []
    allowed: set[str] = set()
    prefix = f"{label}: " if label else ""

    def reject(code: str, message: str) -> None:
        issues.append({"code": code, "severity": "error", "message": prefix + message})

    # Do not approve a payload that disables the ORM balance check or that
    # inherits lines from context instead of the explicitly validated commands.
    context = context or {}
    if "check_move_validity" in context and context["check_move_validity"] is not True:
        reject(
            "unsafe_journal_entry_context", "check_move_validity must remain enabled."
        )
    if "default_line_ids" in context:
        reject(
            "unsafe_journal_entry_context",
            "Pass line_ids in values, not context defaults.",
        )

    type_meta = fields_metadata.get("move_type")
    if isinstance(type_meta, dict) and type_meta.get("type") == "selection":
        allowed.add("move_type")

    if "line_ids" not in values:
        return issues, frozenset(allowed)

    line_meta = fields_metadata.get("line_ids")
    if not (
        isinstance(line_meta, dict)
        and line_meta.get("type") == "one2many"
        and line_meta.get("relation") == "account.move.line"
    ):
        reject(
            "invalid_journal_line_metadata",
            "line_ids must relate to account.move.line.",
        )
        return issues, frozenset(allowed)
    allowed.add("line_ids")

    if not line_fields_metadata or "error" in line_fields_metadata:
        reject(
            "missing_journal_line_metadata", "account.move.line metadata is required."
        )
        return issues, frozenset(allowed)

    commands = values["line_ids"]
    if not isinstance(commands, list) or not commands:
        reject(
            "invalid_journal_line_command",
            "line_ids must be a non-empty list of [0, 0, values] commands.",
        )
        return issues, frozenset(allowed)

    for index, command in enumerate(commands):
        path = f"line_ids[{index}]"
        if not (
            isinstance(command, list)
            and len(command) == 3
            and type(command[0]) is int
            and command[0] == 0
            and type(command[1]) is int
            and command[1] == 0
            and isinstance(command[2], dict)
            and command[2]
        ):
            reject(
                "invalid_journal_line_command",
                f"{path} must create a new item with [0, 0, values].",
            )
            continue
        line_values = command[2]
        line_issues, _ = _metadata_issues_for_values(
            line_values, line_fields_metadata, label=prefix + path
        )
        issues.extend(line_issues)
        for field_name in line_values:
            meta = line_fields_metadata.get(field_name)
            if field_name == "move_id" or (
                isinstance(meta, dict) and meta.get("type") in {"one2many", "many2many"}
            ):
                reject(
                    "unsupported_journal_line_field",
                    f"{path}.{field_name}: reparenting and nested relational commands are not supported.",
                )
    return issues, frozenset(allowed)


def _attachment_link_issues(
    values: dict[str, Any],
    fields_metadata: dict[str, Any],
    *,
    label: str,
) -> tuple[list[dict[str, str]], frozenset[str]]:
    """Allow the explicit ORM resource link, not arbitrary readonly fields.

    Odoo marks these stored fields UI-readonly but accepts them in create/write.
    Requiring the pair avoids inferring an existing attachment's model or taking
    half of the link from context defaults. Odoo still enforces attachment and
    target-record access at execution; validation does not grant those rights.
    """
    link_fields = frozenset({"res_model", "res_id"})
    if not link_fields.intersection(values):
        return [], frozenset()
    prefix = f"{label}: " if label else ""
    res_model, res_id = values.get("res_model"), values.get("res_id")
    if not (
        isinstance(res_model, str)
        and MODEL_NAME_RE.fullmatch(res_model)
        and type(res_id) is int
        and res_id > 0
    ):
        return [
            {
                "code": "invalid_attachment_link",
                "severity": "error",
                "message": (
                    f"{prefix}Pass res_model (a technical model name) and res_id "
                    "(a positive integer record ID) together in values."
                ),
            }
        ], frozenset()
    for field, types in (
        ("res_model", {"char"}),
        ("res_id", {"many2one_reference", "integer"}),
    ):
        meta = fields_metadata.get(field)
        if not isinstance(meta, dict) or meta.get("type") not in types:
            return [
                {
                    "code": "invalid_attachment_link_metadata",
                    "severity": "error",
                    "message": (
                        f"{prefix}{field} metadata must have type "
                        f"{' or '.join(sorted(types))}."
                    ),
                }
            ], frozenset()
    return [], link_fields


def validate_write_report(
    *,
    model: str,
    operation: str,
    values: dict[str, Any] | None,
    record_ids: list[int] | None,
    values_list: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    fields_metadata: dict[str, Any] | None = None,
    line_fields_metadata: dict[str, Any] | None = None,
    metadata_source: str = "none",
    instance: str = "default",
) -> dict[str, Any]:
    """Validate write payload shape against optional fields_get metadata."""
    preview = build_write_preview_report(
        model=model,
        operation=operation,
        values=values,
        values_list=values_list,
        record_ids=record_ids,
        context=context,
        instance=instance,
    )
    issues: list[dict[str, str]] = list(preview["issues"])
    field_hints: list[dict[str, str]] = []
    normalized_values = dict(values or {})
    if fields_metadata is not None:
        entries = (
            [
                (f"values_list[{index}]", entry)
                for index, entry in enumerate(values_list)
            ]
            if values_list is not None
            else [("", normalized_values)]
        )
        for label, entry in entries:
            if not isinstance(entry, dict):
                continue  # preview already flagged the entry shape
            # Exceptions are per entry and mutually exclusive by model/operation;
            # never share them with another batch member or nested line values.
            readonly_allowed_fields: frozenset[str] = frozenset()
            if is_journal_entry_create(model, operation, entry, context):
                journal_issues, readonly_allowed_fields = _journal_entry_create_issues(
                    entry, fields_metadata, line_fields_metadata, context, label=label
                )
                issues.extend(journal_issues)
            elif model == "ir.attachment" and operation.strip().lower() in {
                "create",
                "write",
            }:
                link_issues, readonly_allowed_fields = _attachment_link_issues(
                    entry, fields_metadata, label=label
                )
                issues.extend(link_issues)
            entry_issues, entry_hints = _metadata_issues_for_values(
                entry,
                fields_metadata,
                label=label,
                readonly_allowed_fields=readonly_allowed_fields,
            )
            issues.extend(entry_issues)
            field_hints.extend(entry_hints)

        if operation == "create" and values_list is None:
            for field_name, raw_meta in sorted(fields_metadata.items()):
                if not isinstance(raw_meta, dict):
                    continue
                if (
                    raw_meta.get("required")
                    and not raw_meta.get("readonly")
                    and not raw_meta.get("compute")
                    and field_name not in normalized_values
                ):
                    field_hints.append(
                        {
                            "field": field_name,
                            "hint": "required on create unless Odoo provides a default.",
                        }
                    )

    success = not any(issue["severity"] == "error" for issue in issues)
    return {
        "success": success,
        "tool": "validate_write",
        "model": model,
        "operation": operation,
        "issues": issues,
        "field_hints": field_hints,
        "approval": preview["approval"] if success else None,
        "metadata_used": {
            "fields_get": fields_metadata is not None,
            "source": metadata_source,
        },
    }


def _write_execute_method_args(payload: dict[str, Any]) -> dict[str, Any]:
    operation = str(payload["operation"])
    context = payload.get("context") or {}
    kwargs = {"context": context} if context else {}
    if operation == "create" and payload.get("values_list") is not None:
        # Odoo's create accepts a vals_list natively — one atomic call.
        args: list[Any] = [payload.get("values_list") or []]
    elif operation == "create":
        args = [payload.get("values") or {}]
    elif operation == "write":
        args = [payload.get("record_ids") or [], payload.get("values") or {}]
    elif operation == "unlink":
        args = [payload.get("record_ids") or []]
    else:
        args = []
    return {
        "model": payload.get("model"),
        "method": operation,
        "args": args,
        "kwargs": kwargs,
    }


def build_domain_report(
    *,
    conditions: list[dict[str, Any]],
    logical_operator: str = "and",
    fields_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate an Odoo domain from structured conditions."""
    issues: list[dict[str, str]] = []
    normalized_conditions: list[list[Any]] = []
    for index, condition in enumerate(conditions):
        field = str(condition.get("field", "")).strip()
        operator = str(condition.get("operator", "")).strip()
        value = condition.get("value")
        if not field:
            issues.append(
                {
                    "code": "missing_field",
                    "severity": "error",
                    "message": f"condition {index} is missing field.",
                }
            )
            continue
        if operator not in SAFE_DOMAIN_OPERATORS:
            issues.append(
                {
                    "code": "invalid_operator",
                    "severity": "error",
                    "message": f"{operator!r} is not an allowed Odoo domain operator.",
                }
            )
            continue
        if fields_metadata is not None and field not in fields_metadata:
            issues.append(
                {
                    "code": "unknown_field",
                    "severity": "error",
                    "message": f"{field!r} is not present in fields_get metadata.",
                }
            )
            continue
        if operator in {"in", "not in"} and not isinstance(value, list):
            issues.append(
                {
                    "code": "operator_requires_list",
                    "severity": "error",
                    "message": f"{operator!r} requires a list value.",
                }
            )
            continue
        normalized_conditions.append([field, operator, value])

    operator_name = logical_operator.strip().lower()
    if operator_name not in {"and", "or"}:
        issues.append(
            {
                "code": "invalid_logical_operator",
                "severity": "error",
                "message": "logical_operator must be 'and' or 'or'.",
            }
        )
    if operator_name == "or" and len(normalized_conditions) > 1:
        domain: list[Any] = ["|"] * (len(normalized_conditions) - 1)
        domain.extend(normalized_conditions)
    else:
        domain = normalized_conditions

    return {
        "success": not any(issue["severity"] == "error" for issue in issues),
        "tool": "build_domain",
        "domain": domain,
        "conditions": normalized_conditions,
        "issues": issues,
        "metadata_used": {"fields_get": fields_metadata is not None},
    }


def scan_addons_source_report(
    *,
    addons_paths: list[str] | None = None,
    max_files: int = 200,
    max_file_bytes: int = 300_000,
) -> dict[str, Any]:
    """Scan local Odoo addon source without importing addon code."""
    paths = _normalize_scan_paths(addons_paths)
    findings: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    scanned_files = 0
    skipped_files = 0

    for root in paths:
        if scanned_files >= max_files:
            break
        root_path = Path(root).expanduser()
        if not root_path.exists():
            findings.append(
                {
                    "code": "addons_path_missing",
                    "severity": "warning",
                    "evidence": str(root_path),
                    "recommendation": "Check ODOO_ADDONS_PATHS or pass addons_paths explicitly.",
                }
            )
            continue
        for file_path in root_path.rglob("*"):
            if scanned_files >= max_files:
                break
            if not file_path.is_file() or file_path.is_symlink():
                continue
            if (
                file_path.suffix not in {".py", ".xml", ".csv"}
                and file_path.name != "__manifest__.py"
            ):
                continue
            try:
                if file_path.stat().st_size > max_file_bytes:
                    skipped_files += 1
                    continue
            except OSError:
                skipped_files += 1
                continue
            scanned_files += 1
            relative = str(file_path)
            if file_path.name == "__manifest__.py":
                manifest = _read_manifest(file_path)
                if manifest:
                    modules.append(manifest)
                    if manifest.get("installable") is False:
                        findings.append(
                            {
                                "code": "non_installable_module",
                                "severity": "info",
                                "evidence": relative,
                                "recommendation": "Confirm whether this module should be considered during upgrade.",
                            }
                        )
            elif file_path.suffix == ".py":
                findings.extend(_scan_python_file(file_path))
            elif file_path.suffix == ".xml":
                findings.extend(_scan_xml_file(file_path))
            elif file_path.suffix == ".csv" and "security" in file_path.parts:
                findings.append(
                    {
                        "code": "security_rule_file",
                        "severity": "info",
                        "evidence": relative,
                        "recommendation": "Review access CSV rules during upgrade testing.",
                    }
                )

    from .diagnostics import annotate_finding_actions

    action_summary = annotate_finding_actions(findings)
    return {
        "success": True,
        "tool": "scan_addons_source",
        "paths": paths,
        "summary": {
            "modules": len(modules),
            "findings": len(findings),
            "scanned_files": scanned_files,
            "skipped_files": skipped_files,
            "max_files_reached": scanned_files >= max_files,
            "actions": action_summary,
        },
        "modules": modules,
        "source_findings": findings,
        "metadata_used": {"source_scan": True},
    }


def business_pack_report(
    *,
    pack: str,
    available_models: list[str] | None = None,
    installed_modules: list[str] | None = None,
) -> dict[str, Any]:
    """Build a read-only business-pack discovery report."""
    pack_key = pack.strip().lower()
    if pack_key not in BUSINESS_PACKS:
        return {
            "success": False,
            "tool": "business_pack_report",
            "error": f"Unknown pack {pack!r}.",
            "available_packs": sorted(BUSINESS_PACKS),
        }
    definition = BUSINESS_PACKS[pack_key]
    model_set = set(available_models or [])
    module_set = set(installed_modules or [])
    expected_models = list(definition["models"])
    expected_modules = list(definition["modules"])
    present_models = [model for model in expected_models if model in model_set]
    missing_models = [model for model in expected_models if model not in model_set]
    present_modules = [module for module in expected_modules if module in module_set]

    has_live_evidence = bool(model_set or module_set)
    return {
        "success": True,
        "tool": "business_pack_report",
        "pack": pack_key,
        "expected_modules": expected_modules,
        "installed_modules": present_modules,
        "expected_models": expected_models,
        "available_models": present_models,
        "missing_models": missing_models if has_live_evidence else [],
        "safe_reports": definition["safe_reports"],
        "recommended_next_calls": [
            {"tool": "list_models", "arguments": {"query": model.split(".")[0]}}
            for model in expected_models[:3]
        ],
        "metadata_used": {
            "models": bool(model_set),
            "modules": bool(module_set),
            "source": "live_or_input" if has_live_evidence else "static_pack",
        },
    }


def _normalize_scan_paths(addons_paths: list[str] | None) -> list[str]:
    if addons_paths:
        return [path for path in addons_paths if path]
    env_value = os.environ.get("ODOO_ADDONS_PATHS", "")
    return [path for path in env_value.split(os.pathsep) if path]


def _read_manifest(file_path: Path) -> dict[str, Any] | None:
    try:
        raw = file_path.read_text(encoding="utf-8")
        parsed = ast.literal_eval(raw)
    except (OSError, SyntaxError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    module_dir = file_path.parent
    return {
        "name": str(parsed.get("name", module_dir.name)),
        "module": module_dir.name,
        "version": str(parsed.get("version", "")),
        "depends": (
            list(parsed.get("depends", []))
            if isinstance(parsed.get("depends", []), list)
            else []
        ),
        "installable": parsed.get("installable", True),
        "path": str(file_path),
        "custom": not str(module_dir.name).startswith(("base", "web", "mail")),
    }


def _scan_python_file(file_path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        raw = file_path.read_text(encoding="utf-8")
        tree = ast.parse(raw)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return [
            {
                "code": "python_parse_error",
                "severity": "warning",
                "evidence": f"{file_path}: {exc}",
                "recommendation": "Inspect this file manually before upgrade.",
            }
        ]

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            inherit_names = [_expr_name(base) for base in node.bases]
            if any(
                name.endswith(("models.Model", "Model", "TransientModel"))
                for name in inherit_names
            ):
                findings.append(
                    {
                        "code": "custom_model_class",
                        "severity": "info",
                        "evidence": f"{file_path}:{node.lineno} class {node.name}",
                        "recommendation": "Review model fields, constraints, computes, and overrides.",
                    }
                )
                findings.extend(_scan_model_class(file_path, node))
        elif isinstance(node, ast.FunctionDef):
            if node.name in {"create", "write", "unlink"} or node.name.startswith(
                "action_"
            ):
                findings.append(
                    {
                        "code": "custom_method",
                        "severity": (
                            "warning"
                            if node.name in {"create", "write", "unlink"}
                            else "info"
                        ),
                        "evidence": f"{file_path}:{node.lineno} def {node.name}",
                        "recommendation": "Review side effects and JSON-2 named-argument compatibility.",
                    }
                )
    if re.search(r"\.sudo\(\)", raw):
        findings.append(
            {
                "code": "sudo_usage",
                "severity": "warning",
                "evidence": str(file_path),
                "recommendation": "Review privilege escalation and record-rule assumptions.",
            }
        )
    return findings


def _scan_model_class(file_path: Path, node: ast.ClassDef) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    computed_fields = _computed_fields_by_method(node)
    functions = {
        item.name: item for item in node.body if isinstance(item, ast.FunctionDef)
    }
    for method_name, field_names in computed_fields.items():
        method_node = functions.get(method_name)
        field_list = ", ".join(sorted(field_names))
        if method_node is None:
            findings.append(
                {
                    "code": "computed_method_missing",
                    "severity": "warning",
                    "evidence": f"{file_path}:{node.lineno} compute={method_name!r}",
                    "recommendation": (
                        f"Define {method_name} or update computed fields: {field_list}."
                    ),
                }
            )
            continue

        depends = _api_depends_arguments(method_node)
        if not depends:
            findings.append(
                {
                    "code": "computed_method_missing_depends",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_name}",
                    "recommendation": (
                        f"Add @api.depends(...) for computed fields: {field_list}."
                    ),
                }
            )
            continue

        read_fields = _record_field_reads(method_node)
        declared_roots = {dependency.split(".", 1)[0] for dependency in depends}
        ignored = set(field_names) | {"env", "id", "ids", "display_name"}
        missing = sorted(read_fields - declared_roots - ignored)
        if missing:
            findings.append(
                {
                    "code": "computed_depends_missing_fields",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_name}",
                    "recommendation": (
                        "Review @api.depends for fields read in the compute method: "
                        + ", ".join(missing)
                        + "."
                    ),
                }
            )

    for method_node in functions.values():
        if method_node.name not in WRITE_OPERATIONS:
            continue
        super_call = _super_method_call(method_node, method_node.name)
        if super_call is None:
            findings.append(
                {
                    "code": "crud_override_missing_super",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_node.name}",
                    "recommendation": (
                        f"Call super().{method_node.name}(...) unless this override "
                        "intentionally replaces the full ORM chain."
                    ),
                }
            )
        elif not _super_call_returned(method_node, method_node.name):
            findings.append(
                {
                    "code": "crud_override_super_not_returned",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_node.name}",
                    "recommendation": (
                        f"Return the result from super().{method_node.name}(...) "
                        "or document why the ORM return contract is intentionally changed."
                    ),
                }
            )
    return findings


def _computed_fields_by_method(node: ast.ClassDef) -> dict[str, set[str]]:
    computed_fields: dict[str, set[str]] = {}
    for statement in node.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        if value is None:
            continue
        compute_method = _field_compute_method(value)
        if compute_method is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                computed_fields.setdefault(compute_method, set()).add(target.id)
    return computed_fields


def _field_compute_method(expr: ast.expr) -> str | None:
    if not isinstance(expr, ast.Call):
        return None
    if not _expr_name(expr.func).startswith("fields."):
        return None
    for keyword in expr.keywords:
        if keyword.arg == "compute":
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, str
            ):
                return keyword.value.value
            return "<dynamic-compute>"
    return None


def _api_depends_arguments(node: ast.FunctionDef) -> set[str]:
    depends: set[str] = set()
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if _expr_name(decorator.func) != "api.depends":
            continue
        for arg in decorator.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                depends.add(arg.value)
    return depends


def _record_field_reads(node: ast.FunctionDef) -> set[str]:
    record_names = {"self"}
    for child in ast.walk(node):
        if (
            isinstance(child, ast.For)
            and isinstance(child.target, ast.Name)
            and isinstance(child.iter, ast.Name)
            and child.iter.id == "self"
        ):
            record_names.add(child.target.id)

    reads: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute) or not isinstance(child.ctx, ast.Load):
            continue
        if isinstance(child.value, ast.Name) and child.value.id in record_names:
            reads.add(child.attr)
    return reads


def _super_method_call(node: ast.FunctionDef, method_name: str) -> ast.Call | None:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _is_super_method_call(child, method_name):
            return child
    return None


def _super_call_returned(node: ast.FunctionDef, method_name: str) -> bool:
    assigned_super_vars: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and _contains_super_method_call(
            child.value, method_name
        ):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    assigned_super_vars.add(target.id)
        elif isinstance(child, ast.AnnAssign) and _contains_super_method_call(
            child.value, method_name
        ):
            if isinstance(child.target, ast.Name):
                assigned_super_vars.add(child.target.id)

    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        if _contains_super_method_call(child.value, method_name):
            return True
        if isinstance(child.value, ast.Name) and child.value.id in assigned_super_vars:
            return True
    return False


def _contains_super_method_call(expr: ast.AST | None, method_name: str) -> bool:
    if expr is None:
        return False
    return any(_is_super_method_call(child, method_name) for child in ast.walk(expr))


def _is_super_method_call(node: ast.AST, method_name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != method_name:
        return False
    value = func.value
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "super"
    )


def _scan_xml_file(file_path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        tree = ElementTree.parse(file_path)
    except (OSError, ElementTree.ParseError) as exc:
        return [
            {
                "code": "xml_parse_error",
                "severity": "warning",
                "evidence": f"{file_path}: {exc}",
                "recommendation": "Inspect this XML file manually before upgrade.",
            }
        ]
    for record in tree.findall(".//record"):
        model = record.attrib.get("model", "")
        if model in {"ir.cron", "base.automation", "ir.actions.server"}:
            findings.append(
                {
                    "code": "automated_action",
                    "severity": "warning",
                    "evidence": f"{file_path}: record model={model}",
                    "recommendation": "Verify automated actions and server actions on staging.",
                }
            )
        elif model.startswith("ir.ui.view"):
            findings.append(
                {
                    "code": "custom_view",
                    "severity": "info",
                    "evidence": f"{file_path}: record model={model}",
                    "recommendation": "Review view inheritance after Odoo upgrades.",
                }
            )
    return findings


def _expr_name(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        value = _expr_name(expr.value)
        return f"{value}.{expr.attr}" if value else expr.attr
    return ""


def token_age_seconds(created_at: float | None) -> float | None:
    """Return token age in seconds for callers that include a timestamp."""
    if created_at is None:
        return None
    return max(0.0, time.time() - created_at)





# Model rename history — static catalog of well-known Odoo model renames and
# removals, so agents stop hallucinating pre-rename names (account.invoice,
# mail.channel, ...) against modern databases.

_RENAME_CATALOG_CACHE: dict[str, Any] | None = None


def load_model_rename_catalog() -> dict[str, Any]:
    """Load the packaged rename catalog (cached after first read)."""
    global _RENAME_CATALOG_CACHE
    if _RENAME_CATALOG_CACHE is None:
        raw = (
            resources.files("odoo_mcp")
            .joinpath("data/odoo_renames.json")
            .read_text(encoding="utf-8")
        )
        _RENAME_CATALOG_CACHE = json.loads(raw)
    return _RENAME_CATALOG_CACHE


def lookup_model_history_report(name: str) -> dict[str, Any]:
    """Look up rename/removal history for a model name (old or new)."""
    catalog = load_model_rename_catalog()
    entries = catalog.get("entries", [])
    normalized = name.strip().lower()
    if not normalized:
        return {
            "success": False,
            "tool": "lookup_model_history",
            "error": "name must be a non-empty model name like 'account.invoice'",
        }

    exact = [
        entry
        for entry in entries
        if normalized in (entry.get("old_model"), entry.get("new_model"))
    ]
    matches = exact
    match_type = "exact" if exact else "none"
    if not exact:
        partial = [
            entry
            for entry in entries
            if normalized in str(entry.get("old_model") or "")
            or normalized in str(entry.get("new_model") or "")
        ]
        if partial:
            matches = partial
            match_type = "partial"

    guidance: list[str] = []
    for entry in matches:
        if entry.get("old_model") == normalized or match_type == "partial":
            if entry.get("new_model"):
                guidance.append(
                    f"{entry['old_model']} was {entry['kind']} in Odoo "
                    f"{entry['changed_in']}; use {entry['new_model']} instead."
                )
            else:
                guidance.append(
                    f"{entry['old_model']} was removed in Odoo "
                    f"{entry['changed_in']}; {entry.get('notes', '')}".strip()
                )
        elif entry.get("new_model") == normalized:
            guidance.append(
                f"{normalized} is the current name; it was previously "
                f"{entry['old_model']} (changed in Odoo {entry['changed_in']})."
            )
    if match_type == "none":
        model_names = {
            model_name
            for entry in entries
            for model_name in (entry.get("old_model"), entry.get("new_model"))
            if model_name
        }
        suggestions = difflib.get_close_matches(
            normalized, model_names, n=5, cutoff=0.65
        )
        guidance.append(
            "No rename history found in the curated catalog. The name may be "
            "current, custom, or missing from the catalog — verify with "
            "list_models or schema_catalog."
        )
        if suggestions:
            guidance.append("Did you mean: " + ", ".join(suggestions) + "?")
    else:
        suggestions = []

    return {
        "success": True,
        "tool": "lookup_model_history",
        "query": name,
        "match_type": match_type,
        "matches": matches,
        "suggestions": suggestions,
        "guidance": guidance,
        "catalog": {
            "catalog_version": catalog.get("catalog_version"),
            "entry_count": len(entries),
            "coverage_note": catalog.get("coverage_note"),
        },
        "metadata_used": {"live_odoo": False, "source": "static_catalog"},
    }
