"""End-to-end model ACL enforcement: client choke point + tool-level refusals."""

import importlib
import json

import pytest

from tests.test_batch_write import FakeCtx

import odoo_mcp.odoo_client as oc
from odoo_mcp.model_policy import ModelAccessDenied, reset_model_policy

server = importlib.import_module("odoo_mcp.server")

POLICY = {
    "model_acl": {
        "default": {
            "default": "deny",
            "rules": [
                {"allow": "project.*:@read"},
                {"allow": "project.task:@write"},
                {"allow": "project.project:message_subscribe"},
                {"allow": "project.task:message_post"},
                {"allow": "project.task:action_*"},
                {"deny": "project.project:unlink", "reason": "never via agents"},
            ],
        }
    }
}


@pytest.fixture
def acl(monkeypatch, tmp_path):
    pf = tmp_path / "policy.json"
    pf.write_text(json.dumps(POLICY))
    monkeypatch.setenv("ODOO_MCP_MODEL_POLICY_FILE", str(pf))
    monkeypatch.setattr(server, "resolve_instance_name", lambda name: name or "default")
    monkeypatch.setattr(server, "resolve_default_instance_name", lambda: "default")
    reset_model_policy()
    yield
    reset_model_policy()


class _Models:
    def __init__(self):
        self.calls = []

    def execute_kw(self, db, uid, password, model, method, args, kwargs):
        self.calls.append((model, method))
        if model == "ir.model" and method == "search":
            return [1, 2, 3]
        if model == "ir.model" and method == "read":
            return [
                {"model": "project.task", "name": "Task"},
                {"model": "res.partner", "name": "Contact"},
                {"model": "project.project", "name": "Project"},
            ]
        if method == "fields_get":
            return {"name": {"type": "char", "string": "Name"}}
        if method == "search_read":
            return [{"id": 1, "name": "x"}]
        return True


def real_client(instance="default"):
    """A real OdooClient (bypassing network connect) so _execute is exercised."""
    client = oc.OdooClient.__new__(oc.OdooClient)
    client.transport = "xmlrpc"
    client.db = "db"
    client.uid = 1
    client.password = "pw"
    client.lang = None
    client.instance = instance
    client._models = _Models()
    return client


# --------------------------------------------------------------------------
# Client choke point (covers every tool, plugins, and approved writes)
# --------------------------------------------------------------------------


def test_client_refuses_denied_call_before_rpc(acl):
    client = real_client()
    with pytest.raises(ModelAccessDenied) as excinfo:
        client.execute_method("res.partner", "search_read", [])
    assert "res.partner:search_read" in str(excinfo.value)
    assert client._models.calls == []


def test_client_allows_matching_call(acl):
    client = real_client()
    assert client.execute_method("project.project", "message_subscribe", [1]) is True
    assert client._models.calls == [("project.project", "message_subscribe")]


def test_client_refuses_explicit_deny_inside_allowed_namespace(acl):
    client = real_client()
    with pytest.raises(ModelAccessDenied) as excinfo:
        client.execute_method("project.project", "unlink", [1])
    assert "never via agents" in str(excinfo.value)


def test_client_metadata_helpers_are_exempt(acl):
    client = real_client()
    assert "model_names" in client.get_models()
    assert client.get_model_fields("res.partner") == {
        "name": {"type": "char", "string": "Name"}
    }
    assert ("ir.model", "search") in client._models.calls


def test_client_instance_scoping(acl):
    other = real_client(instance="other")
    other.execute_method("res.partner", "search_read", [])
    assert other._models.calls == [("res.partner", "search_read")]


def test_no_policy_changes_nothing(monkeypatch):
    monkeypatch.delenv("ODOO_MCP_MODEL_POLICY_FILE", raising=False)
    monkeypatch.delenv("ODOO_MCP_POLICY_FILE", raising=False)
    reset_model_policy()
    client = real_client()
    client.execute_method("res.partner", "unlink", [1])
    assert client._models.calls == [("res.partner", "unlink")]


# --------------------------------------------------------------------------
# Tool-level refusals
# --------------------------------------------------------------------------


