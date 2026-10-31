#!/usr/bin/env python3
"""Verify attachment writes and another user's downloads in disposable Odoo stacks."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from odoo_mcp.odoo_client import OdooClient
from odoo_compose_smoke import (
    ADMIN_LOGIN,
    ADMIN_PASSWORD,
    DB_PASSWORD,
    DB_USER,
    DEFAULT_VERSIONS,
    VersionTarget,
    compose_cmd,
    compose_env,
    decode_tool_json,
    generate_json2_api_key,
    mcp_env,
    run,
    wait_for_http,
    wait_for_xmlrpc,
)


def singleton(value: Any) -> int:
    return int(value[0] if isinstance(value, list) else value)


async def attachment_smoke(
    target: VersionTarget, *, transport: str, api_key: str | None = None
) -> dict[str, Any]:
    url = f"http://127.0.0.1:{target.port}"
    admin = OdooClient(url, target.database, ADMIN_LOGIN, ADMIN_PASSWORD)
    # Fixtures use XML-RPC on the disposable database, even for the JSON-2 run.
    group = admin.execute_method(
        "ir.model.data", "search_read",
        [["module", "=", "base"], ["name", "=", "group_user"]],
        fields=["res_id"],
    )[0]["res_id"]
    login = f"attachment-reader-{transport}@example.test"
    password = uuid4().hex
    user_fields = admin.get_model_fields("res.users")
    group_field = "group_ids" if "group_ids" in user_fields else "groups_id"
    reader_id = singleton(admin.execute_method("res.users", "create", {
        "name": "Attachment reader", "login": login, "password": password,
        group_field: [[6, 0, [group]]],
    }))
    browser = requests.Session()
    auth = browser.post(url + "/web/session/authenticate", json={
        "jsonrpc": "2.0", "method": "call", "id": 1,
        "params": {"db": target.database, "login": login, "password": password},
    }, timeout=30)
    auth.raise_for_status()
    assert auth.json().get("result", {}).get("uid") == reader_id, auth.text

    content = b"%PDF-1.4\n% attachment transport regression fixture\n%%EOF\n"
    with TemporaryDirectory(prefix="odoo-attachment-smoke-") as directory:
        source = Path(directory) / "offer.pdf"
        source.write_bytes(content)
        env = mcp_env(target, transport=transport, api_key=api_key)
        env.update({
            "ODOO_MCP_ENABLE_WRITES": "1",
            "ODOO_MCP_ELICIT_WRITES": "0",
            "ODOO_MCP_ATTACHMENT_UPLOAD_ROOTS": directory,
        })
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "odoo_mcp"], env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    return decode_tool_json(
                        await session.call_tool(tool, arguments=arguments), tool
                    )

                async def execute(arguments: dict[str, Any]) -> Any:
                    preview = await call("preview_write", arguments)
                    assert preview.get("success"), preview
                    report = await call("validate_write", arguments)
                    assert report.get("success"), report
                    assert report["approval_status"]["stored"], report
                    result = await call("execute_approved_write", {
                        "approval": report["approval"], "confirm": True,
                    })
                    assert result.get("success"), result
                    return result["result"]

                partner = singleton(await execute({
                    "model": "res.partner", "operation": "create",
                    "values": {"name": "Attachment target"},
                }))
                link = {"res_model": "res.partner", "res_id": partner}
                upload = {"name": "offer.pdf", "datas_from_path": str(source)}
                orphan = singleton(await execute({
                    "model": "ir.attachment", "operation": "create", "values": upload,
                }))
                # The creator can read the orphan but another internal user cannot.
                before = await call("read_attachment", {
                    "attachment_id": orphan, "include_data": False,
                })
                assert before["attachment"]["res_id"] == 0, before
                denied = browser.get(f"{url}/web/content/{orphan}?download=true", timeout=30)
                assert denied.status_code in {403, 404}, (denied.status_code, denied.text)

                await execute({
                    "model": "ir.attachment", "operation": "write",
                    "record_ids": [orphan], "values": link,
                })
                after = await call("read_attachment", {
                    "attachment_id": orphan, "include_data": False,
                })
                assert after["attachment"]["checksum"] == before["attachment"]["checksum"]
                assert after["attachment"]["checksum"] == hashlib.sha1(content).hexdigest()
                assert after["attachment"]["res_model"] == link["res_model"]
                assert after["attachment"]["res_id"] == partner

                linked = singleton(await execute({
                    "model": "ir.attachment", "operation": "create",
                    "values": {**upload, **link},
                }))
                for attachment_id in (orphan, linked):
                    response = browser.get(
                        f"{url}/web/content/{attachment_id}?download=true", timeout=30,
                    )
                    assert response.status_code == 200, response.status_code
                    assert response.content == content
                    assert response.headers["Content-Type"].startswith("application/pdf")

                # A batch uses the same live metadata and preserves each link.
                batch = await execute({
                    "model": "ir.attachment", "operation": "create",
                    "values_list": [
                        {"name": "batch-a.pdf", **link},
                        {"name": "batch-b.pdf", **link},
                    ],
                })
                assert len(batch) == 2, batch
                for attachment_id in batch:
                    record = await call("read_record", {
                        "model": "ir.attachment", "record_id": attachment_id,
                        "fields": ["res_model", "res_id"],
                    })
                    assert record["result"]["res_id"] == partner, record

                # Live metadata must still reject an unrelated readonly field.
                rejected = await call("validate_write", {
                    "model": "ir.attachment", "operation": "write",
                    "record_ids": [orphan], "values": {**link, "checksum": "forged"},
                })
                assert not rejected.get("success"), rejected
                assert not rejected["approval_status"]["stored"], rejected
    browser.close()
    return {
        "version": target.version, "transport": transport,
        "orphan_repaired": True, "linked_upload": True, "batch_create": True,
        "reader_download": True, "checksum_preserved": True,
    }


def smoke_one(version: str, timeout: int) -> list[dict[str, Any]]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    slug = version.replace(".", "")
    target = VersionTarget(
        version, f"mcp-attachment-smoke-{slug}-{uuid4().hex[:8]}", port, 0,
        f"mcp_attachment_smoke_{slug}",
    )
    env = compose_env(target)
    try:
        run(compose_cmd(target, "up", "-d", "db"), env=env)
        run(compose_cmd(target, "pull", "odoo"), env=env, timeout=900)
        run(compose_cmd(
            target, "run", "--rm", "odoo", "--stop-after-init", "-d", target.database,
            "-i", "base,web", "--without-demo=all", "--db_host=db", "--db_port=5432",
            "--db_user", DB_USER, "--db_password", DB_PASSWORD,
        ), env=env, timeout=900)
        api_key = generate_json2_api_key(target, env) if version.startswith("19.") else None
        run(compose_cmd(target, "up", "-d", "odoo"), env=env)
        wait_for_http(target.port, timeout)
        wait_for_xmlrpc(target, timeout)
        results = []
        for transport in ["xmlrpc", "json2"] if api_key else ["xmlrpc"]:
            result = asyncio.run(attachment_smoke(target, transport=transport, api_key=api_key))
            results.append(result)
            print(json.dumps(result, sort_keys=True), flush=True)
        return results
    finally:
        run(compose_cmd(target, "down", "-v", "--remove-orphans"), env=env, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", nargs="+", default=list(DEFAULT_VERSIONS))
    parser.add_argument("--timeout", type=int, default=360)
    args = parser.parse_args()
    results = []
    failures = []
    for version in args.versions:
        try:
            results.extend(smoke_one(version, args.timeout))
        except Exception as exc:
            traceback.print_exc()
            failures.append({"version": version, "error": repr(exc)})
    print(json.dumps({"passed": results, "failed": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
