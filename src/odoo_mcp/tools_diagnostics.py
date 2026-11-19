"""
MCP tools: diagnostics domain.

Includes: diagnose_odoo_call, generate_json2_payload, inspect_model_relationships,
diagnose_access, upgrade_risk_report, lookup_model_history, fit_gap_report,
scan_addons_source, build_domain, business_pack_report.
"""

from typing import Annotated, Any, Dict, List, Optional

from mcp.server.mcpserver import Context
from pydantic import Field

from .access_helpers import (
    _access_diagnosis_codes,
    _acl_row_applies,
    _available_user_read_fields,
    _field_names,
    _group_field_names,
    _m2m_ids,
    _record_id_domain,
    _rule_applies,
    _safe_metadata_read,
    _safe_odoo_read,
    access_permission_field,
)
from .agent_tools import (
    business_pack_report as build_business_pack_report,
    lookup_model_history_report,
    scan_addons_source_report,
)
from .diagnostics import (
    analyze_upgrade_log_report,
    classify_access_error,
    diagnose_odoo_call_report,
    generate_json2_payload_report,
    inspect_model_relationships_report,
    sanitize_odoo_error,
)
from .diagnostics import fit_gap_report as build_fit_gap_report
from .diagnostics import upgrade_risk_report as build_upgrade_risk_report
from .model_policy import get_model_policy
from .schemas import BuildDomainResponse
from .tool_helpers import (
    clamp_limit,
    normalize_domain_input,
    validate_model_name,
)
from .server_core import (
    PREVIEW_TOOL,
    READ_ONLY_TOOL,
    mcp,
    _resolve_odoo,
    restrict_addons_paths,
)


def _srv() -> Any:
    """Late import of server module to resolve patchable symbols at call time."""
    from . import server
    return server