class _NoCallClient:
    """Fake client that fails the test if any data call is attempted."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise AssertionError(f"ACL refusal must happen before {name}")

        return fail


def assert_refused(out, model, method):
    assert out["success"] is False
    assert "Model ACL denies" in out["error"]
    assert out["acl"]["model"] == model
    assert out["acl"]["method"] == method


def test_search_records_refused(acl):
    out = server.search_records(FakeCtx(_NoCallClient()), model="res.partner")
    assert_refused(out, "res.partner", "search_read")


def test_search_records_allowed(acl):
    out = server.search_records(
        FakeCtx(real_client()), model="project.task", fields=["name"]
    )
    assert out["success"] is True


def test_read_record_refused(acl):
    out = server.read_record(FakeCtx(_NoCallClient()), model="res.partner", record_id=1)
    assert_refused(out, "res.partner", "read")


def test_aggregate_records_refused(acl):
    out = server.aggregate_records(
        FakeCtx(_NoCallClient()), model="res.partner", group_by=["country_id"]
    )
    assert_refused(out, "res.partner", "read_group")


def test_execute_method_refused_before_side_effect_check(acl):
    out = server.execute_method(
        FakeCtx(_NoCallClient()), "sale.order", "action_confirm", args=[[1]]
    )
    assert_refused(out, "sale.order", "action_confirm")


def test_execute_method_exact_allow_counts_as_review(acl, monkeypatch):
    monkeypatch.delenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", raising=False)
    monkeypatch.delenv("ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", raising=False)
    client = real_client()
    out = server.execute_method(
        FakeCtx(client), "project.project", "message_subscribe", args=[[1]]
    )
    assert out["success"] is True
    assert client._models.calls == [("project.project", "message_subscribe")]


def test_execute_method_wildcard_allow_still_needs_review(acl, monkeypatch):
    monkeypatch.delenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", raising=False)
    monkeypatch.delenv("ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", raising=False)
    out = server.execute_method(
        FakeCtx(_NoCallClient()), "project.task", "action_done", args=[[1]]
    )
    assert out["success"] is False
    assert "blocked by default" in out["error"]
    assert "project.task:action_done" in out["error"]


def test_execute_method_default_allow_still_needs_review(monkeypatch, tmp_path):
    pf = tmp_path / "policy.json"
    pf.write_text(
        json.dumps({"model_acl": {"default": {"default": "allow", "rules": [{"deny": "hr.*"}]}}})
    )
    monkeypatch.setenv("ODOO_MCP_MODEL_POLICY_FILE", str(pf))
    monkeypatch.delenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", raising=False)
    monkeypatch.delenv("ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", raising=False)
    monkeypatch.setattr(server, "resolve_instance_name", lambda name: name or "default")
    reset_model_policy()
    out = server.execute_method(
        FakeCtx(_NoCallClient()), "sale.order", "action_confirm", args=[[1]]
    )
    assert out["success"] is False
    assert "blocked by default" in out["error"]


def test_execute_method_legacy_allowlist_still_works(acl, monkeypatch):
    monkeypatch.delenv("ODOO_MCP_ALLOW_UNKNOWN_METHODS", raising=False)
    monkeypatch.setenv("ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS", "project.task.action_done")
    client = real_client()
    out = server.execute_method(FakeCtx(client), "project.task", "action_done", args=[[1]])
    assert out["success"] is True


def test_chatter_post_refused(acl, monkeypatch):
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    out = server.chatter_post(
        FakeCtx(_NoCallClient()), model="res.partner", record_id=1, body="hi"
    )
    assert_refused(out, "res.partner", "message_post")


def test_preview_write_refused(acl):
    out = server.preview_write(
        model="res.partner", operation="write", values={"name": "x"}, record_ids=[1]
    )
    assert_refused(out, "res.partner", "write")
    assert out["tool"] == "preview_write"


def test_validate_write_refused(acl):
    out = server.validate_write(
        FakeCtx(_NoCallClient()),
        model="project.project",
        operation="unlink",
        record_ids=[1],
    )
    assert_refused(out, "project.project", "unlink")
    assert out["tool"] == "validate_write"


def test_validate_write_allowed_model_proceeds(acl):
    out = server.validate_write(
        FakeCtx(real_client()),
        model="project.task",
        operation="write",
        values={"name": "Renamed"},
        record_ids=[1],
    )
    assert "acl" not in out
    assert out["success"] is True


# --------------------------------------------------------------------------
# Schema visibility
# --------------------------------------------------------------------------


def test_list_models_hides_fully_denied_models(acl):
    out = server.list_models(FakeCtx(real_client()))
    assert out["success"] is True
    assert [row["model"] for row in out["result"]] == ["project.project", "project.task"]


def test_schema_catalog_hides_fully_denied_models(acl):
    out = server.schema_catalog(FakeCtx(real_client()), refresh=True)
    assert out["success"] is True
    assert {row["model"] for row in out["result"]} == {"project.project", "project.task"}


def test_get_model_fields_refused_for_hidden_model(acl):
    out = server.get_model_fields(FakeCtx(_NoCallClient()), model="res.partner")
    assert out["success"] is False
    assert "every method" in out["error"]


def test_get_model_fields_allowed_for_visible_model(acl):
    out = server.get_model_fields(FakeCtx(real_client()), model="project.task")
    assert out["success"] is True


def test_inspect_model_relationships_refused_for_hidden_model(acl):
    out = server.inspect_model_relationships(
        FakeCtx(_NoCallClient()), model="res.partner"
    )
    assert out["success"] is False
    assert "every method" in out["error"]


def test_models_resource_filters(acl, monkeypatch):
    monkeypatch.setattr(server, "get_odoo_client", lambda: real_client())
    payload = json.loads(server.get_models())
    assert payload["model_names"] == ["project.project", "project.task"]
    assert set(payload["models_details"]) == {"project.project", "project.task"}


def test_model_resource_refused_for_hidden_model(acl, monkeypatch):
    monkeypatch.setattr(server, "get_odoo_client", lambda: real_client())
    payload = json.loads(server.get_model_info("res.partner"))
    assert "every method" in payload["error"]


def test_record_resource_refused_by_choke_point(acl, monkeypatch):
    monkeypatch.setattr(server, "get_odoo_client", lambda: real_client())
    payload = json.loads(server.get_record("res.partner", "1"))
    assert "Model ACL denies res.partner:read" in payload["error"]


def test_health_check_reports_model_acl(acl):
    runtime = server.health_check()["runtime"]
    assert runtime["model_acl"]["active"] is True
    assert runtime["model_acl"]["instances"]["default"]["default"] == "deny"
