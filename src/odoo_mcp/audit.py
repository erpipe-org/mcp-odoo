"""Append-only JSONL audit trail for write-path events.

Opt-in via ODOO_MCP_AUDIT_LOG=<path>. One line per event so operators can
answer "what did the agent change, when, and was it approved?" without
trusting chat transcripts. Tokens are stored as truncated SHA-256 digests,
never in clear text. On authenticated HTTP transports each line also names
the OAuth principal (client id, issuer, subject) that made the call; the MCP
session id is digested like tokens.

Audit failures never block the underlying operation (fail-open); they are
logged as warnings instead. Operators who need fail-closed semantics should
alert on the warning log line.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_LOG_ENV = "ODOO_MCP_AUDIT_LOG"
_write_lock = threading.Lock()


def audit_log_path() -> str | None:
    """Return the configured audit log path, or None when disabled."""
    path = os.environ.get(AUDIT_LOG_ENV, "").strip()
    return path or None


def audit_posture() -> dict[str, Any]:
    """Non-secret audit posture for health_check / runtime_security_report."""
    path = audit_log_path()
    return {"enabled": path is not None, "path": path, "env": AUDIT_LOG_ENV}


def _token_digest(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _principal_entry(principal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not principal:
        return None
    return {
        "client_id": principal.get("client_id"),
        "issuer": principal.get("issuer"),
        "subject": principal.get("subject"),
        "session_sha256": _token_digest(principal.get("session_id")),
    }


def record_write_event(
    event: str,
    *,
    outcome: str,
    model: str | None = None,
    operation: str | None = None,
    record_ids: list[int] | None = None,
    instance: str | None = None,
    token: str | None = None,
    detail: str | None = None,
    principal: dict[str, Any] | None = None,
) -> bool:
    """Append one audit line; returns True when a line was written."""
    path = audit_log_path()
    if path is None:
        return False
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "outcome": outcome,
        "model": model,
        "operation": operation,
        "record_ids": list(record_ids or []),
        "instance": instance or "default",
        "token_sha256": _token_digest(token),
        "detail": detail,
        "principal": _principal_entry(principal),
    }
    try:
        line = json.dumps(entry, sort_keys=True, default=str)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True
    except OSError as exc:
        logger.warning("audit log write failed (%s): %s", path, exc)
        return False