@mcp.tool(
    description="Diagnose an Odoo model call without executing it",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def diagnose_odoo_call(
    model: Annotated[str, Field(description="Technical Odoo model name to diagnose.")],
    method: Annotated[str, Field(description="Odoo model method name to diagnose.")],
    args: Annotated[
        Optional[List[Any]], Field(description="Optional positional call arguments.")
    ] = None,
    kwargs: Annotated[
        Optional[Dict[str, Any]], Field(description="Optional keyword call arguments.")
    ] = None,
    transport: Annotated[
        str, Field(description="Transport to assess, such as 'auto', 'xmlrpc', or 'json2'.")
    ] = "auto",
    target_version: Annotated[
        Optional[str], Field(description="Optional target Odoo version for compatibility checks.")
    ] = None,
    observed_error: Annotated[
        Optional[Any], Field(description="Optional error text or structured error to classify.")
    ] = None,
    include_debug: Annotated[
        bool, Field(description="Whether to include additional diagnostic details.")
    ] = False,
    metadata: Annotated[
        Optional[Dict[str, Any]],
        Field(description="Optional model or method metadata used by the diagnosis."),
    ] = None,
    use_live_metadata: Annotated[
        bool,
        Field(
            description="Whether to request live metadata; this preview tool does not fetch it."
        ),
    ] = False,
) -> Dict[str, Any]:
    """Diagnose model/method/payload issues without executing the candidate call."""
    report = diagnose_odoo_call_report(
        model=model,
        method=method,
        args=args,
        kwargs=kwargs,
        transport=transport,
        target_version=target_version,
        observed_error=observed_error,
        include_debug=include_debug,
        metadata=metadata,
    )
    if use_live_metadata:
        report["issues"].append(
            {
                "code": "live_metadata_not_used",
                "severity": "info",
                "message": (
                    "diagnose_odoo_call is preview-only; pass metadata explicitly "
                    "or use inspect_model_relationships for live fields_get metadata."
                ),
            }
        )
    return report


@mcp.tool(
    description="Build a JSON-2 request preview without network access",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def generate_json2_payload(
    model: str,
    method: str,
    args: Optional[List[Any]] = None,
    kwargs: Optional[Dict[str, Any]] = None,
    base_url: Optional[str] = None,
    database: Optional[str] = None,
    include_database_header: bool = True,
) -> Dict[str, Any]:
    """Generate a JSON-2 endpoint, headers, and named JSON body."""
    return generate_json2_payload_report(
        model=model,
        method=method,
        args=args,
        kwargs=kwargs,
        base_url=base_url,
        database=database,
        include_database_header=include_database_header,
    )


@mcp.tool(
    description="Inspect model relationships and required field metadata",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def inspect_model_relationships(
    ctx: Context,
    model: str,
    fields_metadata: Optional[Dict[str, Any]] = None,
    include_readonly: bool = True,
    include_computed: bool = True,
    use_live_metadata: bool = True,
    instance: Optional[str] = None,
) -> Dict[str, Any]:
    """Summarize relationship fields using provided metadata or bounded fields_get."""
    try:
        validate_model_name(model)
        metadata_source = "input" if fields_metadata is not None else "none"
        metadata_error = None
        if fields_metadata is None and use_live_metadata:
            metadata_source = "server"
            try:
                instance_name, odoo = _resolve_odoo(ctx, instance)
                acl_refusal = get_model_policy().visibility_refusal(
                    instance_name, model, tool="inspect_model_relationships"
                )
                if acl_refusal is not None:
                    return acl_refusal
                fields_metadata = odoo.get_model_fields(model)
                if "error" in fields_metadata:
                    metadata_error = str(fields_metadata["error"])
                    fields_metadata = None
            except Exception as exc:
                metadata_error = str(exc)
                fields_metadata = None
        return inspect_model_relationships_report(
            model=model,
            fields_metadata=fields_metadata,
            metadata_source=metadata_source,
            metadata_error=metadata_error,
            include_readonly=include_readonly,
            include_computed=include_computed,
        )
    except Exception as e:
        return {
            "success": False,
            "tool": "inspect_model_relationships",
            "error": str(e),
        }


@mcp.tool(
    description="Diagnose ACL and record-rule visibility for an Odoo model",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def diagnose_access(
    ctx: Context,
    model: Annotated[str, Field(description="Technical Odoo model name to inspect.")],
    operation: Annotated[
        str, Field(description="Access operation to diagnose, such as read or write.")
    ] = "read",
    domain: Annotated[
        Optional[Any], Field(description="Optional Odoo domain used for the visibility check.")
    ] = None,
    record_ids: Annotated[
        Optional[List[int]], Field(description="Optional record IDs to check directly.")
    ] = None,
    expected_count: Annotated[
        Optional[int],
        Field(description="Optional expected visible record count for comparison."),
    ] = None,
    include_rules: Annotated[
        bool, Field(description="Whether to include matching record-rule metadata.")
    ] = True,
    observed_error: Annotated[
        Optional[Any], Field(description="Optional error text or structured error to classify.")
    ] = None,
    limit: Annotated[
        int, Field(description="Maximum metadata rows to inspect; capped at 500.")
    ] = 50,
    instance: Annotated[
        Optional[str],
        Field(description="Optional configured Odoo instance name; uses the default if omitted."),
    ] = None,
) -> Dict[str, Any]:
    """
    Inspect readable ACL/rule metadata for the current Odoo credential.

    This tool never uses sudo, never impersonates another user, and only performs
    read-only metadata/count calls. Pass the failing call's error text or JSON
    as ``observed_error`` to get a root-cause classification (ACL vs record
    rule vs multi-company vs routing).
    """
    try:
        validate_model_name(model)
        limit = clamp_limit(limit, maximum=500)
        if expected_count is not None and expected_count < 0:
            raise ValueError("expected_count must be greater than or equal to 0")
        normalized_record_ids = [
            int(record_id) for record_id in record_ids or [] if int(record_id) > 0
        ]
        permission_field = access_permission_field(operation)
        normalized_domain = normalize_domain_input(domain)
        count_domain = (
            _record_id_domain(normalized_record_ids)
            if normalized_record_ids
            else normalized_domain
        )

        _, odoo = _resolve_odoo(ctx, instance)
        metadata_errors: list[Dict[str, Any]] = []

        model_rows, error = _safe_metadata_read(
            "ir.model",
            lambda: odoo.execute_method(
                "ir.model",
                "search_read",
                [["model", "=", model]],
                fields=["id", "name", "model"],
                limit=1,
            ),
        )
        if error:
            metadata_errors.append(error)
            model_rows = []
        model_record = (
            model_rows[0] if isinstance(model_rows, list) and model_rows else None
        )
        model_id = (
            int(model_record["id"])
            if isinstance(model_record, dict) and model_record.get("id")
            else None
        )
        if model_id is None:
            metadata_errors.append(
                {
                    "stage": "ir.model",
                    "error": {"message": f"Model metadata not found for {model}."},
                }
            )

        user_context, error = _safe_metadata_read(
            "res.users.context_get",
            lambda: (
                odoo.get_user_context()
                if hasattr(odoo, "get_user_context")
                else odoo.execute_method("res.users", "context_get")
            ),
        )
        if error:
            metadata_errors.append(error)
            user_context = {}
        if isinstance(user_context, dict) and user_context.get("error"):
            metadata_errors.append(
                {
                    "stage": "res.users.context_get",
                    "error": sanitize_odoo_error(str(user_context["error"])),
                }
            )
            user_context = {}

        uid = getattr(odoo, "uid", None)
        if uid is None and isinstance(user_context, dict):
            uid = user_context.get("uid")
        current_user: Dict[str, Any] = {
            "uid": uid,
            "context": user_context if isinstance(user_context, dict) else {},
            "record": None,
            "group_ids": None,
            "direct_group_ids": None,
            "group_field": None,
            "all_group_field": None,
        }
        user_group_ids: set[int] | None = None
        if isinstance(uid, int) and uid > 0:
            user_fields, error = _safe_metadata_read(
                "res.users.fields_get",
                lambda: odoo.execute_method(
                    "res.users",
                    "fields_get",
                    [],
                    attributes=["type", "relation", "string"],
                ),
            )
            if error:
                metadata_errors.append(error)
            available_user_fields = _field_names(user_fields)
            user_rows, error = _safe_metadata_read(
                "res.users.read",
                lambda: odoo.execute_method(
                    "res.users",
                    "read",
                    [uid],
                    fields=_available_user_read_fields(available_user_fields),
                ),
            )
            if error:
                metadata_errors.append(error)
            elif isinstance(user_rows, list) and user_rows:
                current_user["record"] = user_rows[0]
                direct_group_field, all_group_field = _group_field_names(user_rows[0])
                current_user["group_field"] = direct_group_field
                current_user["all_group_field"] = all_group_field
                direct_group_ids = (
                    _m2m_ids(user_rows[0].get(direct_group_field))
                    if direct_group_field
                    else set()
                )
                all_group_ids = (
                    _m2m_ids(user_rows[0].get(all_group_field))
                    if all_group_field
                    else set()
                )
                user_group_ids = all_group_ids or direct_group_ids
                current_user["group_ids"] = sorted(user_group_ids)
                current_user["direct_group_ids"] = sorted(direct_group_ids)

        acl_rows: list[Dict[str, Any]] = []
        if model_id is not None:
            acl_rows_raw, error = _safe_metadata_read(
                "ir.model.access",
                lambda: odoo.execute_method(
                    "ir.model.access",
                    "search_read",
                    [["model_id", "=", model_id]],
                    fields=[
                        "id",
                        "name",
                        "model_id",
                        "group_id",
                        "perm_read",
                        "perm_write",
                        "perm_create",
                        "perm_unlink",
                    ],
                    limit=limit,
                ),
            )
            if error:
                metadata_errors.append(error)
            elif isinstance(acl_rows_raw, list):
                acl_rows = [row for row in acl_rows_raw if isinstance(row, dict)]

        active_rules: list[Dict[str, Any]] = []
        global_rules: list[Dict[str, Any]] = []
        group_bound_rules: list[Dict[str, Any]] = []
        applicable_rules: list[Dict[str, Any]] = []
        if include_rules and model_id is not None:
            rules_raw, error = _safe_metadata_read(
                "ir.rule",
                lambda: odoo.execute_method(
                    "ir.rule",
                    "search_read",
                    [["model_id", "=", model_id]],
                    fields=[
                        "id",
                        "name",
                        "model_id",
                        "domain_force",
                        "groups",
                        "active",
                        "perm_read",
                        "perm_write",
                        "perm_create",
                        "perm_unlink",
                    ],
                    limit=limit,
                ),
            )
            if error:
                metadata_errors.append(error)
            elif isinstance(rules_raw, list):
                for rule in rules_raw:
                    if not isinstance(rule, dict):
                        continue
                    if not rule.get("active", True) or not rule.get(
                        permission_field, True
                    ):
                        continue
                    active_rules.append(rule)
                    if _m2m_ids(rule.get("groups")):
                        group_bound_rules.append(rule)
                    else:
                        global_rules.append(rule)
                    if _rule_applies(rule, user_group_ids):
                        applicable_rules.append(rule)

        actual_count: int | None = None
        if expected_count is not None or normalized_record_ids:
            count_value, error = _safe_odoo_read(
                f"{model}.search_count",
                lambda: odoo.execute_method(model, "search_count", count_domain),
            )
            if error:
                metadata_errors.append(error)
            elif isinstance(count_value, int):
                actual_count = count_value

        granting_acl_rows = [
            row
            for row in acl_rows
            if bool(row.get(permission_field)) and _acl_row_applies(row, user_group_ids)
        ]
        diagnosis_codes = _access_diagnosis_codes(
            metadata_errors=metadata_errors,
            acl_rows=acl_rows,
            granting_acl_rows=granting_acl_rows,
            active_rules=active_rules,
            applicable_rules=applicable_rules,
            actual_count=actual_count,
            expected_count=expected_count,
            record_ids=normalized_record_ids,
        )
        return {
            "success": True,
            "tool": "diagnose_access",
            "model": model,
            "operation": operation,
            "permission_field": permission_field,
            "domain": normalized_domain,
            "record_ids": normalized_record_ids,
            "expected_count": expected_count,
            "actual_count": actual_count,
            "model_metadata": {"record": model_record},
            "current_user": current_user,
            "access": {
                "rows": acl_rows,
                "granting_rows": granting_acl_rows,
                "granting_count": len(granting_acl_rows),
            },
            "rules": {
                "included": include_rules,
                "active": active_rules,
                "global": global_rules,
                "group_bound": group_bound_rules,
                "applicable": applicable_rules,
            },
            "diagnosis": {"codes": diagnosis_codes},
            "error_classification": classify_access_error(observed_error),
            "metadata_errors": metadata_errors,
            "metadata_used": {
                "live_odoo": True,
                "acl": bool(acl_rows),
                "rules": include_rules,
                "current_user": current_user["record"] is not None,
                "sudo": False,
                "impersonation": False,
            },
        }
    except Exception as e:
        return {"success": False, "tool": "diagnose_access", "error": str(e)}


@mcp.tool(
    description="Report Odoo upgrade and JSON-2 migration risks",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def upgrade_risk_report(
    source_version: Annotated[
        Optional[str], Field(description="Optional current Odoo version.")
    ] = None,
    target_version: Annotated[
        Optional[str], Field(description="Optional target Odoo version.")
    ] = None,
    modules: Annotated[
        Optional[List[Dict[str, Any]]],
        Field(description="Optional module metadata to assess for upgrade risks."),
    ] = None,
    methods: Annotated[
        Optional[List[Dict[str, Any]]],
        Field(description="Optional model method metadata to assess for compatibility."),
    ] = None,
    source_findings: Annotated[
        Optional[List[Dict[str, Any]]],
        Field(description="Optional source-code findings to include in the risk report."),
    ] = None,
    observed_errors: Annotated[
        Optional[List[Any]],
        Field(description="Optional observed upgrade or migration errors to classify."),
    ] = None,
    use_live_metadata: Annotated[
        bool,
        Field(
            description="Whether to request live metadata; this preview tool does not fetch it."
        ),
    ] = False,
    include_debug: Annotated[
        bool, Field(description="Whether to include additional diagnostic details.")
    ] = False,
) -> Dict[str, Any]:
    """Build an input-driven upgrade risk report without executing Odoo calls."""
    report = build_upgrade_risk_report(
        source_version=source_version,
        target_version=target_version,
        modules=modules,
        methods=methods,
        source_findings=source_findings,
        observed_errors=observed_errors,
        include_debug=include_debug,
    )
    if use_live_metadata:
        report["risks"].append(
            {
                "code": "live_metadata_not_used",
                "severity": "info",
                "evidence": "upgrade_risk_report is input-driven in this release.",
                "recommendation": "Pass module/method/source findings explicitly.",
            }
        )
    return report


@mcp.tool(
    description=(
        "Classify Odoo install/update log errors into a migration worklist "
        "(no_action / needs_review / needs_script) with fix suggestions"
    ),
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def analyze_upgrade_log(
    log_text: str,
    source_version: Optional[str] = None,
    target_version: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Parse an Odoo install/update/upgrade log and classify known failure
    patterns (xpath breaks, missing fields/models/external ids, NOT NULL
    violations, dependency errors, attrs removal, ORM signature changes)
    into an actionable worklist. Input-driven — never contacts Odoo. Paste
    the relevant log slice (up to ~1 MB); findings are deduplicated.
    """
    try:
        return analyze_upgrade_log_report(
            log_text,
            source_version=source_version,
            target_version=target_version,
        )
    except Exception as e:
        return {"success": False, "tool": "analyze_upgrade_log", "error": str(e)}


@mcp.tool(
    description=(
        "Look up Odoo model rename/removal history by old or new model name "
        "(e.g. account.invoice -> account.move)"
    ),
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def lookup_model_history(name: str) -> Dict[str, Any]:
    """
    Resolve a possibly outdated model name against a curated rename catalog.

    Call this before assuming a model exists when working across Odoo
    versions; it is static and never contacts Odoo.
    """
    try:
        return lookup_model_history_report(name)
    except Exception as e:
        return {"success": False, "tool": "lookup_model_history", "error": str(e)}


@mcp.tool(
    description="Classify Odoo requirements into fit/gap implementation buckets",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def fit_gap_report(
    requirements: List[Any],
    available_models: Optional[List[str]] = None,
    available_fields: Optional[Dict[str, Any]] = None,
    installed_modules: Optional[List[Any]] = None,
    business_context: Optional[Dict[str, Any]] = None,
    use_live_metadata: bool = False,
) -> Dict[str, Any]:
    """Normalize requirements into standard/config/Studio/custom/avoid/unknown buckets."""
    report = build_fit_gap_report(
        requirements=requirements,
        available_models=available_models,
        available_fields=available_fields,
        installed_modules=installed_modules,
        business_context=business_context,
    )
    if use_live_metadata:
        report["assumptions"].append(
            "fit_gap_report is input-driven in this release; use list_models/get_model_fields first."
        )
    return report


@mcp.tool(
    description="Scan local Odoo addon source without importing addon code",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def scan_addons_source(
    addons_paths: Optional[List[str]] = None,
    max_files: int = 200,
    max_file_bytes: int = 300_000,
) -> Dict[str, Any]:
    """Summarize manifests, custom models, risky methods, views, and ACL files."""
    try:
        max_files = clamp_limit(max_files, maximum=1000)
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be greater than 0")
        return scan_addons_source_report(
            addons_paths=restrict_addons_paths(addons_paths),
            max_files=max_files,
            max_file_bytes=max_file_bytes,
        )
    except Exception as e:
        return {"success": False, "tool": "scan_addons_source", "error": str(e)}


@mcp.tool(
    description="Build a validated Odoo domain from structured conditions",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def build_domain(
    conditions: List[Dict[str, Any]],
    logical_operator: str = "and",
    fields_metadata: Optional[Dict[str, Any]] = None,
) -> BuildDomainResponse:
    """Build safe domain arrays for search_records and Odoo ORM calls."""
    try:
        result: Dict[str, Any] = _srv().build_domain_report(
            conditions=conditions,
            logical_operator=logical_operator,
            fields_metadata=fields_metadata,
        )
        return result
    except Exception as e:
        return {"success": False, "tool": "build_domain", "error": str(e)}


@mcp.tool(
    description="Report expected modules, models, and safe discovery calls for a business pack",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def business_pack_report(
    ctx: Context,
    pack: Annotated[
        str,
        Field(
            description="Business pack to report, such as sales, crm, inventory, accounting, or hr."
        ),
    ],
    use_live_metadata: Annotated[
        bool,
        Field(description="Whether to inspect live models and installed modules."),
    ] = True,
    instance: Annotated[
        Optional[str],
        Field(description="Optional configured Odoo instance name; uses the default if omitted."),
    ] = None,
) -> Dict[str, Any]:
    """Summarize a domain pack such as sales, crm, inventory, accounting, or hr."""
    try:
        available_models: List[str] | None = None
        installed_modules: List[str] | None = None
        if use_live_metadata:
            _, odoo = _resolve_odoo(ctx, instance)
            models_report = odoo.get_models()
            if "error" not in models_report:
                available_models = list(models_report.get("model_names", []))
            installed_modules = [
                str(module.get("name"))
                for module in odoo.get_installed_modules(limit=200)
                if module.get("name")
            ]
        return build_business_pack_report(
            pack=pack,
            available_models=available_models,
            installed_modules=installed_modules,
        )
    except Exception as e:
        return {"success": False, "tool": "business_pack_report", "error": str(e)}
