#!/usr/bin/env python3
"""Exercise journal-entry writes against disposable Odoo Compose databases.

Uses the same Compose fixture and MCP client helpers as odoo_compose_smoke.py.
No existing Odoo instance or database is used. Each stack is removed on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import traceback
from copy import deepcopy
from datetime import date
from typing import Any
from uuid import uuid4

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


async def journal_entry_smoke(
    target: VersionTarget,
    *,
    transport: str = "xmlrpc",
    api_key: str | None = None,
    batch: bool = False,
) -> dict[str, Any]:
    client = OdooClient(
        f"http://127.0.0.1:{target.port}",
        target.database,
        ADMIN_LOGIN,
        ADMIN_PASSWORD,
        transport=transport,
        api_key=api_key,
    )
    suffix = ("J" if transport == "json2" else "X") + ("B" if batch else "S")
    company_id = client.execute_method("res.company", "search", [], limit=1)[0]
    account_fields = client.get_model_fields("account.account")
    company_values = (
        {"company_ids": [[6, 0, [company_id]]]}
        if "company_ids" in account_fields
        else {"company_id": company_id}
    )
    account_id = client.execute_method(
        "account.account",
        "create",
        {
            "name": "Smoke reconcilable account",
            "code": f"MCP{suffix}01",
            "account_type": "asset_current",
            "reconcile": True,
            **company_values,
        },
    )
    # JSON-2 serializes recordsets as ID lists, including singleton creates.
    if isinstance(account_id, list):
        account_id = account_id[0]
    counterpart_id = client.execute_method(
        "account.account",
        "create",
        {
            "name": "Smoke counterpart",
            "code": f"MCP{suffix}02",
            "account_type": "expense",
            **company_values,
        },
    )
    if isinstance(counterpart_id, list):
        counterpart_id = counterpart_id[0]
    journal_id = client.execute_method(
        "account.journal",
        "create",
        {
            "name": "MCP journal smoke",
            "code": f"M{suffix}",
            "type": "general",
            "company_id": company_id,
        },
    )
    if isinstance(journal_id, list):
        journal_id = journal_id[0]
    parent_fields = client.get_model_fields("account.move")
    line_fields = client.get_model_fields("account.move.line")
    assert parent_fields["line_ids"]["relation"] == "account.move.line"
    assert parent_fields["move_type"]["type"] == "selection"
    assert all(
        field in line_fields for field in ("name", "account_id", "debit", "credit")
    )
    values = {
        "move_type": "entry",
        "journal_id": journal_id,
        "date": date.today().isoformat(),
        "line_ids": [
            [0, 0, {"name": "Item A", "account_id": account_id, "debit": 10.0}],
            [0, 0, {"name": "Item B", "account_id": account_id, "debit": 20.0}],
            [
                0,
                0,
                {"name": "Counterpart", "account_id": counterpart_id, "credit": 30.0},
            ],
        ],
    }
    env = mcp_env(target, transport=transport, api_key=api_key)
    env.update(
        {
            "ODOO_MCP_ENABLE_WRITES": "1",
            "ODOO_MCP_ELICIT_WRITES": "0",
            "ODOO_MCP_ALLOW_UNKNOWN_METHODS": "0",
            "ODOO_MCP_ALLOWED_SIDE_EFFECT_METHODS": "account.move.action_post,account.move.line.reconcile",
        }
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "odoo_mcp"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return decode_tool_json(
                    await session.call_tool(tool, arguments=arguments), tool
                )

            async def create(
                payload: dict[str, Any], *, success: bool = True
            ) -> list[int]:
                arguments = {
                    "model": "account.move",
                    "operation": "create",
                    **(
                        {"values_list": [deepcopy(values), payload]}
                        if batch
                        else {"values": payload}
                    ),
                }
                original = deepcopy(arguments)
                preview = await call("preview_write", arguments)
                assert preview.get("success"), preview
                unvalidated = await call(
                    "execute_approved_write",
                    {
                        "approval": preview["approval"],
                        "confirm": True,
                    },
                )
                assert not unvalidated.get("success"), unvalidated
                validation = await call("validate_write", arguments)
                assert validation.get("success"), validation
                assert validation["approval_status"]["stored"], validation
                assert validation["metadata_used"] == {
                    "fields_get": True,
                    "source": "server",
                }
                assert preview["approval"]["token"] == validation["approval"]["token"]
                # Live validation adds TTL timestamps outside the canonical
                # payload; all preview fields must remain identical.
                assert preview["approval"] == {
                    key: validation["approval"][key] for key in preview["approval"]
                }
                assert arguments == original
                key = "values_list" if batch else "values"
                assert validation["approval"][key] == original[key]
                unconfirmed = await call(
                    "execute_approved_write",
                    {
                        "approval": validation["approval"],
                        "confirm": False,
                    },
                )
                assert not unconfirmed.get("success"), unconfirmed
                tampered = deepcopy(validation["approval"])
                (tampered["values_list"][0] if batch else tampered["values"])[
                    "line_ids"
                ][0][2]["debit"] = 999
                rejected = await call(
                    "execute_approved_write",
                    {
                        "approval": tampered,
                        "confirm": True,
                    },
                )
                assert not rejected.get("success"), rejected
                result = await call(
                    "execute_approved_write",
                    {
                        "approval": validation["approval"],
                        "confirm": True,
                    },
                )
                assert result.get("success") is success, result
                if not success:
                    assert "balanc" in result.get("error", "").lower(), result
                    return []
                ids = result["result"]
                ids = ids if isinstance(ids, list) else [ids]
                assert len(ids) == (2 if batch else 1), result
                # Check every batch member, including its lines and posting.
                for move_id in ids:
                    draft = client.execute_method(
                        "account.move", "read", [move_id], fields=["state"]
                    )[0]
                    assert draft["state"] == "draft", draft
                    expected = (
                        original[key][ids.index(move_id)] if batch else original[key]
                    )
                    actual = lines(move_id)
                    assert len(actual) == len(expected["line_ids"]), actual
                    for command in expected["line_ids"]:
                        item = next(
                            line
                            for line in actual
                            if line["name"] == command[2]["name"]
                        )
                        assert item["account_id"][0] == command[2]["account_id"], item
                        assert item["debit"] == command[2].get("debit", 0), item
                        assert item["credit"] == command[2].get("credit", 0), item
                    await post(move_id)
                    assert {line["id"] for line in lines(move_id)} == {
                        line["id"] for line in actual
                    }
                return ids

            def lines(move_id: int) -> list[dict[str, Any]]:
                return client.execute_method(
                    "account.move.line",
                    "search_read",
                    [
                        ["move_id", "=", move_id],
                    ],
                    fields=[
                        "name",
                        "account_id",
                        "debit",
                        "credit",
                        "reconciled",
                        "amount_residual",
                    ],
                )

            async def post(move_id: int) -> None:
                result = await call(
                    "execute_method",
                    {
                        "model": "account.move",
                        "method": "action_post",
                        **(
                            {"kwargs": {"ids": [move_id]}}
                            if transport == "json2"
                            else {"args": [[move_id]]}
                        ),
                    },
                )
                assert result.get("success"), result
                record = client.execute_method(
                    "account.move", "read", [move_id], fields=["state"]
                )[0]
                assert record["state"] == "posted", record

            # Negative validation must never authorize a write on any version.
            for field, invalid in (
                ("unknown_field", 1),
                ("amount_residual", 10),
                ("move_id", 1),
                ("tax_ids", [[6, 0, []]]),
            ):
                bad = deepcopy(values)
                bad["line_ids"][0][2][field] = invalid
                rejected = await call(
                    "validate_write",
                    {"model": "account.move", "operation": "create", "values": bad},
                )
                assert not rejected.get("success"), rejected
                assert not rejected.get("approval_status", {}).get("stored"), rejected
            for context in ({"check_move_validity": False}, {"default_line_ids": []}):
                rejected = await call(
                    "validate_write",
                    {
                        "model": "account.move",
                        "operation": "create",
                        "values": values,
                        "context": context,
                    },
                )
                assert not rejected.get("success"), rejected
            linked = deepcopy(values)
            linked["line_ids"] = [[4, 1, 0]]
            rejected = await call(
                "validate_write",
                {"model": "account.move", "operation": "create", "values": linked},
            )
            assert not rejected.get("success"), rejected
            readonly = deepcopy(values)
            readonly["name"] = "CUSTOM/1"
            # Parent name is readonly only in some supported Odoo versions.
            if parent_fields["name"].get("readonly"):
                rejected = await call(
                    "validate_write",
                    {
                        "model": "account.move",
                        "operation": "create",
                        "values": readonly,
                    },
                )
                assert not rejected.get("success"), rejected
            blocked = await call(
                "execute_method",
                {"model": "account.move", "method": "create", "args": [values]},
            )
            assert not blocked.get("success"), blocked

            move_ids = await create(values)
            move_id = move_ids[0]
            # Exercise both readonly contracts in the same MCP server session,
            # linking an attachment to the journal entry just created above.
            attachment_args = {
                "model": "ir.attachment",
                "operation": "create",
                "values": {
                    "name": "Journal evidence",
                    "res_model": "account.move",
                    "res_id": move_id,
                },
            }
            preview = await call("preview_write", attachment_args)
            validation = await call("validate_write", attachment_args)
            assert validation.get("success"), validation
            assert validation["approval_status"]["stored"], validation
            assert preview["approval"]["token"] == validation["approval"]["token"]
            attachment = await call(
                "execute_approved_write",
                {"approval": validation["approval"], "confirm": True},
            )
            assert attachment.get("success"), attachment
            attachment_ids = attachment["result"]
            attachment_ids = (
                attachment_ids if isinstance(attachment_ids, list) else [attachment_ids]
            )
            linked_attachment = client.execute_method(
                "ir.attachment", "read", attachment_ids, fields=["res_model", "res_id"]
            )[0]
            assert linked_attachment["res_model"] == "account.move", linked_attachment
            assert linked_attachment["res_id"] == move_id, linked_attachment
            items = lines(move_id)
            assert len(items) == 3, items
            by_name = {line["name"]: line for line in items}
            assert by_name["Item A"]["id"] != by_name["Item B"]["id"]
            assert (
                by_name["Item A"]["account_id"][0]
                == by_name["Item B"]["account_id"][0]
                == account_id
            )
            assert by_name["Item A"]["debit"] == 10 and by_name["Item B"]["debit"] == 20
            assert sum(line["debit"] - line["credit"] for line in items) == 0
            assert {line["id"] for line in lines(move_id)} == {
                line["id"] for line in items
            }

            # Real ORM rejection must roll back both the move and all its items.
            before = {
                model: client.execute_method(model, "search_count", [])
                for model in [
                    "account.move",
                    "account.move.line",
                ]
            }
            unbalanced = deepcopy(values)
            unbalanced["line_ids"][0][2]["debit"] = 11.0
            await create(unbalanced, success=False)
            assert before == {
                model: client.execute_method(model, "search_count", [])
                for model in before
            }

            # Omitted move_type must use the entry default. Reconcile only A;
            # B on the same account must remain an independent open item.
            offset = {
                "journal_id": journal_id,
                "date": values["date"],
                "line_ids": [
                    [
                        0,
                        0,
                        {
                            "name": "Settlement A",
                            "account_id": account_id,
                            "credit": 10.0,
                        },
                    ],
                    [
                        0,
                        0,
                        {
                            "name": "Counterpart",
                            "account_id": counterpart_id,
                            "debit": 10.0,
                        },
                    ],
                ],
            }
            # In batch mode the first member is another original entry; the
            # second is the settlement. Both have been checked and posted.
            offset_id = (await create(offset))[-1]
            offset_line = next(
                line for line in lines(offset_id) if line["name"] == "Settlement A"
            )
            reconciled = await call(
                "execute_method",
                {
                    "model": "account.move.line",
                    "method": "reconcile",
                    **(
                        {
                            "kwargs": {
                                "ids": [by_name["Item A"]["id"], offset_line["id"]]
                            }
                        }
                        if transport == "json2"
                        else {"args": [[by_name["Item A"]["id"], offset_line["id"]]]}
                    ),
                },
            )
            # Some Odoo versions cannot serialize reconcile's recordset result;
            # a read is the authoritative check of the committed outcome.
            after = {line["name"]: line for line in lines(move_id)}
            assert after["Item A"]["reconciled"] is True, reconciled
            assert after["Item B"]["reconciled"] is False, after
            assert after["Item B"]["amount_residual"] == 20, after
            if batch:
                untouched = {line["name"]: line for line in lines(move_ids[1])}
                assert untouched["Item A"]["reconciled"] is False, untouched
                assert untouched["Item A"]["amount_residual"] == 10, untouched
                assert untouched["Item B"]["amount_residual"] == 20, untouched

    return {
        "version": target.version,
        "status": "passed",
        "transport": f"stdio/{transport}",
        "create": "batch" if batch else "single",
        "live_parent_and_child_metadata": True,
        "attachment_linked_to_created_move": True,
        "parent_readonly": {
            field: bool(parent_fields[field].get("readonly"))
            for field in ("line_ids", "move_type")
        },
        "reconcile_response_success": bool(reconciled.get("success")),
        "negative_checks_and_gates": True,
        "distinct_lines": True,
        "posted": True,
        "atomic_rollback": True,
        "independent_reconciliation": True,
    }


def smoke_one(version: str, timeout: int) -> list[dict[str, Any]]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    slug = version.replace(".", "")
    target = VersionTarget(
        version,
        f"mcp-journal-smoke-{slug}-{uuid4().hex[:8]}",
        port,
        0,
        f"mcp_journal_smoke_{slug}",
    )
    env = compose_env(target)
    try:
        run(compose_cmd(target, "up", "-d", "db"), env=env)
        run(compose_cmd(target, "pull", "odoo"), env=env, timeout=900)
        run(
            compose_cmd(
                target,
                "run",
                "--rm",
                "odoo",
                "--stop-after-init",
                "-d",
                target.database,
                "-i",
                "base,account",
                "--without-demo=all",
                "--db_host=db",
                "--db_port=5432",
                "--db_user",
                DB_USER,
                "--db_password",
                DB_PASSWORD,
            ),
            env=env,
            timeout=900,
        )
        api_key = (
            generate_json2_api_key(target, env) if version.startswith("19.") else None
        )
        run(compose_cmd(target, "up", "-d", "odoo"), env=env)
        wait_for_http(target.port, timeout)
        wait_for_xmlrpc(target, timeout)
        results = []
        for transport in ["xmlrpc", "json2"] if api_key else ["xmlrpc"]:
            for batch in (False, True):
                print(f"Journal smoke: {version} {transport} batch={batch}", flush=True)
                result = asyncio.run(
                    journal_entry_smoke(
                        target, transport=transport, api_key=api_key, batch=batch
                    )
                )
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
    print(json.dumps({"passed": results, "failed": failures}, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
