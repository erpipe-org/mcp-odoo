"""
MCP server for Odoo integration

Provides MCP tools and resources for interacting with Odoo ERP systems
"""

# ruff: noqa: F401  — this module is the public re-export surface; all imports are intentional.

# Import core first (creates mcp instance, AppContext, resources)
from .server_core import (
    apply_tool_filter,
    load_plugins,
    plugin_posture,
    DESTRUCTIVE_TOOL,
    ELICIT_WRITES_ENV,
    N_PLUS_ONE_WARN_THRESHOLD,
    N_PLUS_ONE_WINDOW_SECONDS,
    PREVIEW_TOOL,
    READ_ONLY_TOOL,
    RESOURCE_HINT,
    WRITE_APPROVAL_TTL_SECONDS,
    AppContext,
    WriteConfirmation,
    _cached_fields_metadata,
    _is_relative_to,
    _resolve_odoo,
    _side_effect_policy_posture,
    _single_read_events,
    _single_read_lock,
    app_lifespan,
    approval_principal_matches,
    configured_addons_roots,
    configured_attachment_upload_roots,
    get_model_info,
    get_models,
    current_principal,
    get_record,
    instance_posture,
    mcp,
    mcp_surface_counts,
    n_plus_one_report,
    note_single_record_read,
    register_write_approval,
    require_validated_write_approval,
    resolve_default_instance_name,
    resolve_instance_name,
    resolve_read_fields,
    restrict_addons_paths,
    restrict_attachment_upload_path,
    runtime_security_report,
    search_records_resource,
    write_approval_payload,
)

# Re-export patchable symbols from odoo_client so monkeypatches on this module work
from .odoo_client import (
    OdooClient,
    get_odoo_client,
    get_odoo_client_for,
    list_configured_instances,
    load_instances_config,
)

# Re-export build_domain_report so monkeypatches on server.build_domain_report work
from .agent_tools import (
    build_approval_token,
    build_domain_report,
    select_smart_fields,
    verify_write_approval,
)

# Import tool/prompt modules — side-effect: registers @mcp.tool / @mcp.prompt decorators
from . import tools_data_quality
from . import tools_diagnostics
from . import tools_read
from . import tools_write
from . import tools_knowledge
from . import tools_accounting
from . import tools_cross_instance
from . import tools_async
from . import prompts
from . import prompts_workflows

# Re-export write tool functions
from .tools_write import (
    _build_chatter_payload,
    _elicit_write_confirmation,
    _execute_approved_write_gated,
    _write_elicitation_message,
    chatter_post,
    execute_approved_write,
    execute_approved_write_tool,
    execute_method,
    preview_write,
    validate_write,
)

# Re-export read tool functions
from .tools_read import (
    aggregate_records,
    get_model_fields,
    get_odoo_profile,
    health_check,
    list_instances,
    list_models,
    read_attachment,
    read_record,
    schema_catalog,
    search_employee,
    search_holidays,
    search_records,
)

# Re-export diagnostics tool functions
from .tools_data_quality import (
    data_quality_report,
)
from .tools_diagnostics import (
    analyze_upgrade_log,
    build_domain,
    business_pack_report,
    diagnose_access,
    diagnose_odoo_call,
    fit_gap_report,
    generate_json2_payload,
    inspect_model_relationships,
    lookup_model_history,
    scan_addons_source,
    upgrade_risk_report,
)

# Re-export knowledge tool functions
from .tools_knowledge import (
    index_knowledge,
    knowledge_stats,
    search_knowledge,
)

# Re-export accounting tool functions
from .tools_accounting import (
    accounting_health_summary,
    receivable_payable_aging,
)

# Re-export async task tool functions
from .tools_async import (
    cancel_async_task,
    get_async_task,
    list_async_tasks,
    submit_async_task,
)

# Re-export cross-instance tool functions
from .tools_cross_instance import (
    accounting_health_across_instances,
    aggregate_across_instances,
    search_across_instances,
)

# Re-export prompt functions
from .prompts import (
    prompt_custom_module_audit,
    prompt_diagnose_failed_odoo_call,
    prompt_fit_gap_workshop,
    prompt_json2_migration_plan,
    prompt_safe_write_review,
)

