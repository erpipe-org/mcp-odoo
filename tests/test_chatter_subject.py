"""chatter_post accepts an optional one-line, plain-text subject.

Without a subject Odoo titles the notification email with the record name.
The subject is part of the canonical payload, so the approval token covers it.
"""

import importlib


class FakeRequest:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeLife:
    def __init__(self, odoo):
        self.odoo = odoo
        self.schema_cache = {}
        self.write_approvals = {}

    def get_client(self, instance=None):
        return "default", self.odoo


class FakeCtx:
    def __init__(self, odoo):
        self.request_context = FakeRequest(FakeLife(odoo))


class ChatterClient:
    def __init__(self):
        self.calls = []

    def execute_method(self, model, method, *args, **kwargs):
        self.calls.append((model, method, args, kwargs))
        return 4242


def _server():
    return importlib.import_module("odoo_mcp.server")


def _preview(client, **arguments):
    return _server().chatter_post(
        FakeCtx(client), model="account.move", record_id=36, body="Hola", **arguments
    )


def test_without_subject_payload_and_call_have_no_subject(monkeypatch):
    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")
    client = ChatterClient()

    result = _preview(client)

    assert result["success"] is True
    assert "subject" not in client.calls[0][3]


def test_subject_is_passed_to_message_post_with_recipients(monkeypatch):
    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")
    client = ChatterClient()

    result = _preview(
        client,
        subject="Factura INV/2026/00036",
        subtype_xmlid="mail.mt_comment",
        partner_ids=[15],
    )

    assert result["success"] is True
    model, method, args, kwargs = client.calls[0]
    assert (model, method, args) == ("account.move", "message_post", ([36],))
    assert kwargs["subject"] == "Factura INV/2026/00036"
    assert kwargs["message_type"] == "comment"
    assert kwargs["subtype_xmlid"] == "mail.mt_comment"
    assert kwargs["partner_ids"] == [15]


def test_subject_is_flattened_to_one_line_and_kept_as_plain_text(monkeypatch):
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)

    preview = _preview(
        ChatterClient(),
        subject="  Factura\r\nBcc: x@evil.example\t<b>A & B</b> fin\x00 ",
    )

    subject = preview["approval"]["kwargs"]["subject"]
    assert subject == "Factura Bcc: x@evil.example <b>A & B</b> fin"
    assert "\n" not in subject and "\r" not in subject
    # Char field: Odoo escapes it at render time; no HTML entities added here.
    assert "&amp;" not in subject


def test_blank_subject_is_treated_as_absent(monkeypatch):
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)

    with_blank = _preview(ChatterClient(), subject=" \n\t ")
    without = _preview(ChatterClient())

    assert "subject" not in with_blank["approval"]["kwargs"]
    assert with_blank["approval"]["token"] == without["approval"]["token"]


def test_subject_over_the_limit_is_rejected(monkeypatch):
    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")
    client = ChatterClient()
    server = _server()

    at_limit = _preview(client, subject="x" * server.tools_write.MAX_CHATTER_SUBJECT_LENGTH)
    too_long = _preview(
        client, subject="x" * (server.tools_write.MAX_CHATTER_SUBJECT_LENGTH + 1)
    )

    assert at_limit["success"] is True
    assert too_long["success"] is False
    assert "at most 200 characters" in too_long["error"]
    assert len(client.calls) == 1


def test_token_covers_subject(monkeypatch):
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = ChatterClient()
    server = _server()
    ctx = FakeCtx(client)

    preview = server.chatter_post(
        ctx, model="account.move", record_id=36, body="Hola", subject="Asunto A"
    )
    no_subject = server.chatter_post(ctx, model="account.move", record_id=36, body="Hola")
    assert preview["approval"]["token"] != no_subject["approval"]["token"]

    changed = server.chatter_post(
        ctx,
        model="account.move",
        record_id=36,
        body="Hola",
        subject="Asunto B",
        approval=preview["approval"],
        confirm=True,
    )
    dropped = server.chatter_post(
        ctx,
        model="account.move",
        record_id=36,
        body="Hola",
        approval=preview["approval"],
        confirm=True,
    )
    assert changed["success"] is False
    assert "does not match" in changed["error"]
    assert dropped["success"] is False
    assert client.calls == []

    approved = server.chatter_post(
        ctx,
        model="account.move",
        record_id=36,
        body="Hola",
        subject="Asunto A",
        approval=preview["approval"],
        confirm=True,
    )
    assert approved["success"] is True
    assert client.calls[0][3]["subject"] == "Asunto A"
