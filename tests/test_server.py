import asyncio
import importlib
import json
import xmlrpc.client
from pathlib import Path

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import ElicitResult, TextContent


def call_tool_json(server, name, arguments):
    content = asyncio.run(server.mcp.call_tool(name, arguments))
    if hasattr(content, "structured_content"):
        structured = content.structured_content
        if isinstance(structured, dict):
            return structured.get("result", structured)
        content = content.content
    if isinstance(content, tuple):
        content, structured = content
        if isinstance(structured, dict):
            return structured.get("result", structured)
    if content and isinstance(content[0], list):
        content = content[0]
    assert isinstance(content[0], TextContent)
    return json.loads(content[0].text)


class FakeRequest:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeCtx:
    def __init__(self, odoo, clients=None):
        self.request_context = FakeRequest(FakeLife(odoo, clients))


class FakeLife:
    def __init__(self, odoo, clients=None):
        self.odoo = odoo
        self.schema_cache = {}
        self.write_approvals = {}
        self._named_clients = dict(clients or {})

    def get_client(self, instance=None):
        if not instance:
            return "default", self.odoo
        if instance in self._named_clients:
            return instance, self._named_clients[instance]
        raise ValueError(
            f"Unknown Odoo instance {instance!r}. "
            f"Available instances: {sorted(self._named_clients)}"
        )


def test_server_import_initializes_mcpserver_with_current_sdk_without_lifespan():
    server = importlib.import_module("odoo_mcp.server")

    assert isinstance(server.mcp, MCPServer)
    assert server.mcp.name == "Odoo MCP Server"
    assert server.mcp.instructions == "MCP Server for interacting with Odoo ERP systems"


def test_server_negotiates_modern_and_legacy_protocol_eras():
    server = importlib.import_module("odoo_mcp.server")

    async def negotiate(mode):
        async with Client(server.mcp, mode=mode) as client:
            tools = await client.list_tools()
            return client.protocol_version, {tool.name for tool in tools.tools}

    modern_version, modern_tools = asyncio.run(negotiate("auto"))
    legacy_version, legacy_tools = asyncio.run(negotiate("legacy"))

    assert modern_version == "2026-07-28"
    assert legacy_version == "2025-11-25"
    assert modern_tools == legacy_tools


def test_modern_and_legacy_protocols_use_write_confirmation(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    questions = []
    monkeypatch.setenv(server.ELICIT_WRITES_ENV, "1")

    async def decline(_context, params):
        questions.append(params.message)
        return ElicitResult(action="decline")

    async def call(mode):
        async with Client(
            server.mcp, mode=mode, elicitation_callback=decline
        ) as client:
            result = await client.call_tool(
                "execute_approved_write",
                {
                    "approval": {
                        "model": "res.partner",
                        "operation": "write",
                        "record_ids": [7],
                        "values": {"name": "Ada"},
                        "token": "bogus",
                    },
                    "confirm": True,
                },
            )
            return client.protocol_version, result

    modern_version, modern_result = asyncio.run(call("auto"))
    legacy_version, legacy_result = asyncio.run(call("legacy"))

    assert modern_version == "2026-07-28"
    assert legacy_version == "2025-11-25"
    assert len(questions) == 2
    assert all("write on res.partner" in question for question in questions)
    assert "declined" in modern_result.content[0].text
    assert "declined" in legacy_result.content[0].text


def test_server_registers_expected_tools_and_resources_without_lifespan():
    server = importlib.import_module("odoo_mcp.server")

    tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    resources = {
        str(resource.uri) for resource in asyncio.run(server.mcp.list_resources())
    }
    templates = {
        str(template.uri_template)
        for template in asyncio.run(server.mcp.list_resource_templates())
    }

    expected_tools = {
        "execute_method",
        "list_models",
        "get_model_fields",
        "search_records",
        "read_record",
        "search_employee",
        "search_holidays",
        "diagnose_odoo_call",
        "diagnose_access",
        "inspect_model_relationships",
        "generate_json2_payload",
        "upgrade_risk_report",
        "fit_gap_report",
        "get_odoo_profile",
        "schema_catalog",
        "preview_write",
        "validate_write",
        "execute_approved_write",
        "scan_addons_source",
        "build_domain",
        "business_pack_report",
        "health_check",
        "aggregate_records",
        "chatter_post",
        "list_instances",
        "lookup_model_history",
        "read_attachment",
        "index_knowledge",
        "search_knowledge",
        "knowledge_stats",
        "receivable_payable_aging",
        "accounting_health_summary",
        "submit_async_task",
        "get_async_task",
        "cancel_async_task",
        "list_async_tasks",
        "search_across_instances",
        "aggregate_across_instances",
        "accounting_health_across_instances",
    }
    assert expected_tools <= tools
    assert len(tools) == 41
    assert "odoo://models" in resources
    assert {
        "odoo://model/{model_name}",
        "odoo://record/{model_name}/{record_id}",
        "odoo://search/{model_name}/{domain}",
    } <= templates

    prompts = {prompt.name for prompt in asyncio.run(server.mcp.list_prompts())}
    assert {
        "diagnose_failed_odoo_call",
        "fit_gap_workshop",
        "json2_migration_plan",
        "safe_write_review",
        "custom_module_audit",
    } <= prompts


def test_tools_expose_safety_annotations_and_output_schemas():
    server = importlib.import_module("odoo_mcp.server")

    tools = {
        tool.name: tool.model_dump(by_alias=True)
        for tool in asyncio.run(server.mcp.list_tools())
    }

    assert tools["list_models"]["annotations"]["readOnlyHint"] is True
    assert tools["list_models"]["annotations"]["destructiveHint"] is False
    assert tools["diagnose_access"]["annotations"]["readOnlyHint"] is True
    assert tools["preview_write"]["annotations"]["destructiveHint"] is False
    assert tools["execute_approved_write"]["annotations"]["destructiveHint"] is True
    assert tools["execute_method"]["annotations"]["destructiveHint"] is True
    assert tools["get_odoo_profile"]["outputSchema"]["type"] == "object"


def test_resources_are_json_with_assistant_annotations():
    server = importlib.import_module("odoo_mcp.server")

    resources = {
        str(resource.uri): resource.model_dump(by_alias=True)
        for resource in asyncio.run(server.mcp.list_resources())
    }
    templates = {
        str(template.uri_template): template.model_dump(by_alias=True)
        for template in asyncio.run(server.mcp.list_resource_templates())
    }

    assert resources["odoo://models"]["mimeType"] == "application/json"
    assert resources["odoo://models"]["annotations"]["audience"] == ["assistant"]
    assert templates["odoo://model/{model_name}"]["mimeType"] == "application/json"


def test_domain_normalization_accepts_json_object_and_standard_domain_list():
    server = importlib.import_module("odoo_mcp.server")

    assert server.normalize_domain_input('[["name", "ilike", "ada"]]') == [
        ["name", "ilike", "ada"]
    ]
    assert server.normalize_domain_input(
        {"conditions": [{"field": "id", "operator": ">", "value": 0}]}
    ) == [["id", ">", 0]]
    assert server.normalize_domain_input(
        ["|", ["name", "=", "Ada"], ["id", ">", 0]]
    ) == [
        "|",
        ["name", "=", "Ada"],
        ["id", ">", 0],
    ]
    assert server.normalize_domain_input('[("tag_ids", "in", [7])]') == [
        ["tag_ids", "in", [7]]
    ]


def test_safety_helpers_reject_bad_model_names_and_bounds_limits():
    server = importlib.import_module("odoo_mcp.server")

    assert server.clamp_limit(999) == 100
    server.validate_model_name("res.partner")
    server.validate_method_name("search_read")

    try:
        server.validate_model_name("res.partner;DROP")
    except ValueError as exc:
        assert "Invalid model name" in str(exc)
    else:
        raise AssertionError("unsafe model name should be rejected")

    try:
        server.validate_method_name("search-read")
    except ValueError as exc:
        assert "Invalid method name" in str(exc)
    else:
        raise AssertionError("unsafe method name should be rejected")

    try:
        server.clamp_limit(0)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("zero limit should be rejected")


def test_lifespan_is_lazy_and_preview_tools_call_tool_succeed_when_client_raises(
    monkeypatch,
):
    server = importlib.import_module("odoo_mcp.server")

    def fail_client():
        raise AssertionError("get_odoo_client should not be called for preview tools")

    monkeypatch.setattr(server, "get_odoo_client", fail_client)

    async def enter_lifespan():
        async with server.app_lifespan(server.mcp) as lifespan_context:
            return lifespan_context

    context = asyncio.run(enter_lifespan())
    assert context._odoo is None

    payload = call_tool_json(
        server,
        "generate_json2_payload",
        {
            "model": "res.partner",
            "method": "search_read",
            "args": [[["id", ">", 0]]],
            "kwargs": {"fields": ["name"], "limit": 1},
            "database": "demo-db",
        },
    )
    assert payload["success"] is True
    assert payload["metadata_used"]["client_instantiated"] is False
    assert payload["headers"]["X-Odoo-Database"] == "demo-db"

    diagnosis = call_tool_json(
        server,
        "diagnose_odoo_call",
        {"model": "res.partner", "method": "write", "args": [[7], {"name": "Ada"}]},
    )
    assert diagnosis["classification"]["safety"] == "destructive"

    upgrade = call_tool_json(
        server,
        "upgrade_risk_report",
        {
            "target_version": "20.0",
            "methods": [{"model": "res.partner", "method": "write"}],
        },
    )
    assert upgrade["transport"]["xmlrpc_jsonrpc_deprecation"] == "Odoo 22 fall 2028"

    fit_gap = call_tool_json(
        server,
        "fit_gap_report",
        {"requirements": ["Track contacts", "Complex custom workflow"]},
    )
    assert fit_gap["success"] is True
    assert fit_gap["summary"]["fit"] >= 1


def test_report_tools_do_not_execute_candidate_methods():
    server = importlib.import_module("odoo_mcp.server")

    class ExplodingClient:
        def execute_method(self, *args, **kwargs):
            raise AssertionError("diagnostic tools must not execute candidate methods")

        def get_model_fields(self, model):
            return {"partner_id": {"type": "many2one", "relation": "res.partner"}}

    assert server.diagnose_odoo_call("res.partner", "write")["success"] is True
    assert server.generate_json2_payload("res.partner", "write")["success"] is True
    assert (
        server.upgrade_risk_report(
            methods=[{"model": "res.partner", "method": "write"}]
        )["success"]
        is True
    )
    assert server.fit_gap_report(["Track contacts"])["success"] is True
    relationships = server.inspect_model_relationships(
        FakeCtx(ExplodingClient()),
        "res.partner",
        use_live_metadata=True,
    )
    assert relationships["success"] is True
    assert relationships["relationships"]["many2one"][0]["relation"] == "res.partner"


def test_inspect_model_relationships_uses_only_get_model_fields_for_live_metadata():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def get_model_fields(self, model):
            calls.append(("get_model_fields", model))
            return {"partner_id": {"type": "many2one", "relation": "res.partner"}}

        def execute_method(self, *args, **kwargs):
            calls.append(("execute_method", args, kwargs))
            raise AssertionError("execute_method must not be used")

    report = server.inspect_model_relationships(FakeCtx(FakeClient()), "res.partner")

    assert report["success"] is True
    assert calls == [("get_model_fields", "res.partner")]


def test_new_tools_return_stable_top_level_response_keys():
    server = importlib.import_module("odoo_mcp.server")

    payload = call_tool_json(
        server,
        "generate_json2_payload",
        {"model": "res.partner", "method": "search_read", "args": [[]]},
    )
    assert {
        "success",
        "tool",
        "model",
        "method",
        "endpoint",
        "headers",
        "body",
        "warnings",
        "transaction",
        "classification",
        "metadata_used",
    } <= payload.keys()

    diagnosis = call_tool_json(
        server,
        "diagnose_odoo_call",
        {"model": "res.partner", "method": "search_read", "args": [[]]},
    )
    assert {
        "success",
        "tool",
        "classification",
        "issues",
        "suggested_payload",
    } <= diagnosis.keys()

    access = server.diagnose_access(
        FakeCtx(AccessDiagnosticClient()),
        "res.partner",
        "read",
        expected_count=1,
    )
    assert {"success", "tool", "diagnosis", "access", "rules"} <= access.keys()

    upgrade = call_tool_json(server, "upgrade_risk_report", {"target_version": "20.0"})
    assert {"success", "tool", "summary", "risks", "transport"} <= upgrade.keys()

    fit_gap = call_tool_json(
        server, "fit_gap_report", {"requirements": ["Track contacts"]}
    )
    assert {"success", "tool", "summary", "items", "metadata_used"} <= fit_gap.keys()


def test_safe_write_preview_validate_and_execute_gates(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    preview = call_tool_json(
        server,
        "preview_write",
        {
            "model": "res.partner",
            "operation": "write",
            "record_ids": [7],
            "values": {"name": "Ada"},
        },
    )

    assert preview["success"] is True
    assert preview["approval"]["token"].startswith("odoo-write:")

    class FakeClient:
        def get_model_fields(self, model):
            return {"name": {"type": "char", "readonly": False}}

        def execute_method(self, *args, **kwargs):
            raise AssertionError("writes must stay disabled without env gate")

    validation = server.validate_write(
        FakeCtx(FakeClient()),
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
    )
    assert validation["success"] is True
    assert validation["approval"]["token"] == preview["approval"]["token"]

    monkeypatch.delenv("ODOO_MCP_ENABLE_WRITES", raising=False)
    preview_only_blocked = server.execute_approved_write(
        FakeCtx(FakeClient()), preview["approval"], confirm=True
    )
    assert preview_only_blocked["success"] is False
    assert "validated" in preview_only_blocked["error"]

    ctx = FakeCtx(FakeClient())
    validation = server.validate_write(
        ctx,
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
    )
    assert validation["success"] is True
    assert validation["approval"]["token"] == preview["approval"]["token"]

    blocked = server.execute_approved_write(ctx, validation["approval"], confirm=True)
    assert blocked["success"] is False
    assert "disabled" in blocked["error"]

    bad = dict(preview["approval"])
    bad["values"] = {"name": "Grace"}
    rejected = server.execute_approved_write(ctx, bad, confirm=True)
    assert rejected["success"] is False
    assert "token" in rejected["error"]


def test_execute_method_validates_model_and_method_before_client_call():
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            raise AssertionError("invalid input must fail before client call")

    result = server.execute_method(FakeCtx(FakeClient()), "res.partner", "bad-method")

    assert result["success"] is False
    assert "Invalid method name" in result["error"]


def test_execute_method_blocks_direct_writes_and_unknown_methods(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            raise AssertionError("blocked methods must fail before client call")

    monkeypatch.delenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", raising=False)

    blocked_write = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "write",
        args=[[7], {"name": "Ada"}],
    )
    assert blocked_write["success"] is False
    assert "preview_write" in blocked_write["error"]

    blocked_unknown = server.execute_method(
        FakeCtx(FakeClient()),
        "sale.order",
        "action_confirm",
        args=[[7]],
    )
    assert blocked_unknown["success"] is False
    assert "blocked by default" in blocked_unknown["error"]
    assert blocked_unknown["classification"]["safety"] == "side_effect"


def test_execute_method_can_opt_into_unknown_methods(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

    monkeypatch.setenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", "1")

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "sale.order",
        "action_confirm",
        args=[[7]],
    )

    assert result["success"] is True
    assert calls == [(("sale.order", "action_confirm", [7]), {})]


def test_execute_method_allows_exact_side_effect_allowlist(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

    monkeypatch.delenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", raising=False)
    monkeypatch.setenv(
        "ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", "sale.order.action_confirm"
    )

    allowed = server.execute_method(
        FakeCtx(FakeClient()),
        "sale.order",
        "action_confirm",
        args=[[7]],
    )
    blocked = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "message_post",
        args=[[7]],
    )

    assert allowed["success"] is True
    assert calls == [(("sale.order", "action_confirm", [7]), {})]
    assert blocked["success"] is False
    assert "Unreviewed side-effect" in blocked["error"]
    assert "odoo_mcp_policy.json" in blocked["error"]


def test_execute_method_translates_none_marshal_fault(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            raise xmlrpc.client.Fault(
                1,
                "Traceback (most recent call last): ... TypeError: cannot "
                "marshal None unless allow_none is enabled",
            )

    monkeypatch.delenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", raising=False)
    monkeypatch.setenv(
        "ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", "account.move.button_draft"
    )

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "account.move",
        "button_draft",
        args=[[394304]],
    )

    assert result["success"] is True
    assert result["result"] is None
    assert "committed" in result["warning"]


def test_execute_method_rejects_invalid_search_domain_without_rpc():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return []

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "search_read",
        args=["not-a-domain"],
    )

    assert result["success"] is False
    assert "domain must be" in result["error"]
    assert calls == []


def test_execute_method_rejects_invalid_kwargs_domain_without_rpc():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return []

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "search_read",
        kwargs={"domain": "not-a-domain"},
    )

    assert result["success"] is False
    assert "domain must be" in result["error"]
    assert calls == []


