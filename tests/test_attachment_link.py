"""Attachment ownership uses ORM-writable, UI-readonly resource fields."""

from copy import deepcopy

import pytest

from odoo_mcp import server
from tests.test_batch_write import FakeCtx


# Real Odoo 16 fields_get types/readonly flags, not a writable-field mock.
FIELDS = {
    "name": {"type": "char", "readonly": False},
    "datas": {"type": "binary", "readonly": False},
    "res_model": {"type": "char", "readonly": True},
    "res_id": {"type": "many2one_reference", "readonly": True},
    "res_field": {"type": "char", "readonly": True},
    "checksum": {"type": "char", "readonly": True},
    "mimetype": {"type": "char", "readonly": True},
}
LINK = {"res_model": "crm.lead", "res_id": 42}


class Client:
    def __init__(self):
        self.fields = deepcopy(FIELDS)
        self.calls = []

    def get_model_fields(self, model):
        return self.fields

    def execute_method(self, model, method, *args, **kwargs):
        self.calls.append((model, method, args, kwargs))
        return True if method == "write" else 501


def validate(ctx, values=None, **kwargs):
    return server.validate_write(
        ctx,
        kwargs.pop("model", "ir.attachment"),
        kwargs.pop("operation", "write"),
        record_ids=kwargs.pop("record_ids", [501]),
        values=LINK.copy() if values is None else values,
        **kwargs,
    )


@pytest.mark.parametrize("operation", ["create", "write", "batch_create"])
def test_link_preview_validate_execute_preserves_payload(monkeypatch, operation):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = Client()
    ctx = FakeCtx(client)
    values = {"name": "offer.pdf", **LINK} if operation != "write" else LINK.copy()
    payload = (
        {"values_list": [values, {**values, "res_id": 43}]}
        if operation == "batch_create"
        else {"values": values}
    )
    operation = "create" if operation == "batch_create" else operation
    args = {"model": "ir.attachment", "operation": operation, **payload}
    if operation == "write":
        args["record_ids"] = [501]
    original = deepcopy(args)
    preview = server.preview_write(**args)
    report = server.validate_write(ctx, **args)
    assert report["success"], report
    assert report["approval_status"]["stored"]
    assert report["approval"]["token"] == preview["approval"]["token"]
    assert args == original
    assert client.calls == []

    result = server.execute_approved_write(ctx, report["approval"], confirm=True)
    assert result["success"], result
    expected = ([501], values) if operation == "write" else (
        payload.get("values_list", values),
    )
    assert client.calls == [("ir.attachment", operation, expected, {})]
    assert not ctx.request_context.lifespan_context.write_approvals


@pytest.mark.parametrize(
    "values",
    [
        {"res_model": "crm.lead"},
        {"res_id": 42},
        {**LINK, "res_id": 0},
        {**LINK, "res_id": -1},
        {**LINK, "res_id": True},
        {**LINK, "res_id": "42"},
        {**LINK, "res_id": 1.5},
        {**LINK, "res_model": False},
        {**LINK, "res_model": ""},
        {**LINK, "res_model": "crm.lead invalid"},
    ],
)
@pytest.mark.parametrize("readonly", [True, False])
def test_link_requires_explicit_model_and_positive_integer_id(values, readonly):
    client = Client()
    for field in LINK:
        client.fields[field]["readonly"] = readonly
    ctx = FakeCtx(client)
    report = validate(ctx, values)
    assert not report["success"]
    assert any(i["code"] == "invalid_attachment_link" for i in report["issues"])
    assert not report["approval_status"]["stored"]
    assert client.calls == []


@pytest.mark.parametrize("field", ["res_model", "res_id"])
@pytest.mark.parametrize("metadata", [None, {}, {"type": "many2one", "readonly": False}])
def test_link_requires_known_resource_field_types(field, metadata):
    client = Client()
    client.fields[field] = metadata
    report = validate(FakeCtx(client))
    assert not report["success"]
    assert not report["approval_status"]["stored"]


@pytest.mark.parametrize("field", ["res_field", "checksum", "mimetype", "unknown"])
def test_other_fields_are_still_validated(field):
    report = validate(FakeCtx(Client()), {**LINK, field: "not allowed"})
    assert not report["success"]
    assert any(field in issue["message"] for issue in report["issues"])


def test_exception_is_specific_to_attachments():
    report = validate(FakeCtx(Client()), model="custom.document")
    assert not report["success"]
    assert {i["code"] for i in report["issues"]} == {"readonly_field"}


def test_bad_link_in_batch_cannot_be_approved():
    report = validate(
        FakeCtx(Client()), {}, operation="create", record_ids=None,
        values_list=[LINK.copy(), {**LINK, "res_id": 0}],
    )
    assert not report["success"]
    assert any("values_list[1]" in i["message"] for i in report["issues"])
    assert not report["approval_status"]["stored"]


def test_context_default_cannot_complete_partial_link():
    report = validate(
        FakeCtx(Client()), {"res_model": "crm.lead"}, operation="create",
        context={"default_res_id": 42},
    )
    assert not report["success"]


@pytest.mark.parametrize("options", [{"fields_metadata": FIELDS}, {"use_live_metadata": False}])
def test_untrusted_metadata_cannot_authorize_link(options):
    client = Client()
    ctx = FakeCtx(client)
    report = validate(ctx, **options)
    assert not report["approval_status"]["stored"]
    preview = server.preview_write("ir.attachment", "write", values=LINK, record_ids=[501])
    result = server.execute_approved_write(ctx, preview["approval"], confirm=True)
    assert not result["success"]
    assert client.calls == []


@pytest.mark.parametrize("gate", ["confirm", "writes", "tamper"])
def test_link_keeps_execution_gates(monkeypatch, gate):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "0" if gate == "writes" else "1")
    client = Client()
    ctx = FakeCtx(client)
    report = validate(ctx)
    assert report["success"], report
    approval = deepcopy(report["approval"])
    if gate == "tamper":
        approval["values"]["res_id"] = 99
    result = server.execute_approved_write(ctx, approval, confirm=gate != "confirm")
    assert not result["success"]
    assert client.calls == []


def test_odoo_access_error_is_not_hidden(monkeypatch):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = Client()
    ctx = FakeCtx(client)
    report = validate(ctx)
    assert report["success"], report

    def denied(*args, **kwargs):
        raise RuntimeError("AccessError: target document is not writable")

    monkeypatch.setattr(client, "execute_method", denied)
    result = server.execute_approved_write(ctx, report["approval"], confirm=True)
    assert not result["success"]
    assert "AccessError" in result["error"]
