"""The journal and attachment exceptions coexist without sharing permissions."""

from copy import deepcopy

import pytest

from odoo_mcp import server
from tests.test_attachment_link import FIELDS as ATTACHMENT_FIELDS
from tests.test_journal_entry_write import (
    LINE_FIELDS,
    MOVE_FIELDS,
    RecordingClient,
    make_context,
)


MOVE = {
    "move_type": "entry",
    "journal_id": 7,
    "line_ids": [
        [0, 0, {"account_id": 11, "debit": 10}],
        [0, 0, {"account_id": 12, "credit": 10}],
    ],
}
LINK = {"res_model": "account.move", "res_id": 101}


class Client(RecordingClient):
    def __init__(self):
        super().__init__()
        self.fields = deepcopy({
            "account.move": MOVE_FIELDS,
            "account.move.line": LINE_FIELDS,
            "ir.attachment": ATTACHMENT_FIELDS,
        })

    def get_model_fields(self, model):
        self.metadata_calls.append(model)
        return self.fields[model]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("attachment_operation", ["create", "write", "batch_create"])
def test_both_approvals_coexist_in_one_session(monkeypatch, batch, attachment_operation):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = Client()
    ctx = make_context(client)
    move_payload = {"values_list": [MOVE, deepcopy(MOVE)]} if batch else {"values": MOVE}
    attachment_payload = (
        {"values_list": [LINK, {**LINK, "res_id": 102}]}
        if attachment_operation == "batch_create"
        else {"values": LINK}
    )
    operation = "write" if attachment_operation == "write" else "create"
    if operation == "write":
        attachment_payload["record_ids"] = [501]
    requests = [
        {"model": "account.move", "operation": "create", **move_payload},
        {"model": "ir.attachment", "operation": operation, **attachment_payload},
    ]
    original = deepcopy(requests)
    approvals = []
    for request in requests:
        preview = server.preview_write(**request)
        report = server.validate_write(ctx, **request)
        assert report["success"], report
        assert report["approval_status"]["stored"]
        assert report["approval"]["token"] == preview["approval"]["token"]
        approvals.append(report["approval"])
    assert client.metadata_calls == ["account.move", "account.move.line", "ir.attachment"]
    assert client.calls == []
    stored = ctx.request_context.lifespan_context.write_approvals
    assert set(stored) == {approval["token"] for approval in approvals}
    for approval in approvals:
        result = server.execute_approved_write(ctx, approval, confirm=True)
        assert result["success"], result
        assert approval["token"] not in stored
    assert not stored
    assert requests == original
    expected_attachment = (
        ([501], LINK) if operation == "write"
        else (attachment_payload.get("values_list", LINK),)
    )
    assert client.calls == [
        ("account.move", "create", (move_payload.get("values_list", MOVE),), {}),
        ("ir.attachment", operation, expected_attachment, {}),
    ]


@pytest.mark.parametrize("model", ["account.move", "ir.attachment", "account.move.line"])
def test_readonly_exceptions_do_not_cross_model_or_child_boundaries(model):
    client = Client()
    # Give the other contract's fields known, readonly metadata so this checks
    # scope rather than merely rejecting unknown fields on standard Odoo models.
    if model == "ir.attachment":
        extra = MOVE
        client.fields[model].update(deepcopy(MOVE_FIELDS))
        values = {**LINK, **extra}
    else:
        extra = LINK
        client.fields[model].update(deepcopy(ATTACHMENT_FIELDS))
        values = deepcopy(MOVE)
        if model == "account.move.line":
            values["line_ids"][0][2].update(extra)
        else:
            values.update(extra)
    ctx = make_context(client)
    report = server.validate_write(
        ctx, "ir.attachment" if model == "ir.attachment" else "account.move",
        "create", values=values,
    )
    assert not report["success"]
    assert not report["approval_status"]["stored"]
    assert not ctx.request_context.lifespan_context.write_approvals
    for field in ("move_type", "line_ids") if model == "ir.attachment" else LINK:
        assert any(
            issue["code"] == "readonly_field" and f"'{field}'" in issue["message"]
            for issue in report["issues"]
        )
    assert client.calls == []