# Re-export workflow prompt functions
from .prompts_workflows import (
    prompt_accounting_close_checklist,
    prompt_customer_onboarding,
    prompt_expense_claim_review,
    prompt_invoice_approval_chain,
    prompt_po_to_receipt,
)

# Re-export legacy / backwards-compat symbols
from .schema_cache import (
    DEFAULT_SCHEMA_CACHE_MAX_ENTRIES,
    DEFAULT_SCHEMA_CACHE_TTL_SECONDS,
    BoundedTTLCache,
    _build_schema_cache,
    _schema_cache_settings,
)
from .tool_helpers import (
    _AGGREGATION_FUNCTIONS,
    ATTACHMENT_BYTES_HARD_CAP,
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES,
    DEFAULT_MAX_SMART_FIELDS,
    MAX_SEARCH_LIMIT,
    METHOD_NAME_RE,
    MODEL_NAME_RE,
    DomainCondition,
    SearchDomain,
    clamp_limit,
    max_attachment_bytes,
    max_attachment_upload_bytes,
    max_smart_fields,
    normalize_domain_input,
    odoo_major_version,
    parse_measure_spec,
    parse_odoo_major_version,
    truthy_env,
    validate_method_name,
    validate_model_name,
)
from .access_helpers import (
    _access_diagnosis_codes,
    _acl_row_applies,
    _available_user_read_fields,
    _field_names,
    _group_field_names,
    _m2m_ids,
    _m2o_id,
    _record_id_domain,
    _rule_applies,
    _safe_odoo_read,
    access_permission_field,
)
from .write_policy import (
    DEFAULT_POLICY_FILENAME,
    POLICY_FILE_ENV,
    allowed_side_effect_methods,
    chatter_direct_enabled,
    load_side_effect_policy,
    policy_file_path,
    side_effect_method_allowed,
    writes_enabled,
)
from .field_policy import (
    FIELD_POLICY_FILE_ENV,
    FieldPolicy,
    FieldPolicyError,
    field_policy_posture,
    get_field_policy,
    load_field_policy,
    reset_field_policy,
)

