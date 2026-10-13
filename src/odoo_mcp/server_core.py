"""
Core infrastructure for the Odoo MCP server.

Contains: mcp instance, AppContext, app_lifespan, shared infra helpers,
resources (odoo:// URIs), and write-approval store.
All functions that call patchable symbols (get_odoo_client, get_odoo_client_for,
load_instances_config, resolve_instance_name, resolve_default_instance_name)
use late-binding via _srv() so monkeypatches applied to the server module work.
"""

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import principal_components
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import Annotations, ToolAnnotations
from pydantic import BaseModel, Field

from .odoo_client import OdooClient
from .schema_cache import _build_schema_cache
from .agent_tools import select_smart_fields
from .tool_helpers import (
    max_smart_fields,
    normalize_domain_input,
    truthy_env,
    validate_model_name,
)
from .write_policy import (
    allowed_side_effect_methods,
    chatter_direct_enabled,
    load_side_effect_policy,
    writes_enabled,
)
from .audit import audit_posture
from .auth import auth_posture as oauth_posture
from .field_policy import field_policy_posture, get_field_policy


def _srv() -> Any:
    """Late import of server module to resolve patchable symbols at call time."""
    from . import server
    return server


# ---------------------------------------------------------------------------
# Application context  (defined before mcp so app_lifespan can be passed directly)
# ---------------------------------------------------------------------------

WRITE_APPROVAL_TTL_SECONDS = 10 * 60


@dataclass
class AppContext:
    """Application context with lazy Odoo client access."""

    odoo_factory: Callable[[], OdooClient] = field(
        default_factory=lambda: _srv().get_odoo_client
    )
    _odoo: OdooClient | None = None
    _default_instance_name: str | None = None
    _clients: Dict[str, OdooClient] = field(default_factory=dict)
    _clients_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    schema_cache: Any = field(default_factory=_build_schema_cache)
    write_approvals: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def odoo(self) -> OdooClient:
        """Resolve the Odoo client only when a live Odoo tool needs it."""
        if self._odoo is None:
            self._odoo = self.odoo_factory()
        return self._odoo

    def get_client(self, instance: Optional[str] = None) -> tuple[str, OdooClient]:
        """Resolve a named Odoo instance, lazily connecting and caching per name."""
        if not instance:
            return _srv().resolve_default_instance_name(), self.odoo
        with self._clients_lock:
            if instance not in self._clients:
                name, client = _srv().get_odoo_client_for(instance)
                self._clients[name] = client
            return instance, self._clients[instance]


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """Application lifespan for initialization and cleanup."""
    # Validate the field ACL policy at startup so a malformed policy fails
    # closed (aborts) instead of silently running unprotected at first read.
    get_field_policy()
    yield AppContext()


# ---------------------------------------------------------------------------
# MCP instance (shared singleton — re-exported from server.py)
# ---------------------------------------------------------------------------

DEFAULT_SERVER_INSTRUCTIONS = "MCP Server for interacting with Odoo ERP systems"
MAX_INSTRUCTIONS_CHARS = 16_000


def load_server_instructions() -> str:
    """Server-level MCP instructions, optionally extended from a file.

    ``ODOO_MCP_INSTRUCTIONS_FILE`` points at a plain-text file whose content
    is appended to the default instructions and surfaced to every client via
    the MCP ``instructions`` field — deployment-specific guidance without
    touching tool descriptions (idea: GH-19, thanks @oadiazp). A set-but-
    unreadable path fails at startup rather than silently running without
    the operator's guidance.
    """
    path = os.environ.get("ODOO_MCP_INSTRUCTIONS_FILE", "").strip()
    if not path:
        return DEFAULT_SERVER_INSTRUCTIONS
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(
            f"ODOO_MCP_INSTRUCTIONS_FILE is set but unreadable: {exc}"
        ) from exc
    if not text:
        return DEFAULT_SERVER_INSTRUCTIONS
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        text = text[:MAX_INSTRUCTIONS_CHARS]
    return f"{DEFAULT_SERVER_INSTRUCTIONS}\n\n{text}"


mcp = MCPServer(
    "Odoo MCP Server",
    instructions=load_server_instructions(),
    dependencies=["requests"],
    lifespan=app_lifespan,
)

READ_ONLY_TOOL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
PREVIEW_TOOL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
DESTRUCTIVE_TOOL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
RESOURCE_HINT = Annotations(audience=["assistant"], priority=0.8)