def test_execute_method_forwards_kwargs_tuple_domain():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return []

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "search_read",
        kwargs={"domain": '[("tag_ids", "in", [7])]'},
    )

    assert result["success"] is True
    assert calls == [
        (("res.partner", "search_read"), {"domain": [["tag_ids", "in", [7]]]})
    ]


def test_execute_method_normalizes_name_search_domain_arg():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return []

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "name_search",
        args=["Ada", '[("is_company", "=", True)]'],
    )

    assert result["success"] is True
    assert calls == [
        (("res.partner", "name_search", "Ada", [["is_company", "=", True]]), {})
    ]


def test_execute_method_rejects_invalid_name_search_args_kwarg_without_rpc():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return []

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "name_search",
        kwargs={"args": "not-a-domain"},
    )

    assert result["success"] is False
    assert "domain must be" in result["error"]
    assert calls == []


def test_execute_method_normalizes_positional_domain_for_mapped_methods(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", "1")
    raw = '[("tag_ids", "in", [7])]'
    expected = [["tag_ids", "in", [7]]]
    cases = [
        ("search", [raw], (expected,)),
        ("search_count", [raw], (expected,)),
        ("search_read", [raw], (expected,)),
        ("read_group", [raw], (expected,)),
        ("formatted_read_group", [raw], (expected,)),
        ("name_search", ["Ada", raw], ("Ada", expected)),
    ]

    for method, incoming, expected_tail in cases:
        calls = []

        class FakeClient:
            def execute_method(self, *args, **kwargs):
                calls.append((args, kwargs))
                return []

        result = server.execute_method(
            FakeCtx(FakeClient()), "res.partner", method, args=incoming
        )
        assert result["success"] is True, method
        assert calls == [(("res.partner", method, *expected_tail), {})], method


def test_execute_method_leaves_unmapped_custom_domain_kwarg_unchanged(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", "1")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "custom_lookup",
        kwargs={"domain": "example.com"},
    )

    assert result["success"] is True
    assert calls == [
        (("res.partner", "custom_lookup"), {"domain": "example.com"})
    ]


def test_execute_method_forwards_name_search_args_kwarg_tuple_domain():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return []

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "res.partner",
        "name_search",
        kwargs={"name": "Ada", "args": '[("tag_ids", "in", [7])]'},
    )

    assert result["success"] is True
    assert calls == [
        (
            ("res.partner", "name_search"),
            {"name": "Ada", "args": [["tag_ids", "in", [7]]]},
        )
    ]


