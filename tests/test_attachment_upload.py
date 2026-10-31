"""Tests for `<field>_from_path` binary uploads on validate_write / execute_approved_write.

Covers the fingerprint-only approval contract: real file bytes must never
appear in a response, in the stored approval token payload, or in anything
the caller echoes back — only a `sha256:<hex>:<size>` fingerprint does.
"""

import base64
import hashlib
import importlib

from odoo_mcp import tools_write
from tests.test_batch_write import FakeCtx


class _Client:
    def get_model_fields(self, model):
        return {
            "name": {"type": "char", "readonly": False},
            "datas": {"type": "binary", "readonly": False},
            "res_model": {"type": "char", "readonly": True},
            "res_id": {"type": "many2one_reference", "readonly": True},
        }

    def __init__(self):
        self.calls = []

    def execute_method(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return 501


def _write_file(tmp_path, content=b"%PDF-1.4 not a real cv\n"):
    path = tmp_path / "resume.pdf"
    path.write_bytes(content)
    return path, content


def test_datas_from_path_resolves_to_fingerprint_not_raw_bytes(tmp_path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    path, content = _write_file(tmp_path)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(tmp_path))

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={
            "name": "resume.pdf",
            "datas_from_path": str(path),
            "res_model": "hr.applicant",
            "res_id": 192,
        },
    )

    assert report["success"] is True
    digest = hashlib.sha256(content).hexdigest()
    expected_fingerprint = f"sha256:{digest}:{len(content)}"
    assert report["approval"]["values"]["datas"] == expected_fingerprint
    assert "datas_from_path" not in report["approval"]["values"]
    # The real bytes must not leak into anything returned to the caller.
    serialized = str(report)
    assert base64.b64encode(content).decode("ascii") not in serialized


def test_execute_approved_write_substitutes_real_bytes_server_side(tmp_path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    path, content = _write_file(tmp_path)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(tmp_path))

    client = _Client()
    ctx = FakeCtx(client)
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={
            "name": "resume.pdf",
            "datas_from_path": str(path),
            "res_model": "hr.applicant",
            "res_id": 192,
        },
    )
    assert report["approval_status"]["stored"] is True

    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, report["approval"], confirm=True)

    assert result["success"] is True
    (args, _kwargs) = client.calls[0]
    executed_values = args[2]
    assert executed_values["datas"] == base64.b64encode(content).decode("ascii")
    assert executed_values["res_model"] == "hr.applicant"


def test_from_path_requires_configured_upload_roots(tmp_path):
    server = importlib.import_module("odoo_mcp.server")
    path, _content = _write_file(tmp_path)

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={"name": "resume.pdf", "datas_from_path": str(path)},
    )

    assert report["success"] is False
    assert "ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS" in report["error"]


def test_from_path_rejects_paths_outside_configured_roots(tmp_path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    path, _content = _write_file(outside_root)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(allowed_root))

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={"name": "resume.pdf", "datas_from_path": str(path)},
    )

    assert report["success"] is False
    assert "outside configured" in report["error"]


def test_from_path_rejects_oversized_files(tmp_path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    path, _content = _write_file(tmp_path, content=b"x" * 100)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(tmp_path))
    monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_UPLOAD_BYTES", "10")

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={"name": "resume.pdf", "datas_from_path": str(path)},
    )

    assert report["success"] is False
    assert "cap is 10" in report["error"]


def test_from_path_rejects_both_field_and_field_from_path(tmp_path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    path, _content = _write_file(tmp_path)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(tmp_path))

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={"datas": "already-base64", "datas_from_path": str(path)},
    )

    assert report["success"] is False
    assert "not both" in report["error"]


def test_from_path_requires_live_metadata(tmp_path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    path, _content = _write_file(tmp_path)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(tmp_path))

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={"name": "resume.pdf", "datas_from_path": str(path)},
        fields_metadata={"name": {"type": "char", "readonly": False}},
    )

    assert report["success"] is False
    assert "live" in report["error"]


def test_tampered_fingerprint_is_rejected_before_reaching_odoo(tmp_path, monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    path, content = _write_file(tmp_path)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(tmp_path))

    client = _Client()
    ctx = FakeCtx(client)
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={"name": "resume.pdf", "datas_from_path": str(path)},
    )
    tampered_approval = dict(report["approval"])
    tampered_approval["values"] = dict(tampered_approval["values"])
    tampered_approval["values"]["datas"] = "sha256:deadbeef:9999"

    monkeypatch.setenv("ODOO_MCP_ENABLE_WRITES", "1")
    result = server.execute_approved_write(ctx, tampered_approval, confirm=True)

    assert result["success"] is False
    assert client.calls == []


def test_from_path_rejects_symlink_escape_within_upload_root(tmp_path, monkeypatch):
    """A symlink placed inside the allowed root but pointing outside it must
    resolve to its real (outside) target and be rejected — the root check
    has to run on the fully-resolved path, not the raw name."""
    server = importlib.import_module("odoo_mcp.server")
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret, _content = _write_file(outside, content=b"outside-root-secret")
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(upload_root))

    escaping_link = upload_root / "resume.pdf"
    escaping_link.symlink_to(secret)

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={"name": "resume.pdf", "datas_from_path": str(escaping_link)},
    )

    assert report["success"] is False
    assert "outside configured" in report["error"]


def test_from_path_rejects_file_swapped_for_symlink_after_root_check(
    tmp_path, monkeypatch
):
    """Regression test for the TOCTOU window between the root-containment
    check and the actual file read: if the checked path is swapped for a
    symlink pointing outside the upload root right before the read, the read
    must fail closed (no O_NOFOLLOW-bypass) instead of silently following the
    symlink and leaking the secret target's bytes."""
    server = importlib.import_module("odoo_mcp.server")
    upload_root = tmp_path / "allowed"
    upload_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret, secret_content = _write_file(outside, content=b"top-secret-content")
    target, _content = _write_file(upload_root)
    monkeypatch.setenv("ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS", str(upload_root))

    real_restrict = tools_write.restrict_attachment_upload_path

    def swap_after_check(raw_path):
        resolved = real_restrict(raw_path)
        # Simulate an attacker winning the race right after the root check
        # passes: replace the validated file with a symlink escaping the root.
        resolved.unlink()
        resolved.symlink_to(secret)
        return resolved

    monkeypatch.setattr(tools_write, "restrict_attachment_upload_path", swap_after_check)

    ctx = FakeCtx(_Client())
    report = server.validate_write(
        ctx,
        "ir.attachment",
        "create",
        values={
            "name": "resume.pdf",
            "datas_from_path": str(target),
            "res_model": "hr.applicant",
            "res_id": 192,
        },
    )

    assert report["success"] is False
    serialized = str(report)
    assert secret_content.decode() not in serialized
    assert base64.b64encode(secret_content).decode("ascii") not in serialized


def test_max_attachment_upload_bytes_clamped(monkeypatch):
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_UPLOAD_BYTES", "999999999999")
    assert server.max_attachment_upload_bytes() == server.ATTACHMENT_BYTES_HARD_CAP
    monkeypatch.setenv("ODOO_MCP_MAX_ATTACHMENT_UPLOAD_BYTES", "junk")
    assert (
        server.max_attachment_upload_bytes() == server.DEFAULT_MAX_ATTACHMENT_UPLOAD_BYTES
    )