# ---------------------------------------------------------------------------
# Instance resolution helpers
# ---------------------------------------------------------------------------


def resolve_default_instance_name() -> str:
    """Resolve the configured default instance name; fall back to 'default'."""
    try:
        default_name, _ = _srv().load_instances_config()
        return str(default_name)
    except FileNotFoundError:
        return "default"


def resolve_instance_name(instance: Optional[str]) -> str:
    """Resolve and validate an instance name from config without building a client."""
    if not instance:
        return str(_srv().resolve_default_instance_name())
    try:
        _, instances = _srv().load_instances_config()
    except FileNotFoundError:
        return instance
    if instance not in instances:
        raise ValueError(
            f"Unknown Odoo instance {instance!r}. "
            f"Available instances: {sorted(instances)}"
        )
    return instance


def _app_context(ctx: Context) -> AppContext:
    """Return this server's typed lifespan state."""
    return cast(AppContext, ctx.request_context.lifespan_context)


def _resolve_odoo(ctx: Context, instance: Optional[str]) -> tuple[str, OdooClient]:
    """Resolve the Odoo client for a tool call, honoring the optional instance name."""
    app_context = _app_context(ctx)
    if not instance:
        name = getattr(app_context, "_default_instance_name", None)
        if name is None:
            name = _srv().resolve_default_instance_name()
            app_context._default_instance_name = name
        return name, app_context.odoo
    return cast("tuple[str, OdooClient]", app_context.get_client(instance))


# ---------------------------------------------------------------------------
# Shared infra: field cache, read-field resolution
# ---------------------------------------------------------------------------


def _cached_fields_metadata(
    app_context: AppContext,
    odoo: OdooClient,
    model: str,
    instance_name: str = "default",
) -> Dict[str, Any]:
    """Return fields_get metadata for ``model`` using the lifespan cache."""
    cache_key = f"{instance_name}:{model}"
    cached = app_context.schema_cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    fields_metadata = odoo.get_model_fields(model)
    if isinstance(fields_metadata, dict) and "error" not in fields_metadata:
        app_context.schema_cache[cache_key] = fields_metadata
        return fields_metadata
    return {}


def resolve_read_fields(
    app_context: AppContext,
    odoo: OdooClient,
    model: str,
    fields: Optional[List[str]],
    instance_name: str = "default",
) -> Optional[List[str]]:
    """Pick the field list for read-only tools.

    - ``fields=None`` → smart selection (cap via ODOO_MCP_MAX_SMART_FIELDS).
    - ``fields=["*"]`` → caller wants every field; return None to skip filtering.
    - Otherwise return the caller list unchanged.
    """
    if fields is None:
        metadata = _cached_fields_metadata(app_context, odoo, model, instance_name)
        if not metadata:
            return None
        return select_smart_fields(metadata, max_fields=max_smart_fields())
    if len(fields) == 1 and fields[0] == "*":
        return None
    return fields


# ---------------------------------------------------------------------------
# Write-approval helpers
# ---------------------------------------------------------------------------

MCP_SESSION_ID_HEADER = "mcp-session-id"


def _request_session_id(ctx: Optional[Context]) -> Optional[str]:
    """Return the Streamable HTTP session id of this request, if any.

    ``None`` on stdio, stateless HTTP, and direct Python calls (no request).
    """
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except (AttributeError, ValueError):
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    session_id = headers.get(MCP_SESSION_ID_HEADER)
    return str(session_id) if session_id else None


def current_principal(ctx: Optional[Context] = None) -> Optional[Dict[str, Optional[str]]]:
    """Return the authenticated caller identity, or None when unauthenticated.

    Populated only when the OAuth resource server (``auth.py``) authenticated
    this request: the token's ``(client_id, issuer, subject)`` plus the MCP
    session id when the transport has one. stdio and unauthenticated HTTP
    return None, so approval binding degrades to the legacy per-process store.
    """
    token = get_access_token()
    if token is None:
        return None
    client_id, issuer, subject = principal_components(token)
    return {
        "client_id": client_id,
        "issuer": issuer,
        "subject": subject,
        "session_id": _request_session_id(ctx),
    }



def write_approval_payload(approval: Dict[str, Any]) -> Dict[str, Any]:
    """Return the canonical approval payload fields used for execution."""
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
    return payload


