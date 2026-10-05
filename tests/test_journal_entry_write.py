"""Journal-entry creation through the existing, trusted-metadata write gate."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from odoo_mcp import agent_tools, server

# Odoo 16 fields_get shape, including UI-readonly fields and no `states`
# (OdooClient intentionally requests only its portable metadata attributes).
MOVE_FIELDS = {
    "move_type": {"type": "selection", "readonly": True},
    "state": {"type": "selection", "readonly": True},
    "name": {"type": "char", "readonly": True},
    "journal_id": {"type": "many2one", "readonly": False},
    "date": {"type": "date", "readonly": False},
    "line_ids": {
        "type": "one2many",
        "relation": "account.move.line",
        "readonly": True,
    },
}
LINE_FIELDS = {
    "name": {"type": "char", "readonly": False},
    "account_id": {"type": "many2one", "readonly": False},
    "partner_id": {"type": "many2one", "readonly": False},
    "debit": {"type": "monetary", "readonly": False},
    "credit": {"type": "monetary", "readonly": False},
    "amount_residual": {"type": "monetary", "readonly": True},
    "move_id": {"type": "many2one", "readonly": False},
    "tax_ids": {"type": "many2many", "readonly": False},
}


@pytest.fixture
def values():
    return {
        "move_type": "entry",
        "journal_id": 7,
        "date": "2026-01-15",
        "line_ids": [
            [0, 0, {"name": "Tax item A", "account_id": 11, "debit": 10.0}],
            [0, 0, {"name": "Tax item B", "account_id": 11, "debit": 20.0}],
            [0, 0, {"name": "Counterpart", "account_id": 12, "credit": 30.0}],
        ],
    }


class RecordingClient:
    def __init__(self):
        self.metadata_calls = []
        self.calls = []
        self.move_fields = deepcopy(MOVE_FIELDS)
        self.line_fields = deepcopy(LINE_FIELDS)

    def get_model_fields(self, model):
        self.metadata_calls.append(model)
        return self.line_fields if model == "account.move.line" else self.move_fields

    def execute_method(self, model, method, *args, **kwargs):
        self.calls.append((model, method, args, kwargs))
        return 101


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


def validate(values, **kwargs):
    return agent_tools.validate_write_report(
        model=kwargs.pop("model", "account.move"),
        operation=kwargs.pop("operation", "create"),
        values=values,
        record_ids=kwargs.pop("record_ids", None),
        fields_metadata=kwargs.pop("fields_metadata", MOVE_FIELDS),
        line_fields_metadata=kwargs.pop("line_fields_metadata", LINE_FIELDS),
        **kwargs,
    )


@pytest.mark.parametrize("explicit_type", [True, False])
@pytest.mark.parametrize("batch", [True, False])
def test_journal_entry_preview_validate_execute_preserves_each_line(
    monkeypatch,
    values,
    explicit_type,
    batch,
):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    if not explicit_type:
        values.pop("move_type")
    payload = (
        {"values_list": [values, deepcopy(values)]} if batch else {"values": values}
    )
    original = deepcopy(payload)
    client = RecordingClient()
    ctx = make_context(client)
    preview = server.preview_write("account.move", "create", **payload)
    validation = server.validate_write(ctx, "account.move", "create", **payload)

    assert preview["success"] is True
    assert validation["success"] is True, validation
    assert validation["approval_status"]["stored"] is True
    assert validation["approval"]["token"] == preview["approval"]["token"]
    assert payload == original
    assert client.metadata_calls == ["account.move", "account.move.line"]
    assert client.calls == []

    result = server.execute_approved_write(ctx, validation["approval"], confirm=True)
    assert result["success"] is True
    expected = original["values_list"] if batch else original["values"]
    # One atomic call; two distinct commands with the same account survive.
    assert client.calls == [("account.move", "create", (expected,), {})]
    assert not ctx.request_context.lifespan_context.write_approvals


@pytest.mark.parametrize(
    "changes",
    [
        {"move_type": "in_invoice"},
        {"state": "posted"},
        {"name": "CUSTOM/1"},
    ],
)
def test_other_readonly_fields_and_move_types_remain_blocked(values, changes):
    values.update(changes)
    report = validate(values)
    assert report["success"] is False
    assert "readonly_field" in {issue["code"] for issue in report["issues"]}


@pytest.mark.parametrize(
    "model,operation",
    [
        ("account.move", "write"),
        ("custom.move", "create"),
    ],
)
def test_exception_does_not_apply_to_updates_or_other_models(values, model, operation):
    report = validate(values, model=model, operation=operation, record_ids=[101])
    assert report["success"] is False
    assert any("line_ids" in issue["message"] for issue in report["issues"])


@pytest.mark.parametrize(
    "context",
    [
        {"default_move_type": "in_invoice"},
        {"default_state": "posted"},
        {"check_move_validity": False},
        {"check_move_validity": 0},
        {"check_move_validity": "false"},
        {"default_line_ids": []},
    ],
)
def test_context_cannot_switch_contract_or_disable_balance_check(values, context):
    values.pop("move_type")
    assert validate(values, context=context)["success"] is False


def test_explicit_entry_type_overrides_context_default(values):
    report = validate(values, context={"default_move_type": "in_invoice"})
    assert report["success"] is True


@pytest.mark.parametrize("readonly", [True, False])
def test_line_validation_is_required_even_when_parent_is_ui_writable(values, readonly):
    metadata = deepcopy(MOVE_FIELDS)
    metadata["line_ids"]["readonly"] = readonly
    metadata["move_type"]["readonly"] = readonly
    assert validate(values, fields_metadata=metadata)["success"] is True
    values["line_ids"][0][2]["amount_residual"] = 10
    report = validate(values, fields_metadata=metadata)
    assert report["success"] is False
    assert any(
        issue["code"] == "readonly_field" and "line_ids[0]" in issue["message"]
        for issue in report["issues"]
    )


def test_explicit_draft_does_not_override_readonly_state_validation(values):
    values["state"] = "draft"
    report = validate(values, context={"default_state": "posted"})
    assert report["success"] is False
    assert any("'state' is readonly" in issue["message"] for issue in report["issues"])


def test_mixed_batch_cannot_authorize_invoice_via_entry_exception(values):
    invoice = deepcopy(values)
    invoice["move_type"] = "in_invoice"
    report = validate(None, values_list=[values, invoice])
    assert report["success"] is False
    assert any(
        issue["code"] == "readonly_field" and "values_list[1]" in issue["message"]
        for issue in report["issues"]
    )


@pytest.mark.parametrize(
    "commands",
    [
        [],
        False,
        {"debit": 10},
        [[0, 0]],
        [[0, 0, {}]],
        [[0, 0, "not values"]],
        [[False, 0, {"debit": 1}]],
        [[0, False, {"debit": 1}]],
        [[0, 1, {"debit": 1}]],
        [[1, 123, {"debit": 1}]],
        [[2, 123, 0]],
        [[3, 123, 0]],
        [[4, 123, 0]],
        [[5, 0, 0]],
        [[6, 0, [123]]],
    ],
)
def test_only_new_line_commands_are_accepted(values, commands):
    values["line_ids"] = commands
    report = validate(values)
    assert report["success"] is False
    assert "invalid_journal_line_command" in {i["code"] for i in report["issues"]}


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("unknown_field", 1, "unknown_field"),
        ("amount_residual", 10, "readonly_field"),
        ("move_id", 123, "unsupported_journal_line_field"),
        ("tax_ids", [[6, 0, [1]]], "unsupported_journal_line_field"),
    ],
)
def test_nested_line_fields_are_validated(values, field, value, code):
    values["line_ids"][1][2][field] = value
    report = validate(values)
    assert report["success"] is False
    assert any(
        i["code"] == code and "line_ids[1]" in i["message"] for i in report["issues"]
    )


def test_batch_checks_every_journal_entry_and_reports_line_path(values):
    bad_values = deepcopy(values)
    bad_values["line_ids"][1][2]["unknown_field"] = True
    report = validate(None, values_list=[values, bad_values])
    assert report["success"] is False
    assert any("values_list[1]: line_ids[1]" in i["message"] for i in report["issues"])


@pytest.mark.parametrize("metadata", [None, {}, {"error": "access denied"}])
def test_missing_live_child_metadata_never_stores_approval(values, metadata):
    client = RecordingClient()
    client.line_fields = metadata
    ctx = make_context(client)
    report = server.validate_write(ctx, "account.move", "create", values=values)
    assert report["success"] is False
    assert report["approval_status"]["stored"] is False
    assert not ctx.request_context.lifespan_context.write_approvals
    assert client.calls == []


@pytest.mark.parametrize(
    "line_meta", [None, {}, {"type": "one2many", "relation": "custom.line"}]
)
def test_parent_relation_must_match_reviewed_contract(values, line_meta):
    metadata = deepcopy(MOVE_FIELDS)
    metadata["line_ids"] = line_meta
    assert validate(values, fields_metadata=metadata)["success"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"fields_metadata": MOVE_FIELDS},
        {"use_live_metadata": False},
    ],
)
def test_input_or_missing_metadata_cannot_authorize_execution(values, options):
    client = RecordingClient()
    ctx = make_context(client)
    report = server.validate_write(
        ctx, "account.move", "create", values=values, **options
    )
    assert report["approval_status"]["stored"] is False
    assert client.metadata_calls == []
    preview = server.preview_write("account.move", "create", values=values)
    result = server.execute_approved_write(ctx, preview["approval"], confirm=True)
    assert result["success"] is False
    assert client.calls == []


def test_child_metadata_is_fetched_from_selected_instance(monkeypatch, values):
    monkeypatch.setattr(
        server, "resolve_instance_name", lambda instance: instance or "default"
    )
    default = RecordingClient()
    selected = RecordingClient()
    selected.line_fields = {"error": "selected instance denies line metadata"}
    ctx = make_context(default)
    ctx.request_context.lifespan_context.get_client = lambda instance: (
        instance,
        selected,
    )
    result = server.validate_write(
        ctx, "account.move", "create", values=values, instance="other"
    )
    assert result["success"] is False
    assert result["approval_status"]["stored"] is False
    assert default.metadata_calls == []
    assert selected.metadata_calls == ["account.move", "account.move.line"]


@pytest.mark.parametrize("gate", ["confirm", "writes", "tamper", "expired"])
def test_existing_execution_gates_are_preserved(monkeypatch, values, gate):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "0" if gate == "writes" else "1")
    client = RecordingClient()
    ctx = make_context(client)
    report = server.validate_write(ctx, "account.move", "create", values=values)
    assert report["approval_status"]["stored"] is True
    approval = deepcopy(report["approval"])
    if gate == "tamper":
        approval["values"]["line_ids"][0][2]["debit"] = 999
    elif gate == "expired":
        ctx.request_context.lifespan_context.write_approvals[approval["token"]][
            "expires_at"
        ] = 0
    result = server.execute_approved_write(ctx, approval, confirm=gate != "confirm")
    assert result["success"] is False
    assert client.calls == []


def test_odoo_accounting_errors_are_not_reported_as_success(monkeypatch, values):
    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    client = RecordingClient()
    ctx = make_context(client)

    def reject_unbalanced(*args, **kwargs):
        raise ValueError("The move is not balanced.")

    client.execute_method = reject_unbalanced
    values["line_ids"][0][2]["debit"] = 100
    report = server.validate_write(ctx, "account.move", "create", values=values)
    assert report["approval_status"]["stored"] is True
    result = server.execute_approved_write(ctx, report["approval"], confirm=True)
    assert result["success"] is False
    assert "not balanced" in result["error"]
