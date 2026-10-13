import asyncio
import json
import time

import httpx2
import pytest

from odoo_mcp import auth

RESOURCE = "https://mcp.example.com/mcp"
INTROSPECTION = "https://as.example.com/introspect"


def _verifier(handler, **kwargs):
    return auth.IntrospectionTokenVerifier(
        INTROSPECTION,
        resource_url=RESOURCE,
        transport=httpx2.MockTransport(handler),
        **kwargs,
    )


def _json_response(payload, status_code=200):
    return httpx2.Response(status_code, json=payload)


def test_verify_token_accepts_active_token_with_matching_audience():
    def handler(request):
        assert request.url == INTROSPECTION
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        assert body["token"] == "tok-1"
        return _json_response(
            {
                "active": True,
                "aud": RESOURCE,
                "scope": "odoo.read odoo.write",
                "client_id": "agent-1",
                "exp": int(time.time()) + 600,
            }
        )

    token = asyncio.run(_verifier(handler).verify_token("tok-1"))
    assert token is not None
    assert token.client_id == "agent-1"
    assert token.scopes == ["odoo.read", "odoo.write"]
    assert token.resource == RESOURCE
    assert token.subject is None
    assert token.claims is None


def test_verify_token_carries_subject_and_issuer_for_principal_binding():
    def handler(request):
        return _json_response(
            {
                "active": True,
                "aud": RESOURCE,
                "client_id": "shared-app",
                "sub": "user-1",
                "iss": "https://as.example.com/",
            }
        )

    token = asyncio.run(
        _verifier(handler, issuer_url="https://as.example.com").verify_token("tok")
    )
    assert token is not None
    assert token.client_id == "shared-app"
    assert token.subject == "user-1"
    assert token.claims == {"iss": "https://as.example.com/"}


def test_verify_token_rejects_inactive_expired_and_wrong_audience():
    cases = [
        {"active": False},
        {"active": True, "exp": int(time.time()) - 10},
        {"active": True, "aud": "https://other.example.com"},
    ]
    for payload in cases:
        token = asyncio.run(
            _verifier(lambda req, p=payload: _json_response(p)).verify_token("tok")
        )
        assert token is None, payload


def test_verify_token_rejects_http_errors_and_bad_json():
    assert (
        asyncio.run(
            _verifier(lambda req: httpx2.Response(500, text="boom")).verify_token("t")
        )
        is None
    )
    assert (
        asyncio.run(
            _verifier(lambda req: httpx2.Response(200, text="not-json")).verify_token(
                "t"
            )
        )
        is None
    )

    def raise_error(request):
        raise httpx2.ConnectError("refused")

    assert asyncio.run(_verifier(raise_error).verify_token("t")) is None


def test_verify_token_sends_client_credentials_when_configured():
    seen = {}

    def handler(request):
        seen["authorization"] = request.headers.get("authorization", "")
        return _json_response({"active": True, "sub": "user-7"})

    token = asyncio.run(
        _verifier(handler, client_id="mcp", client_secret="s3cret").verify_token("t")
    )
    assert token is not None
    assert seen["authorization"].startswith("Basic ")
    # No audience in the response → audience check is skipped (AS not RFC 8707-bound).
    assert token.client_id == "user-7"