def _sweep_expired_write_approvals(app_context: AppContext, now: float) -> None:
    """Evict expired write-approval records so their contents stop lingering.

    An approval a caller validates but never executes otherwise sits in
    ``app_context.write_approvals`` until *that exact token* is looked up
    again after expiry (``require_validated_write_approval`` only evicts on
    access). That is especially costly for ``*_from_path`` uploads, whose
    ``resolved_binary_values`` hold a full file's base64 content server-side
    (see ``register_write_approval``) — abandoned approvals would otherwise
    keep that content in memory indefinitely.
    """
    expired_tokens = [
        token
        for token, record in app_context.write_approvals.items()
        if now > float(record.get("expires_at", 0))
    ]
    for token in expired_tokens:
        app_context.write_approvals.pop(token, None)


def register_write_approval(
    app_context: AppContext,
    report: Dict[str, Any],
    resolved_binary_values: Optional[Dict[str, str]] = None,
    principal: Optional[Dict[str, Optional[str]]] = None,
) -> bool:
    """Persist validated write approvals inside the current server lifespan.

    ``principal`` (see ``current_principal``) binds the record to the
    authenticated caller that validated it; ``execute_approved_write`` refuses
    any other caller. ``None`` (stdio / unauthenticated HTTP) keeps the record
    usable by any caller of this process, as before.

    ``resolved_binary_values`` (field name -> real base64), when given, is
    stored only in this server-side record — never in ``report["approval"]``,
    which is what gets echoed back to the caller and hashed into the approval
    token. See ``_resolve_binary_from_path_fields`` in ``tools_write.py``.
    """
    approval = report.get("approval")
    if not report.get("success") or not isinstance(approval, dict):
        return False
    token = str(approval.get("token", ""))
    if not token:
        return False
    now = time.time()
    _sweep_expired_write_approvals(app_context, now)
    record: Dict[str, Any] = {
        "approval": dict(approval),
        "payload": write_approval_payload(approval),
        "validated_at": now,
        "expires_at": now + WRITE_APPROVAL_TTL_SECONDS,
        "principal": dict(principal) if principal else None,
    }
    if resolved_binary_values:
        record["resolved_binary_values"] = dict(resolved_binary_values)
    app_context.write_approvals[token] = record
    approval["validated_at"] = now
    approval["expires_at"] = now + WRITE_APPROVAL_TTL_SECONDS
    return True


def require_validated_write_approval(
    app_context: AppContext, approval: Dict[str, Any]
) -> Dict[str, Any] | None:
    """Return a server-side validation record or None when it is missing/expired."""
    token = str(approval.get("token", ""))
    record = app_context.write_approvals.get(token)
    if record is None:
        return None
    if time.time() > float(record.get("expires_at", 0)):
        app_context.write_approvals.pop(token, None)
        return None
    return record


def approval_principal_matches(
    record: Dict[str, Any], principal: Optional[Dict[str, Optional[str]]]
) -> bool:
    """True when ``principal`` is the caller the validation record is bound to.

    Strict equality: an authenticated record never matches an unauthenticated
    caller (or a different client, subject, issuer, or MCP session), and an
    unauthenticated record never matches an authenticated caller.
    """
    return record.get("principal") == (dict(principal) if principal else None)


# ---------------------------------------------------------------------------
# Addon path helpers
# ---------------------------------------------------------------------------


def configured_addons_roots() -> List[Path]:
    """Return trusted local addon roots configured by the operator."""
    roots: List[Path] = []
    for raw_path in os.environ.get("ODOO_ADDONS_PATHS", "").split(os.pathsep):
        if not raw_path:
            continue
        roots.append(Path(raw_path).expanduser().resolve(strict=False))
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def restrict_addons_paths(addons_paths: Optional[List[str]]) -> Optional[List[str]]:
    """Restrict source scans to ODOO_ADDONS_PATHS roots."""
    if addons_paths is None:
        return None
    roots = configured_addons_roots()
    if not roots:
        raise ValueError(
            "scan_addons_source requires ODOO_ADDONS_PATHS when addons_paths are provided."
        )
    restricted_paths: List[str] = []
    for raw_path in addons_paths:
        candidate = Path(raw_path).expanduser().resolve(strict=False)
        if not any(
            candidate == root or _is_relative_to(candidate, root) for root in roots
        ):
            raise ValueError(
                f"{candidate} is outside configured ODOO_ADDONS_PATHS roots."
            )
        restricted_paths.append(str(candidate))
    return restricted_paths


# ---------------------------------------------------------------------------
# Attachment upload path helpers
# ---------------------------------------------------------------------------