def test_execute_method_surfaces_other_faults(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            raise xmlrpc.client.Fault(1, "AccessError: operation not allowed")

    monkeypatch.setenv(
        "ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", "account.move.button_draft"
    )

    result = server.execute_method(
        FakeCtx(FakeClient()),
        "account.move",
        "button_draft",
        args=[[394304]],
    )

    assert result["success"] is False
    assert "AccessError" in result["error"]


def test_validate_write_only_registers_live_metadata_approvals(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def execute_method(self, *args, **kwargs):
            raise AssertionError("non-registered approvals must fail before execution")

    ctx = FakeCtx(FakeClient())
    shape_only = server.validate_write(
        ctx,
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        use_live_metadata=False,
    )

    assert shape_only["success"] is True
    assert shape_only["approval_status"]["stored"] is False
    assert ctx.request_context.lifespan_context.write_approvals == {}

    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    blocked = server.execute_approved_write(ctx, shape_only["approval"], confirm=True)
    assert blocked["success"] is False
    assert "validated" in blocked["error"]

    empty_metadata = server.validate_write(
        FakeCtx(FakeClient()),
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        fields_metadata={},
        use_live_metadata=False,
    )
    assert empty_metadata["success"] is False
    assert empty_metadata["issues"][0]["code"] == "unknown_field"
    assert empty_metadata["approval"] is None


def test_validate_write_rejects_empty_live_metadata_for_unlink(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class EmptyMetadataClient:
        def get_model_fields(self, model):
            return {}

        def execute_method(self, *args, **kwargs):
            raise AssertionError("empty metadata must fail before execution")

    ctx = FakeCtx(EmptyMetadataClient())
    validation = server.validate_write(
        ctx,
        "res.partner",
        "unlink",
        record_ids=[7],
    )

    assert validation["success"] is False
    assert "metadata was empty" in validation["error"]
    assert validation["approval_status"]["stored"] is False
    assert ctx.request_context.lifespan_context.write_approvals == {}

    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    preview = server.preview_write("res.partner", "unlink", record_ids=[7])
    blocked = server.execute_approved_write(ctx, preview["approval"], confirm=True)
    assert blocked["success"] is False
    assert "validated" in blocked["error"]


def test_execute_approved_write_runs_only_after_all_gates(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def get_model_fields(self, model):
            return {"name": {"type": "char", "readonly": False}}

        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return True

    calls = []
    ctx = FakeCtx(FakeClient())
    validation = server.validate_write(
        ctx,
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        context={"lang": "en_US"},
    )

    assert validation["approval_status"]["stored"] is True
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, validation["approval"], confirm=True)

    assert result["success"] is True
    assert calls == [
        (
            ("res.partner", "write", [7], {"name": "Ada"}),
            {"context": {"lang": "en_US"}},
        )
    ]


def test_schema_catalog_caches_and_business_pack_uses_live_metadata():
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def __init__(self):
            self.model_calls = 0

        def get_models(self):
            self.model_calls += 1
            return {
                "model_names": ["res.partner", "sale.order"],
                "models_details": {
                    "res.partner": {"name": "Contact"},
                    "sale.order": {"name": "Sales Order"},
                },
            }

        def get_model_fields(self, model):
            return {"name": {"type": "char"}}

        def get_installed_modules(self, limit=100):
            return [{"name": "sale"}, {"name": "crm"}]

    fake = FakeClient()
    ctx = FakeCtx(fake)
    first = server.schema_catalog(ctx, query="sale", include_fields=True)
    second = server.schema_catalog(ctx, query="sale", include_fields=True)
    pack = server.business_pack_report(ctx, "sales")

    assert first["success"] is True
    assert first["result"][0]["model"] == "sale.order"
    assert second["metadata_used"]["cache_hit"] is True
    assert fake.model_calls == 2
    assert pack["success"] is True
    assert "sale" in pack["installed_modules"]


def test_domain_builder_and_addon_scanner(tmp_path: Path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    domain = call_tool_json(
        server,
        "build_domain",
        {
            "conditions": [
                {"field": "name", "operator": "ilike", "value": "Ada"},
                {"field": "id", "operator": "in", "value": [1, 2]},
            ],
            "logical_operator": "or",
        },
    )
    assert domain["success"] is True
    assert domain["domain"][0] == "|"

    addon = tmp_path / "custom_sale"
    addon.mkdir()
    (addon / "__manifest__.py").write_text(
        "{'name': 'Custom Sale', 'depends': ['sale'], 'installable': True}",
        encoding="utf-8",
    )
    (addon / "models.py").write_text(
        "from odoo import api, fields, models\n"
        "class X(models.Model):\n"
        "    _name = 'x.demo'\n"
        "    amount = fields.Float(compute='_compute_amount')\n"
        "    total = fields.Float(compute='_compute_total')\n"
        "    def _compute_amount(self):\n"
        "        for record in self:\n"
        "            record.amount = record.qty * record.price\n"
        "    @api.depends('qty', 'price')\n"
        "    def _compute_total(self):\n"
        "        for record in self:\n"
        "            record.total = record.qty * record.price\n"
        "    def create(self, vals):\n"
        "        return {'id': 1}\n"
        "    def write(self, vals):\n"
        "        result = super().write(vals)\n"
        "        return result\n"
        "    def unlink(self):\n"
        "        super().unlink()\n"
        "        return True\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ODOO_ADDONS_PATHS", str(tmp_path))
    scan = call_tool_json(
        server,
        "scan_addons_source",
        {"addons_paths": [str(tmp_path)], "max_files": 20},
    )

    assert scan["success"] is True
    assert scan["summary"]["modules"] == 1
    codes = {item["code"] for item in scan["source_findings"]}
    assert "custom_method" in codes
    assert "computed_method_missing_depends" in codes
    assert "computed_depends_missing_fields" not in codes
    assert "crud_override_missing_super" in codes
    assert "crud_override_super_not_returned" in codes

    blocked_scan = call_tool_json(
        server,
        "scan_addons_source",
        {"addons_paths": [str(tmp_path.parent)], "max_files": 20},
    )
    assert blocked_scan["success"] is False
    assert "outside configured ODOO_ADDONS_PATHS" in blocked_scan["error"]


def test_profile_health_and_prompts_are_available():
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        url = "https://odoo.example.test"
        hostname = "odoo.example.test"
        db = "demo-db"
        username = "demo"
        transport = "xmlrpc"
        timeout = 3
        verify_ssl = True
        json2_database_header = True

        def get_server_version(self):
            return {"server_version": "19.0"}

        def get_user_context(self):
            return {"lang": "en_US"}

    profile = server.get_odoo_profile(
        FakeCtx(FakeClient()), include_modules=False, module_limit=10
    )
    assert profile["success"] is True
    assert profile["profile"]["server_version"]["server_version"] == "19.0"

    health = call_tool_json(server, "health_check", {})
    assert health["success"] is True
    assert health["server"]["tool_count"] == 41
    assert health["runtime"]["chatter_direct_enabled"] is False
    assert health["runtime"]["broad_unknown_method_mode"]["enabled"] is False

    prompt = asyncio.run(
        server.mcp.get_prompt(
            "safe_write_review",
            {"model": "res.partner", "operation": "write"},
        )
    )
    assert "execute_approved_write" in prompt.messages[0].content.text


class AccessDiagnosticClient:
    uid = 7

    def __init__(self, *, fail_acl=False, fail_rules=False, count=1):
        self.fail_acl = fail_acl
        self.fail_rules = fail_rules
        self.count = count

    def get_user_context(self):
        return {"lang": "en_US", "uid": 7}

    def execute_method(self, model, method, *args, **kwargs):
        if model == "ir.model" and method == "search_read":
            return [{"id": 11, "name": "Contact", "model": "res.partner"}]
        if model == "res.users" and method == "fields_get":
            return {
                "id": {"type": "integer"},
                "name": {"type": "char"},
                "groups_id": {"type": "many2many", "relation": "res.groups"},
                "company_id": {"type": "many2one", "relation": "res.company"},
                "company_ids": {"type": "many2many", "relation": "res.company"},
            }
        if model == "res.users" and method == "read":
            return [{"id": 7, "name": "Demo", "groups_id": [3, 9]}]
        if model == "ir.model.access" and method == "search_read":
            if self.fail_acl:
                raise ValueError("Access denied reading ir.model.access")
            return [
                {
                    "id": 21,
                    "name": "partner user",
                    "model_id": [11, "Contact"],
                    "group_id": [3, "Sales / User"],
                    "perm_read": True,
                    "perm_write": False,
                    "perm_create": False,
                    "perm_unlink": False,
                }
            ]
        if model == "ir.rule" and method == "search_read":
            if self.fail_rules:
                raise ValueError("Access denied reading ir.rule")
            return [
                {
                    "id": 31,
                    "name": "own contacts",
                    "model_id": [11, "Contact"],
                    "domain_force": "[('user_id', '=', user.id)]",
                    "groups": [3],
                    "active": True,
                    "perm_read": True,
                    "perm_write": True,
                    "perm_create": True,
                    "perm_unlink": True,
                }
            ]
        if model == "res.partner" and method == "search_count":
            return self.count
        raise AssertionError(f"unexpected call: {model}.{method}")


def test_diagnose_access_reports_acl_rules_and_count_mismatch():
    server = importlib.import_module("odoo_mcp.server")

    report = server.diagnose_access(
        FakeCtx(AccessDiagnosticClient(count=1)),
        "res.partner",
        "read",
        expected_count=2,
    )

    codes = {item["code"] for item in report["diagnosis"]["codes"]}
    assert report["success"] is True
    assert report["permission_field"] == "perm_read"
    assert report["actual_count"] == 1
    assert report["access"]["granting_count"] == 1
    assert report["rules"]["group_bound"][0]["name"] == "own contacts"
    assert "record_rule_filter_likely" in codes


def test_diagnose_access_classifies_observed_error():
    server = importlib.import_module("odoo_mcp.server")

    report = server.diagnose_access(
        FakeCtx(AccessDiagnosticClient(count=1)),
        "res.partner",
        "read",
        observed_error=(
            "Due to security restrictions, you are not allowed to access "
            "'Contact' (res.partner) records."
        ),
    )

    assert report["success"] is True
    assert report["error_classification"]["category"] == "record_rule"

    report_without_error = server.diagnose_access(
        FakeCtx(AccessDiagnosticClient(count=1)), "res.partner", "read"
    )
    assert report_without_error["error_classification"] is None


def test_diagnose_access_uses_odoo19_group_ids_field():
    server = importlib.import_module("odoo_mcp.server")

    class Odoo19AccessDiagnosticClient(AccessDiagnosticClient):
        def execute_method(self, model, method, *args, **kwargs):
            if model == "res.users" and method == "fields_get":
                return {
                    "id": {"type": "integer"},
                    "name": {"type": "char"},
                    "group_ids": {"type": "many2many", "relation": "res.groups"},
                    "all_group_ids": {"type": "many2many", "relation": "res.groups"},
                    "company_id": {"type": "many2one", "relation": "res.company"},
                    "company_ids": {"type": "many2many", "relation": "res.company"},
                }
            if model == "res.users" and method == "read":
                return [
                    {
                        "id": 7,
                        "name": "Demo",
                        "group_ids": [3],
                        "all_group_ids": [3, 9],
                    }
                ]
            return super().execute_method(model, method, *args, **kwargs)

    report = server.diagnose_access(
        FakeCtx(Odoo19AccessDiagnosticClient(count=1)),
        "res.partner",
        "read",
        expected_count=1,
    )

    assert report["success"] is True
    assert report["metadata_errors"] == []
    assert report["current_user"]["group_field"] == "group_ids"
    assert report["current_user"]["all_group_field"] == "all_group_ids"
    assert report["current_user"]["direct_group_ids"] == [3]
    assert report["current_user"]["group_ids"] == [3, 9]
    assert report["access"]["granting_count"] == 1


def test_diagnose_access_reports_missing_permission_metadata():
    server = importlib.import_module("odoo_mcp.server")

    report = server.diagnose_access(
        FakeCtx(AccessDiagnosticClient(fail_acl=True)),
        "res.partner",
        "write",
        expected_count=1,
    )

    codes = {item["code"] for item in report["diagnosis"]["codes"]}
    assert report["success"] is True
    assert report["permission_field"] == "perm_write"
    assert report["metadata_errors"][0]["stage"] == "ir.model.access"
    assert "metadata_access_unavailable" in codes


def test_diagnose_access_survives_record_rule_read_failure():
    server = importlib.import_module("odoo_mcp.server")

    report = server.diagnose_access(
        FakeCtx(AccessDiagnosticClient(fail_rules=True)),
        "res.partner",
        "read",
        expected_count=1,
    )

    codes = {item["code"] for item in report["diagnosis"]["codes"]}
    assert report["success"] is True
    assert report["access"]["granting_count"] == 1
    assert report["metadata_errors"][0]["stage"] == "ir.rule"
    assert "metadata_access_unavailable" in codes


# ----- Smart field selection (Phase 1.1) -----


class _SmartFieldsClient:
    """Recording client used to verify smart-field plumbing."""

    def __init__(self, fields_meta=None, records=None):
        self.fields_meta = fields_meta or {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "code": {"type": "char"},
            "state": {"type": "selection"},
            "partner_id": {"type": "many2one"},
            "create_uid": {"type": "many2one"},
            "create_date": {"type": "datetime"},
            "message_ids": {"type": "one2many"},
            "avatar_128": {"type": "binary"},
        }
        self.records = records or [{"id": 1, "name": "Demo", "code": "D-1"}]
        self.search_read_calls = []
        self.read_calls = []
        self.fields_get_calls = 0

    def get_model_fields(self, model):
        self.fields_get_calls += 1
        return self.fields_meta

    def search_read(self, model_name, domain, fields=None, **kwargs):
        self.search_read_calls.append({"model": model_name, "fields": fields, **kwargs})
        return list(self.records)

    def read_records(self, model_name, ids, fields=None):
        self.read_calls.append({"model": model_name, "ids": ids, "fields": fields})
        return list(self.records)


def test_search_records_applies_smart_fields_when_caller_omits_fields():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()

    result = server.search_records(FakeCtx(client), "res.partner")

    assert result["success"] is True
    assert result["smart_fields_applied"] is True
    used = result["fields_used"]
    assert "name" in used and "id" in used
    assert "create_uid" not in used
    assert "message_ids" not in used
    assert "avatar_128" not in used
    assert client.search_read_calls[0]["fields"] == used


def test_search_records_star_fields_disables_filtering():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()

    result = server.search_records(FakeCtx(client), "res.partner", fields=["*"])

    assert result["success"] is True
    assert result["smart_fields_applied"] is False
    assert result["fields_used"] is None
    assert client.search_read_calls[0]["fields"] is None


def test_search_records_explicit_fields_pass_through():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()

    result = server.search_records(
        FakeCtx(client), "res.partner", fields=["name", "email"]
    )

    assert result["success"] is True
    assert result["smart_fields_applied"] is False
    assert result["fields_used"] == ["name", "email"]
    assert client.search_read_calls[0]["fields"] == ["name", "email"]
    # No fields_get lookup needed when caller specified fields explicitly.
    assert client.fields_get_calls == 0


def test_search_records_forwards_tuple_literal_domain():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def search_read(self, model_name, domain, fields=None, **kwargs):
            calls.append(domain)
            return [{"id": 1}]

    result = server.search_records(
        FakeCtx(FakeClient()),
        "res.partner",
        domain='[("tag_ids", "in", [7])]',
        fields=["id"],
    )

    assert result["success"] is True
    assert calls == [[["tag_ids", "in", [7]]]]


def test_search_records_reports_rpc_failure_instead_of_empty_success():
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def search_read(self, model_name, domain, fields=None, **kwargs):
            raise RuntimeError("xmlrpc unavailable")

    result = server.search_records(
        FakeCtx(FakeClient()),
        "res.partner",
        domain=[["id", ">", 0]],
        fields=["id"],
    )

    assert result["success"] is False
    assert "xmlrpc unavailable" in result["error"]


def test_read_record_reports_rpc_failure_instead_of_not_found():
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def read_records(self, model, ids, fields=None):
            raise RuntimeError("xmlrpc unavailable")

    result = server.read_record(FakeCtx(FakeClient()), "res.partner", 1, fields=["id"])

    assert result["success"] is False
    assert "xmlrpc unavailable" in result["error"]


def test_search_records_rejects_invalid_nonempty_domain_without_search():
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def search_read(self, model_name, domain, fields=None, **kwargs):
            calls.append(domain)
            return [{"id": 1}]

    result = server.search_records(
        FakeCtx(FakeClient()),
        "res.partner",
        domain="not-a-domain",
        fields=["id"],
    )

    assert result["success"] is False
    assert "domain must be" in result["error"]
    assert calls == []


def test_read_record_applies_smart_fields_and_uses_schema_cache():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()
    ctx = FakeCtx(client)

    first = server.read_record(ctx, "res.partner", 1)
    second = server.read_record(ctx, "res.partner", 1)

    assert first["success"] is True
    assert first["smart_fields_applied"] is True
    assert "create_date" not in first["fields_used"]
    # schema_cache should be reused — only one fields_get call.
    assert client.fields_get_calls == 1
    assert second["fields_used"] == first["fields_used"]


def test_max_smart_fields_env_caps_selection(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "3")
    client = _SmartFieldsClient()

    result = server.search_records(FakeCtx(client), "res.partner")
    assert len(result["fields_used"]) == 3
    assert result["fields_used"][0] == "id"


# ----- aggregate_records (Phase 1.2) -----


class _AggregateClient:
    def __init__(
        self,
        version="17.0+e",
        version_info=None,
        rows=None,
        formatted_exception=None,
        base_latest_version=None,
    ):
        self.version = version
        self.version_info = version_info
        self.rows = rows if rows is not None else []
        self.formatted_exception = formatted_exception
        self.base_latest_version = base_latest_version
        self.calls: list[tuple[str, str, dict]] = []

    def get_server_version(self):
        info = {}
        if self.version is not None:
            info["server_version"] = self.version
        if self.version_info is not None:
            info["server_version_info"] = self.version_info
        return info

    def execute_method(self, model, method, *args, **kwargs):
        self.calls.append((model, method, kwargs))
        if model == "ir.module.module" and method == "search_read":
            if self.base_latest_version is None:
                return []
            return [{"latest_version": self.base_latest_version}]
        if method == "formatted_read_group" and self.formatted_exception is not None:
            raise self.formatted_exception
        return list(self.rows)


def test_aggregate_records_uses_read_group_on_legacy_odoo():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(
        version="17.0+e",
        rows=[{"state": "draft", "amount_total": 100.0}],
    )

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:sum"],
    )

    assert result["success"] is True
    assert result["method"] == "read_group"
    assert result["major_version"] == 17
    assert result["measures"] == ["amount_total:sum"]
    assert result["row_count"] == 1
    method, kwargs = client.calls[0][1], client.calls[0][2]
    assert method == "read_group"
    assert kwargs["fields"] == ["amount_total:sum"]
    assert kwargs["groupby"] == ["state"]


def test_aggregate_records_uses_formatted_read_group_on_odoo_19():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(
        version="19.0+e",
        rows=[{"state": "sale", "amount_total:sum": 5000.0}],
    )

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:sum"],
    )

    assert result["success"] is True
    assert result["method"] == "formatted_read_group"
    assert result["major_version"] == 19
    assert client.calls[0][1] == "formatted_read_group"
    assert client.calls[0][2]["aggregates"] == ["amount_total:sum"]


def test_aggregate_records_orders_by_a_measure_on_odoo_19():
    """Odoo 19 rejects "amount_untaxed desc"; the measure is sent as "field:agg"."""
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="19.0+e", rows=[])

    result = server.aggregate_records(
        FakeCtx(client),
        model="account.move",
        group_by=["partner_id"],
        measures=["amount_untaxed:sum", "amount_total:max"],
        order="amount_untaxed desc, amount_total, partner_id asc",
    )

    assert result["success"] is True
    assert client.calls[0][2]["order"] == (
        "amount_untaxed:sum desc, amount_total:max, partner_id asc"
    )


def test_aggregate_records_keeps_the_order_on_legacy_odoo():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0+e", rows=[])

    server.aggregate_records(
        FakeCtx(client),
        model="account.move",
        group_by=["partner_id"],
        measures=["amount_untaxed:sum"],
        order="amount_untaxed desc",
    )

    assert client.calls[0][2]["orderby"] == "amount_untaxed desc"


def test_aggregate_records_uses_formatted_read_group_on_saas_19_metadata():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(
        version="17.0+e",
        version_info=["saas~19", 1, 0, "final", 0, "e"],
        rows=[{"state": "sale", "amount_total:sum": 5000.0}],
    )

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:sum"],
    )

    assert result["success"] is True
    assert result["method"] == "formatted_read_group"
    assert result["major_version"] == 19
    assert client.calls[0][1] == "formatted_read_group"


def test_aggregate_records_known_odoo_19_does_not_fallback_on_formatted_error():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(
        version="19.0",
        formatted_exception=ValueError("Invalid field 'bad_field'"),
    )

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:sum"],
    )

    assert result["success"] is False
    assert "Invalid field 'bad_field'" in result["error"]
    methods = [call[1] for call in client.calls]
    assert methods == ["formatted_read_group"]


def test_aggregate_records_unknown_version_falls_back_only_when_formatted_missing():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(
        version="no-digits-here",
        rows=[{"state": "draft"}],
        formatted_exception=AttributeError(
            "The method 'sale.order.formatted_read_group' does not exist"
        ),
    )

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:sum"],
    )

    assert result["success"] is True
    assert result["method"] == "read_group"
    assert result["fallback_reason"]
    methods = [call[1] for call in client.calls]
    assert methods == ["search_read", "formatted_read_group", "read_group"]


def test_aggregate_records_unknown_version_preserves_non_missing_formatted_error():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(
        version="no-digits-here",
        formatted_exception=ValueError("Invalid domain leaf"),
    )

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:sum"],
    )

    assert result["success"] is False
    assert "Invalid domain leaf" in result["error"]
    methods = [call[1] for call in client.calls]
    assert methods == ["search_read", "formatted_read_group"]


def test_aggregate_records_rejects_invalid_measure():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0")

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:bogus"],
    )

    assert result["success"] is False
    assert "bogus" in result["error"]


def test_aggregate_records_requires_group_by():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0")

    result = server.aggregate_records(FakeCtx(client), model="sale.order", group_by=[])

    assert result["success"] is False
    assert "group_by" in result["error"]


def test_odoo_major_version_falls_back_to_base_module_latest_version():
    server = importlib.import_module("odoo_mcp.server")

    class StubClient:
        def __init__(self):
            self.execute_calls = []

        def get_server_version(self):
            return {"error": "Not Found"}

        def execute_method(self, model, method, *args, **kwargs):
            self.execute_calls.append((model, method))
            assert model == "ir.module.module"
            return [{"latest_version": "19.0.1.0.0"}]

    stub = StubClient()
    assert server.odoo_major_version(stub) == 19
    assert stub.execute_calls == [("ir.module.module", "search_read")]


def test_odoo_major_version_uses_server_version_metadata_when_present():
    server = importlib.import_module("odoo_mcp.server")

    class StubClient:
        def get_server_version(self):
            return {"server_version": "17.0+e"}

        def execute_method(self, *args, **kwargs):
            raise AssertionError(
                "execute_method should not run when metadata is present"
            )

    assert server.odoo_major_version(StubClient()) == 17


