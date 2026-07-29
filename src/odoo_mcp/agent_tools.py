"""Pure helper builders for agent-facing Odoo MCP tools.

This module avoids network, config, and Odoo client side effects. Server
adapters pass live metadata in when they have it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import time
from importlib import resources
from typing import Any

from .addon_scanner import (
    _scan_addons_source_report as _scan_addons_source_report,
    _api_depends_arguments as _api_depends_arguments,
    _computed_fields_by_method as _computed_fields_by_method,
    _contains_super_method_call as _contains_super_method_call,
    _expr_name as _expr_name,
    _field_compute_method as _field_compute_method,
    _is_super_method_call as _is_super_method_call,
    _normalize_scan_paths as _normalize_scan_paths,
    _read_manifest as _read_manifest,
    _record_field_reads as _record_field_reads,
    _scan_model_class as _scan_model_class,
    _scan_python_file as _scan_python_file,
    _scan_xml_file as _scan_xml_file,
    _super_call_returned as _super_call_returned,
    _super_method_call as _super_method_call,
)
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


def scan_addons_source_report(
    *,
    addons_paths: list[str] | None = None,
    max_files: int = 200,
    max_file_bytes: int = 300_000,
) -> dict[str, Any]:
    """Scan addon source while preserving legacy helper monkeypatches."""
    return _scan_addons_source_report(
        paths=_normalize_scan_paths(addons_paths),
        max_files=max_files,
        max_file_bytes=max_file_bytes,
    )


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
        if meta.get("readonly"):
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


def validate_write_report(
    *,
    model: str,
    operation: str,
    values: dict[str, Any] | None,
    record_ids: list[int] | None,
    values_list: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    fields_metadata: dict[str, Any] | None = None,
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
        if values_list is not None:
            for index, entry in enumerate(values_list):
                if not isinstance(entry, dict):
                    continue  # preview already flagged the entry shape
                entry_issues, entry_hints = _metadata_issues_for_values(
                    entry, fields_metadata, label=f"values_list[{index}]"
                )
                issues.extend(entry_issues)
                field_hints.extend(entry_hints)
        else:
            value_issues, value_hints = _metadata_issues_for_values(
                normalized_values, fields_metadata
            )
            issues.extend(value_issues)
            field_hints.extend(value_hints)

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