def configured_attachment_upload_roots() -> List[Path]:
    """Return trusted local roots operators allow ``*_from_path`` uploads from."""
    roots: List[Path] = []
    for raw_path in os.environ.get("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", "").split(
        os.pathsep
    ):
        if not raw_path:
            continue
        roots.append(Path(raw_path).expanduser().resolve(strict=False))
    return roots


def restrict_attachment_upload_path(raw_path: str) -> Path:
    """Resolve and confine a ``*_from_path`` value to configured upload roots.

    Fails closed: ``ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS`` must be set, and the
    resolved path must sit inside one of its roots. Without this, a
    prompt-injected agent could read and exfiltrate arbitrary local files
    (SSH keys, other clients' data, ...) as an Odoo attachment — mirrors
    ``restrict_addons_paths`` above.
    """
    roots = configured_attachment_upload_roots()
    if not roots:
        raise ValueError(
            "*_from_path uploads require ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS to be "
            "set to one or more trusted local directories."
        )
    candidate = Path(raw_path).expanduser().resolve(strict=False)
    if not any(candidate == root or _is_relative_to(candidate, root) for root in roots):
        raise ValueError(
            f"{candidate} is outside configured ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS."
        )
    return candidate


# ---------------------------------------------------------------------------
# N+1 detection
# ---------------------------------------------------------------------------

N_PLUS_ONE_WINDOW_SECONDS = 60
N_PLUS_ONE_WARN_THRESHOLD = 10
_single_read_lock = threading.Lock()
_single_read_events: Dict[tuple[str, str], List[float]] = {}


def note_single_record_read(instance: str, model: str) -> None:
    """Track read_record calls so health_check can flag N+1 loops."""
    now = time.time()
    cutoff = now - N_PLUS_ONE_WINDOW_SECONDS
    with _single_read_lock:
        events = _single_read_events.setdefault((instance, model), [])
        events.append(now)
        while events and events[0] < cutoff:
            events.pop(0)


def n_plus_one_report() -> Dict[str, Any]:
    """Summarize models hammered by repeated single-record reads."""
    cutoff = time.time() - N_PLUS_ONE_WINDOW_SECONDS
    hot_models: List[Dict[str, Any]] = []
    with _single_read_lock:
        for (instance, model), events in list(_single_read_events.items()):
            recent = [stamp for stamp in events if stamp >= cutoff]
            if recent:
                _single_read_events[(instance, model)] = recent
            else:
                del _single_read_events[(instance, model)]
                continue
            if len(recent) >= N_PLUS_ONE_WARN_THRESHOLD:
                hot_models.append(
                    {
                        "instance": instance,
                        "model": model,
                        "reads_in_window": len(recent),
                        "recommendation": (
                            "Batch with search_records using an "
                            '["id", "in", [...]] domain instead of looping '
                            "read_record."
                        ),
                    }
                )
    return {
        "window_seconds": N_PLUS_ONE_WINDOW_SECONDS,
        "warn_threshold": N_PLUS_ONE_WARN_THRESHOLD,
        "hot_models": hot_models,
    }


# ---------------------------------------------------------------------------
# Security / health posture helpers
# ---------------------------------------------------------------------------


def _side_effect_policy_posture() -> Dict[str, Any]:
    """Summarize where reviewed side-effect methods come from."""
    policy = load_side_effect_policy()
    raw_env = os.environ.get("ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", "")
    env_methods = [item.strip() for item in raw_env.split(",") if item.strip()]
    return {
        "file": policy["path"],
        "file_method_count": len(policy["methods"]),
        "env_method_count": len(env_methods),
        "error": policy["error"],
    }


def instance_posture() -> Dict[str, Any]:
    """Summarize configured Odoo instances without touching the network."""
    try:
        default_name, instances = _srv().load_instances_config()
        return {"instance_count": len(instances), "default_instance": default_name}
    except Exception:
        return {"instance_count": 0, "default_instance": None}


def mcp_surface_counts() -> Dict[str, int]:
    """Read current registered MCP surface counts from MCPServer managers."""
    tool_manager = getattr(mcp, "_tool_manager", None)
    resource_manager = getattr(mcp, "_resource_manager", None)
    prompt_manager = getattr(mcp, "_prompt_manager", None)
    resources = getattr(resource_manager, "_resources", {})
    templates = getattr(resource_manager, "_templates", {})
    return {
        "tool_count": len(getattr(tool_manager, "_tools", {})),
        "resource_count": len(resources) + len(templates),
        "prompt_count": len(getattr(prompt_manager, "_prompts", {})),
    }