def test_parse_odoo_major_version_accepts_numeric_and_saas_shapes():
    server = importlib.import_module("odoo_mcp.server")

    assert server.parse_odoo_major_version(19) == 19
    assert server.parse_odoo_major_version(19.1) == 19
    assert server.parse_odoo_major_version("19.0+e") == 19
    assert server.parse_odoo_major_version("saas~19") == 19
    assert server.parse_odoo_major_version("saas~19.1+e") == 19
    assert server.parse_odoo_major_version("no-digits-here") is None


def test_odoo_major_version_prefers_server_version_info_first_value():
    server = importlib.import_module("odoo_mcp.server")

    class StubClient:
        def get_server_version(self):
            return {
                "server_version_info": ["saas~19", 1, 0, "final", 0, "e"],
                "server_version": "17.0+e",
            }

        def execute_method(self, *args, **kwargs):
            raise AssertionError(
                "execute_method should not run when metadata is present"
            )

    assert server.odoo_major_version(StubClient()) == 19


def test_odoo_major_version_parses_saas_server_version_string():
    server = importlib.import_module("odoo_mcp.server")

    class StubClient:
        def get_server_version(self):
            return {"server_version": "saas~19.1+e"}

        def execute_method(self, *args, **kwargs):
            raise AssertionError(
                "execute_method should not run when metadata is present"
            )

    assert server.odoo_major_version(StubClient()) == 19


def test_parse_measure_spec_defaults_to_sum_and_validates_aggregator():
    server = importlib.import_module("odoo_mcp.server")

    assert server.parse_measure_spec("amount_total") == ("amount_total", "sum")
    assert server.parse_measure_spec("price:avg") == ("price", "avg")
    try:
        server.parse_measure_spec("name:concat")
    except ValueError as exc:
        assert "concat" in str(exc)
    else:  # pragma: no cover - assertion path
        raise AssertionError("expected ValueError for invalid aggregator")


# ----- chatter_post (Phase 1.3) -----


class _ChatterClient:
    def __init__(self, post_result=4242):
        self.post_result = post_result
        self.calls: list[tuple[str, str, tuple, dict]] = []

    def execute_method(self, model, method, *args, **kwargs):
        self.calls.append((model, method, args, kwargs))
        return self.post_result


def test_chatter_post_default_returns_preview_without_executing(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()

    result = server.chatter_post(
        FakeCtx(client),
        model="res.partner",
        record_id=7,
        body="Hello there",
    )

    assert result["success"] is True
    assert result["mode"] == "preview"
    assert result["approval"]["token"].startswith("odoo-write:")
    assert client.calls == []


def test_chatter_post_execute_with_valid_approval_posts(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()
    ctx = FakeCtx(client)

    preview = server.chatter_post(ctx, model="res.partner", record_id=7, body="Hi")
    approved = server.chatter_post(
        ctx,
        model="res.partner",
        record_id=7,
        body="Hi",
        approval=preview["approval"],
        confirm=True,
    )

    assert approved["success"] is True
    assert approved["mode"] == "execute"
    assert client.calls[0][0] == "res.partner"
    assert client.calls[0][1] == "message_post"
    assert client.calls[0][2] == ([7],)
    assert client.calls[0][3]["body"] == "Hi"
    assert client.calls[0][3]["message_type"] == "comment"


def test_chatter_post_rejects_token_mismatch(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()
    ctx = FakeCtx(client)

    preview = server.chatter_post(ctx, model="res.partner", record_id=7, body="Hi")
    bad = server.chatter_post(
        ctx,
        model="res.partner",
        record_id=7,
        body="Different body",
        approval=preview["approval"],
        confirm=True,
    )

    assert bad["success"] is False
    assert "token" in bad["error"].lower()
    assert client.calls == []


def test_chatter_post_requires_confirm_in_gated_mode(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()
    ctx = FakeCtx(client)

    preview = server.chatter_post(ctx, model="res.partner", record_id=7, body="Hi")
    no_confirm = server.chatter_post(
        ctx,
        model="res.partner",
        record_id=7,
        body="Hi",
        approval=preview["approval"],
        confirm=False,
    )

    assert no_confirm["success"] is False
    assert "confirm" in no_confirm["error"].lower()
    assert client.calls == []


def test_chatter_post_direct_mode_posts_immediately(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")
    client = _ChatterClient(post_result=999)

    result = server.chatter_post(
        FakeCtx(client),
        model="res.partner",
        record_id=7,
        body="urgent",
    )

    assert result["success"] is True
    assert result["mode"] == "direct"
    assert result["approval_required"] is False
    assert result["result"] == 999
    assert client.calls[0][1] == "message_post"


def test_chatter_post_validates_inputs(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()

    empty = server.chatter_post(
        FakeCtx(client), model="res.partner", record_id=7, body="   "
    )
    assert empty["success"] is False
    assert "body" in empty["error"]

    bad_type = server.chatter_post(
        FakeCtx(client),
        model="res.partner",
        record_id=7,
        body="hi",
        message_type="email",
    )
    assert bad_type["success"] is False
    assert "message_type" in bad_type["error"]

    assert client.calls == []


# ----- Edge cases (Group A) ------------------------------------------------


def test_resolve_read_fields_returns_none_when_metadata_unavailable():
    server = importlib.import_module("odoo_mcp.server")

    class FailingClient:
        def get_model_fields(self, model):
            return {"error": "Access denied"}

    app_context = FakeLife(FailingClient())
    resolved = server.resolve_read_fields(
        app_context, app_context.odoo, "res.partner", None
    )
    # No metadata → fall back to "all fields" (None) so the search still works
    assert resolved is None


def test_resolve_read_fields_treats_empty_metadata_as_no_smart():
    server = importlib.import_module("odoo_mcp.server")

    class EmptyClient:
        def get_model_fields(self, model):
            return {}

    app_context = FakeLife(EmptyClient())
    resolved = server.resolve_read_fields(
        app_context, app_context.odoo, "res.partner", None
    )
    assert resolved is None


def test_resolve_read_fields_passes_explicit_fields_through():
    server = importlib.import_module("odoo_mcp.server")

    class ExplodingClient:
        def get_model_fields(self, model):
            raise AssertionError(
                "should not call fields_get when caller specifies fields"
            )

    app_context = FakeLife(ExplodingClient())
    resolved = server.resolve_read_fields(
        app_context, app_context.odoo, "res.partner", ["name", "email"]
    )
    assert resolved == ["name", "email"]


def test_lookup_model_history_tool_returns_catalog_match():
    server = importlib.import_module("odoo_mcp.server")

    report = call_tool_json(server, "lookup_model_history", {"name": "account.invoice"})
    assert report["success"] is True
    assert report["matches"][0]["new_model"] == "account.move"
    assert report["metadata_used"]["live_odoo"] is False


def test_get_model_fields_relevance_top_returns_ranked_subset():
    server = importlib.import_module("odoo_mcp.server")

    class WideModelClient:
        def get_model_fields(self, model):
            return {
                "name": {"type": "char", "required": True},
                "email": {"type": "char"},
                "note": {"type": "text"},
                "message_ids": {"type": "one2many"},
                "image_1920": {"type": "binary"},
            }

    result = server.get_model_fields(
        FakeCtx(WideModelClient()), "res.partner", relevance="top", max_fields=2
    )

    assert result["success"] is True
    assert result["relevance_applied"] is True
    assert list(result["result"]) == ["name", "email"]
    assert result["count"] == 2
    assert result["ranking"][0]["field"] == "name"
    assert result["ranking"][0]["score"] > result["ranking"][1]["score"]


def test_get_model_fields_rejects_unknown_relevance_mode():
    server = importlib.import_module("odoo_mcp.server")

    class ExplodingClient:
        def get_model_fields(self, model):
            raise AssertionError("must validate relevance before hitting Odoo")

    result = server.get_model_fields(
        FakeCtx(ExplodingClient()), "res.partner", relevance="bogus"
    )
    assert result["success"] is False
    assert "relevance" in result["error"]


def test_aggregate_records_rejects_invalid_model_name():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0")

    result = server.aggregate_records(
        FakeCtx(client),
        model="not a model",
        group_by=["state"],
        measures=["amount_total:sum"],
    )
    assert result["success"] is False
    assert "model" in result["error"].lower()
    assert client.calls == []


def test_aggregate_records_rejects_negative_offset():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0")

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["id:count"],
        offset=-1,
    )
    assert result["success"] is False
    assert "offset" in result["error"]
    assert client.calls == []


def test_aggregate_records_clamps_high_limits():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0", rows=[])

    server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["id:count"],
        limit=999,
    )
    assert client.calls[0][2]["limit"] == 100  # MAX_SEARCH_LIMIT


def test_aggregate_records_passes_lazy_and_order_through():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="18.0", rows=[])

    server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state", "partner_id"],
        measures=["amount_total:sum", "id:count_distinct"],
        lazy=True,
        order="amount_total:sum desc",
    )
    kwargs = client.calls[0][2]
    assert kwargs["lazy"] is True
    assert kwargs["orderby"] == "amount_total:sum desc"
    assert kwargs["groupby"] == ["state", "partner_id"]
    assert kwargs["fields"] == ["amount_total:sum", "id:count_distinct"]


def test_aggregate_records_accepts_all_supported_aggregators():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0", rows=[])

    measures = [
        "amount_total:sum",
        "amount_total:avg",
        "amount_total:min",
        "amount_total:max",
        "id:count",
        "partner_id:count_distinct",
        "tag_ids:array_agg",
        "is_company:bool_and",
        "is_company:bool_or",
    ]
    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=measures,
    )
    assert result["success"] is True
    assert result["measures"] == measures


def test_aggregate_records_accepts_domain_json_string():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0", rows=[])

    result = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["id:count"],
        domain='[["state", "=", "draft"]]',
    )
    assert result["success"] is True
    assert client.calls[0][2]["domain"] == [["state", "=", "draft"]]


