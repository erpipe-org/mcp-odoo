"""Vendor bill edits must be authorized by live state, never UI flags alone."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from odoo_mcp import server


def make_context(client):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                odoo=client,
                write_approvals={},
                schema_cache={},
                get_client=lambda instance: (instance, client),
            )
        )
    )


class BillClient:
    def __init__(self):
        self.calls = []
        self.move_fields = {}
        self.line_fields = {"name": {"type": "char", "readonly": False}}
        self.move_fields.update(
            {
                "partner_id": {"type": "many2one", "readonly": True},
                "invoice_date": {"type": "date", "readonly": True},
                "invoice_line_ids": {
                    "type": "one2many",
                    "relation": "account.move.line",
                    "readonly": True,
                },
            }
        )
        self.line_fields.update(
            {
                "quantity": {"type": "float", "readonly": False},
                "price_unit": {"type": "float", "readonly": False},
                "tax_ids": {
                    "type": "many2many",
                    "relation": "account.tax",
                    "readonly": False,
                },
            }
        )
        self.rows = [
            {
                "id": 101,
                "state": "draft",
                "move_type": "in_invoice",
                "invoice_line_ids": [201, 202],
            }
        ]

    def execute_method(self, model, method, *args, **kwargs):
        if model == "account.move" and method == "read":
            return deepcopy(self.rows)
        self.calls.append((model, method, args, kwargs))
        return True

    def get_model_fields(self, model):
        return self.line_fields if model == "account.move.line" else self.move_fields


@pytest.fixture
def payload():
    return {
        "partner_id": 9,
        "invoice_date": "2026-11-16",
        "invoice_line_ids": [
            [
                0,
                0,
                {
                    "name": "New",
                    "quantity": 2,
                    "price_unit": 10,
                    "tax_ids": [[6, 0, [3]]],
                },
            ],
            [1, 201, {"name": "Updated"}],
            [2, 202, 0],
        ],
    }


def validate(client, payload, **kwargs):
    ctx = make_context(client)
    report = server.validate_write(
        ctx,
        "account.move",
        "write",
        values=payload,
        record_ids=kwargs.pop("record_ids", [101]),
        **kwargs,
    )
    return ctx, report


def test_live_draft_bill_roundtrip(monkeypatch, payload):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = BillClient()
    original = deepcopy(payload)
    ctx, report = validate(client, payload)
    assert report["success"], report
    assert report["approval_status"]["stored"]
    assert not client.calls
    assert client.move_fields["partner_id"]["readonly"] is True
    result = server.execute_approved_write(ctx, report["approval"], confirm=True)
    assert result["success"], result
    assert payload == original
    assert client.calls == [("account.move", "write", ([101], original), {})]


@pytest.mark.parametrize(
    "change",
    [
        {"state": "posted"},
        {"state": "cancel"},
        {"move_type": "entry"},
        {"move_type": "out_invoice"},
        {"move_type": "in_refund"},
        {"id": 999},
    ],
)
def test_wrong_live_state_or_identity_rejected(payload, change):
    client = BillClient()
    client.rows[0].update(change)
    ctx, report = validate(client, payload)
    assert not report["success"]
    assert not ctx.request_context.lifespan_context.write_approvals
    assert not client.calls


@pytest.mark.parametrize(
    "command",
    [
        [1, 999, {"name": "Foreign"}],
        [2, 999, 0],
        [4, 201, 0],
        [5, 0, 0],
        [6, 0, [201]],
        [0, 0, {"move_id": 999}],
        [0, 0, {"amount_residual": 12}],
        [0, 0, {"tax_ids": [[0, 0, {"name": "New tax"}]]}],
        [False, 0, {"name": "Bad opcode"}],
        [0, False, {"name": "Bad id"}],
        [1, 201],
        [2, 201, {}],
        [0, 0, {}],
    ],
)
def test_unsafe_line_commands_rejected(payload, command):
    payload["invoice_line_ids"] = [command]
    ctx, report = validate(BillClient(), payload)
    assert not report["success"]
    assert not ctx.request_context.lifespan_context.write_approvals


@pytest.mark.parametrize("change", ["posted", "foreign_line"])
def test_execution_rechecks_state_and_line_ownership(monkeypatch, payload, change):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = BillClient()
    ctx, report = validate(client, payload)
    assert report["success"]
    if change == "posted":
        client.rows[0]["state"] = "posted"
    else:
        client.rows[0]["invoice_line_ids"] = []
    result = server.execute_approved_write(ctx, report["approval"], confirm=True)
    assert not result["success"]
    assert not client.calls


@pytest.mark.parametrize(
    "context", [{"check_move_validity": False}, {"default_state": "draft"}]
)
def test_context_cannot_override_live_contract(payload, context):
    assert not validate(BillClient(), payload, context=context)[1]["success"]


def test_input_metadata_cannot_authorize_exception(payload):
    client = BillClient()
    ctx, report = validate(client, payload, fields_metadata=client.move_fields)
    assert not report["success"]
    assert not ctx.request_context.lifespan_context.write_approvals


def test_missing_record_and_multiple_line_targets_rejected(payload):
    client = BillClient()
    assert not validate(client, payload, record_ids=[101, 102])[1]["success"]
    client.rows.append(dict(client.rows[0], id=102))
    assert not validate(client, payload, record_ids=[101, 102])[1]["success"]
    assert validate(client, {"partner_id": 9}, record_ids=[101, 102])[1]["success"]


@pytest.mark.parametrize(
    "field,value", [("state", "draft"), ("move_type", "in_invoice"), ("line_ids", [])]
)
def test_no_state_or_journal_line_mutations(payload, field, value):
    payload[field] = value
    assert not validate(BillClient(), payload)[1]["success"]


def test_child_metadata_required(payload):
    client = BillClient()
    client.line_fields = {"error": "access denied"}
    assert not validate(client, payload)[1]["success"]


def test_readonly_child_not_relaxed(payload):
    client = BillClient()
    client.line_fields["price_unit"]["readonly"] = True
    assert not validate(client, payload)[1]["success"]
