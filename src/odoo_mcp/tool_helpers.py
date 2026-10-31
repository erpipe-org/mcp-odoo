"""Pure request/validation helpers shared by the MCP tools.

Everything here is side-effect-free (env reads at most) and safe to use
without an MCP context, so it can be unit-tested without a live Odoo.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, List, NoReturn, Optional, Union

from pydantic import BaseModel, Field

from .field_ranking import DEFAULT_MAX_SMART_FIELDS
from .odoo_client import OdooClient

MODEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$")
METHOD_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
MAX_SEARCH_LIMIT = 100

DEFAULT_MAX_ATTACHMENT_BYTES = 1024 * 1024
ATTACHMENT_BYTES_HARD_CAP = 16 * 1024 * 1024


# ----- Pydantic models for type safety -----


class DomainCondition(BaseModel):
    """A single condition in a search domain"""

    field: str = Field(description="Field name to search")
    operator: str = Field(
        description="Operator (e.g., '=', '!=', '>', '<', 'in', 'not in', 'like', 'ilike')"
    )
    value: Any = Field(description="Value to compare against")

    def to_tuple(self) -> List:
        """Convert to Odoo domain condition tuple"""
        return [self.field, self.operator, self.value]


class SearchDomain(BaseModel):
    """Search domain for Odoo models"""

    conditions: List[DomainCondition] = Field(
        default_factory=list,
        description="List of conditions for searching. All conditions are combined with AND operator.",
    )

    def to_domain_list(self) -> List[List]:
        """Convert to Odoo domain list format"""
        return [condition.to_tuple() for condition in self.conditions]


class EmployeeSearchResult(BaseModel):
    """Represents a single employee search result."""

    id: int = Field(description="Employee ID")
    name: str = Field(description="Employee name")


class SearchEmployeeResponse(BaseModel):
    """Response model for the search_employee tool."""

    success: bool = Field(description="Indicates if the search was successful")
    result: Optional[List[EmployeeSearchResult]] = Field(
        default=None, description="List of employee search results"
    )
    error: Optional[str] = Field(default=None, description="Error message, if any")


class Holiday(BaseModel):
    """Represents a single holiday."""

    display_name: str = Field(description="Display name of the holiday")
    start_datetime: str = Field(description="Start date and time of the holiday")
    stop_datetime: str = Field(description="End date and time of the holiday")
    employee_id: List[Union[int, str]] = Field(
        description="Employee ID associated with the holiday"
    )
    name: str = Field(description="Name of the holiday")
    state: str = Field(description="State of the holiday")


class SearchHolidaysResponse(BaseModel):
    """Response model for the search_holidays tool."""

    success: bool = Field(description="Indicates if the search was successful")
    result: Optional[List[Holiday]] = Field(
        default=None, description="List of holidays found"
    )
    error: Optional[str] = Field(default=None, description="Error message, if any")


def validate_model_name(model_name: str) -> None:
    """Reject obviously unsafe model names before forwarding to Odoo."""
    if not MODEL_NAME_RE.fullmatch(model_name):
        raise ValueError(
            "Invalid model name. Use Odoo technical model names like 'res.partner'."
        )


def validate_method_name(method_name: str) -> None:
    """Reject obviously unsafe method names before forwarding to Odoo."""
    if not METHOD_NAME_RE.fullmatch(method_name):
        raise ValueError(
            "Invalid method name. Use Odoo method names like 'search_read'."
        )


def clamp_limit(limit: int, maximum: int = MAX_SEARCH_LIMIT) -> int:
    """Keep read-only tools bounded for agent safety."""
    if limit < 1:
        raise ValueError("limit must be greater than 0")
    return min(limit, maximum)


def max_smart_fields() -> int:
    """Read configured cap for smart-field selection (default 15)."""
    raw = os.environ.get("ODOO_MCP_MAX_SMART_FIELDS", "").strip()
    if not raw:
        return DEFAULT_MAX_SMART_FIELDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_SMART_FIELDS
    return max(1, value)


def max_attachment_bytes() -> int:
    """Read the configured attachment download cap (default 1 MiB)."""
    raw = os.environ.get("ODOO_MCP_MAX_ATTACHMENT_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_ATTACHMENT_BYTES
    except ValueError:
        value = DEFAULT_MAX_ATTACHMENT_BYTES
    return max(1, min(value, ATTACHMENT_BYTES_HARD_CAP))


DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES = 10 * 1024 * 1024


def max_attachment_upload_bytes() -> int:
    """Read the configured ``*_from_path`` local-file upload cap (default 10 MiB).

    Uploads never flow through the caller's context (unlike downloads), so the
    default is higher than ``max_attachment_bytes``. Still bounded by the same
    ``ATTACHMENT_BYTES_HARD_CAP`` so operators keep one mental ceiling.
    """
    raw = os.environ.get("ODOO_MCP_MAX_ATTACHMENT_UPLOAD_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES
    except ValueError:
        value = DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES
    return max(1, min(value, ATTACHMENT_BYTES_HARD_CAP))


def truthy_env(name: str) -> bool:
    """Read a common boolean environment flag."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


_AGGREGATION_FUNCTIONS = {
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "count_distinct",
    "array_agg",
    "bool_and",
    "bool_or",
}


