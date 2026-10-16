"""
MCP tools: read domain.

Includes: list_models, get_model_fields, search_records, read_record,
read_attachment, aggregate_records, schema_catalog, search_employee,
search_holidays, list_instances, get_odoo_profile, health_check.
"""

import json
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, List, Optional

from mcp.server.mcpserver import Context
from pydantic import Field

from .agent_tools import (
    DEFAULT_MAX_RELEVANT_FIELDS,
    build_text_query_domain,
    rank_relevant_fields,
)
from .field_policy import get_field_policy
from .odoo_client import list_configured_instances
from .rate_limit import check_rate, rate_report
from .schemas import (
    AggregateRecordsResponse,
    GetModelFieldsResponse,
    GetOdooProfileResponse,
    HealthCheckResponse,
    ListInstancesResponse,
    ListModelsResponse,
    ReadAttachmentResponse,
    ReadRecordResponse,
    SchemaCatalogResponse,
    SearchRecordsResponse,
)
from .tool_helpers import (
    EmployeeSearchResult,
    Holiday,
    SearchEmployeeResponse,
    SearchHolidaysResponse,
    clamp_limit,
    max_attachment_bytes,
    normalize_domain_input,
    validate_model_name,
)
from .server_core import (
    READ_ONLY_TOOL,
    PREVIEW_TOOL,
    mcp,
    plugin_posture,
    _app_context,
    _cached_fields_metadata,
    _resolve_odoo,
    mcp_surface_counts,
    note_single_record_read,
    resolve_read_fields,
    runtime_security_report,
)


def _srv() -> Any:
    """Late import of server module to resolve patchable symbols at call time."""
    from . import server
    return server