__all__ = [
    # Core infra
    "AppContext",
    "WriteConfirmation",
    "mcp",
    "app_lifespan",
    "WRITE_APPROVAL_TTL_SECONDS",
    "ELICIT_WRITES_ENV",
    # Tool annotations
    "READ_ONLY_TOOL",
    "PREVIEW_TOOL",
    "DESTRUCTIVE_TOOL",
    "RESOURCE_HINT",
    # Instance resolution
    "resolve_default_instance_name",
    "resolve_instance_name",
    # odoo_client re-exports (patchable surface)
    "OdooClient",
    "get_odoo_client",
    "get_odoo_client_for",
    "list_configured_instances",
    "load_instances_config",
    # agent_tools re-exports (patchable: build_domain_report)
    "build_approval_token",
    "build_domain_report",
    "select_smart_fields",
    "verify_write_approval",
    # Resources
    "get_models",
    "get_model_info",
    "get_record",
    "search_records_resource",
    # Write tools
    "preview_write",
    "validate_write",
    "execute_approved_write",
    "execute_approved_write_tool",
    "chatter_post",
    "execute_method",
    # Read tools
    "list_models",
    "get_model_fields",
    "search_records",
    "read_record",
    "read_attachment",
    "aggregate_records",
    "schema_catalog",
    "search_employee",
    "search_holidays",
    "list_instances",
    "get_odoo_profile",
    "health_check",
    # Diagnostics tools
    "diagnose_odoo_call",
    "generate_json2_payload",
    "inspect_model_relationships",
    "diagnose_access",
    "upgrade_risk_report",
    "lookup_model_history",
    "fit_gap_report",
    "scan_addons_source",
    "data_quality_report",
    "analyze_upgrade_log",
    "build_domain",
    "business_pack_report",
    # Knowledge tools
    "index_knowledge",
    "search_knowledge",
    "knowledge_stats",
    # Accounting tools
    "receivable_payable_aging",
    "accounting_health_summary",
    # Async task tools
    "submit_async_task",
    "get_async_task",
    "cancel_async_task",
    "list_async_tasks",
    # Cross-instance tools
    "search_across_instances",
    "aggregate_across_instances",
    "accounting_health_across_instances",
    # Prompts
    "prompt_diagnose_failed_odoo_call",
    "prompt_fit_gap_workshop",
    "prompt_json2_migration_plan",
    "prompt_safe_write_review",
    "prompt_custom_module_audit",
    # Workflow prompts
    "prompt_invoice_approval_chain",
    "prompt_po_to_receipt",
    "prompt_customer_onboarding",
    "prompt_expense_claim_review",
    "prompt_accounting_close_checklist",
    # Schema cache
    "BoundedTTLCache",
    "DEFAULT_SCHEMA_CACHE_MAX_ENTRIES",
    "DEFAULT_SCHEMA_CACHE_TTL_SECONDS",
    "_build_schema_cache",
    "_schema_cache_settings",
    # Tool helpers
    "_AGGREGATION_FUNCTIONS",
    "ATTACHMENT_BYTES_HARD_CAP",
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES",
    "DEFAULT_MAX_SMART_FIELDS",
    "MAX_SEARCH_LIMIT",
    "METHOD_NAME_RE",
    "MODEL_NAME_RE",
    "DomainCondition",
    "SearchDomain",
    "clamp_limit",
    "max_attachment_bytes",
    "max_attachment_upload_bytes",
    "max_smart_fields",
    "normalize_domain_input",
    "odoo_major_version",
    "parse_measure_spec",
    "parse_odoo_major_version",
    "truthy_env",
    "validate_method_name",
    "validate_model_name",
    # Access helpers
    "_access_diagnosis_codes",
    "_acl_row_applies",
    "_available_user_read_fields",
    "_field_names",
    "_group_field_names",
    "_m2m_ids",
    "_m2o_id",
    "_record_id_domain",
    "_rule_applies",
    "_safe_odoo_read",
    "access_permission_field",
    # Write policy
    "DEFAULT_POLICY_FILENAME",
    "POLICY_FILE_ENV",
    "allowed_side_effect_methods",
    "chatter_direct_enabled",
    "load_side_effect_policy",
    "policy_file_path",
    "side_effect_method_allowed",
    "writes_enabled",
    # Field ACL
    "FIELD_POLICY_FILE_ENV",
    "FieldPolicy",
    "FieldPolicyError",
    "field_policy_posture",
    "get_field_policy",
    "load_field_policy",
    "reset_field_policy",
    # Misc infra
    "N_PLUS_ONE_WARN_THRESHOLD",
    "N_PLUS_ONE_WINDOW_SECONDS",
    "configured_addons_roots",
    "configured_attachment_upload_roots",
    "instance_posture",
    "mcp_surface_counts",
    "n_plus_one_report",
    "note_single_record_read",
    "register_write_approval",
    "require_validated_write_approval",
    "approval_principal_matches",
    "current_principal",
    "resolve_read_fields",
    "restrict_addons_paths",
    "restrict_attachment_upload_path",
    "runtime_security_report",
    "load_plugins",
    "apply_tool_filter",
    "plugin_posture",
    "_cached_fields_metadata",
    "_is_relative_to",
    "_resolve_odoo",
    "_side_effect_policy_posture",
    "_single_read_events",
    "_single_read_lock",
    # Write internals
    "_build_chatter_payload",
    "_elicit_write_confirmation",
    "_execute_approved_write_gated",
    "_write_elicitation_message",
]


# ---------------------------------------------------------------------------
# Opt-in third-party plugins + per-deployment tool filtering.
# Runs after every builtin registration above; both are no-ops without their
# env vars (ODOO_MCP_PLUGINS, ODOO_MCP_TOOLS_INCLUDE/EXCLUDE).
# ---------------------------------------------------------------------------

from . import plugin_api as _plugin_api  # noqa: E402 — after tool registration

load_plugins(_plugin_api)
apply_tool_filter()
