"""Write approvals are bound to the authenticated principal on HTTP transports.

stdio and unauthenticated HTTP keep the legacy per-process approval store;
with the OAuth resource server enabled, only the principal (client, issuer,
subject, MCP session) that ran ``validate_write`` may execute the approval.
"""

import importlib
import json
from contextlib import contextmanager

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from odoo_mcp import audit


class FakeHTTPRequest:
    def __init__(self, session_id=None):
        self.headers = {"mcp-session-id": session_id} if session_id else {}


class FakeRequest:
    def __init__(self, lifespan_context, request=None):
        self.lifespan_context = lifespan_context
        self.request = request


class FakeLife:
    def __init__(self, odoo):
        self.odoo = odoo
        self.schema_cache = {}
        self.write_approvals = {}

    def get_client(self, instance=None):
        return "default", self.odoo


class FakeCtx:
    """Per-caller context sharing one lifespan (= one server process)."""

    def __init__(self, life, session_id=None):
        self.request_context = FakeRequest(life, FakeHTTPRequest(session_id))


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_model_fields(self, model):
        return {"name": {"type": "char", "readonly": False}}

    def execute_method(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True


@contextmanager
def authenticated_as(client_id, subject=None, issuer="https://as.example.com"):
    token = AccessToken(
        token=f"tok-{client_id}-{subject}",
        client_id=client_id,
        scopes=["odoo.write"],
        subject=subject,
        claims={"iss": issuer},
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _server():
    return importlib.import_module("odoo_mcp.server")


def _validate(server, ctx):
    report = server.validate_write(
        ctx, "res.partner", "write", values={"name": "Ada"}, record_ids=[7]
    )
    assert report["approval_status"]["stored"] is True
    return report["approval"]


def test_stdio_unauthenticated_approval_is_unchanged(monkeypatch):
    server = _server()
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = FakeClient()
    life = FakeLife(client)

    approval = _validate(server, FakeCtx(life))
    assert server.current_principal(FakeCtx(life)) is None
    assert life.write_approvals[approval["token"]]["principal"] is None

    # Any unauthenticated caller of the process may execute, as before.
    result = server.execute_approved_write(FakeCtx(life), approval, confirm=True)
    assert result["success"] is True
    assert len(client.calls) == 1


def test_same_principal_can_execute_its_approval(monkeypatch):
    server = _server()
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = FakeClient()
    life = FakeLife(client)

    with authenticated_as("agent-a", subject="user-1"):
        approval = _validate(server, FakeCtx(life, session_id="s-1"))
        record = life.write_approvals[approval["token"]]
        assert record["principal"] == {
            "client_id": "agent-a",
            "issuer": "https://as.example.com",
            "subject": "user-1",
            "session_id": "s-1",
        }
        result = server.execute_approved_write(
            FakeCtx(life, session_id="s-1"), approval, confirm=True
        )

    assert result["success"] is True
    assert len(client.calls) == 1
    assert approval["token"] not in life.write_approvals


def test_different_principal_is_rejected_and_cannot_consume_approval(monkeypatch):
    server = _server()
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = FakeClient()
    life = FakeLife(client)

    with authenticated_as("agent-a", subject="user-1"):
        approval = _validate(server, FakeCtx(life, session_id="s-1"))

    with authenticated_as("agent-b", subject="user-2"):
        denied = server.execute_approved_write(
            FakeCtx(life, session_id="s-2"), dict(approval), confirm=True
        )

    assert denied["success"] is False
    assert "different authenticated principal" in denied["error"]
    assert client.calls == []
    # The foreign attempt neither executed nor evicted A's pending approval.
    assert approval["token"] in life.write_approvals

    with authenticated_as("agent-a", subject="user-1"):
        allowed = server.execute_approved_write(
            FakeCtx(life, session_id="s-1"), approval, confirm=True
        )
    assert allowed["success"] is True
    assert len(client.calls) == 1


def test_users_sharing_one_oauth_client_are_distinct_principals(monkeypatch):
    server = _server()
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = FakeClient()
    life = FakeLife(client)

    with authenticated_as("shared-app", subject="user-1"):
        approval = _validate(server, FakeCtx(life))
    with authenticated_as("shared-app", subject="user-2"):
        denied = server.execute_approved_write(FakeCtx(life), approval, confirm=True)

    assert denied["success"] is False
    assert "different authenticated principal" in denied["error"]
    assert client.calls == []


def test_same_principal_in_another_mcp_session_is_rejected(monkeypatch):
    server = _server()
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = FakeClient()
    life = FakeLife(client)

    with authenticated_as("agent-a", subject="user-1"):
        approval = _validate(server, FakeCtx(life, session_id="s-1"))
        denied = server.execute_approved_write(
            FakeCtx(life, session_id="s-other"), approval, confirm=True
        )

    assert denied["success"] is False
    assert "MCP session" in denied["error"]
    assert client.calls == []


def test_authentication_state_mismatch_is_rejected_both_ways(monkeypatch):
    server = _server()
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = FakeClient()
    life = FakeLife(client)

    with authenticated_as("agent-a", subject="user-1"):
        bound = _validate(server, FakeCtx(life))
    unauthenticated = server.execute_approved_write(FakeCtx(life), bound, confirm=True)
    assert unauthenticated["success"] is False
    assert "different authenticated principal" in unauthenticated["error"]

    unbound = server.validate_write(
        FakeCtx(life), "res.partner", "write", values={"name": "Bob"}, record_ids=[8]
    )["approval"]
    with authenticated_as("agent-a", subject="user-1"):
        authenticated = server.execute_approved_write(
            FakeCtx(life), unbound, confirm=True
        )
    assert authenticated["success"] is False
    assert "different authenticated principal" in authenticated["error"]
    assert client.calls == []


def test_principal_check_does_not_bypass_existing_gates(monkeypatch):
    server = _server()
    client = FakeClient()
    life = FakeLife(client)

    with authenticated_as("agent-a", subject="user-1"):
        approval = _validate(server, FakeCtx(life))
        monkeypatch.delenv("ODOO_MCP_ENABLE_WRITES", raising=False)
        no_writes = server.execute_approved_write(FakeCtx(life), approval, confirm=True)
        no_confirm = server.execute_approved_write(FakeCtx(life), approval)

    assert no_writes["success"] is False
    assert "ODOO_MCP_ENABLE_WRITES" in no_writes["error"]
    assert no_confirm["success"] is False
    assert "confirm=true" in no_confirm["error"]
    assert client.calls == []


def test_audit_log_records_principal_and_digests_session(monkeypatch, tmp_path):
    server = _server()
    log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.AUDIT_LOG_ENV, str(log_path))
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    life = FakeLife(FakeClient())

    with authenticated_as("agent-a", subject="user-1"):
        approval = _validate(server, FakeCtx(life, session_id="secret-session"))
    with authenticated_as("agent-b", subject="user-2"):
        server.execute_approved_write(
            FakeCtx(life, session_id="other-session"), approval, confirm=True
        )
    server.preview_write("res.partner", "unlink", record_ids=[7])

    text = log_path.read_text()
    assert "secret-session" not in text
    validate, execute, preview = [json.loads(line) for line in text.splitlines()]
    assert validate["event"] == "validate"
    assert validate["principal"]["client_id"] == "agent-a"
    assert validate["principal"]["subject"] == "user-1"
    assert validate["principal"]["issuer"] == "https://as.example.com"
    assert validate["principal"]["session_sha256"]
    assert execute["event"] == "execute"
    assert execute["outcome"] == "denied"
    assert execute["principal"]["client_id"] == "agent-b"
    assert (
        execute["principal"]["session_sha256"]
        != validate["principal"]["session_sha256"]
    )
    assert preview["principal"] is None


def test_chatter_approval_is_bound_to_principal(monkeypatch):
    server = _server()
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = FakeClient()
    life = FakeLife(client)

    with authenticated_as("agent-a", subject="user-1"):
        preview = server.chatter_post(
            FakeCtx(life), model="res.partner", record_id=7, body="Hi"
        )
    unauthenticated_preview = server.chatter_post(
        FakeCtx(life), model="res.partner", record_id=7, body="Hi"
    )
    assert preview["approval"]["token"] != unauthenticated_preview["approval"]["token"]

    with authenticated_as("agent-b", subject="user-2"):
        denied = server.chatter_post(
            FakeCtx(life),
            model="res.partner",
            record_id=7,
            body="Hi",
            approval=preview["approval"],
            confirm=True,
        )
    assert denied["success"] is False
    assert "for this caller" in denied["error"]
    assert client.calls == []

    with authenticated_as("agent-a", subject="user-1"):
        allowed = server.chatter_post(
            FakeCtx(life),
            model="res.partner",
            record_id=7,
            body="Hi",
            approval=preview["approval"],
            confirm=True,
        )
    assert allowed["success"] is True
    assert len(client.calls) == 1
