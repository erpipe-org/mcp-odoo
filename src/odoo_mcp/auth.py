"""OAuth 2.1 resource-server support for the Streamable HTTP transport.

Opt-in: set the ODOO_MCP_AUTH_* environment variables and start an HTTP
transport. The server then advertises RFC 9728 protected-resource metadata
and rejects requests without a valid bearer token. Tokens are validated
against the authorization server's RFC 7662 introspection endpoint —
works with Keycloak, Auth0, Authentik, and any AS that supports
introspection. stdio transport is unaffected.

Env vars:
- ODOO_MCP_AUTH_ISSUER_URL          authorization server issuer (required)
- ODOO_MCP_AUTH_INTROSPECTION_URL   RFC 7662 endpoint (required)
- ODOO_MCP_AUTH_RESOURCE_URL        canonical URL of this MCP server (required)
- ODOO_MCP_AUTH_REQUIRED_SCOPES     comma-separated scopes (optional)
- ODOO_MCP_AUTH_CLIENT_ID/SECRET    client credentials for the
                                    introspection call (optional; many AS
                                    require them)
- ODOO_MCP_AUTH_REQUIRE_AUD=1       reject tokens without an `aud` claim
                                    (default: aud is only checked when present)
- ODOO_MCP_AUTH_REQUIRE_ISS=1       reject introspection responses without an
                                    `iss` claim (a present `iss` must always
                                    match the configured issuer)
- ODOO_MCP_AUTH_CACHE_TTL           seconds to cache introspection verdicts
                                    (default 60; 0 disables; a revoked token
                                    stays accepted for at most this long)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

import httpx2
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from .schema_cache import BoundedTTLCache

logger = logging.getLogger(__name__)

AUTH_ENV_PREFIX = "ODOO_MCP_AUTH_"


def _env(name: str) -> str | None:
    value = os.environ.get(f"{AUTH_ENV_PREFIX}{name}", "").strip()
    return value or None


def auth_required_scopes() -> list[str]:
    raw = _env("REQUIRED_SCOPES") or ""
    return [scope.strip() for scope in raw.split(",") if scope.strip()]


def auth_configured() -> bool:
    """True when the three mandatory auth env vars are all present."""
    return all(
        _env(name) for name in ("ISSUER_URL", "INTROSPECTION_URL", "RESOURCE_URL")
    )


def _env_flag(name: str) -> bool:
    return (_env(name) or "").lower() in {"1", "true", "yes", "on"}


def _cache_ttl() -> float:
    raw = _env("CACHE_TTL")
    if raw is None:
        return 60.0
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 60.0


def auth_posture() -> dict[str, Any]:
    """Non-secret auth posture for health_check / runtime_security_report."""
    return {
        "enabled": auth_configured(),
        "issuer_url": _env("ISSUER_URL"),
        "resource_url": _env("RESOURCE_URL"),
        "required_scopes": auth_required_scopes(),
        "introspection_configured": _env("INTROSPECTION_URL") is not None,
        "require_aud": _env_flag("REQUIRE_AUD"),
        "require_iss": _env_flag("REQUIRE_ISS"),
        "introspection_cache_ttl": _cache_ttl(),
    }


class IntrospectionTokenVerifier(TokenVerifier):
    """Validate bearer tokens via RFC 7662 token introspection."""

    def __init__(
        self,
        introspection_url: str,
        *,
        resource_url: str,
        issuer_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        require_aud: bool = False,
        require_iss: bool = False,
        cache_ttl_seconds: float = 0.0,
        timeout_seconds: float = 10.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.introspection_url = introspection_url
        self.resource_url = resource_url
        self.issuer_url = issuer_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.require_aud = require_aud
        self.require_iss = require_iss
        self.timeout_seconds = timeout_seconds
        self._transport = transport  # test seam (httpx2.MockTransport)
        # Cache introspection verdicts so hot agent loops don't hammer the
        # AS on every MCP request. Verdicts (including rejections) live at
        # most cache_ttl_seconds — that is also the revocation lag bound.
        self._cache: BoundedTTLCache | None = (
            BoundedTTLCache(max_entries=1024, ttl_seconds=cache_ttl_seconds)
            if cache_ttl_seconds > 0
            else None
        )

    @staticmethod
    def _cache_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def verify_token(self, token: str) -> AccessToken | None:
        cache_key = self._cache_key(token)
        if self._cache is not None:
            try:
                cached = self._cache[cache_key]
            except KeyError:
                pass
            else:
                return cached if isinstance(cached, AccessToken) else None
        result = await self._verify_token_uncached(token)
        if self._cache is not None:
            self._cache[cache_key] = result if result is not None else "rejected"
        return result

    async def _verify_token_uncached(self, token: str) -> AccessToken | None:
        auth: httpx2.BasicAuth | None = None
        if self.client_id is not None:
            auth = httpx2.BasicAuth(self.client_id, self.client_secret or "")
        try:
            async with httpx2.AsyncClient(
                timeout=self.timeout_seconds, transport=self._transport, auth=auth
            ) as client:
                response = await client.post(
                    self.introspection_url,
                    data={"token": token},
                    headers={"Accept": "application/json"},
                )
        except httpx2.HTTPError as exc:
            logger.warning("token introspection failed: %s", exc)
            return None
        if response.status_code != 200:
            logger.warning("token introspection returned HTTP %s", response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.warning("token introspection returned invalid JSON")
            return None
        if not isinstance(payload, dict) or not payload.get("active"):
            return None

        expires_at = payload.get("exp")
        if isinstance(expires_at, (int, float)) and expires_at <= time.time():
            return None

        # Issuer binding (SEP hardening, RFC 9207 spirit): an `iss` returned
        # by introspection must match the issuer this server trusts, so a
        # token minted by a different AS behind the same introspection proxy
        # cannot be replayed here (mix-up attack).
        issuer = payload.get("iss")
        if issuer is None:
            if self.require_iss:
                logger.warning("token introspection returned no iss; rejecting")
                return None
        elif self.issuer_url is not None and str(issuer).rstrip("/") != (
            self.issuer_url.rstrip("/")
        ):
            logger.warning("token issuer %s does not match configured issuer", issuer)
            return None

        # RFC 8707 audience check: when the AS binds tokens to a resource,
        # require it to match this server's canonical URL.
        audience = payload.get("aud")
        if audience is None:
            if self.require_aud:
                logger.warning("token has no aud claim; rejecting (REQUIRE_AUD)")
                return None
        else:
            audiences = audience if isinstance(audience, list) else [audience]
            if self.resource_url not in [str(entry) for entry in audiences]:
                logger.warning("token audience %s does not match resource", audiences)
                return None

        scope_raw = payload.get("scope", "")
        scopes = scope_raw.split() if isinstance(scope_raw, str) else []
        subject = payload.get("sub")
        # `subject` and `iss` make (client_id, issuer, subject) the principal
        # (mcp.server.auth.provider.principal_components), so two users of one
        # OAuth client never share write approvals (server_core.current_principal).
        return AccessToken(
            token=token,
            client_id=str(payload.get("client_id") or subject or "unknown"),
            scopes=scopes,
            expires_at=(
                int(expires_at) if isinstance(expires_at, (int, float)) else None
            ),
            resource=self.resource_url,
            subject=str(subject) if subject is not None else None,
            claims={"iss": str(issuer)} if issuer is not None else None,
        )


def build_auth() -> tuple[AuthSettings, IntrospectionTokenVerifier] | None:
    """Build (AuthSettings, verifier) from env, or None when not configured."""
    if not auth_configured():
        partial = [
            name
            for name in ("ISSUER_URL", "INTROSPECTION_URL", "RESOURCE_URL")
            if _env(name)
        ]
        if partial:
            raise ValueError(
                "Incomplete OAuth configuration: set ODOO_MCP_AUTH_ISSUER_URL, "
                "ODOO_MCP_AUTH_INTROSPECTION_URL, and ODOO_MCP_AUTH_RESOURCE_URL "
                f"together (found only {', '.join(partial)})."
            )
        return None
    issuer = _env("ISSUER_URL")
    introspection = _env("INTROSPECTION_URL")
    resource = _env("RESOURCE_URL")
    assert issuer and introspection and resource
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(resource),
        required_scopes=auth_required_scopes() or None,
    )
    verifier = IntrospectionTokenVerifier(
        introspection,
        resource_url=resource,
        issuer_url=issuer,
        client_id=_env("CLIENT_ID"),
        client_secret=_env("CLIENT_SECRET"),
        require_aud=_env_flag("REQUIRE_AUD"),
        require_iss=_env_flag("REQUIRE_ISS"),
        cache_ttl_seconds=_cache_ttl(),
    )
    return settings, verifier
