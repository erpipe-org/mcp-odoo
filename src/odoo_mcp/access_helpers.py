"""Pure helpers behind the diagnose_access tool (ACL/record-rule analysis)."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .diagnostics import classify_method_safety, sanitize_odoo_error
from .model_policy import metadata_scope


def access_permission_field(operation: str) -> str:
    """Map an Odoo operation or method name to the closest ACL permission flag."""
    normalized = operation.strip().lower()
    if normalized in {"create"}:
        return "perm_create"
    if normalized in {"write"}:
        return "perm_write"
    if normalized in {"unlink", "delete"}:
        return "perm_unlink"
    if normalized in {"read", "search", "search_read", "search_count", "name_search"}:
        return "perm_read"
    safety = classify_method_safety(normalized)
    if safety["safety"] in {"side_effect", "unknown"}:
        return "perm_write"
    return "perm_read"


def _safe_odoo_read(
    label: str, callback: Callable[[], Any]
) -> tuple[Any, Dict[str, Any] | None]:
    """Run a read-only Odoo metadata call and normalize failure shape."""
    try:
        return callback(), None
    except Exception as exc:
        return None, {
            "stage": label,
            "error": sanitize_odoo_error(str(exc)),
        }


def _safe_metadata_read(
    label: str, callback: Callable[[], Any]
) -> tuple[Any, Dict[str, Any] | None]:
    """Like ``_safe_odoo_read`` for server-internal metadata (model ACL exempt)."""

    def run() -> Any:
        with metadata_scope():
            return callback()

    return _safe_odoo_read(label, run)


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, list) and value and isinstance(value[0], int):
        return int(value[0])
    if isinstance(value, tuple) and value and isinstance(value[0], int):
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


def _m2m_ids(value: Any) -> set[int]:
    if not isinstance(value, list):
        return set()
    result: set[int] = set()
    for item in value:
        if isinstance(item, int):
            result.add(item)
        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], int):
            result.add(int(item[0]))
    return result


def _field_names(metadata: Any) -> set[str]:
    if not isinstance(metadata, dict):
        return set()
    return {str(name) for name in metadata.keys()}


def _available_user_read_fields(available_fields: set[str]) -> list[str]:
    base_candidates = ["id", "name", "company_id", "company_ids"]
    group_candidates = ["groups_id", "group_ids", "all_group_ids"]
    if not available_fields:
        return base_candidates
    return [
        field_name
        for field_name in base_candidates + group_candidates
        if field_name in available_fields
    ]


def _group_field_names(record: Dict[str, Any]) -> tuple[str | None, str | None]:
    direct_group_field = None
    for field_name in ("groups_id", "group_ids"):
        if field_name in record:
            direct_group_field = field_name
            break
    all_group_field = "all_group_ids" if "all_group_ids" in record else None
    return direct_group_field, all_group_field


def _acl_row_applies(row: Dict[str, Any], user_group_ids: set[int] | None) -> bool:
    group_id = _m2o_id(row.get("group_id"))
    if group_id is None:
        return True
    return user_group_ids is not None and group_id in user_group_ids


def _rule_applies(row: Dict[str, Any], user_group_ids: set[int] | None) -> bool:
    group_ids = _m2m_ids(row.get("groups"))
    if not group_ids:
        return True
    return user_group_ids is not None and bool(group_ids & user_group_ids)


def _record_id_domain(record_ids: Optional[List[int]]) -> List[Any]:
    ids = [int(record_id) for record_id in record_ids or [] if int(record_id) > 0]
    return [["id", "in", ids]] if ids else []


def _access_diagnosis_codes(
    *,
    metadata_errors: list[Dict[str, Any]],
    acl_rows: list[Dict[str, Any]],
    granting_acl_rows: list[Dict[str, Any]],
    active_rules: list[Dict[str, Any]],
    applicable_rules: list[Dict[str, Any]],
    actual_count: int | None,
    expected_count: int | None,
    record_ids: list[int],
) -> list[Dict[str, str]]:
    codes: list[Dict[str, str]] = []
    if metadata_errors:
        codes.append(
            {
                "code": "metadata_access_unavailable",
                "severity": "warning",
                "message": "Some ACL, rule, user, or count metadata could not be read.",
            }
        )
    if acl_rows and not granting_acl_rows:
        codes.append(
            {
                "code": "acl_denied_likely",
                "severity": "warning",
                "message": "No readable ACL row appears to grant the requested operation.",
            }
        )

    mismatch = False
    if expected_count is not None and actual_count is not None:
        mismatch = actual_count < expected_count
    if record_ids and actual_count is not None:
        mismatch = mismatch or actual_count < len(record_ids)
    if mismatch:
        if applicable_rules or active_rules:
            codes.append(
                {
                    "code": "record_rule_filter_likely",
                    "severity": "warning",
                    "message": "Visible record count is lower than expected and active record rules exist.",
                }
            )
        else:
            codes.append(
                {
                    "code": "domain_or_rule_filter_likely",
                    "severity": "warning",
                    "message": "Visible record count is lower than expected; inspect domain and access context.",
                }
            )
    if not codes:
        codes.append(
            {
                "code": "no_access_issue_detected",
                "severity": "info",
                "message": "No obvious ACL or record-rule mismatch was detected from readable metadata.",
            }
        )
    return codes