def runtime_security_report() -> Dict[str, Any]:
    """Expose MCP runtime safety posture without including secrets."""
    runtime = getattr(mcp, "_odoo_runtime", {})
    security = runtime.get("transport_security")
    broad_unknown_enabled = truthy_env("ODOO_MCP_ALLOW_UNKNOWN_METHODS")
    return {
        "transport": runtime.get("transport", os.environ.get("MCP_TRANSPORT", "stdio")),
        "host": runtime.get("host"),
        "port": runtime.get("port"),
        "streamable_http_path": runtime.get("streamable_http_path"),
        "remote_http_allowed": truthy_env("MCP_ALLOW_REMOTE_HTTP"),
        "write_execution_enabled": writes_enabled(),
        "unknown_execute_method_enabled": broad_unknown_enabled,
        "chatter_direct_enabled": chatter_direct_enabled(),
        "allowed_side_effect_methods": allowed_side_effect_methods(),
        "side_effect_policy": _side_effect_policy_posture(),
        "broad_unknown_method_mode": {
            "enabled": broad_unknown_enabled,
            "risk": ("broad" if broad_unknown_enabled else "off"),
            "recommendation": (
                "Prefer ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS exact entries over "
                "ODOO_MCP_ALLOW_UNKNOWN_METHODS=1."
            ),
        },
        "allowed_hosts": getattr(security, "allowed_hosts", None),
        "allowed_origins": getattr(security, "allowed_origins", None),
        "odoo_instances": instance_posture(),
        "audit_log": audit_posture(),
        "oauth": oauth_posture(),
        "field_acl": field_policy_posture(),
        "n_plus_one": n_plus_one_report(),
        "notes": [
            "HTTP transports are local-only by default in the CLI entry point.",
            "execute_approved_write requires ODOO_MCP_ENABLE_WRITES and confirm=true.",
            "execute_method blocks standard destructive methods and unreviewed side-effect methods by default.",
        ],
    }


# ---------------------------------------------------------------------------
# Elicitation (write tools use this)
# ---------------------------------------------------------------------------

ELICIT_WRITES_ENV = "ODOO_MCP_ELICIT_WRITES"


class WriteConfirmation(BaseModel):
    """Elicitation schema for human write approval (primitive fields only)."""

    approve: bool = Field(description="Approve executing this Odoo write?")


# ---------------------------------------------------------------------------
# MCP Resources (registered here so mcp instance owns them)
# ---------------------------------------------------------------------------


@mcp.resource(
    "odoo://models",
    description="List all available models in the Odoo system (default Odoo instance)",
    mime_type="application/json",
    annotations=RESOURCE_HINT,
)
def get_models() -> str:
    """Lists all available models in the Odoo system"""
    odoo_client = _srv().get_odoo_client()
    models = odoo_client.get_models()
    return json.dumps(models, indent=2)