def parse_measure_spec(spec: str) -> tuple[str, str]:
    """Split a 'field:agg' measure into (field, agg).

    Defaults to 'sum' when no aggregator is supplied.
    Raises ValueError on invalid shapes.
    """
    cleaned = str(spec).strip()
    if not cleaned:
        raise ValueError("measure entries must be non-empty strings")
    if ":" not in cleaned:
        return cleaned, "sum"
    field, agg = cleaned.split(":", 1)
    field = field.strip()
    agg = agg.strip().lower()
    if not field or not agg:
        raise ValueError(f"invalid measure spec: {spec!r}")
    if agg not in _AGGREGATION_FUNCTIONS:
        raise ValueError(
            f"unsupported aggregator {agg!r}; expected one of "
            f"{sorted(_AGGREGATION_FUNCTIONS)}."
        )
    return field, agg


def parse_odoo_major_version(value: Any) -> int | None:
    """Extract an Odoo major version from numeric and SaaS version shapes."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value >= 1 else None

    raw = str(value).strip()
    if not raw:
        return None
    match = re.search(r"(\d+)", raw)
    if not match:
        return None
    major = int(match.group(1))
    return major if major > 0 else None


def odoo_major_version(odoo: OdooClient) -> int | None:
    """Return the connected Odoo major version, or None if unknown.

    Tries the server-version metadata first. Odoo Online SaaS can report
    ``server_version_info[0]`` as strings such as ``"saas~19"``; read that
    before the less-specific version strings. Falls back to the
    ``ir.module.module`` ``latest_version`` of the ``base`` module (which
    starts with the major version on every Odoo deployment) so that
    JSON-2 clients still detect the correct major when ``/web/version``
    is unavailable or returns a non-standard payload.
    """
    info = odoo.get_server_version()
    if isinstance(info, dict):
        version_info = info.get("server_version_info")
        candidates: list[Any] = []
        if isinstance(version_info, (list, tuple)) and version_info:
            candidates.append(version_info[0])
        candidates.extend([info.get("server_version"), info.get("server_serie")])
        for raw in candidates:
            major = parse_odoo_major_version(raw)
            if major is not None:
                return major
    try:
        result = odoo.execute_method(
            "ir.module.module",
            "search_read",
            [["name", "=", "base"]],
            fields=["latest_version"],
            limit=1,
        )
    except Exception:
        return None
    if not result:
        return None
    raw_version = str(result[0].get("latest_version", ""))
    return parse_odoo_major_version(raw_version)


def formatted_read_group_missing(exc: Exception) -> bool:
    """Return whether an Odoo error says formatted_read_group is absent."""
    message = str(exc)
    return "formatted_read_group" in message and "does not exist" in message


_DOMAIN_PREFIX_OPERATORS = frozenset({"&", "|", "!"})
_REQUIRED_CONDITION_KEYS = ("field", "operator", "value")


def _is_domain_leaf(cond: Any) -> bool:
    """Return whether *cond* is a 3-item [field, operator, value] leaf."""
    return (
        isinstance(cond, (list, tuple))
        and len(cond) == 3
        and isinstance(cond[0], str)
        and cond[0] not in _DOMAIN_PREFIX_OPERATORS
        and isinstance(cond[1], str)
    )


def _as_domain_leaf(cond: Any) -> List[Any]:
    return [cond[0], cond[1], cond[2]]


def _reject_domain(message: str) -> NoReturn:
    raise ValueError(message)


def normalize_domain_input(domain: Any) -> List[Any]:
    """Normalize common MCP/JSON domain shapes to an Odoo domain list.

    Explicit empty input stays ``[]``. Non-empty input that cannot be
    fully normalized raises ``ValueError`` instead of becoming an
    unfiltered search.
    """
    if domain is None:
        return []
    if isinstance(domain, SearchDomain):
        return domain.to_domain_list()

    domain_value = domain
    if isinstance(domain_value, str):
        if not domain_value.strip():
            return []
        try:
            domain_value = json.loads(domain_value)
        except json.JSONDecodeError:
            try:
                import ast

                domain_value = ast.literal_eval(domain_value)
            except (SyntaxError, ValueError) as exc:
                raise ValueError(
                    "domain must be a JSON array, a Python domain literal, "
                    'or {"conditions": [...]}'
                ) from exc

    if isinstance(domain_value, dict):
        if "conditions" not in domain_value:
            _reject_domain("domain object must include a 'conditions' list")
        conditions = domain_value["conditions"]
        if not isinstance(conditions, list):
            _reject_domain("domain 'conditions' must be a list")
        normalized: List[Any] = []
        for cond in conditions:
            if not (
                isinstance(cond, dict)
                and all(key in cond for key in _REQUIRED_CONDITION_KEYS)
            ):
                _reject_domain(
                    "each domain condition must include field, operator, and value"
                )
            normalized.append([cond["field"], cond["operator"], cond["value"]])
        return normalized

    if _is_domain_leaf(domain_value):
        return [_as_domain_leaf(domain_value)]

    if not isinstance(domain_value, list):
        _reject_domain(
            "domain must be a list, a JSON/Python domain string, "
            'or {"conditions": [...]}'
        )

    if len(domain_value) == 1 and isinstance(domain_value[0], list):
        if not domain_value[0]:
            return []
        domain_value = domain_value[0]

    if not domain_value:
        return []

    domain_list = [domain_value] if _is_domain_leaf(domain_value) else domain_value

    valid_conditions: List[Any] = []
    for cond in domain_list:
        if isinstance(cond, str) and cond in _DOMAIN_PREFIX_OPERATORS:
            valid_conditions.append(cond)
            continue
        if _is_domain_leaf(cond):
            valid_conditions.append(_as_domain_leaf(cond))
            continue
        _reject_domain(
            f"invalid domain term {cond!r}; expected '&', '|', '!' "
            "or a 3-item [field, operator, value] condition"
        )
    return valid_conditions