def test_build_auth_returns_none_when_unconfigured(monkeypatch):
    for key in list(auth.os.environ):
        if key.startswith(auth.AUTH_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    assert auth.build_auth() is None
    assert auth.auth_posture()["enabled"] is False


def test_build_auth_rejects_partial_configuration(monkeypatch):
    monkeypatch.setenv("ODOO_MCP_AUTH_ISSUER_URL", "https://as.example.com")
    monkeypatch.delenv("ODOO_MCP_AUTH_INTROSPECTION_URL", raising=False)
    monkeypatch.delenv("ODOO_MCP_AUTH_RESOURCE_URL", raising=False)
    with pytest.raises(ValueError, match="Incomplete OAuth configuration"):
        auth.build_auth()


def test_build_auth_builds_settings_and_verifier(monkeypatch):
    monkeypatch.setenv("ODOO_MCP_AUTH_ISSUER_URL", "https://as.example.com")
    monkeypatch.setenv("ODOO_MCP_AUTH_INTROSPECTION_URL", INTROSPECTION)
    monkeypatch.setenv("ODOO_MCP_AUTH_RESOURCE_URL", RESOURCE)
    monkeypatch.setenv("ODOO_MCP_AUTH_REQUIRED_SCOPES", "odoo.read, odoo.write")
    monkeypatch.setenv("ODOO_MCP_AUTH_CLIENT_ID", "mcp")

    built = auth.build_auth()
    assert built is not None
    settings, verifier = built
    assert str(settings.issuer_url) == "https://as.example.com/"
    assert str(settings.resource_server_url) == RESOURCE
    assert settings.required_scopes == ["odoo.read", "odoo.write"]
    assert verifier.introspection_url == INTROSPECTION
    assert verifier.client_id == "mcp"

    posture = auth.auth_posture()
    assert posture["enabled"] is True
    assert posture["required_scopes"] == ["odoo.read", "odoo.write"]
    # posture is JSON-serializable and never contains the client secret
    assert "secret" not in json.dumps(posture).lower()


def test_runtime_security_report_exposes_oauth_posture(monkeypatch):
    import importlib

    server = importlib.import_module("odoo_mcp.server")
    for key in list(auth.os.environ):
        if key.startswith(auth.AUTH_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    assert server.runtime_security_report()["oauth"]["enabled"] is False


def test_configure_oauth_wires_fastmcp_only_for_http(monkeypatch, capsys):
    import argparse
    import importlib

    main_mod = importlib.import_module("odoo_mcp.__main__")
    server = importlib.import_module("odoo_mcp.server")
    monkeypatch.setenv("ODOO_MCP_AUTH_ISSUER_URL", "https://as.example.com")
    monkeypatch.setenv("ODOO_MCP_AUTH_INTROSPECTION_URL", INTROSPECTION)
    monkeypatch.setenv("ODOO_MCP_AUTH_RESOURCE_URL", RESOURCE)
    monkeypatch.setattr(server.mcp.settings, "auth", None, raising=False)
    monkeypatch.setattr(server.mcp, "_token_verifier", None, raising=False)

    # stdio → ignored with a warning
    main_mod.configure_oauth(argparse.Namespace(transport="stdio"))
    assert server.mcp.settings.auth is None
    assert "ignored" in capsys.readouterr().err

    # HTTP → wired
    main_mod.configure_oauth(argparse.Namespace(transport="streamable-http"))
    assert server.mcp.settings.auth is not None
    assert isinstance(server.mcp._token_verifier, auth.IntrospectionTokenVerifier)


ISSUER = "https://as.example.com"


def _active_payload(**extra):
    payload = {
        "active": True,
        "aud": RESOURCE,
        "client_id": "agent-1",
        "exp": int(time.time()) + 600,
    }
    payload.update(extra)
    return payload


def test_verify_token_rejects_issuer_mismatch():
    def handler(req):
        return _json_response(_active_payload(iss="https://evil.example.com"))

    token = asyncio.run(
        _verifier(handler, issuer_url=ISSUER).verify_token("tok-iss-bad")
    )
    assert token is None


def test_verify_token_accepts_matching_issuer_with_trailing_slash():
    def handler(req):
        return _json_response(_active_payload(iss=ISSUER + "/"))

    token = asyncio.run(
        _verifier(handler, issuer_url=ISSUER).verify_token("tok-iss-ok")
    )
    assert token is not None


def test_verify_token_missing_iss_passes_unless_required():
    handler = lambda req: _json_response(_active_payload())  # noqa: E731
    assert (
        asyncio.run(_verifier(handler, issuer_url=ISSUER).verify_token("tok-a"))
        is not None
    )
    assert (
        asyncio.run(
            _verifier(handler, issuer_url=ISSUER, require_iss=True).verify_token(
                "tok-b"
            )
        )
        is None
    )


def test_verify_token_missing_aud_rejected_only_with_require_aud():
    payload = _active_payload()
    del payload["aud"]
    handler = lambda req: _json_response(payload)  # noqa: E731
    assert asyncio.run(_verifier(handler).verify_token("tok-c")) is not None
    assert (
        asyncio.run(_verifier(handler, require_aud=True).verify_token("tok-d")) is None
    )


def test_verify_token_caches_positive_and_negative_verdicts():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _json_response(_active_payload())

    verifier = _verifier(handler, cache_ttl_seconds=60.0)
    assert asyncio.run(verifier.verify_token("tok-hot")) is not None
    assert asyncio.run(verifier.verify_token("tok-hot")) is not None
    assert calls["n"] == 1  # second call served from cache

    rejections = {"n": 0}

    def reject_handler(request):
        rejections["n"] += 1
        return _json_response({"active": False})

    neg = _verifier(reject_handler, cache_ttl_seconds=60.0)
    assert asyncio.run(neg.verify_token("tok-cold")) is None
    assert asyncio.run(neg.verify_token("tok-cold")) is None
    assert rejections["n"] == 1  # rejection cached too


def test_verify_token_cache_disabled_by_default():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return _json_response(_active_payload())

    verifier = _verifier(handler)
    asyncio.run(verifier.verify_token("tok-x"))
    asyncio.run(verifier.verify_token("tok-x"))
    assert calls["n"] == 2


def test_build_auth_passes_hardening_env(monkeypatch):
    monkeypatch.setenv("ODOO_MCP_AUTH_ISSUER_URL", ISSUER)
    monkeypatch.setenv("ODOO_MCP_AUTH_INTROSPECTION_URL", INTROSPECTION)
    monkeypatch.setenv("ODOO_MCP_AUTH_RESOURCE_URL", RESOURCE)
    monkeypatch.setenv("ODOO_MCP_AUTH_REQUIRE_AUD", "1")
    monkeypatch.setenv("ODOO_MCP_AUTH_REQUIRE_ISS", "true")
    monkeypatch.setenv("ODOO_MCP_AUTH_CACHE_TTL", "30")
    built = auth.build_auth()
    assert built is not None
    _, verifier = built
    assert verifier.issuer_url == ISSUER
    assert verifier.require_aud is True
    assert verifier.require_iss is True
    assert verifier._cache is not None
    posture = auth.auth_posture()
    assert posture["require_aud"] is True
    assert posture["require_iss"] is True
    assert posture["introspection_cache_ttl"] == 30.0