@mcp.resource(
    "odoo://model/{model_name}",
    description="Get detailed information about a specific model including fields (default Odoo instance)",
    mime_type="application/json",
    annotations=RESOURCE_HINT,
)
def get_model_info(model_name: str) -> str:
    """Get information about a specific model."""
    odoo_client = _srv().get_odoo_client()
    try:
        validate_model_name(model_name)
        model_info = odoo_client.get_model_info(model_name)
        fields = odoo_client.get_model_fields(model_name)
        model_info["fields"] = fields
        return json.dumps(model_info, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.resource(
    "odoo://record/{model_name}/{record_id}",
    description="Get detailed information of a specific record by ID (default Odoo instance)",
    mime_type="application/json",
    annotations=RESOURCE_HINT,
)
def get_record(model_name: str, record_id: str) -> str:
    """Get a specific record by ID."""
    odoo_client = _srv().get_odoo_client()
    try:
        validate_model_name(model_name)
        record_id_int = int(record_id)
        if record_id_int < 1:
            raise ValueError("record_id must be greater than 0")
        record = odoo_client.read_records(model_name, [record_id_int])
        if not record:
            return json.dumps(
                {"error": f"Record not found: {model_name} ID {record_id}"}, indent=2
            )
        instance_name = _srv().resolve_default_instance_name()
        filtered, redacted = get_field_policy().redact_record(
            instance_name, model_name, record[0]
        )
        payload: Dict[str, Any] = dict(filtered)
        if redacted:
            payload["_redacted_fields"] = redacted
        return json.dumps(payload, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.resource(
    "odoo://search/{model_name}/{domain}",
    description="Search for records matching the domain (default Odoo instance)",
    mime_type="application/json",
    annotations=RESOURCE_HINT,
)
def search_records_resource(model_name: str, domain: str) -> str:
    """Search for records that match a domain."""
    odoo_client = _srv().get_odoo_client()
    try:
        validate_model_name(model_name)
        domain_list = normalize_domain_input(domain)
        results = odoo_client.search_read(model_name, domain_list, limit=10)
        instance_name = _srv().resolve_default_instance_name()
        filtered, redacted = get_field_policy().redact_records(
            instance_name, model_name, results
        )
        if redacted:
            return json.dumps(
                {"results": filtered, "_redacted_fields": redacted}, indent=2
            )
        return json.dumps(filtered, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# ---------------------------------------------------------------------------
# Plugin loading + per-deployment tool filtering (driven from server.py)
# ---------------------------------------------------------------------------

PLUGIN_STATE: Dict[str, Any] = {
    "enabled": [],
    "loaded": [],
    "failed": {},
    "tools_filtered": [],
}


def plugin_posture() -> Dict[str, Any]:
    """Non-secret plugin/filter posture for health_check."""
    return {
        "enabled": list(PLUGIN_STATE["enabled"]),
        "loaded": list(PLUGIN_STATE["loaded"]),
        "failed": dict(PLUGIN_STATE["failed"]),
        "tools_filtered": list(PLUGIN_STATE["tools_filtered"]),
    }


def load_plugins(plugin_api: Any) -> None:
    """Load opt-in third-party tool plugins (entry points ``odoo_mcp.tools``).

    Installation alone never activates code: only names listed in
    ``ODOO_MCP_PLUGINS`` load, and each plugin is isolated — a raising
    plugin is recorded in health_check, never fatal. The caller (server.py,
    top layer) passes the ``odoo_mcp.plugin_api`` module so this bottom
    layer never imports the surface.
    """
    raw = os.environ.get("ODOO_MCP_PLUGINS", "")
    requested = [name.strip() for name in raw.split(",") if name.strip()]
    PLUGIN_STATE["enabled"] = requested
    PLUGIN_STATE["loaded"] = []
    PLUGIN_STATE["failed"] = {}
    if not requested:
        return

    from importlib.metadata import entry_points

    found = {ep.name: ep for ep in entry_points(group="odoo_mcp.tools")}

    for name in requested:
        entry = found.get(name)
        if entry is None:
            PLUGIN_STATE["failed"][name] = (
                "no installed package exposes odoo_mcp.tools entry point "
                f"named {name!r}"
            )
            continue
        try:
            register = entry.load()
            register(plugin_api)
            PLUGIN_STATE["loaded"].append(name)
        except Exception as exc:  # noqa: BLE001 — plugin faults must not kill the server
            PLUGIN_STATE["failed"][name] = f"{type(exc).__name__}: {exc}"


def apply_tool_filter() -> None:
    """Trim the registered tool surface per deployment.

    ``ODOO_MCP_TOOLS_INCLUDE`` / ``ODOO_MCP_TOOLS_EXCLUDE`` take CSV fnmatch
    globs (e.g. ``search_*,read_record``). Include (when set) keeps only
    matching tools; exclude then removes matches. Applies to builtin and
    plugin tools alike; removed names are listed in health_check.
    """
    from fnmatch import fnmatch

    include = [
        p.strip()
        for p in os.environ.get("ODOO_MCP_TOOLS_INCLUDE", "").split(",")
        if p.strip()
    ]
    exclude = [
        p.strip()
        for p in os.environ.get("ODOO_MCP_TOOLS_EXCLUDE", "").split(",")
        if p.strip()
    ]
    PLUGIN_STATE["tools_filtered"] = []
    if not include and not exclude:
        return
    registry = getattr(mcp._tool_manager, "_tools", None)
    if not isinstance(registry, dict):  # unexpected SDK shape — do nothing
        return
    removed = []
    for name in list(registry):
        keep = (not include or any(fnmatch(name, pat) for pat in include)) and (
            not any(fnmatch(name, pat) for pat in exclude)
        )
        if not keep:
            del registry[name]
            removed.append(name)
    PLUGIN_STATE["tools_filtered"] = sorted(removed)