@mcp.tool(
    description="Read a bounded profile of the connected Odoo environment",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def get_odoo_profile(
    ctx: Context,
    include_modules: Annotated[
        bool, Field(description="Whether to include installed-module metadata.")
    ] = True,
    module_limit: Annotated[
        int, Field(description="Maximum installed modules to include; capped at 500.")
    ] = 100,
    instance: Annotated[
        Optional[str],
        Field(description="Optional configured Odoo instance name; uses the default if omitted."),
    ] = None,
) -> GetOdooProfileResponse:
    """Return server, user-context, transport, and installed-module metadata."""
    try:
        module_limit = clamp_limit(module_limit, maximum=500)
        _, odoo = _resolve_odoo(ctx, instance)
        if include_modules:
            profile = odoo.get_profile(module_limit=module_limit)
        else:
            profile = {
                "url": getattr(odoo, "url", None),
                "hostname": getattr(odoo, "hostname", None),
                "database": getattr(odoo, "db", None),
                "username": getattr(odoo, "username", None),
                "transport": getattr(odoo, "transport", None),
                "timeout": getattr(odoo, "timeout", None),
                "verify_ssl": getattr(odoo, "verify_ssl", None),
                "json2_database_header": getattr(odoo, "json2_database_header", None),
                "server_version": odoo.get_server_version(),
                "user_context": odoo.get_user_context(),
                "installed_modules": [],
                "installed_module_count": None,
            }
        return {
            "success": True,
            "tool": "get_odoo_profile",
            "profile": profile,
            "metadata_used": {
                "live_odoo": True,
                "installed_modules": include_modules,
            },
        }
    except Exception as e:
        return {"success": False, "tool": "get_odoo_profile", "error": str(e)}


@mcp.tool(
    description="Build and cache a bounded Odoo model schema catalog",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def schema_catalog(
    ctx: Context,
    query: Annotated[
        Optional[str], Field(description="Optional text used to filter catalog models.")
    ] = None,
    models: Annotated[
        Optional[List[str]],
        Field(description="Optional technical model names to include in the catalog."),
    ] = None,
    include_fields: Annotated[
        bool, Field(description="Whether to include field metadata for each model.")
    ] = False,
    refresh: Annotated[
        bool, Field(description="Whether to bypass and refresh the cached catalog.")
    ] = False,
    limit: Annotated[
        int, Field(description="Maximum catalog models to return; capped at 500.")
    ] = 50,
    instance: Annotated[
        Optional[str],
        Field(description="Optional configured Odoo instance name; uses the default if omitted."),
    ] = None,
) -> SchemaCatalogResponse:
    """Return a cached catalog of model names, labels, and optional fields."""
    try:
        limit = clamp_limit(limit, maximum=500)
        if models:
            for model_name in models:
                validate_model_name(model_name)

        app_context = _app_context(ctx)
        instance_name = _srv().resolve_instance_name(instance)
        cache_key = json.dumps(
            {
                "query": query,
                "models": sorted(models or []),
                "include_fields": include_fields,
                "limit": limit,
                "instance": instance_name,
            },
            sort_keys=True,
        )
        if not refresh and cache_key in app_context.schema_cache:
            cached = dict(app_context.schema_cache[cache_key])
            cached["metadata_used"] = {**cached["metadata_used"], "cache_hit": True}
            return cached

        _, odoo = _resolve_odoo(ctx, instance)
        raw_models = odoo.get_models()
        if "error" in raw_models:
            return {
                "success": False,
                "tool": "schema_catalog",
                "error": raw_models["error"],
            }

        model_names = list(raw_models.get("model_names", []))
        model_details = raw_models.get("models_details", {})
        if models:
            model_filter = set(models)
            model_names = [name for name in model_names if name in model_filter]
        if query:
            query_lower = query.lower()
            model_names = [
                name
                for name in model_names
                if query_lower in name.lower()
                or query_lower
                in str(model_details.get(name, {}).get("name", "")).lower()
            ]

        records: List[Dict[str, Any]] = []
        for model_name in model_names[:limit]:
            record: Dict[str, Any] = {
                "model": model_name,
                "name": model_details.get(model_name, {}).get("name", ""),
            }
            if include_fields:
                fields = odoo.get_model_fields(model_name)
                record["fields"] = fields if "error" not in fields else {}
                record["field_error"] = (
                    fields.get("error") if "error" in fields else None
                )
            records.append(record)

        report = {
            "success": True,
            "tool": "schema_catalog",
            "count": len(records),
            "result": records,
            "metadata_used": {
                "live_odoo": True,
                "fields_get": include_fields,
                "cache_hit": False,
            },
        }
        app_context.schema_cache[cache_key] = dict(report)
        return report
    except Exception as e:
        return {"success": False, "tool": "schema_catalog", "error": str(e)}


@mcp.tool(
    description="Report this MCP server's non-secret runtime safety posture",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def health_check() -> HealthCheckResponse:
    """Return local process health and hardening flags without opening Odoo."""
    surface_counts = mcp_surface_counts()
    return {
        "success": True,
        "tool": "health_check",
        "server": {
            "name": mcp.name,
            "instructions": mcp.instructions,
            **surface_counts,
        },
        "runtime": runtime_security_report(),
        "rate_limits": rate_report(),
        "plugins": plugin_posture(),
    }


@mcp.tool(
    description="List configured Odoo instance names without credentials",
    annotations=PREVIEW_TOOL,
    structured_output=True,
)
def list_instances() -> ListInstancesResponse:
    """List configured Odoo instances (name, url, db, transport) — never credentials."""
    try:
        instances = list_configured_instances()
        default_name = next(
            (name for name, entry in instances.items() if entry.get("is_default")),
            None,
        )
        return {
            "success": True,
            "tool": "list_instances",
            "default": default_name,
            "instance_count": len(instances),
            "instances": [
                {"name": name, **entry} for name, entry in sorted(instances.items())
            ],
        }
    except Exception as e:
        return {"success": False, "tool": "list_instances", "error": str(e)}


@mcp.tool(
    description="List Odoo models with optional name filtering",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def list_models(
    ctx: Context,
    query: Optional[str] = None,
    limit: int = 100,
    instance: Optional[str] = None,
) -> ListModelsResponse:
    """
    List available Odoo model technical names and display names.

    Prefer this read-only tool over execute_method when discovering models.
    """
    try:
        _, odoo = _resolve_odoo(ctx, instance)
        limit = clamp_limit(limit, maximum=500)
        models = odoo.get_models()
        if "error" in models:
            return {"success": False, "error": models["error"]}

        model_names = models.get("model_names", [])
        models_details = models.get("models_details", {})
        if query:
            query_lower = query.lower()
            model_names = [
                model_name
                for model_name in model_names
                if query_lower in model_name.lower()
                or query_lower
                in str(models_details.get(model_name, {}).get("name", "")).lower()
            ]

        records = [
            {
                "model": model_name,
                "name": models_details.get(model_name, {}).get("name", ""),
            }
            for model_name in model_names[:limit]
        ]
        return {"success": True, "count": len(records), "result": records}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool(
    description="Get field metadata for a specific Odoo model",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def get_model_fields(
    ctx: Context,
    model: str,
    field_names: Optional[List[str]] = None,
    relevance: Optional[str] = None,
    max_fields: int = DEFAULT_MAX_RELEVANT_FIELDS,
    instance: Optional[str] = None,
) -> GetModelFieldsResponse:
    """
    Read field definitions for a model.

    Prefer this read-only tool over execute_method for model introspection.
    Pass ``relevance="top"`` to rank wide models by business relevance and
    return only the ``max_fields`` most useful fields (with their scores).
    """
    try:
        if relevance not in (None, "top"):
            raise ValueError('relevance must be "top" when provided')
        instance_name, odoo = _resolve_odoo(ctx, instance)
        validate_model_name(model)
        fields = odoo.get_model_fields(model)
        if "error" in fields:
            return {"success": False, "error": fields["error"]}
        if field_names:
            fields = {name: fields[name] for name in field_names if name in fields}
        # Field ACL: mark (don't hide) restricted fields so agents know they
        # exist and can explain redaction, but their values stay unreadable.
        restricted = get_field_policy().restricted_fields(
            instance_name, model, list(fields)
        )
        if restricted:
            restricted_set = set(restricted)
            fields = {
                name: ({**meta, "access": "restricted"} if name in restricted_set
                       else meta)
                for name, meta in fields.items()
            }
        if relevance == "top":
            ranking = rank_relevant_fields(fields, max_fields=max_fields)
            ranked_names = [entry["field"] for entry in ranking]
            fields = {name: fields[name] for name in ranked_names}
            ranked_report = {
                "success": True,
                "count": len(fields),
                "result": fields,
                "relevance_applied": True,
                "ranking": ranking,
            }
            if restricted:
                ranked_report["restricted_fields"] = restricted
            return ranked_report
        report = {"success": True, "count": len(fields), "result": fields}
        if restricted:
            report["restricted_fields"] = restricted
        return report
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool(
    description=(
        "Search Odoo records with read-only search_read; optional free-text "
        "`query` matches across name/ref/email-like fields"
    ),
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def search_records(
    ctx: Context,
    model: str,
    domain: Optional[Any] = None,
    fields: Optional[List[str]] = None,
    limit: int = 10,
    offset: int = 0,
    order: Optional[str] = None,
    query: Optional[str] = None,
    instance: Optional[str] = None,
) -> SearchRecordsResponse:
    """
    Search and read records with bounded read-only semantics.

    Domain accepts standard Odoo domain arrays (lists or 3-item tuples),
    a JSON string, a Python domain literal, or
    {"conditions": [{"field": ..., "operator": ..., "value": ...}]}.
    Non-empty input that cannot be fully parsed is rejected instead of
    becoming an unfiltered search.

    ``query`` is a free-text shortcut: the server builds an OR ``ilike``
    domain over the model's searchable text fields (name, ref, email, ...)
    and ANDs it with ``domain``, so agents don't have to hand-craft
    fuzzy-match domains.
    """
    app_context = _app_context(ctx)
    try:
        instance_name, odoo = _resolve_odoo(ctx, instance)
        refusal = check_rate(instance_name, "search_records")
        if refusal is not None:
            return refusal
        validate_model_name(model)
        limit = clamp_limit(limit)
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0")
        normalized_domain = normalize_domain_input(domain)
        query_fields_used: Optional[List[str]] = None
        if query is not None and str(query).strip():
            metadata = _cached_fields_metadata(
                app_context, odoo, model, instance_name
            )
            query_domain, query_fields_used = build_text_query_domain(
                query, metadata
            )
            normalized_domain = query_domain + normalized_domain
        resolved_fields = resolve_read_fields(
            app_context, odoo, model, fields, instance_name
        )
        records = odoo.search_read(
            model_name=model,
            domain=normalized_domain,
            fields=resolved_fields,
            offset=offset,
            limit=limit,
            order=order,
        )
        records, redacted_fields = get_field_policy().redact_records(
            instance_name, model, records
        )
        report = {
            "success": True,
            "count": len(records),
            "result": records,
            "smart_fields_applied": fields is None,
            "fields_used": resolved_fields,
        }
        if query_fields_used is not None:
            report["query_fields_used"] = query_fields_used
        if redacted_fields:
            report["redacted_fields"] = redacted_fields
        return report
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool(
    description="Read a single Odoo record by model and ID",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def read_record(
    ctx: Context,
    model: str,
    record_id: int,
    fields: Optional[List[str]] = None,
    instance: Optional[str] = None,
) -> ReadRecordResponse:
    """
    Read one record by ID with bounded read-only semantics.

    When ``fields`` is omitted the server picks a curated subset
    (business identifiers + state + relations) to keep LLM context small.
    Pass ``fields=["*"]`` to fetch every available field.
    """
    app_context = _app_context(ctx)
    try:
        instance_name, odoo = _resolve_odoo(ctx, instance)
        refusal = check_rate(instance_name, "read_record")
        if refusal is not None:
            return refusal
        validate_model_name(model)
        if record_id < 1:
            raise ValueError("record_id must be greater than 0")
        resolved_fields = resolve_read_fields(
            app_context, odoo, model, fields, instance_name
        )
        records = odoo.read_records(model, [record_id], fields=resolved_fields)
        note_single_record_read(instance_name, model)
        if not records:
            return {
                "success": False,
                "error": f"Record not found: {model} ID {record_id}",
            }
        record, redacted_fields = get_field_policy().redact_record(
            instance_name, model, records[0]
        )
        result = {
            "success": True,
            "result": record,
            "smart_fields_applied": fields is None,
            "fields_used": resolved_fields,
        }
        if redacted_fields:
            result["redacted_fields"] = redacted_fields
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool(
    description=("Read an ir.attachment's metadata and size-capped base64 content"),
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def read_attachment(
    ctx: Context,
    attachment_id: int,
    include_data: bool = True,
    instance: Optional[str] = None,
) -> ReadAttachmentResponse:
    """
    Read one ir.attachment record: metadata always, base64 content when it
    fits under the cap (ODOO_MCP_MAX_ATTACHMENT_BYTES, default 1 MiB).
    URL-type attachments return their URL instead of content.
    """
    try:
        _, odoo = _resolve_odoo(ctx, instance)
        if attachment_id < 1:
            raise ValueError("attachment_id must be greater than 0")
        rows = odoo.execute_method(
            "ir.attachment",
            "read",
            [attachment_id],
            fields=[
                "name",
                "mimetype",
                "file_size",
                "type",
                "url",
                "res_model",
                "res_id",
                "checksum",
                "create_date",
            ],
        )
        if not isinstance(rows, list) or not rows:
            return {
                "success": False,
                "tool": "read_attachment",
                "error": f"Attachment not found: ir.attachment ID {attachment_id}",
            }
        attachment = rows[0]
        warnings: List[str] = []
        data_base64: Optional[str] = None
        cap = max_attachment_bytes()
        file_size = int(attachment.get("file_size") or 0)
        is_binary = str(attachment.get("type") or "binary") == "binary"

        if include_data and is_binary:
            if file_size > cap:
                warnings.append(
                    f"Attachment is {file_size} bytes; cap is {cap} "
                    "(raise ODOO_MCP_MAX_ATTACHMENT_BYTES to fetch it)."
                )
            else:
                data_rows = odoo.execute_method(
                    "ir.attachment", "read", [attachment_id], fields=["datas"]
                )
                raw = (
                    data_rows[0].get("datas")
                    if isinstance(data_rows, list) and data_rows
                    else None
                )
                if isinstance(raw, str) and raw:
                    if (len(raw) * 3) // 4 > cap:
                        warnings.append(
                            "Attachment content exceeded the cap when fetched; "
                            "content omitted."
                        )
                    else:
                        data_base64 = raw
        elif include_data and not is_binary:
            warnings.append("URL-type attachment; fetch the url field directly.")

        return {
            "success": True,
            "tool": "read_attachment",
            "attachment": attachment,
            "data_base64": data_base64,
            "data_included": data_base64 is not None,
            "max_bytes": cap,
            "warnings": warnings,
        }
    except Exception as e:
        return {"success": False, "tool": "read_attachment", "error": str(e)}


@mcp.tool(
    description=(
        "Aggregate Odoo records server-side using Postgres groupby/sum/count. "
        "Uses formatted_read_group on Odoo 19+ and read_group on earlier versions."
    ),
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def aggregate_records(
    ctx: Context,
    model: str,
    group_by: List[str],
    measures: Optional[List[str]] = None,
    domain: Optional[Any] = None,
    lazy: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
    order: Optional[str] = None,
    instance: Optional[str] = None,
) -> AggregateRecordsResponse:
    """Group records server-side and aggregate measures.

    ``measures`` are ``"field:agg"`` strings (default agg ``sum``).
    Allowed aggregators: sum, avg, min, max, count, count_distinct,
    array_agg, bool_and, bool_or.

    Returns ``rows`` (list of dicts) plus the chosen ``method`` and
    detected Odoo ``major_version``. Limit is capped at ``MAX_SEARCH_LIMIT``
    when provided.
    """
    from .tool_helpers import (
        formatted_read_group_missing,
        normalize_group_order,
        odoo_major_version,
        parse_measure_spec,
    )

    try:
        instance_name, odoo = _resolve_odoo(ctx, instance)
        refusal = check_rate(instance_name, "aggregate_records")
        if refusal is not None:
            return refusal
        validate_model_name(model)
        if not group_by:
            raise ValueError("group_by must include at least one field")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0")
        clamped_limit = clamp_limit(limit) if limit is not None else None
        normalized_domain = normalize_domain_input(domain)
        normalized_measures: List[str] = []
        parsed_measures: List[tuple[str, str]] = []
        for spec in measures or []:
            field, agg = parse_measure_spec(spec)
            normalized_measures.append(f"{field}:{agg}")
            parsed_measures.append((field, agg))

        # Field ACL: aggregating/grouping on a denied field is an inference
        # channel, so reject it outright (groupby may carry a ":granularity").
        referenced_fields = [entry.split(":", 1)[0] for entry in group_by]
        referenced_fields += [field for field, _ in parsed_measures]
        aggregate_block = get_field_policy().check_aggregate(
            instance_name, model, referenced_fields
        )
        if aggregate_block is not None:
            return {"success": False, "error": aggregate_block}

        major = odoo_major_version(odoo)
        method_used = "read_group"
        rows: list[dict[str, Any]]
        fallback_reason = ""

        formatted_kwargs: Dict[str, Any] = {
            "domain": normalized_domain,
            "groupby": group_by,
            "aggregates": normalized_measures,
        }
        if offset:
            formatted_kwargs["offset"] = offset
        if clamped_limit is not None:
            formatted_kwargs["limit"] = clamped_limit
        if order:
            # Odoo 19 wants measures as "field:agg" in the order; legacy read_group
            # (below) keeps the caller's order unchanged.
            formatted_kwargs["order"] = normalize_group_order(order, parsed_measures)

        legacy_kwargs: Dict[str, Any] = {
            "domain": normalized_domain,
            "fields": normalized_measures,
            "groupby": group_by,
            "lazy": lazy,
        }
        if offset:
            legacy_kwargs["offset"] = offset
        if clamped_limit is not None:
            legacy_kwargs["limit"] = clamped_limit
        if order:
            legacy_kwargs["orderby"] = order

        if major is not None and major >= 19:
            method_used = "formatted_read_group"
            rows = odoo.execute_method(
                model, "formatted_read_group", **formatted_kwargs
            )
        elif major is not None:
            rows = odoo.execute_method(model, "read_group", **legacy_kwargs)
        else:
            method_used = "formatted_read_group"
            try:
                rows = odoo.execute_method(
                    model, "formatted_read_group", **formatted_kwargs
                )
            except Exception as exc:
                if not formatted_read_group_missing(exc):
                    raise
                method_used = "read_group"
                rows = odoo.execute_method(model, "read_group", **legacy_kwargs)
                fallback_reason = str(exc)

        return {
            "success": True,
            "method": method_used,
            "major_version": major,
            "fallback_reason": fallback_reason or None,
            "model": model,
            "group_by": group_by,
            "measures": normalized_measures,
            "row_count": len(rows),
            "rows": rows,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool(
    description="Search for employees by name",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def search_employee(
    ctx: Context,
    name: str,
    limit: int = 20,
    instance: Optional[str] = None,
) -> SearchEmployeeResponse:
    """
    Search for employees by name using Odoo's name_search method.

    Parameters:
        name: The name (or part of the name) to search for.
        limit: The maximum number of results to return (default 20).

    Returns:
        SearchEmployeeResponse containing results or error information.
    """
    model = "hr.employee"
    method = "name_search"

    args: List[Any] = []
    kwargs: Dict[str, Any] = {"name": name, "limit": limit}

    try:
        _, odoo = _resolve_odoo(ctx, instance)
        result = odoo.execute_method(model, method, *args, **kwargs)
        parsed_result = [
            EmployeeSearchResult(id=item[0], name=item[1]) for item in result
        ]
        return SearchEmployeeResponse(success=True, result=parsed_result)
    except Exception as e:
        return SearchEmployeeResponse(success=False, error=str(e))


@mcp.tool(
    description="Search for holidays within a date range",
    annotations=READ_ONLY_TOOL,
    structured_output=True,
)
def search_holidays(
    ctx: Context,
    start_date: Annotated[
        str, Field(description="Start date in YYYY-MM-DD format.")
    ],
    end_date: Annotated[str, Field(description="End date in YYYY-MM-DD format.")],
    employee_id: Annotated[
        Optional[int], Field(description="Optional employee ID used to filter holidays.")
    ] = None,
    instance: Annotated[
        Optional[str],
        Field(description="Optional configured Odoo instance name; uses the default if omitted."),
    ] = None,
) -> SearchHolidaysResponse:
    """
    Searches for holidays within a specified date range.

    Parameters:
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        employee_id: Optional employee ID to filter holidays.

    Returns:
        SearchHolidaysResponse:  Object containing the search results.
    """
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return SearchHolidaysResponse(
            success=False, error="Invalid start_date format. Use YYYY-MM-DD."
        )
    try:
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return SearchHolidaysResponse(
            success=False, error="Invalid end_date format. Use YYYY-MM-DD."
        )

    start_date_dt = datetime.strptime(start_date, "%Y-%m-%d")
    adjusted_start_date_dt = start_date_dt - timedelta(days=1)
    adjusted_start_date = adjusted_start_date_dt.strftime("%Y-%m-%d")

    domain: List[Any] = [
        "&",
        ["start_datetime", "<=", f"{end_date} 22:59:59"],
        ["stop_datetime", ">=", f"{adjusted_start_date} 23:00:00"],
    ]
    if employee_id:
        domain.append(
            ["employee_id", "=", employee_id],
        )

    try:
        _, odoo = _resolve_odoo(ctx, instance)
        holidays = odoo.search_read(
            model_name="hr.leave.report.calendar",
            domain=domain,
        )
        parsed_holidays = [Holiday(**holiday) for holiday in holidays]
        return SearchHolidaysResponse(success=True, result=parsed_holidays)

    except Exception as e:
        return SearchHolidaysResponse(success=False, error=str(e))
