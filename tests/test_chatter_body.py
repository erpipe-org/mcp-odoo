"""chatter_post sends plain text as escaped HTML that keeps line breaks.

Odoo 17+ escapes a plain-str message_post body and wraps it in one <p>, so
newlines were lost. The server escapes the caller's text, maps line breaks to
<br/>, and passes body_is_html=True where Odoo supports it.
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
    def __init__(self, major=None):
        self.major = major
        self.calls = []

    def get_server_version(self):
        if self.major is None:
            return {"error": "unavailable"}
        return {"server_version_info": [self.major, 0, 0, "final", 0, ""]}

    def execute_method(self, model, method, *args, **kwargs):
        if model == "ir.module.module":
            return []
        self.calls.append((model, method, args, kwargs))
        return 4242


def _server():
    return importlib.import_module("odoo_mcp.server")


def _post_direct(monkeypatch, client, body, **extra):
    monkeypatch.setenv("MCP_CHATTER_DIRECT", "1")
    result = _server().chatter_post(
        FakeCtx(client), model="crm.lead", record_id=12, body=body, **extra
    )
    assert result["success"] is True
    assert len(client.calls) == 1
    return client.calls[0][3]


def test_line_breaks_are_preserved(monkeypatch):
    kwargs = _post_direct(
        monkeypatch,
        ChatterClient(19),
        "Línea 1\nLínea 2\r\nLínea 3\rFin",
        subtype_xmlid="mail.mt_note",
    )

    assert kwargs["body"] == "Línea 1<br/>Línea 2<br/>Línea 3<br/>Fin"
    assert kwargs["body_is_html"] is True
    assert kwargs["subtype_xmlid"] == "mail.mt_note"


def test_script_link_and_br_in_text_stay_escaped(monkeypatch):
    kwargs = _post_direct(
        monkeypatch,
        ChatterClient(19),
        "<script>alert(1)</script>\n"
        '<a href="https://evil.example">click</a>\n'
        "<br>literal & 'quoted'",
    )

    body = kwargs["body"]
    assert body == (
        "&lt;script&gt;alert(1)&lt;/script&gt;<br/>"
        "&lt;a href=&quot;https://evil.example&quot;&gt;click&lt;/a&gt;<br/>"
        "&lt;br&gt;literal &amp; &#x27;quoted&#x27;"
    )
    # The only markup left is the server-generated line breaks.
    assert "<" not in body.replace("<br/>", "")


def test_odoo_16_gets_html_string_without_body_is_html(monkeypatch):
    kwargs = _post_direct(monkeypatch, ChatterClient(16), "a\nb")

    assert kwargs["body"] == "a<br/>b"
    assert "body_is_html" not in kwargs


def test_unknown_version_still_sends_body_is_html(monkeypatch):
    kwargs = _post_direct(monkeypatch, ChatterClient(None), "a\nb")

    assert kwargs["body"] == "a<br/>b"
    assert kwargs["body_is_html"] is True


def test_gated_approval_keeps_plain_text_and_posts_escaped_html(monkeypatch):
    server = _server()
    monkeypatch.delenv("MCP_CHATTER_DIRECT", raising=False)
    client = ChatterClient(17)
    ctx = FakeCtx(client)

    preview = server.chatter_post(ctx, model="res.partner", record_id=7, body="a\n<b>")
    assert preview["approval"]["kwargs"]["body"] == "a\n<b>"
    assert client.calls == []

    approved = server.chatter_post(
        ctx,
        model="res.partner",
        record_id=7,
        body="a\n<b>",
        approval=preview["approval"],
        confirm=True,
    )

    assert approved["success"] is True
    kwargs = client.calls[0][3]
    assert kwargs["body"] == "a<br/>&lt;b&gt;"
    assert kwargs["body_is_html"] is True