def test_chatter_post_rejects_negative_record_id(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()

    result = server.chatter_post(
        FakeCtx(client), model="res.partner", record_id=0, body="hi"
    )
    assert result["success"] is False
    assert "record_id" in result["error"]
    assert client.calls == []


def test_chatter_post_rejects_invalid_model_name(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()

    result = server.chatter_post(
        FakeCtx(client), model="bad model", record_id=1, body="hi"
    )
    assert result["success"] is False
    assert client.calls == []


def test_chatter_post_passes_optional_kwargs_through(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")
    client = _ChatterClient(post_result=42)

    result = server.chatter_post(
        FakeCtx(client),
        model="crm.lead",
        record_id=12,
        body="Body",
        subtype_xmlid="mail.mt_note",
        partner_ids=[1, 2, 3],
        attachment_ids=[7, 8],
    )
    assert result["success"] is True
    kwargs = client.calls[0][3]
    assert kwargs["subtype_xmlid"] == "mail.mt_note"
    assert kwargs["partner_ids"] == [1, 2, 3]
    assert kwargs["attachment_ids"] == [7, 8]
    assert kwargs["body"] == "Body"


def test_chatter_post_token_is_deterministic_for_same_payload(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = _ChatterClient()
    ctx = FakeCtx(client)

    a = server.chatter_post(ctx, model="res.partner", record_id=1, body="x")
    b = server.chatter_post(ctx, model="res.partner", record_id=1, body="x")
    assert a["approval"]["token"] == b["approval"]["token"]


def test_chatter_post_propagates_execute_method_failure(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")

    class BoomClient:
        def execute_method(self, *args, **kwargs):
            raise RuntimeError("Odoo refused")

    result = server.chatter_post(
        FakeCtx(BoomClient()), model="res.partner", record_id=1, body="hi"
    )
    assert result["success"] is False
    assert "Odoo refused" in result["error"]


def test_max_smart_fields_invalid_env_falls_back_to_default(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "not-a-number")
    assert server.max_smart_fields() == server.DEFAULT_MAX_SMART_FIELDS

    monkeypatch.setenv("ODOO_MCP_MAX_SMART_FIELDS", "0")
    assert server.max_smart_fields() == 1  # clamped to >=1


# ----- Schema-shape regression (Group B) -----------------------------------


def test_mcp_surface_counts_reports_v030_totals():
    server = importlib.import_module("odoo_mcp.server")
    counts = server.mcp_surface_counts()
    assert counts["tool_count"] == 41
    assert counts["prompt_count"] == 11
    # 1 fixed resource + 3 templates = 4
    assert counts["resource_count"] == 4


def test_new_tools_expose_output_schema_and_annotations():
    import asyncio

    server = importlib.import_module("odoo_mcp.server")
    tools = {
        tool.name: tool.model_dump(by_alias=True)
        for tool in asyncio.run(server.mcp.list_tools())
    }

    aggregate = tools["aggregate_records"]
    assert aggregate["annotations"]["readOnlyHint"] is True
    assert aggregate["annotations"]["destructiveHint"] is False
    assert aggregate["outputSchema"]["type"] == "object"

    chatter = tools["chatter_post"]
    assert chatter["annotations"]["readOnlyHint"] is False
    assert chatter["annotations"]["destructiveHint"] is True
    assert chatter["outputSchema"]["type"] == "object"


def test_search_records_and_read_record_response_carry_new_keys():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()

    search = server.search_records(FakeCtx(client), "res.partner", limit=1)
    for required in (
        "success",
        "count",
        "result",
        "smart_fields_applied",
        "fields_used",
    ):
        assert required in search

    read = server.read_record(FakeCtx(client), "res.partner", 1)
    for required in ("success", "result", "smart_fields_applied", "fields_used"):
        assert required in read


def test_aggregate_records_response_shape_is_stable():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0", rows=[{"state": "draft"}])

    response = server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["id:count"],
    )
    expected_keys = {
        "success",
        "method",
        "major_version",
        "fallback_reason",
        "model",
        "group_by",
        "measures",
        "row_count",
        "rows",
    }
    assert expected_keys <= set(response)


def test_chatter_post_preview_response_shape_is_stable(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)

    response = server.chatter_post(
        FakeCtx(_ChatterClient()),
        model="res.partner",
        record_id=1,
        body="hi",
    )
    assert response["success"] is True
    assert response["mode"] == "preview"
    approval = response["approval"]
    for key in ("model", "method", "record_ids", "kwargs", "token"):
        assert key in approval
    assert approval["method"] == "message_post"


def test_runtime_security_report_surfaces_chatter_direct(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    report = server.runtime_security_report()
    assert "chatter_direct_enabled" in report
    assert report["chatter_direct_enabled"] is False

    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")
    assert server.runtime_security_report()["chatter_direct_enabled"] is True


# ----- AppContext / Resources / Pydantic helpers (low-line-count branches) ----


def test_app_context_caches_odoo_factory_result_lazily():
    server = importlib.import_module("odoo_mcp.server")
    calls = {"count": 0}

    class Sentinel:
        pass

    sentinel = Sentinel()

    def factory():
        calls["count"] += 1
        return sentinel

    ctx = server.AppContext(odoo_factory=factory)
    assert ctx._odoo is None
    a = ctx.odoo
    b = ctx.odoo
    assert a is b is sentinel
    assert calls["count"] == 1


def test_search_domain_to_domain_list_converts_conditions_to_tuples():
    server = importlib.import_module("odoo_mcp.server")
    sd = server.SearchDomain(
        conditions=[
            server.DomainCondition(field="name", operator="=", value="Ada"),
            server.DomainCondition(field="active", operator="=", value=True),
        ]
    )
    assert sd.to_domain_list() == [["name", "=", "Ada"], ["active", "=", True]]


def test_resource_get_models_returns_json_when_client_provides_models(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def get_models(self):
            return {"model_names": ["res.partner"], "models_details": {}}

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    payload = json.loads(server.get_models())
    assert payload["model_names"] == ["res.partner"]


def test_resource_get_model_info_returns_combined_payload(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def get_model_info(self, model):
            return {"id": 1, "model": model, "name": "Contact"}

        def get_model_fields(self, model):
            return {"name": {"type": "char"}}

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    info = json.loads(server.get_model_info("res.partner"))
    assert info["fields"] == {"name": {"type": "char"}}
    assert info["id"] == 1


def test_resource_get_model_info_returns_error_for_invalid_name(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        pass

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    info = json.loads(server.get_model_info("bad model"))
    assert "error" in info


def test_resource_get_record_returns_record_when_found(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def read_records(self, model, ids):
            return [{"id": 1, "name": "Ada"}]

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    payload = json.loads(server.get_record("res.partner", "1"))
    assert payload["name"] == "Ada"


def test_resource_get_record_returns_error_when_not_found(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def read_records(self, model, ids):
            return []

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    payload = json.loads(server.get_record("res.partner", "1"))
    assert "Record not found" in payload["error"]


def test_resource_get_record_rejects_invalid_id(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        pass

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    payload = json.loads(server.get_record("res.partner", "0"))
    assert "error" in payload


def test_resource_search_records_returns_results(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class FakeClient:
        def search_read(self, model, domain, limit=10):
            return [{"id": 1, "name": "Ada"}]

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    payload = json.loads(
        server.search_records_resource("res.partner", '[["name","=","Ada"]]')
    )
    assert payload[0]["name"] == "Ada"


def test_resource_search_records_returns_error_on_invalid_domain(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class FakeClient:
        def search_read(self, model, domain, limit=10):
            calls.append(domain)
            return [{"id": 1}]

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    payload = json.loads(server.search_records_resource("res.partner", '"oops"'))
    assert "error" in payload
    assert calls == []


def test_resource_search_records_accepts_tuple_literal_domain(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    seen = []

    class FakeClient:
        def search_read(self, model, domain, limit=10):
            seen.append(domain)
            return [{"id": 1, "name": "Ada"}]

    monkeypatch.setattr(server, "get_odoo_client", lambda: FakeClient())
    payload = json.loads(
        server.search_records_resource("res.partner", '[("name", "=", "Ada")]')
    )
    assert payload[0]["name"] == "Ada"
    assert seen == [[["name", "=", "Ada"]]]


# ----- parse_measure_spec error branches ---------------------------------


def test_parse_measure_spec_rejects_empty_string():
    server = importlib.import_module("odoo_mcp.server")
    try:
        server.parse_measure_spec("")
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("empty spec must raise")


def test_parse_measure_spec_rejects_blank_field_or_aggregator():
    server = importlib.import_module("odoo_mcp.server")
    try:
        server.parse_measure_spec(":sum")
    except ValueError as exc:
        assert "invalid measure spec" in str(exc)
    else:
        raise AssertionError("blank field must raise")
    try:
        server.parse_measure_spec("amount:")
    except ValueError as exc:
        assert "invalid measure spec" in str(exc)
    else:
        raise AssertionError("blank agg must raise")


# ----- odoo_major_version fallback paths ---------------------------------


def test_odoo_major_version_returns_none_when_module_search_returns_empty():
    server = importlib.import_module("odoo_mcp.server")

    class StubClient:
        def get_server_version(self):
            return {"server_version": "no-digits-here"}

        def execute_method(self, *args, **kwargs):
            return []

    assert server.odoo_major_version(StubClient()) is None


def test_odoo_major_version_returns_none_when_latest_version_lacks_digits():
    server = importlib.import_module("odoo_mcp.server")

    class StubClient:
        def get_server_version(self):
            return {}

        def execute_method(self, *args, **kwargs):
            return [{"latest_version": "alpha"}]

    assert server.odoo_major_version(StubClient()) is None


def test_odoo_major_version_returns_none_when_get_server_version_returns_non_dict():
    server = importlib.import_module("odoo_mcp.server")

    class StubClient:
        def get_server_version(self):
            return "string-not-dict"

        def execute_method(self, *args, **kwargs):
            raise RuntimeError("rpc")

    assert server.odoo_major_version(StubClient()) is None


# ----- normalize_domain_input edge cases ---------------------------------


def test_normalize_domain_input_accepts_search_domain_object():
    server = importlib.import_module("odoo_mcp.server")
    sd = server.SearchDomain(
        conditions=[server.DomainCondition(field="name", operator="=", value="Ada")]
    )
    assert server.normalize_domain_input(sd) == [["name", "=", "Ada"]]


def test_normalize_domain_input_rejects_invalid_string():
    server = importlib.import_module("odoo_mcp.server")
    try:
        server.normalize_domain_input("def x():")
    except ValueError as exc:
        assert "domain must be" in str(exc)
    else:
        raise AssertionError("unparseable domain must raise")


def test_normalize_domain_input_handles_python_literal_via_ast():
    server = importlib.import_module("odoo_mcp.server")
    # Python tuple literal that json can't parse but ast.literal_eval can
    assert server.normalize_domain_input("[('name', '=', 'Ada')]") == [
        ["name", "=", "Ada"]
    ]


def test_normalize_domain_input_rejects_dict_without_conditions_list():
    server = importlib.import_module("odoo_mcp.server")
    try:
        server.normalize_domain_input({"foo": "bar"})
    except ValueError as exc:
        assert "conditions" in str(exc)
    else:
        raise AssertionError("dict without conditions must raise")
    try:
        server.normalize_domain_input({"conditions": "not-a-list"})
    except ValueError as exc:
        assert "conditions" in str(exc)
    else:
        raise AssertionError("non-list conditions must raise")


def test_normalize_domain_input_rejects_non_list_value():
    server = importlib.import_module("odoo_mcp.server")
    try:
        server.normalize_domain_input(42)
    except ValueError as exc:
        assert "domain must be" in str(exc)
    else:
        raise AssertionError("scalar domain must raise")


def test_normalize_domain_input_returns_empty_for_empty_list():
    server = importlib.import_module("odoo_mcp.server")
    assert server.normalize_domain_input([]) == []
    assert server.normalize_domain_input([[]]) == []


def test_normalize_domain_input_returns_empty_for_empty_string():
    server = importlib.import_module("odoo_mcp.server")
    assert server.normalize_domain_input("") == []


# ----- write approval helpers -------------------------------------------


def test_register_write_approval_returns_false_for_failed_report():
    server = importlib.import_module("odoo_mcp.server")
    ctx = server.AppContext()
    assert server.register_write_approval(ctx, {"success": False}) is False


def test_register_write_approval_returns_false_for_missing_token():
    server = importlib.import_module("odoo_mcp.server")
    ctx = server.AppContext()
    assert (
        server.register_write_approval(
            ctx,
            {
                "success": True,
                "approval": {"model": "res.partner", "operation": "write"},
            },
        )
        is False
    )


def test_require_validated_write_approval_returns_none_when_token_missing():
    server = importlib.import_module("odoo_mcp.server")
    ctx = server.AppContext()
    assert server.require_validated_write_approval(ctx, {"token": "ghost"}) is None


def test_require_validated_write_approval_clears_expired_records():
    server = importlib.import_module("odoo_mcp.server")
    ctx = server.AppContext()
    ctx.write_approvals["odoo-write:expired"] = {
        "approval": {},
        "payload": {},
        "validated_at": 0,
        "expires_at": 0,  # already expired
    }
    assert (
        server.require_validated_write_approval(ctx, {"token": "odoo-write:expired"})
        is None
    )
    assert "odoo-write:expired" not in ctx.write_approvals


def test_register_write_approval_sweeps_other_expired_records():
    """An abandoned approval (never executed, never re-looked-up) must not
    linger forever — including any resolved_binary_values it holds — just
    because its own token is never queried again after expiry."""
    server = importlib.import_module("odoo_mcp.server")
    ctx = server.AppContext()
    ctx.write_approvals["odoo-write:expired"] = {
        "approval": {},
        "payload": {},
        "validated_at": 0,
        "expires_at": 0,  # already expired
        "resolved_binary_values": {"datas": "stale-base64-content"},
    }

    stored = server.register_write_approval(
        ctx,
        {
            "success": True,
            "approval": {
                "token": "odoo-write:new",
                "model": "res.partner",
                "operation": "write",
            },
        },
    )

    assert stored is True
    assert "odoo-write:expired" not in ctx.write_approvals
    assert "odoo-write:new" in ctx.write_approvals


# ----- configured_addons_roots / restrict_addons_paths ------------------


def test_configured_addons_roots_skips_blank_entries(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    import os as _os

    monkeypatch.setenv(
        "ODOO_ADDONS_PATHS", f"{_os.pathsep}/tmp/x{_os.pathsep}{_os.pathsep}/tmp/y"
    )
    roots = server.configured_addons_roots()
    assert all(str(p).endswith(("x", "y")) for p in roots)
    assert len(roots) == 2


def test_restrict_addons_paths_returns_none_when_input_none():
    server = importlib.import_module("odoo_mcp.server")
    assert server.restrict_addons_paths(None) is None


def test_restrict_addons_paths_raises_when_no_root_configured(monkeypatch, tmp_path):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("ODOO_ADDONS_PATHS", raising=False)
    try:
        server.restrict_addons_paths([str(tmp_path)])
    except ValueError as exc:
        assert "ODOO_ADDONS_PATHS" in str(exc)
    else:
        raise AssertionError("missing roots must raise")


# ----- access_permission_field branches ---------------------------------


def test_access_permission_field_maps_each_operation():
    server = importlib.import_module("odoo_mcp.server")
    assert server.access_permission_field("create") == "perm_create"
    assert server.access_permission_field("write") == "perm_write"
    assert server.access_permission_field("unlink") == "perm_unlink"
    assert server.access_permission_field("delete") == "perm_unlink"
    assert server.access_permission_field("read") == "perm_read"
    assert server.access_permission_field("search") == "perm_read"
    assert server.access_permission_field("search_read") == "perm_read"


def test_access_permission_field_classifies_side_effect_methods_as_write():
    server = importlib.import_module("odoo_mcp.server")
    assert server.access_permission_field("action_confirm") == "perm_write"


def test_access_permission_field_defaults_to_read_for_get_helpers():
    server = importlib.import_module("odoo_mcp.server")
    # safety = read_only (because of get_ prefix), so falls to perm_read
    assert server.access_permission_field("get_thing") == "perm_read"


# ----- _safe_odoo_read --------------------------------------------------


def test_safe_odoo_read_returns_normalized_error_on_exception():
    server = importlib.import_module("odoo_mcp.server")

    def boom():
        raise RuntimeError("oh no")

    result, error = server._safe_odoo_read("stage", boom)
    assert result is None
    assert error["stage"] == "stage"


# ----- _m2o_id, _m2m_ids, _field_names, _available_user_read_fields ----


def test_m2o_id_handles_list_tuple_int_and_other():
    server = importlib.import_module("odoo_mcp.server")
    assert server._m2o_id([3, "Sales"]) == 3
    assert server._m2o_id((4, "X")) == 4
    assert server._m2o_id(7) == 7
    assert server._m2o_id("nope") is None
    assert server._m2o_id([]) is None
    assert server._m2o_id(()) is None


def test_m2m_ids_handles_lists_tuples_and_filters_unknown_entries():
    server = importlib.import_module("odoo_mcp.server")
    assert server._m2m_ids([1, 2, 3]) == {1, 2, 3}
    assert server._m2m_ids([(5, "label"), [9, "x"], "skip"]) == {5, 9}
    assert server._m2m_ids("not-a-list") == set()


def test_field_names_returns_empty_for_non_dict():
    server = importlib.import_module("odoo_mcp.server")
    assert server._field_names("not-a-dict") == set()
    assert server._field_names({"a": {}, "b": {}}) == {"a", "b"}


def test_available_user_read_fields_returns_base_when_metadata_missing():
    server = importlib.import_module("odoo_mcp.server")
    assert server._available_user_read_fields(set()) == [
        "id",
        "name",
        "company_id",
        "company_ids",
    ]


def test_available_user_read_fields_includes_group_fields_when_present():
    server = importlib.import_module("odoo_mcp.server")
    # When the model exposes both groups_id and all_group_ids, we want both
    fields = server._available_user_read_fields(
        {"id", "name", "groups_id", "all_group_ids"}
    )
    assert "groups_id" in fields
    assert "all_group_ids" in fields


def test_group_field_names_returns_pair_or_none():
    server = importlib.import_module("odoo_mcp.server")
    direct, all_field = server._group_field_names({"groups_id": [1]})
    assert direct == "groups_id"
    assert all_field is None
    direct, all_field = server._group_field_names(
        {"group_ids": [], "all_group_ids": []}
    )
    assert direct == "group_ids"
    assert all_field == "all_group_ids"
    direct, all_field = server._group_field_names({})
    assert direct is None
    assert all_field is None


def test_acl_row_applies_returns_true_when_no_group_id():
    server = importlib.import_module("odoo_mcp.server")
    # _m2o_id returns None for None/strings/empty containers
    assert server._acl_row_applies({"group_id": None}, {1, 2}) is True
    assert server._acl_row_applies({}, {1, 2}) is True


def test_acl_row_applies_returns_false_when_user_group_unknown():
    server = importlib.import_module("odoo_mcp.server")
    assert server._acl_row_applies({"group_id": [3, "x"]}, None) is False
    assert server._acl_row_applies({"group_id": [3, "x"]}, {4}) is False


def test_rule_applies_returns_true_when_no_groups_or_user_match():
    server = importlib.import_module("odoo_mcp.server")
    assert server._rule_applies({"groups": []}, {1}) is True
    assert server._rule_applies({"groups": [3]}, None) is False
    assert server._rule_applies({"groups": [3]}, {3}) is True


def test_record_id_domain_filters_non_positive_ids():
    server = importlib.import_module("odoo_mcp.server")
    assert server._record_id_domain([1, 0, -2, 3]) == [["id", "in", [1, 3]]]
    assert server._record_id_domain([]) == []
    assert server._record_id_domain(None) == []


# ----- _access_diagnosis_codes branches ---------------------------------


def test_access_diagnosis_codes_reports_record_rule_filter_likely():
    server = importlib.import_module("odoo_mcp.server")
    codes = server._access_diagnosis_codes(
        metadata_errors=[],
        acl_rows=[{"perm_read": True}],
        granting_acl_rows=[{"perm_read": True}],
        active_rules=[{"x": 1}],
        applicable_rules=[{"x": 1}],
        actual_count=0,
        expected_count=2,
        record_ids=[],
    )
    code_names = {c["code"] for c in codes}
    assert "record_rule_filter_likely" in code_names


def test_access_diagnosis_codes_reports_domain_or_rule_filter_likely():
    server = importlib.import_module("odoo_mcp.server")
    codes = server._access_diagnosis_codes(
        metadata_errors=[],
        acl_rows=[],
        granting_acl_rows=[],
        active_rules=[],
        applicable_rules=[],
        actual_count=0,
        expected_count=2,
        record_ids=[],
    )
    code_names = {c["code"] for c in codes}
    assert "domain_or_rule_filter_likely" in code_names


def test_access_diagnosis_codes_reports_no_issue_detected_when_clean():
    server = importlib.import_module("odoo_mcp.server")
    codes = server._access_diagnosis_codes(
        metadata_errors=[],
        acl_rows=[{"perm_read": True}],
        granting_acl_rows=[{"perm_read": True}],
        active_rules=[],
        applicable_rules=[],
        actual_count=5,
        expected_count=5,
        record_ids=[],
    )
    code_names = {c["code"] for c in codes}
    assert "no_access_issue_detected" in code_names


def test_access_diagnosis_codes_reports_record_count_mismatch_via_record_ids():
    server = importlib.import_module("odoo_mcp.server")
    codes = server._access_diagnosis_codes(
        metadata_errors=[],
        acl_rows=[],
        granting_acl_rows=[],
        active_rules=[],
        applicable_rules=[],
        actual_count=1,
        expected_count=None,
        record_ids=[1, 2, 3],
    )
    code_names = {c["code"] for c in codes}
    assert "domain_or_rule_filter_likely" in code_names


# ----- diagnose_access boundary inputs ---------------------------------


def test_diagnose_access_rejects_negative_expected_count():
    server = importlib.import_module("odoo_mcp.server")

    class _C:
        pass

    result = server.diagnose_access(
        FakeCtx(_C()), "res.partner", "read", expected_count=-5
    )
    assert result["success"] is False
    assert "expected_count" in result["error"]


def test_diagnose_access_rejects_invalid_model_name():
    server = importlib.import_module("odoo_mcp.server")

    class _C:
        pass

    result = server.diagnose_access(FakeCtx(_C()), "bad model", "read")
    assert result["success"] is False


def test_diagnose_access_handles_user_context_raise():
    """Cover lines 998-999: error metadata when get_user_context() raises."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            raise RuntimeError("context blocked")

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {}
            if model == "res.users" and method == "read":
                return [{"id": 7}]
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                return []
            if model == "res.partner" and method == "search_count":
                return 1
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(
        FakeCtx(_Client()), "res.partner", "read", expected_count=1
    )
    stages = {err["stage"] for err in report["metadata_errors"]}
    assert "res.users.context_get" in stages


def test_diagnose_access_handles_user_context_error_dict():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"error": "Access denied"}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {"id": {"type": "integer"}, "groups_id": {"type": "many2many"}}
            if model == "res.users" and method == "read":
                return [{"id": 7, "groups_id": [1]}]
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                return []
            if model == "res.partner" and method == "search_count":
                return 0
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(
        FakeCtx(_Client()), "res.partner", "read", expected_count=1
    )
    assert report["success"] is True
    assert any(
        err["stage"] == "res.users.context_get" for err in report["metadata_errors"]
    )


def test_diagnose_access_skips_inactive_or_unrelated_permission_rules():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {}
            if model == "res.users" and method == "read":
                return [{"id": 7}]
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                return [
                    {"id": 1, "name": "off", "active": False},  # inactive → skipped
                    {
                        "id": 2,
                        "name": "no-write-perm",
                        "active": True,
                        "perm_read": False,
                    },  # perm_read False → skipped
                ]
            if model == "res.partner" and method == "search_count":
                return 0
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(
        FakeCtx(_Client()), "res.partner", "read", expected_count=1
    )
    assert report["success"] is True
    assert report["rules"]["active"] == []


def test_diagnose_access_rules_branch_with_global_rule_no_groups():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {"groups_id": {"type": "many2many"}}
            if model == "res.users" and method == "read":
                return [{"id": 7, "groups_id": [3]}]
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                return [
                    {
                        "id": 5,
                        "name": "global",
                        "active": True,
                        "groups": False,  # no groups → global rule
                        "perm_read": True,
                    }
                ]
            if model == "res.partner" and method == "search_count":
                return 5
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(
        FakeCtx(_Client()), "res.partner", "read", expected_count=5
    )
    assert report["success"] is True
    assert len(report["rules"]["global"]) == 1
    assert report["rules"]["group_bound"] == []


# ----- schema_catalog error branches -------------------------------------


def test_schema_catalog_rejects_invalid_model_name():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        pass

    result = server.schema_catalog(FakeCtx(_Client()), models=["bad model"])
    assert result["success"] is False


def test_schema_catalog_returns_error_when_get_models_fails():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_models(self):
            return {"error": "rpc fail"}

    result = server.schema_catalog(FakeCtx(_Client()))
    assert result["success"] is False
    assert "rpc" in result["error"]


def test_schema_catalog_filters_explicit_models_and_skips_field_errors():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_models(self):
            return {
                "model_names": ["res.partner", "res.users"],
                "models_details": {"res.partner": {"name": "Contact"}},
            }

        def get_model_fields(self, model):
            return {"error": "ACL"}

    result = server.schema_catalog(
        FakeCtx(_Client()), models=["res.partner"], include_fields=True
    )
    assert result["success"] is True
    assert result["result"][0]["model"] == "res.partner"
    assert result["result"][0]["fields"] == {}
    assert result["result"][0]["field_error"] == "ACL"


# ----- preview_write / validate_write / execute_approved_write extras ----


def test_preview_write_rejects_invalid_model_name():
    server = importlib.import_module("odoo_mcp.server")
    result = server.preview_write("bad model", "create", values={"x": 1})
    assert result["success"] is False
    assert "Invalid" in result["error"]


def test_validate_write_returns_error_when_live_metadata_call_fails():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {"error": "no access"}

    result = server.validate_write(
        FakeCtx(_Client()), "res.partner", "write", values={"x": 1}, record_ids=[1]
    )
    assert result["success"] is False
    assert "no access" in result["error"]


def test_execute_approved_write_rejects_invalid_operation(monkeypatch):
    """Cover line 1563: ValueError raised for unsupported operation.

    Build a canonical payload with an invalid operation, register it
    server-side as a validated approval, then call execute_approved_write
    which must reject the operation despite token + confirm + env all passing.
    """
    server = importlib.import_module("odoo_mcp.server")
    import time as _time

    class _Client:
        def execute_method(self, *args, **kwargs):
            raise AssertionError("must reject before reaching the client")

    ctx = FakeCtx(_Client())
    canonical = {
        "model": "res.partner",
        "operation": "destroy",  # invalid
        "record_ids": [7],
        "values": {"name": "Ada"},
        "context": {},
        "instance": "default",
    }
    token = server.build_approval_token(canonical)
    approval = {**canonical, "token": token}
    # Register as if validate_write had stored it
    ctx.request_context.lifespan_context.write_approvals[token] = {
        "approval": dict(approval),
        "payload": server.write_approval_payload(approval),
        "validated_at": _time.time(),
        "expires_at": _time.time() + 600,
    }
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, approval, confirm=True)
    assert result["success"] is False
    assert "operation must be one of" in result["error"]


def test_execute_approved_write_rejects_when_payload_does_not_match(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {"name": {"type": "char", "readonly": False}}

        def execute_method(self, *args, **kwargs):
            return True

    ctx = FakeCtx(_Client())
    validation = server.validate_write(
        ctx, "res.partner", "write", values={"name": "Ada"}, record_ids=[7]
    )
    # Mutate stored validation record's payload so equality fails
    record = list(ctx.request_context.lifespan_context.write_approvals.values())[0]
    record["payload"]["values"] = {"name": "DIFFERENT"}
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, validation["approval"], confirm=True)
    assert result["success"] is False
    assert "stored validation record" in result["error"]


def test_execute_approved_write_requires_confirm_true(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {"name": {"type": "char", "readonly": False}}

        def execute_method(self, *args, **kwargs):
            return True

    ctx = FakeCtx(_Client())
    validation = server.validate_write(
        ctx, "res.partner", "write", values={"name": "Ada"}, record_ids=[7]
    )
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, validation["approval"], confirm=False)
    assert result["success"] is False
    assert "confirm" in result["error"]


def test_execute_approved_write_runs_create_path(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class _Client:
        def get_model_fields(self, model):
            return {"name": {"type": "char", "readonly": False}}

        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return 7

    ctx = FakeCtx(_Client())
    validation = server.validate_write(
        ctx, "res.partner", "create", values={"name": "Ada"}
    )
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, validation["approval"], confirm=True)
    assert result["success"] is True
    assert calls[0][0] == ("res.partner", "create", {"name": "Ada"})


def test_execute_approved_write_runs_unlink_path(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    calls = []

    class _Client:
        def get_model_fields(self, model):
            return {"name": {"type": "char", "readonly": False}}

        def execute_method(self, *args, **kwargs):
            calls.append((args, kwargs))
            return True

    ctx = FakeCtx(_Client())
    validation = server.validate_write(ctx, "res.partner", "unlink", record_ids=[7])
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, validation["approval"], confirm=True)
    assert result["success"] is True
    assert calls[0][0] == ("res.partner", "unlink", [7])


# ----- scan_addons_source / build_domain / business_pack tool wrappers --


def test_scan_addons_source_tool_rejects_invalid_max_file_bytes():
    server = importlib.import_module("odoo_mcp.server")
    result = server.scan_addons_source(max_file_bytes=0)
    assert result["success"] is False
    assert "max_file_bytes" in result["error"]


def test_build_domain_tool_returns_error_payload_on_unexpected_exception(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    def boom(*args, **kwargs):
        raise RuntimeError("oh no")

    monkeypatch.setattr(server, "build_domain_report", boom)
    result = server.build_domain([])
    assert result["success"] is False
    assert "oh no" in result["error"]


def test_business_pack_report_tool_returns_error_payload_on_exception(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_models(self):
            raise RuntimeError("rpc")

        def get_installed_modules(self, limit=200):
            return []

    result = server.business_pack_report(FakeCtx(_Client()), "sales")
    assert result["success"] is False
    assert "rpc" in result["error"]


def test_business_pack_report_skips_live_metadata_when_disabled():
    server = importlib.import_module("odoo_mcp.server")

    class _NopeClient:
        def get_models(self):  # pragma: no cover
            raise AssertionError("must not be called")

    result = server.business_pack_report(
        FakeCtx(_NopeClient()), "sales", use_live_metadata=False
    )
    assert result["success"] is True


def test_business_pack_report_skips_modules_when_get_models_returns_error():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_models(self):
            return {"error": "ACL"}

        def get_installed_modules(self, limit=200):
            return [{"name": "sale"}]

    result = server.business_pack_report(FakeCtx(_Client()), "sales")
    assert result["success"] is True


# ----- get_odoo_profile error path -------------------------------------


def test_get_odoo_profile_returns_error_when_module_limit_invalid():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        pass

    result = server.get_odoo_profile(FakeCtx(_Client()), module_limit=0)
    assert result["success"] is False
    assert "limit" in result["error"]


# ----- inspect_model_relationships error path --------------------------


def test_inspect_model_relationships_returns_error_when_live_metadata_fails():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            raise RuntimeError("rpc")

    report = server.inspect_model_relationships(
        FakeCtx(_Client()), "res.partner", use_live_metadata=True
    )
    assert report["success"] is False or report["metadata_used"]["error"]


def test_inspect_model_relationships_returns_error_payload_when_metadata_dict_has_error():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {"error": "Access denied"}

    report = server.inspect_model_relationships(
        FakeCtx(_Client()), "res.partner", use_live_metadata=True
    )
    # The function returns a "no metadata" report with metadata_error set
    assert report["success"] is False
    assert "Access denied" in str(report["metadata_used"].get("error"))


def test_inspect_model_relationships_outer_exception_path():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            raise RuntimeError("rpc")

    # Using invalid model name forces validate_model_name to raise before
    # reaching get_model_fields, demonstrating the outer except path.
    report = server.inspect_model_relationships(
        FakeCtx(_Client()), "bad model", use_live_metadata=False
    )
    assert report["success"] is False
    assert "Invalid" in report["error"]


# ----- diagnose_odoo_call use_live_metadata flag ----------------------


def test_diagnose_odoo_call_includes_live_metadata_not_used_note():
    server = importlib.import_module("odoo_mcp.server")
    report = server.diagnose_odoo_call(
        "res.partner", "search_read", use_live_metadata=True
    )
    codes = {issue["code"] for issue in report["issues"]}
    assert "live_metadata_not_used" in codes


def test_upgrade_risk_report_includes_live_metadata_not_used_note():
    server = importlib.import_module("odoo_mcp.server")
    report = server.upgrade_risk_report(use_live_metadata=True, target_version="18.0")
    codes = {risk["code"] for risk in report["risks"]}
    assert "live_metadata_not_used" in codes


def test_fit_gap_report_includes_live_metadata_not_used_assumption():
    server = importlib.import_module("odoo_mcp.server")
    report = server.fit_gap_report(["Track contacts"], use_live_metadata=True)
    assert any(
        "fit_gap_report is input-driven" in str(item) for item in report["assumptions"]
    )


# ----- list_models / get_model_fields / search_records / read_record err paths -


def test_list_models_returns_error_when_get_models_fails():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_models(self):
            return {"error": "ACL"}

    result = server.list_models(FakeCtx(_Client()))
    assert result["success"] is False
    assert "ACL" in result["error"]


def test_list_models_filters_by_query_against_model_and_display_name():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_models(self):
            return {
                "model_names": ["res.partner", "res.users", "sale.order"],
                "models_details": {
                    "res.partner": {"name": "Contact"},
                    "res.users": {"name": "User"},
                    "sale.order": {"name": "Sales Order"},
                },
            }

    result = server.list_models(FakeCtx(_Client()), query="contact")
    names = [r["model"] for r in result["result"]]
    assert names == ["res.partner"]


def test_list_models_propagates_clamp_error():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        pass

    result = server.list_models(FakeCtx(_Client()), limit=0)
    assert result["success"] is False
    assert "limit" in result["error"]


def test_get_model_fields_tool_filters_by_field_names():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {
                "name": {"type": "char"},
                "ref": {"type": "char"},
                "active": {"type": "boolean"},
            }

    result = server.get_model_fields(
        FakeCtx(_Client()), "res.partner", field_names=["name", "ref", "ghost"]
    )
    assert set(result["result"].keys()) == {"name", "ref"}


def test_get_model_fields_tool_returns_error_when_model_returns_error():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {"error": "no access"}

    result = server.get_model_fields(FakeCtx(_Client()), "res.partner")
    assert result["success"] is False


def test_get_model_fields_tool_rejects_invalid_model_name():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        pass

    result = server.get_model_fields(FakeCtx(_Client()), "bad model")
    assert result["success"] is False


def test_search_records_rejects_negative_offset():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()
    result = server.search_records(FakeCtx(client), "res.partner", offset=-1)
    assert result["success"] is False
    assert "offset" in result["error"]


def test_search_records_rejects_invalid_model_name():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()
    result = server.search_records(FakeCtx(client), "bad model")
    assert result["success"] is False


def test_read_record_rejects_zero_id():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()
    result = server.read_record(FakeCtx(client), "res.partner", record_id=0)
    assert result["success"] is False
    assert "record_id" in result["error"]


def test_read_record_returns_error_when_no_record_found():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {"id": {"type": "integer"}, "name": {"type": "char"}}

        def read_records(self, model, ids, fields=None):
            return []

    result = server.read_record(FakeCtx(_Client()), "res.partner", record_id=99)
    assert result["success"] is False
    assert "Record not found" in result["error"]


def test_read_record_rejects_invalid_model_name():
    server = importlib.import_module("odoo_mcp.server")
    client = _SmartFieldsClient()
    result = server.read_record(FakeCtx(client), "bad model", record_id=1)
    assert result["success"] is False


# ----- aggregate_records additional branches ---------------------------


def test_aggregate_records_no_measures_passes_empty_aggregates():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="19.0", rows=[])
    result = server.aggregate_records(
        FakeCtx(client), model="sale.order", group_by=["state"], measures=None
    )
    assert result["success"] is True
    assert result["measures"] == []


def test_aggregate_records_offset_propagates_for_legacy_path():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="17.0", rows=[])
    server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["id:count"],
        offset=5,
    )
    assert client.calls[0][2]["offset"] == 5


def test_aggregate_records_falls_back_propagates_offset_and_order():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(
        version="no-digits-here",
        rows=[],
        formatted_exception=AttributeError(
            "The method 'sale.order.formatted_read_group' does not exist"
        ),
    )
    server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["id:count"],
        offset=3,
        limit=50,
        order="id:count desc",
    )
    fallback_kwargs = next(
        kwargs for _, method, kwargs in client.calls if method == "read_group"
    )
    assert fallback_kwargs["offset"] == 3
    assert fallback_kwargs["limit"] == 50
    assert fallback_kwargs["orderby"] == "id:count desc"


def test_aggregate_records_passes_offset_and_order_in_formatted_path():
    server = importlib.import_module("odoo_mcp.server")
    client = _AggregateClient(version="19.0", rows=[])
    server.aggregate_records(
        FakeCtx(client),
        model="sale.order",
        group_by=["state"],
        measures=["amount_total:sum"],
        offset=2,
        limit=20,
        order="amount_total:sum desc",
    )
    kwargs = client.calls[0][2]
    assert kwargs["offset"] == 2
    assert kwargs["limit"] == 20
    assert kwargs["order"] == "amount_total:sum desc"


# ----- chatter_post additional branches ---------------------------------


def test_chatter_post_returns_error_for_empty_body_validation_message(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    result = server.chatter_post(
        FakeCtx(_ChatterClient()),
        model="res.partner",
        record_id=1,
        body="",
    )
    assert result["success"] is False


# ----- search_employee / search_holidays branches -----------------------


def test_search_employee_returns_results_via_name_search():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def execute_method(self, model, method, *args, **kwargs):
            return [(7, "Ada Lovelace"), (8, "Grace Hopper")]

    result = server.search_employee(FakeCtx(_Client()), name="Ada")
    assert result.success is True
    assert result.result[0].name == "Ada Lovelace"


def test_search_employee_returns_error_on_failure():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def execute_method(self, *args, **kwargs):
            raise RuntimeError("rpc")

    result = server.search_employee(FakeCtx(_Client()), name="Ada")
    assert result.success is False
    assert "rpc" in result.error


def test_search_holidays_rejects_invalid_start_date():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        pass

    result = server.search_holidays(
        FakeCtx(_Client()), start_date="garbage", end_date="2024-01-01"
    )
    assert result.success is False
    assert "start_date" in result.error


def test_search_holidays_rejects_invalid_end_date():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        pass

    result = server.search_holidays(
        FakeCtx(_Client()), start_date="2024-01-01", end_date="oops"
    )
    assert result.success is False
    assert "end_date" in result.error


def test_search_holidays_returns_results_with_employee_filter():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def search_read(self, model_name, domain, **kwargs):
            return [
                {
                    "display_name": "Holiday",
                    "start_datetime": "2024-01-01 00:00:00",
                    "stop_datetime": "2024-01-02 00:00:00",
                    "employee_id": [7, "Ada"],
                    "name": "Vacation",
                    "state": "validate",
                }
            ]

    result = server.search_holidays(
        FakeCtx(_Client()),
        start_date="2024-01-01",
        end_date="2024-01-31",
        employee_id=7,
    )
    assert result.success is True
    assert result.result[0].name == "Vacation"


def test_search_holidays_returns_error_on_search_failure():
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def search_read(self, *args, **kwargs):
            raise RuntimeError("rpc")

    result = server.search_holidays(
        FakeCtx(_Client()), start_date="2024-01-01", end_date="2024-01-31"
    )
    assert result.success is False
    assert "rpc" in result.error


# ----- prompt rendering -------------------------------------------------


def test_prompt_diagnose_failed_odoo_call_renders_arguments():
    server = importlib.import_module("odoo_mcp.server")
    text = server.prompt_diagnose_failed_odoo_call(
        "res.partner", "write", error="Access denied"
    )
    assert "res.partner" in text
    assert "write" in text
    assert "Access denied" in text


def test_prompt_diagnose_failed_odoo_call_uses_placeholder_when_no_error():
    server = importlib.import_module("odoo_mcp.server")
    text = server.prompt_diagnose_failed_odoo_call("res.partner", "write")
    assert "<not provided>" in text


def test_prompt_fit_gap_workshop_renders_requirement():
    server = importlib.import_module("odoo_mcp.server")
    text = server.prompt_fit_gap_workshop("Track expenses")
    assert "Track expenses" in text


def test_prompt_json2_migration_plan_renders_inputs():
    server = importlib.import_module("odoo_mcp.server")
    text = server.prompt_json2_migration_plan("sale.order", "action_confirm")
    assert "sale.order" in text
    assert "action_confirm" in text


def test_prompt_custom_module_audit_renders_addons_path():
    server = importlib.import_module("odoo_mcp.server")
    text = server.prompt_custom_module_audit("/srv/addons")
    assert "/srv/addons" in text


# ----- execute_method search-method domain normalization ---------------


def test_is_relative_to_returns_true_when_path_inside_root(tmp_path):
    server = importlib.import_module("odoo_mcp.server")
    sub = tmp_path / "sub"
    sub.mkdir()
    assert server._is_relative_to(sub, tmp_path) is True
    assert server._is_relative_to(tmp_path, sub) is False


def test_diagnose_access_reports_acl_denied_likely_when_no_granting_rows():
    """Cover the acl_denied_likely branch (lines 755-762, 756 in particular)."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {"groups_id": {"type": "many2many"}}
            if model == "res.users" and method == "read":
                return [{"id": 7, "groups_id": [99]}]  # not in ACL group
            if model == "ir.model.access":
                return [
                    {
                        "id": 1,
                        "name": "ACL row",
                        "model_id": [11, "C"],
                        "group_id": [3, "Sales"],
                        "perm_read": False,  # not granting read
                        "perm_write": False,
                        "perm_create": False,
                        "perm_unlink": False,
                    }
                ]
            if model == "ir.rule":
                return []
            if model == "res.partner" and method == "search_count":
                return 0
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(FakeCtx(_Client()), "res.partner", "read")
    code_names = {c["code"] for c in report["diagnosis"]["codes"]}
    assert "acl_denied_likely" in code_names


def test_diagnose_access_reports_metadata_error_when_ir_model_search_fails():
    """Cover lines 971-972, 982 (ir.model error, model_id None branch)."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                raise RuntimeError("ACL on ir.model")
            if model == "res.users" and method == "fields_get":
                return {}
            if model == "res.users" and method == "read":
                return [{"id": 7}]
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(FakeCtx(_Client()), "res.partner", "read")
    stages = {err["stage"] for err in report["metadata_errors"]}
    assert "ir.model" in stages


def test_diagnose_access_recovers_uid_from_user_context_when_client_lacks_attribute():
    """Cover line 1011: uid from user_context.get('uid')."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        # NOTE: no `uid` attribute → falls back to user_context["uid"]
        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {"groups_id": {"type": "many2many"}}
            if model == "res.users" and method == "read":
                return [{"id": 7, "groups_id": [3]}]
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                return []
            if model == "res.partner" and method == "search_count":
                return 1
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(FakeCtx(_Client()), "res.partner", "read")
    assert report["current_user"]["uid"] == 7


def test_diagnose_access_records_user_fields_and_read_errors():
    """Cover lines 1033, 1045 (errors on res.users.fields_get/read)."""
    server = importlib.import_module("odoo_mcp.server")

    state = {"fields_called": False}

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                if not state["fields_called"]:
                    state["fields_called"] = True
                    raise RuntimeError("fields_get blocked")
                return {}
            if model == "res.users" and method == "read":
                raise RuntimeError("read blocked")
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                return []
            if model == "res.partner" and method == "search_count":
                return 0
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(FakeCtx(_Client()), "res.partner", "read")
    stages = {err["stage"] for err in report["metadata_errors"]}
    assert "res.users.fields_get" in stages or "res.users.read" in stages


def test_diagnose_access_skips_non_dict_rule_entries():
    """Cover line 1122: skip non-dict rule entries."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {"groups_id": {"type": "many2many"}}
            if model == "res.users" and method == "read":
                return [{"id": 7, "groups_id": [3]}]
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                # Mix dict and non-dict entries to exercise the skip
                return [
                    "broken",
                    {
                        "id": 1,
                        "name": "valid",
                        "active": True,
                        "perm_read": True,
                        "groups": False,
                    },
                ]
            if model == "res.partner" and method == "search_count":
                return 1
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(FakeCtx(_Client()), "res.partner", "read")
    assert len(report["rules"]["active"]) == 1


def test_diagnose_access_reports_search_count_error():
    """Cover line 1142: error on search_count metadata read."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        uid = 7

        def get_user_context(self):
            return {"lang": "en_US", "uid": 7}

        def execute_method(self, model, method, *args, **kwargs):
            if model == "ir.model":
                return [{"id": 11, "name": "C", "model": "res.partner"}]
            if model == "res.users" and method == "fields_get":
                return {}
            if model == "res.users" and method == "read":
                return [{"id": 7}]
            if model == "ir.model.access":
                return []
            if model == "ir.rule":
                return []
            if model == "res.partner" and method == "search_count":
                raise RuntimeError("count blocked")
            raise AssertionError(f"unexpected: {model}.{method}")

    report = server.diagnose_access(
        FakeCtx(_Client()), "res.partner", "read", expected_count=5
    )
    stages = {err["stage"] for err in report["metadata_errors"]}
    assert "res.partner.search_count" in stages


def test_get_odoo_profile_with_include_modules_true_calls_get_profile():
    """Cover line 1284: include_modules=True path uses get_profile."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_profile(self, module_limit=100):
            return {
                "url": "u",
                "hostname": "h",
                "database": "db",
                "username": "user",
                "transport": "xmlrpc",
                "timeout": 3,
                "verify_ssl": True,
                "json2_database_header": True,
                "server_version": {},
                "user_context": {},
                "installed_modules": [{"name": "base"}],
                "installed_module_count": 1,
            }

    report = server.get_odoo_profile(
        FakeCtx(_Client()), include_modules=True, module_limit=10
    )
    assert report["success"] is True
    assert report["profile"]["installed_module_count"] == 1


def test_validate_write_outer_exception_returns_error_payload(monkeypatch):
    """Cover lines 1505-1506: outer except in validate_write."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            raise RuntimeError("boom")

    # Live metadata raises → outer except wraps the error
    result = server.validate_write(
        FakeCtx(_Client()), "res.partner", "write", values={"x": 1}, record_ids=[1]
    )
    assert result["success"] is False
    assert "boom" in result["error"]


def test_execute_approved_write_outer_exception_returns_error_payload(monkeypatch):
    """Cover lines 1585-1586: outer except in execute_approved_write."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def get_model_fields(self, model):
            return {"name": {"type": "char", "readonly": False}}

        def execute_method(self, *args, **kwargs):
            raise RuntimeError("exec blew up")

    ctx = FakeCtx(_Client())
    validation = server.validate_write(
        ctx, "res.partner", "write", values={"name": "Ada"}, record_ids=[7]
    )
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, validation["approval"], confirm=True)
    assert result["success"] is False
    assert "exec blew up" in result["error"]


def test_execute_method_normalizes_domain_for_search_methods(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", "0")
    captured = []

    class _Client:
        def execute_method(self, *args, **kwargs):
            captured.append((args, kwargs))
            return []

    # search is read-only; normalization runs because args[0] is a JSON string
    server.execute_method(
        FakeCtx(_Client()),
        "res.partner",
        "search_read",
        args=['[["name","=","Ada"]]'],
    )
    assert captured[0][0] == ("res.partner", "search_read", [["name", "=", "Ada"]])


# ----- Multi-instance support (v0.4) ----------------------------------------


class _NamedClient:
    """Minimal fake client that records calls for routing assertions."""

    def __init__(self, label):
        self.label = label
        self.calls = []

    def search_read(self, **kwargs):
        self.calls.append(("search_read", kwargs))
        return [{"id": 1, "name": self.label}]

    def get_model_fields(self, model):
        self.calls.append(("get_model_fields", model))
        return {"name": {"type": "char", "readonly": False}}

    def execute_method(self, *args, **kwargs):
        self.calls.append(("execute_method", args, kwargs))
        return {"posted_by": self.label}


def test_search_records_routes_to_named_instance():
    server = importlib.import_module("odoo_mcp.server")
    default_client = _NamedClient("default")
    globex_client = _NamedClient("globex")
    ctx = FakeCtx(default_client, clients={"globex": globex_client})

    result = server.search_records(
        ctx, "res.partner", fields=["name"], instance="globex"
    )

    assert result["success"] is True
    assert result["result"][0]["name"] == "globex"
    assert globex_client.calls and not default_client.calls


def test_tool_with_unknown_instance_returns_error_listing_names():
    server = importlib.import_module("odoo_mcp.server")
    ctx = FakeCtx(_NamedClient("default"), clients={"globex": _NamedClient("globex")})

    result = server.search_records(ctx, "res.partner", instance="ghost")

    assert result["success"] is False
    assert "ghost" in result["error"]
    assert "globex" in result["error"]


def test_schema_cache_is_partitioned_by_instance(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setattr(server, "resolve_default_instance_name", lambda: "default")
    default_client = _NamedClient("default")
    globex_client = _NamedClient("globex")
    ctx = FakeCtx(default_client, clients={"globex": globex_client})
    app_context = ctx.request_context.lifespan_context

    server.search_records(ctx, "res.partner")
    server.search_records(ctx, "res.partner", instance="globex")

    cache_keys = set(app_context.schema_cache)
    assert any(key == "default:res.partner" for key in cache_keys)
    assert any(key == "globex:res.partner" for key in cache_keys)


def test_cross_instance_approval_token_replay_is_rejected(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setattr(server, "resolve_default_instance_name", lambda: "default")
    default_client = _NamedClient("default")
    globex_client = _NamedClient("globex")
    ctx = FakeCtx(default_client, clients={"globex": globex_client})

    validation = server.validate_write(
        ctx, "res.partner", "write", values={"name": "Ada"}, record_ids=[7]
    )
    assert validation["success"] is True
    assert validation["approval"]["instance"] == "default"

    # Replay the approved token against another instance: hash must not verify.
    tampered = dict(validation["approval"])
    tampered["instance"] = "globex"
    is_valid, _ = server.verify_write_approval(tampered)
    assert is_valid is False

    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    rejected = server.execute_approved_write(ctx, tampered, confirm=True)
    assert rejected["success"] is False
    assert "token" in rejected["error"]
    assert not globex_client.calls


def test_validate_write_tokens_differ_between_instances(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setattr(server, "resolve_default_instance_name", lambda: "default")
    default_client = _NamedClient("default")
    globex_client = _NamedClient("globex")
    ctx = FakeCtx(default_client, clients={"globex": globex_client})

    on_default = server.validate_write(
        ctx, "res.partner", "write", values={"name": "Ada"}, record_ids=[7]
    )
    on_globex = server.validate_write(
        ctx,
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        instance="globex",
    )

    assert on_default["approval"]["token"] != on_globex["approval"]["token"]
    assert on_globex["approval"]["instance"] == "globex"


def test_execute_approved_write_runs_on_instance_named_in_approval(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setattr(server, "resolve_default_instance_name", lambda: "default")
    default_client = _NamedClient("default")
    globex_client = _NamedClient("globex")
    ctx = FakeCtx(default_client, clients={"globex": globex_client})

    validation = server.validate_write(
        ctx,
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        instance="globex",
    )
    assert validation["success"] is True

    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, validation["approval"], confirm=True)

    assert result["success"] is True
    assert result["instance"] == "globex"
    assert any(call[0] == "execute_method" for call in globex_client.calls)
    assert not any(call[0] == "execute_method" for call in default_client.calls)


def test_chatter_post_tokens_differ_between_instances(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setattr(server, "resolve_default_instance_name", lambda: "default")
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    default_client = _NamedClient("default")
    globex_client = _NamedClient("globex")
    ctx = FakeCtx(default_client, clients={"globex": globex_client})

    on_default = server.chatter_post(ctx, "res.partner", 7, "hello")
    on_globex = server.chatter_post(ctx, "res.partner", 7, "hello", instance="globex")

    assert on_default["mode"] == "preview"
    assert on_globex["mode"] == "preview"
    assert on_default["approval"]["token"] != on_globex["approval"]["token"]
    assert on_globex["approval"]["instance"] == "globex"


def test_list_instances_tool_reports_names_without_credentials(monkeypatch, tmp_path):
    server = importlib.import_module("odoo_mcp.server")
    for key in (
        "ODOO_URL",
        "ODOO_DB",
        "ODOO_USERNAME",
        "ODOO_PASSWORD",
        "ODOO_CONFIG_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "odoo_config.json").write_text(
        json.dumps(
            {
                "default": "acme",
                "instances": {
                    "acme": {
                        "url": "https://acme.odoo.test",
                        "db": "acme",
                        "username": "bot",
                        "api_key": "super-secret-key",
                        "transport": "json2",
                    },
                    "globex": {
                        "url": "https://globex.odoo.test",
                        "db": "globex",
                        "username": "demo",
                        "password": "super-secret-pass",
                    },
                },
            }
        )
    )

    result = call_tool_json(server, "list_instances", {})

    assert result["success"] is True
    assert result["default"] == "acme"
    assert result["instance_count"] == 2
    names = [item["name"] for item in result["instances"]]
    assert names == ["acme", "globex"]
    serialized = json.dumps(result)
    assert "super-secret-key" not in serialized
    assert "super-secret-pass" not in serialized


def test_health_check_reports_instance_posture(monkeypatch, tmp_path):
    server = importlib.import_module("odoo_mcp.server")
    for key in (
        "ODOO_URL",
        "ODOO_DB",
        "ODOO_USERNAME",
        "ODOO_PASSWORD",
        "ODOO_CONFIG_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "odoo_config.json").write_text(
        json.dumps(
            {
                "default": "acme",
                "instances": {
                    "acme": {"url": "https://acme.odoo.test", "db": "acme"},
                    "globex": {"url": "https://globex.odoo.test", "db": "globex"},
                },
            }
        )
    )

    health = call_tool_json(server, "health_check", {})

    assert health["runtime"]["odoo_instances"] == {
        "instance_count": 2,
        "default_instance": "acme",
    }


def test_preview_write_offline_accepts_instance_when_no_config(monkeypatch):
    """Offline preview must still mint an instance-bound token without config."""
    server = importlib.import_module("odoo_mcp.server")

    def no_config():
        raise FileNotFoundError("no config in offline preview")

    monkeypatch.setattr(server, "load_instances_config", no_config)
    preview = server.preview_write(
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        instance="globex",
    )

    assert preview["success"] is True
    assert preview["approval"]["instance"] == "globex"
    assert preview["approval"]["token"].startswith("odoo-write:")


def test_preview_write_rejects_unknown_instance_when_config_present(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setattr(
        server,
        "load_instances_config",
        lambda: ("acme", {"acme": {}, "globex": {}}),
    )

    preview = server.preview_write(
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        instance="ghost",
    )

    assert preview["success"] is False
    assert "ghost" in preview["error"]
    assert "acme" in preview["error"] and "globex" in preview["error"]


def test_validate_write_with_input_metadata_binds_instance_without_client(monkeypatch):
    """Caller-supplied metadata path must bind the token without touching Odoo."""
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setattr(server, "resolve_default_instance_name", lambda: "default")

    class _Untouchable:
        def __getattr__(self, name):
            raise AssertionError("client must not be used with input metadata")

    ctx = FakeCtx(_Untouchable(), clients={})
    # FakeLife.get_client would raise for unknown names; bypass config validation.
    monkeypatch.setattr(server, "resolve_instance_name", lambda name: name or "default")

    report = server.validate_write(
        ctx,
        "res.partner",
        "write",
        values={"name": "Ada"},
        record_ids=[7],
        fields_metadata={"name": {"type": "char", "readonly": False}},
        instance="globex",
    )

    assert report["success"] is True
    assert report["approval"]["instance"] == "globex"
    # Input metadata never authorizes execution.
    assert report["approval_status"]["stored"] is False


def test_execute_approved_write_rejects_legacy_token_without_instance(monkeypatch):
    """A 0.3.x-style approval (no instance key) hashes differently and must fail."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def execute_method(self, *args, **kwargs):
            raise AssertionError("legacy token must fail before execution")

    legacy_payload = {
        "model": "res.partner",
        "operation": "write",
        "record_ids": [7],
        "values": {"name": "Ada"},
        "context": {},
    }
    legacy_token = server.build_approval_token(legacy_payload)
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")

    result = server.execute_approved_write(
        FakeCtx(_Client()),
        {**legacy_payload, "token": legacy_token},
        confirm=True,
    )

    assert result["success"] is False
    assert "token" in result["error"]


def test_app_context_get_client_reuses_cached_named_client(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    built = []

    def fake_get_odoo_client_for(name):
        built.append(name)
        return name, object()

    monkeypatch.setattr(server, "get_odoo_client_for", fake_get_odoo_client_for)
    app_context = server.AppContext()

    name1, client1 = app_context.get_client("globex")
    name2, client2 = app_context.get_client("globex")

    assert name1 == name2 == "globex"
    assert client1 is client2
    assert built == ["globex"]


def test_execute_approved_write_never_echoes_expected_token():
    """Token mismatch must not return the correct token (minting oracle)."""
    server = importlib.import_module("odoo_mcp.server")

    class _Client:
        def execute_method(self, *args, **kwargs):
            raise AssertionError("mismatched token must fail before execution")

    tampered = {
        "model": "res.partner",
        "operation": "write",
        "record_ids": [7],
        "values": {"name": "Ada"},
        "context": {},
        "instance": "default",
        "token": "odoo-write:forged",
    }
    result = server.execute_approved_write(FakeCtx(_Client()), tampered, confirm=True)

    assert result["success"] is False
    assert "expected_token" not in result
    assert "odoo-write:" not in json.dumps(result)


# ----- Free-text query= on search_records -----


class _QuerySearchClient:
    """Recording client capturing the domain search_records sends."""

    def __init__(self):
        self.fields_meta = {
            "id": {"type": "integer"},
            "name": {"type": "char", "searchable": True},
            "email": {"type": "char", "searchable": True},
            "state": {"type": "selection", "searchable": True},
        }
        self.search_read_calls = []

    def get_model_fields(self, model):
        return self.fields_meta

    def search_read(self, model_name, domain, fields=None, **kwargs):
        self.search_read_calls.append({"model": model_name, "domain": domain})
        return [{"id": 1, "name": "Ada"}]


def test_search_records_query_builds_or_ilike_domain():
    server = importlib.import_module("odoo_mcp.server")
    client = _QuerySearchClient()

    result = server.search_records(
        FakeCtx(client),
        "res.partner",
        domain=[["state", "=", "done"]],
        query="ada",
    )

    assert result["success"] is True
    assert result["query_fields_used"] == ["name", "email"]
    sent = client.search_read_calls[0]["domain"]
    assert sent == [
        "|",
        ["name", "ilike", "ada"],
        ["email", "ilike", "ada"],
        ["state", "=", "done"],
    ]


def test_search_records_blank_query_is_ignored():
    server = importlib.import_module("odoo_mcp.server")
    client = _QuerySearchClient()

    result = server.search_records(FakeCtx(client), "res.partner", query="   ")

    assert result["success"] is True
    assert "query_fields_used" not in result
    assert client.search_read_calls[0]["domain"] == []


def test_search_records_query_falls_back_to_name_without_metadata():
    server = importlib.import_module("odoo_mcp.server")
    client = _QuerySearchClient()
    client.fields_meta = {"error": "boom"}

    result = server.search_records(FakeCtx(client), "res.partner", query="ada")

    assert result["success"] is True
    assert result["query_fields_used"] == ["name"]
    assert client.search_read_calls[0]["domain"] == [["name", "ilike", "ada"]]


def test_legacy_names_remain_importable_from_server():
    """Helpers moved out in the 0.8 refactor must stay importable from server."""
    server = importlib.import_module("odoo_mcp.server")
    legacy_names = [
        "MODEL_NAME_RE",
        "METHOD_NAME_RE",
        "MAX_SEARCH_LIMIT",
        "POLICY_FILE_ENV",
        "DEFAULT_POLICY_FILENAME",
        "policy_file_path",
        "_AGGREGATION_FUNCTIONS",
        "BoundedTTLCache",
        "DomainCondition",
        "SearchDomain",
        "normalize_domain_input",
        "clamp_limit",
        "validate_model_name",
        "validate_method_name",
        "parse_measure_spec",
        "parse_odoo_major_version",
        "odoo_major_version",
        "max_attachment_bytes",
        "truthy_env",
        "writes_enabled",
        "allowed_side_effect_methods",
        "side_effect_method_allowed",
        "chatter_direct_enabled",
        "load_side_effect_policy",
        "access_permission_field",
    ]
    missing = [name for name in legacy_names if not hasattr(server, name)]
    assert missing == []
