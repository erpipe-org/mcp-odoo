#!/usr/bin/env python3
"""
Boot disposable Odoo Docker Compose stacks and run live XML-RPC + MCP smoke tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xmlrpc.client
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.integration.yml"
DEFAULT_VERSIONS = ("16.0", "17.0", "18.0", "19.0")
DB_USER = "odoo"
DB_PASSWORD = "odoo"
TEST_DB_PREFIX = "mcp_smoke"
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "admin"
RESTRICTED_LOGIN = "mcp.smoke.restricted@example.test"
RESTRICTED_PASSWORD = "mcp-smoke-restricted"
RULE_AUDITOR_LOGIN = "mcp.smoke.rule.auditor@example.test"
RULE_AUDITOR_PASSWORD = "mcp-smoke-rule-auditor"
RULE_SMOKE_RULE_NAME = "MCP smoke complex partner visibility"
PACKAGED_MODULE_NAME = "mcp_smoke_access"
PACKAGED_RULE_NAME = "MCP packaged partner visibility"
PACKAGED_AUDITOR_LOGIN = "mcp.smoke.packaged.auditor@example.test"
PACKAGED_AUDITOR_PASSWORD = "mcp-smoke-packaged-auditor"
CONTAINER_ADDONS_PATH = "/mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons"


@dataclass(frozen=True)
class VersionTarget:
    version: str
    project: str
    port: int
    mcp_port: int
    database: str


def run(
    args: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
    timeout: int = 600,
    attempts: int = 1,
    retry_delay_seconds: int = 10,
    input_text: str | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    last_error: subprocess.CalledProcessError | subprocess.TimeoutExpired | None = None
    for attempt in range(1, attempts + 1):
        prefix = f"+ [{attempt}/{attempts}] " if attempts > 1 else "+ "
        print(prefix + " ".join(args), flush=True)
        try:
            return subprocess.run(
                args,
                cwd=ROOT,
                env=env,
                check=check,
                timeout=timeout,
                text=True,
                input=input_text,
                capture_output=capture_output,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            stdout = getattr(exc, "stdout", None)
            stderr = getattr(exc, "stderr", None)
            if stdout:
                print(str(stdout), file=sys.stderr, flush=True)
            if stderr:
                print(str(stderr), file=sys.stderr, flush=True)
            if attempt == attempts or not check:
                raise
            print(
                f"Command failed: {exc!r}; retrying in {retry_delay_seconds}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(retry_delay_seconds)

    raise AssertionError(f"unreachable retry state: {last_error!r}")


def compose_env(target: VersionTarget) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ODOO_VERSION": target.version,
            "ODOO_PORT": str(target.port),
            "COMPOSE_PROJECT_NAME": target.project,
        }
    )
    return env


def compose_cmd(target: VersionTarget, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "--project-name",
        target.project,
        *args,
    ]


def make_target(version: str, index: int) -> VersionTarget:
    version_slug = "".join(ch if ch.isalnum() else "" for ch in version)
    return VersionTarget(
        version=version,
        project=f"mcp-odoo-smoke-{version_slug}",
        port=18069 + index,
        mcp_port=19069 + index,
        database=f"{TEST_DB_PREFIX}_{version_slug}",
    )


def wait_for_http(port: int, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/web/login"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            last_error = exc
        time.sleep(2)
    raise TimeoutError(f"Odoo HTTP did not become ready at {url}: {last_error}")


def wait_for_xmlrpc(target: VersionTarget, timeout_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    common = xmlrpc.client.ServerProxy(
        f"http://127.0.0.1:{target.port}/xmlrpc/2/common", allow_none=True
    )
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            uid = common.authenticate(target.database, ADMIN_LOGIN, ADMIN_PASSWORD, {})
            if isinstance(uid, int) and uid > 0:
                return uid
        except (ConnectionError, OSError, xmlrpc.client.Error) as exc:
            last_error = exc
        time.sleep(2)
    raise TimeoutError(
        f"XML-RPC auth did not become ready for {target.version}: {last_error}"
    )


def init_database(target: VersionTarget, env: dict[str, str]) -> None:
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
            "base,mail",
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


def run_packaged_addon_lifecycle(
    target: VersionTarget, env: dict[str, str]
) -> dict[str, Any]:
    """Install and update the packaged smoke addon through Odoo's module CLI."""
    lifecycle: dict[str, Any] = {"module": PACKAGED_MODULE_NAME}
    for operation, flag in (("install", "-i"), ("update", "-u")):
        run(
            compose_cmd(
                target,
                "run",
                "--rm",
                "odoo",
                "--stop-after-init",
                "-d",
                target.database,
                flag,
                PACKAGED_MODULE_NAME,
                "--without-demo=all",
                "--addons-path",
                CONTAINER_ADDONS_PATH,
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
        lifecycle[operation] = "passed"
    return lifecycle


def direct_xmlrpc_smoke(target: VersionTarget, uid: int) -> dict[str, Any]:
    models = xmlrpc.client.ServerProxy(
        f"http://127.0.0.1:{target.port}/xmlrpc/2/object", allow_none=True
    )
    partners = models.execute_kw(
        target.database,
        uid,
        ADMIN_PASSWORD,
        "res.partner",
        "search_read",
        [[["id", ">", 0]]],
        {"fields": ["id", "name"], "limit": 1},
    )
    model_info = models.execute_kw(
        target.database,
        uid,
        ADMIN_PASSWORD,
        "ir.model",
        "search_read",
        [[["model", "=", "res.partner"]]],
        {"fields": ["name", "model"], "limit": 1},
    )
    if not isinstance(partners, list):
        raise AssertionError("res.partner search_read did not return a list")
    if not isinstance(model_info, list) or not model_info:
        raise AssertionError("ir.model lookup did not return a list")
    model_record = model_info[0]
    if not isinstance(model_record, dict):
        raise AssertionError("ir.model lookup did not return a dict record")
    model_record = cast(dict[str, Any], model_record)
    if model_record.get("model") != "res.partner":
        raise AssertionError("ir.model lookup did not return res.partner")
    return {
        "partner_count_sample": len(partners),
        "model_name": str(model_record["name"]),
    }


def create_restricted_user(target: VersionTarget, env: dict[str, str]) -> dict[str, Any]:
    """Create a disposable non-admin internal user for access diagnosis smoke."""
    script = f"""
login = {json.dumps(RESTRICTED_LOGIN)}
password = {json.dumps(RESTRICTED_PASSWORD)}
Users = env["res.users"].sudo()
user = Users.search([("login", "=", login)], limit=1)
group = env.ref("base.group_user")
company = env.ref("base.main_company")
group_field = "groups_id" if "groups_id" in Users._fields else "group_ids"
vals = {{
    "name": "MCP Smoke Restricted",
    "login": login,
    "password": password,
    "active": True,
    "company_id": company.id,
    "company_ids": [(6, 0, [company.id])],
    group_field: [(6, 0, [group.id])],
}}
if user:
    user.write(vals)
else:
    user = Users.create(vals)
env.cr.commit()
print("USERID=" + str(user.id))
print("GROUPFIELD=" + group_field)
"""
    completed = run(
        compose_cmd(
            target,
            "run",
            "--rm",
            "-T",
            "odoo",
            "odoo",
            "shell",
            "-d",
            target.database,
            "--db_host=db",
            "--db_port=5432",
            "--db_user",
            DB_USER,
            "--db_password",
            DB_PASSWORD,
        ),
        env=env,
        timeout=300,
        input_text=script,
        capture_output=True,
    )
    result: dict[str, Any] = {"login": RESTRICTED_LOGIN}
    for line in completed.stdout.splitlines():
        if line.startswith("USERID="):
            result["uid"] = int(line.removeprefix("USERID=").strip())
        elif line.startswith("GROUPFIELD="):
            result["group_field"] = line.removeprefix("GROUPFIELD=").strip()
    if "uid" not in result:
        raise AssertionError("restricted user creation did not print USERID=<id>")
    return result


def create_complex_record_rule_fixture(
    target: VersionTarget, env: dict[str, str]
) -> dict[str, Any]:
    """Seed custom partner records and a nested-domain record rule."""
    script = f"""
import json

login = {json.dumps(RULE_AUDITOR_LOGIN)}
password = {json.dumps(RULE_AUDITOR_PASSWORD)}
rule_name = {json.dumps(RULE_SMOKE_RULE_NAME)}
Users = env["res.users"].sudo()
Groups = env["res.groups"].sudo()
Access = env["ir.model.access"].sudo()
Partners = env["res.partner"].sudo()
Tags = env["res.partner.category"].sudo()
Rules = env["ir.rule"].sudo()
Models = env["ir.model"].sudo()
company = env.ref("base.main_company")
group_user = env.ref("base.group_user")
group_system = env.ref("base.group_system")
group_field = "groups_id" if "groups_id" in Users._fields else "group_ids"

group_vals = {{"name": "MCP Smoke Rule Auditor"}}
category = env.ref("base.module_category_hidden", raise_if_not_found=False)
if category and "category_id" in Groups._fields:
    group_vals["category_id"] = category.id
smoke_group = Groups.search([("name", "=", group_vals["name"])], limit=1)
if not smoke_group:
    smoke_group = Groups.create(group_vals)

user_model = Models.search([("model", "=", "res.users")], limit=1)
access_name = "mcp_smoke_rule_auditor_res_users_read"
access_vals = {{
    "name": access_name,
    "model_id": user_model.id,
    "group_id": smoke_group.id,
    "perm_read": True,
    "perm_write": False,
    "perm_create": False,
    "perm_unlink": False,
}}
access = Access.search([("name", "=", access_name)], limit=1)
if access:
    access.write(access_vals)
else:
    Access.create(access_vals)

user = Users.search([("login", "=", login)], limit=1)
user_vals = {{
    "name": "MCP Smoke Rule Auditor",
    "login": login,
    "password": password,
    "active": True,
    "company_id": company.id,
    "company_ids": [(6, 0, [company.id])],
    group_field: [(6, 0, [group_user.id, group_system.id, smoke_group.id])],
}}
if user:
    user.write(user_vals)
else:
    user = Users.create(user_vals)

visible_tag = Tags.search([("name", "=", "MCP Smoke Visible")], limit=1)
if not visible_tag:
    visible_tag = Tags.create({{"name": "MCP Smoke Visible"}})
hidden_tag = Tags.search([("name", "=", "MCP Smoke Hidden")], limit=1)
if not hidden_tag:
    hidden_tag = Tags.create({{"name": "MCP Smoke Hidden"}})

Rules.search([("name", "=", rule_name)]).unlink()
Partners.search([("ref", "like", "MCP-RULE-SMOKE-%")]).unlink()

visible_partner = Partners.create({{
    "name": "MCP Rule Smoke Visible",
    "ref": "MCP-RULE-SMOKE-VISIBLE",
    "company_id": company.id,
    "category_id": [(6, 0, [visible_tag.id])],
}})
owned_partner = Partners.create({{
    "name": "MCP Rule Smoke Owned Hidden",
    "ref": "MCP-RULE-SMOKE-OWNED",
    "company_id": company.id,
    "user_id": user.id,
    "category_id": [(6, 0, [hidden_tag.id])],
}})
hidden_partner = Partners.create({{
    "name": "MCP Rule Smoke Hidden",
    "ref": "MCP-RULE-SMOKE-HIDDEN",
    "company_id": company.id,
    "category_id": [(6, 0, [hidden_tag.id])],
}})
untagged_partner = Partners.create({{
    "name": "MCP Rule Smoke Untagged",
    "ref": "MCP-RULE-SMOKE-UNTAGGED",
    "company_id": company.id,
}})
record_ids = [
    visible_partner.id,
    owned_partner.id,
    hidden_partner.id,
    untagged_partner.id,
]
domain_force = str([
    "&",
    ("active", "=", True),
    "|",
    ("user_id", "=", "user.id"),
    ("category_id", "in", [visible_tag.id]),
]).replace("'user.id'", "user.id")
model = Models.search([("model", "=", "res.partner")], limit=1)
rule = Rules.create({{
    "name": rule_name,
    "model_id": model.id,
    "domain_force": domain_force,
    "perm_read": True,
    "perm_write": False,
    "perm_create": False,
    "perm_unlink": False,
}})

visible_ids = Partners.with_user(user).search([("id", "in", record_ids)]).ids
visible_count = Partners.with_user(user).search_count([("id", "in", record_ids)])
env.cr.commit()
print("RULEFIXTURE=" + json.dumps({{
    "login": login,
    "uid": user.id,
    "group_field": group_field,
    "rule_id": rule.id,
    "rule_name": rule.name,
    "domain_force": domain_force,
    "record_ids": record_ids,
    "expected_count": len(record_ids),
    "visible_ids": visible_ids,
    "visible_count": visible_count,
}}, sort_keys=True))
"""
    completed = run(
        compose_cmd(
            target,
            "run",
            "--rm",
            "-T",
            "odoo",
            "odoo",
            "shell",
            "-d",
            target.database,
            "--db_host=db",
            "--db_port=5432",
            "--db_user",
            DB_USER,
            "--db_password",
            DB_PASSWORD,
        ),
        env=env,
        timeout=300,
        input_text=script,
        capture_output=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("RULEFIXTURE="):
            payload = json.loads(line.removeprefix("RULEFIXTURE="))
            if payload.get("visible_count") != 2:
                raise AssertionError(f"complex rule fixture expected 2 visible: {payload}")
            return cast(dict[str, Any], payload)
    raise AssertionError("complex record-rule fixture did not print RULEFIXTURE=<json>")


def create_packaged_addon_rule_fixture(
    target: VersionTarget, env: dict[str, str]
) -> dict[str, Any]:
    """Bind a real user to the packaged addon group and verify XML rule data."""
    script = f"""
import json

login = {json.dumps(PACKAGED_AUDITOR_LOGIN)}
password = {json.dumps(PACKAGED_AUDITOR_PASSWORD)}
module_name = {json.dumps(PACKAGED_MODULE_NAME)}
rule_name = {json.dumps(PACKAGED_RULE_NAME)}
Users = env["res.users"].sudo()
Partners = env["res.partner"].sudo()
company = env.ref("base.main_company")
group_user = env.ref("base.group_user")
group_system = env.ref("base.group_system")
packaged_group = env.ref(module_name + ".group_mcp_packaged_rule_auditor")
rule = env.ref(module_name + ".rule_mcp_packaged_partner_visibility")
module = env["ir.module.module"].sudo().search([("name", "=", module_name)], limit=1)
if not module or module.state != "installed":
    raise Exception("Packaged smoke addon is not installed: " + str(module.read(["state"])))
if rule.name != rule_name:
    raise Exception("Unexpected packaged rule name: " + rule.name)

group_field = "groups_id" if "groups_id" in Users._fields else "group_ids"
user = Users.search([("login", "=", login)], limit=1)
user_vals = {{
    "name": "MCP Smoke Packaged Rule Auditor",
    "login": login,
    "password": password,
    "active": True,
    "company_id": company.id,
    "company_ids": [(6, 0, [company.id])],
    group_field: [(6, 0, [group_user.id, group_system.id, packaged_group.id])],
}}
if user:
    user.write(user_vals)
else:
    user = Users.create(user_vals)

visible_partner = env.ref(module_name + ".partner_mcp_packaged_visible")
owned_partner = env.ref(module_name + ".partner_mcp_packaged_owned")
hidden_partner = env.ref(module_name + ".partner_mcp_packaged_hidden")
untagged_partner = env.ref(module_name + ".partner_mcp_packaged_untagged")
owned_partner.write({{"user_id": user.id}})
record_ids = [
    visible_partner.id,
    owned_partner.id,
    hidden_partner.id,
    untagged_partner.id,
]
visible_ids = Partners.with_user(user).search([("id", "in", record_ids)]).ids
visible_count = Partners.with_user(user).search_count([("id", "in", record_ids)])
env.cr.commit()
print("PACKAGEDFIXTURE=" + json.dumps({{
    "login": login,
    "uid": user.id,
    "module": module.name,
    "module_id": module.id,
    "module_state": module.state,
    "module_latest_version": module.latest_version,
    "module_installed_version": module.installed_version,
    "group_field": group_field,
    "group_id": packaged_group.id,
    "group_name": packaged_group.name,
    "rule_id": rule.id,
    "rule_name": rule.name,
    "domain_force": rule.domain_force,
    "record_ids": record_ids,
    "expected_count": len(record_ids),
    "visible_ids": visible_ids,
    "visible_count": visible_count,
}}, sort_keys=True))
"""
    completed = run(
        compose_cmd(
            target,
            "run",
            "--rm",
            "-T",
            "odoo",
            "odoo",
            "shell",
            "-d",
            target.database,
            "--addons-path",
            CONTAINER_ADDONS_PATH,
            "--db_host=db",
            "--db_port=5432",
            "--db_user",
            DB_USER,
            "--db_password",
            DB_PASSWORD,
        ),
        env=env,
        timeout=300,
        input_text=script,
        capture_output=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("PACKAGEDFIXTURE="):
            payload = json.loads(line.removeprefix("PACKAGEDFIXTURE="))
            if payload.get("visible_count") != 2:
                raise AssertionError(
                    f"packaged addon fixture expected 2 visible: {payload}"
                )
            return cast(dict[str, Any], payload)
    raise AssertionError("packaged addon fixture did not print PACKAGEDFIXTURE=<json>")


def deactivate_record_rule(target: VersionTarget, env: dict[str, str], rule_name: str) -> None:
    """Disable a smoke-only global rule before installing the next scenario."""
    script = f"""
rule_name = {json.dumps(rule_name)}
rules = env["ir.rule"].sudo().search([("name", "=", rule_name)])
rules.write({{"active": False}})
env.cr.commit()
print("DEACTIVATED_RULES=" + str(len(rules)))
"""
    run(
        compose_cmd(
            target,
            "run",
            "--rm",
            "-T",
            "odoo",
            "odoo",
            "shell",
            "-d",
            target.database,
            "--db_host=db",
            "--db_port=5432",
            "--db_user",
            DB_USER,
            "--db_password",
            DB_PASSWORD,
        ),
        env=env,
        timeout=300,
        input_text=script,
        capture_output=True,
    )


def generate_json2_api_key(
    target: VersionTarget, env: dict[str, str], *, login: str = ADMIN_LOGIN
) -> str:
    """Create a disposable Odoo API key for JSON-2 smoke testing."""
    script = f"""
from datetime import datetime, timedelta

login = {json.dumps(login)}
user = env["res.users"].sudo().search([("login", "=", login)], limit=1)
if not user:
    raise Exception("No user found for API key: " + login)
expiration_date = datetime.now() + timedelta(days=7)
key = env["res.users.apikeys"].sudo().with_user(user)._generate(
    None,
    "mcp-odoo json2 smoke",
    expiration_date,
)
env.cr.commit()
print("APIKEY=" + key)
"""
    completed = run(
        compose_cmd(
            target,
            "run",
            "--rm",
            "-T",
            "odoo",
            "odoo",
            "shell",
            "-d",
            target.database,
            "--db_host=db",
            "--db_port=5432",
            "--db_user",
            DB_USER,
            "--db_password",
            DB_PASSWORD,
        ),
        env=env,
        timeout=300,
        input_text=script,
        capture_output=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("APIKEY="):
            return line.removeprefix("APIKEY=").strip()
    raise AssertionError("API key generation did not print APIKEY=<key>")


def direct_json2_smoke(target: VersionTarget, api_key: str) -> dict[str, Any]:
    """Validate Odoo 19 External JSON-2 directly, without MCP in the middle."""
    endpoint = f"http://127.0.0.1:{target.port}/json/2/res.partner/search_read"
    body = json.dumps(
        {"domain": [["id", ">", 0]], "fields": ["id", "name"], "limit": 1}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"JSON-2 HTTP {exc.code}: {error_body}") from exc

    if not isinstance(payload, list):
        raise AssertionError(f"JSON-2 search_read did not return a list: {payload!r}")
    return {"partner_count_sample": len(payload)}


def mcp_env(
    target: VersionTarget,
    *,
    transport: str = "xmlrpc",
    api_key: str | None = None,
    username: str = ADMIN_LOGIN,
    password: str = ADMIN_PASSWORD,
    locale: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ODOO_URL": f"http://127.0.0.1:{target.port}",
            "ODOO_DB": target.database,
            "ODOO_USERNAME": username,
            "ODOO_PASSWORD": password,
            "ODOO_TRANSPORT": transport,
            "ODOO_TIMEOUT": "30",
            "ODOO_VERIFY_SSL": "1",
            "ODOO_ADDONS_PATHS": str(ROOT / "src"),
            "PYTHONPATH": str(ROOT / "src"),
        }
    )
    if api_key:
        env["ODOO_API_KEY"] = api_key
    if locale:
        env["ODOO_LOCALE"] = locale
    return env


def assert_tool_surface(tool_names: set[str]) -> None:
    expected_tools = {
        "execute_method",
        "list_models",
        "get_model_fields",
        "search_records",
        "read_record",
        "search_employee",
        "search_holidays",
        "diagnose_odoo_call",
        "diagnose_access",
        "inspect_model_relationships",
        "generate_json2_payload",
        "upgrade_risk_report",
        "fit_gap_report",
        "get_odoo_profile",
        "schema_catalog",
        "preview_write",
        "validate_write",
        "execute_approved_write",
        "scan_addons_source",
        "build_domain",
        "business_pack_report",
        "health_check",
        "aggregate_records",
        "chatter_post",
        "list_instances",
        "lookup_model_history",
        "read_attachment",
    }
    if not expected_tools <= tool_names:
        raise AssertionError(f"Missing MCP tools: {expected_tools - tool_names}")


def decode_tool_json(result: Any, tool_name: str) -> dict[str, Any]:
    content = result.content[0]
    if not isinstance(content, TextContent):
        raise AssertionError(f"MCP {tool_name} did not return text content")
    payload = json.loads(content.text)
    if not isinstance(payload, dict):
        raise AssertionError(f"MCP {tool_name} did not return a JSON object")
    return cast(dict[str, Any], payload)


def parse_inspector_json(stdout: str) -> dict[str, Any]:
    """Parse Inspector JSON even if npm prints non-JSON noise around it."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise AssertionError(f"Inspector did not print JSON: {stdout!r}") from None
        payload = json.loads(stdout[start : end + 1])
    if not isinstance(payload, dict):
        raise AssertionError(f"Inspector JSON was not an object: {payload!r}")
    return payload


async def mcp_stdio_smoke(
    target: VersionTarget,
    *,
    transport: str = "xmlrpc",
    api_key: str | None = None,
    username: str = ADMIN_LOGIN,
    password: str = ADMIN_PASSWORD,
) -> dict[str, Any]:
    env = mcp_env(
        target,
        transport=transport,
        api_key=api_key,
        username=username,
        password=password,
    )

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odoo_mcp"],
        env=env,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert_tool_surface(tool_names)

            resources = await session.list_resources()
            resource_uris = {str(resource.uri) for resource in resources.resources}
            if "odoo://models" not in resource_uris:
                raise AssertionError("Missing odoo://models resource")

            templates = await session.list_resource_templates()
            template_uris = {
                str(template.uri_template) for template in templates.resource_templates
            }
            expected_templates = {
                "odoo://model/{model_name}",
                "odoo://record/{model_name}/{record_id}",
                "odoo://search/{model_name}/{domain}",
            }
            if not expected_templates <= template_uris:
                raise AssertionError(
                    f"Missing MCP resource templates: {expected_templates - template_uris}"
                )

            prompts = await session.list_prompts()
            prompt_names = {prompt.name for prompt in prompts.prompts}
            expected_prompts = {
                "diagnose_failed_odoo_call",
                "fit_gap_workshop",
                "json2_migration_plan",
                "safe_write_review",
                "custom_module_audit",
            }
            if not expected_prompts <= prompt_names:
                raise AssertionError(
                    f"Missing MCP prompts: {expected_prompts - prompt_names}"
                )

            models_resource = await session.read_resource("odoo://models")
            if not models_resource.contents:
                raise AssertionError("odoo://models returned no content")

            result = await session.call_tool(
                "execute_method",
                arguments={
                    "model": "res.partner",
                    "method": "search_read",
                    "args": [[["id", ">", 0]]],
                    "kwargs": {"fields": ["id", "name"], "limit": 1},
                },
            )
            payload = decode_tool_json(result, "execute_method")
            if not payload.get("success"):
                raise AssertionError(f"MCP execute_method failed: {payload}")
            payload_result = payload.get("result")
            if not isinstance(payload_result, list):
                raise AssertionError("MCP execute_method did not return list result")

            profile = decode_tool_json(
                await session.call_tool(
                    "get_odoo_profile",
                    arguments={"include_modules": False, "module_limit": 10},
                ),
                "get_odoo_profile",
            )
            if not profile.get("success"):
                raise AssertionError(f"MCP get_odoo_profile failed: {profile}")

            catalog = decode_tool_json(
                await session.call_tool(
                    "schema_catalog",
                    arguments={"query": "partner", "limit": 5},
                ),
                "schema_catalog",
            )
            if not catalog.get("success") or catalog.get("count", 0) < 1:
                raise AssertionError(f"MCP schema_catalog failed: {catalog}")

            json2_preview = decode_tool_json(
                await session.call_tool(
                    "generate_json2_payload",
                    arguments={
                        "model": "res.partner",
                        "method": "search_read",
                        "args": [[["id", ">", 0]]],
                        "kwargs": {"fields": ["id", "name"], "limit": 1},
                        "database": target.database,
                    },
                ),
                "generate_json2_payload",
            )
            if json2_preview.get("body", {}).get("domain") != [["id", ">", 0]]:
                raise AssertionError(f"Bad JSON-2 preview body: {json2_preview}")
            if (
                json2_preview.get("headers", {}).get("X-Odoo-Database")
                != target.database
            ):
                raise AssertionError(f"Missing JSON-2 database header: {json2_preview}")

            domain = decode_tool_json(
                await session.call_tool(
                    "build_domain",
                    arguments={
                        "conditions": [{"field": "id", "operator": ">", "value": 0}]
                    },
                ),
                "build_domain",
            )
            if domain.get("domain") != [["id", ">", 0]]:
                raise AssertionError(f"Bad domain builder result: {domain}")

            diagnosis = decode_tool_json(
                await session.call_tool(
                    "diagnose_odoo_call",
                    arguments={
                        "model": "res.partner",
                        "method": "write",
                        "args": [[1], {"name": "Smoke"}],
                    },
                ),
                "diagnose_odoo_call",
            )
            if diagnosis.get("classification", {}).get("safety") != "destructive":
                raise AssertionError(
                    f"write call was not marked destructive: {diagnosis}"
                )

            relationships = decode_tool_json(
                await session.call_tool(
                    "inspect_model_relationships",
                    arguments={"model": "res.partner"},
                ),
                "inspect_model_relationships",
            )
            if "many2one" not in relationships.get("relationships", {}):
                raise AssertionError(f"Missing relationship groups: {relationships}")

            access = decode_tool_json(
                await session.call_tool(
                    "diagnose_access",
                    arguments={
                        "model": "res.partner",
                        "operation": "read",
                        "expected_count": 1,
                    },
                ),
                "diagnose_access",
            )
            if not access.get("success") or "diagnosis" not in access:
                raise AssertionError(f"diagnose_access failed: {access}")

            preview = decode_tool_json(
                await session.call_tool(
                    "preview_write",
                    arguments={
                        "model": "res.partner",
                        "operation": "write",
                        "record_ids": [1],
                        "values": {"name": "Smoke Preview"},
                    },
                ),
                "preview_write",
            )
            if not preview.get("success"):
                raise AssertionError(f"write preview failed: {preview}")

            validation = decode_tool_json(
                await session.call_tool(
                    "validate_write",
                    arguments={
                        "model": "res.partner",
                        "operation": "write",
                        "record_ids": [1],
                        "values": {"name": "Smoke Preview"},
                    },
                ),
                "validate_write",
            )
            if not validation.get("success"):
                raise AssertionError(f"write validation failed: {validation}")

            blocked_write = decode_tool_json(
                await session.call_tool(
                    "execute_approved_write",
                    arguments={
                        "approval": validation["approval"],
                        "confirm": True,
                    },
                ),
                "execute_approved_write",
            )
            if blocked_write.get("success") or "disabled" not in str(
                blocked_write.get("error")
            ):
                raise AssertionError(
                    f"approved write did not fail closed while disabled: {blocked_write}"
                )

            upgrade = decode_tool_json(
                await session.call_tool(
                    "upgrade_risk_report",
                    arguments={
                        "source_version": target.version,
                        "target_version": "20.0",
                        "methods": [{"model": "res.partner", "method": "write"}],
                    },
                ),
                "upgrade_risk_report",
            )
            if (
                upgrade.get("transport", {}).get("xmlrpc_jsonrpc_deprecation")
                != "Odoo 22 fall 2028"
            ):
                raise AssertionError(f"Bad upgrade risk transport info: {upgrade}")

            fit_gap = decode_tool_json(
                await session.call_tool(
                    "fit_gap_report",
                    arguments={"requirements": ["Track contacts"]},
                ),
                "fit_gap_report",
            )
            classifications = {
                item.get("classification") for item in fit_gap.get("items", [])
            }
            if not classifications <= {
                "standard",
                "configuration",
                "studio",
                "custom_module",
                "avoid",
                "unknown",
            }:
                raise AssertionError(f"Bad fit/gap classifications: {fit_gap}")

            pack = decode_tool_json(
                await session.call_tool(
                    "business_pack_report",
                    arguments={"pack": "sales"},
                ),
                "business_pack_report",
            )
            if not pack.get("success"):
                raise AssertionError(f"business_pack_report failed: {pack}")

            source_scan = decode_tool_json(
                await session.call_tool(
                    "scan_addons_source",
                    arguments={"addons_paths": [str(ROOT / "src")], "max_files": 20},
                ),
                "scan_addons_source",
            )
            if not source_scan.get("success"):
                raise AssertionError(f"scan_addons_source failed: {source_scan}")

            health = decode_tool_json(
                await session.call_tool("health_check", arguments={}),
                "health_check",
            )
            if health.get("server", {}).get("tool_count") != 41:
                raise AssertionError(f"health_check did not report 41 tools: {health}")
            if "chatter_direct_enabled" not in health.get("runtime", {}):
                raise AssertionError(
                    f"health_check did not surface chatter_direct posture: {health}"
                )

            # --- 0.3.0 features: smart fields, aggregate, chatter --------
            smart_search = decode_tool_json(
                await session.call_tool(
                    "search_records",
                    arguments={"model": "res.partner", "limit": 1},
                ),
                "search_records",
            )
            if not smart_search.get("success"):
                raise AssertionError(f"search_records (smart) failed: {smart_search}")
            if smart_search.get("smart_fields_applied") is not True:
                raise AssertionError(
                    "search_records did not apply smart fields when caller omitted fields"
                )
            fields_used = smart_search.get("fields_used") or []
            if "name" not in fields_used or "id" not in fields_used:
                raise AssertionError(
                    f"smart fields missing core columns: {smart_search}"
                )

            star_search = decode_tool_json(
                await session.call_tool(
                    "search_records",
                    arguments={"model": "res.partner", "limit": 1, "fields": ["*"]},
                ),
                "search_records",
            )
            if star_search.get("smart_fields_applied") is not False:
                raise AssertionError(
                    "search_records did not honour fields=['*'] opt-out"
                )

            aggregate = decode_tool_json(
                await session.call_tool(
                    "aggregate_records",
                    arguments={
                        "model": "res.partner",
                        "group_by": ["is_company"],
                        "measures": ["id:count"],
                    },
                ),
                "aggregate_records",
            )
            if not aggregate.get("success"):
                raise AssertionError(f"aggregate_records failed: {aggregate}")
            if aggregate.get("major_version") is None:
                raise AssertionError(
                    f"aggregate_records did not detect Odoo version: {aggregate}"
                )
            expected_method = (
                "formatted_read_group"
                if (aggregate.get("major_version") or 0) >= 19
                else "read_group"
            )
            if aggregate.get("method") != expected_method:
                raise AssertionError(
                    f"aggregate_records picked wrong method (expected {expected_method}): {aggregate}"
                )

            chatter_body = "Smoke chatter execute round-trip"
            chatter_subject = f"Smoke subject {transport} A & <b>B</b>"
            chatter_preview = decode_tool_json(
                await session.call_tool(
                    "chatter_post",
                    arguments={
                        "model": "res.partner",
                        "record_id": 1,
                        "body": chatter_body,
                        "subject": chatter_subject,
                    },
                ),
                "chatter_post",
            )
            if chatter_preview.get("mode") != "preview":
                raise AssertionError(
                    f"chatter_post default mode should be preview: {chatter_preview}"
                )
            if not chatter_preview.get("approval", {}).get("token", "").startswith(
                "odoo-write:"
            ):
                raise AssertionError(
                    f"chatter_post preview missing approval token: {chatter_preview}"
                )

            chatter_executed = decode_tool_json(
                await session.call_tool(
                    "chatter_post",
                    arguments={
                        "model": "res.partner",
                        "record_id": 1,
                        "body": chatter_body,
                        "subject": chatter_subject,
                        "approval": chatter_preview["approval"],
                        "confirm": True,
                    },
                ),
                "chatter_post",
            )
            if not chatter_executed.get("success"):
                raise AssertionError(
                    f"chatter_post execute failed: {chatter_executed}"
                )
            if chatter_executed.get("mode") != "execute":
                raise AssertionError(
                    f"chatter_post execute mode mismatch: {chatter_executed}"
                )
            chatter_persisted = decode_tool_json(
                await session.call_tool(
                    "search_records",
                    arguments={
                        "model": "mail.message",
                        "domain": [
                            ["model", "=", "res.partner"],
                            ["res_id", "=", 1],
                            ["body", "ilike", "round-trip"],
                            ["subject", "=", chatter_subject],
                        ],
                        "fields": ["id", "body", "subject"],
                        "limit": 5,
                    },
                ),
                "search_records",
            )
            if chatter_persisted.get("count", 0) < 1:
                raise AssertionError(
                    f"chatter_post execute did not persist a mail.message: {chatter_persisted}"
                )
            stored_subject = (chatter_persisted.get("result") or [{}])[0].get("subject")
            # Plain-text Char: stored verbatim, not HTML-escaped.
            if stored_subject != chatter_subject:
                raise AssertionError(
                    f"chatter_post subject not stored verbatim: {stored_subject!r}"
                )

            return {
                "transport": transport,
                "tools": sorted(tool_names),
                "resource_count": len(resource_uris),
                "resource_template_count": len(template_uris),
                "prompt_count": len(prompt_names),
                "mcp_partner_sample_count": len(payload_result),
                "diagnostic_tools_smoke": True,
                "agent_tools_smoke": True,
                "smart_fields_smoke": True,
                "aggregate_records_smoke": True,
                "chatter_post_smoke": True,
                "chatter_execute_smoke": True,
                "chatter_persisted_message_count": chatter_persisted.get("count", 0),
                "aggregate_method": aggregate.get("method"),
            }


async def mcp_restricted_access_smoke(
    target: VersionTarget,
    *,
    expected_uid: int,
    transport: str = "xmlrpc",
    api_key: str | None = None,
) -> dict[str, Any]:
    env = mcp_env(
        target,
        transport=transport,
        api_key=api_key,
        username=RESTRICTED_LOGIN,
        password=RESTRICTED_PASSWORD,
    )
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odoo_mcp"],
        env=env,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert_tool_surface(tool_names)
            access = decode_tool_json(
                await session.call_tool(
                    "diagnose_access",
                    arguments={
                        "model": "res.partner",
                        "operation": "read",
                        "expected_count": 1,
                    },
                ),
                "diagnose_access",
            )
            if not access.get("success"):
                raise AssertionError(f"restricted diagnose_access failed: {access}")
            current_user = access.get("current_user", {})
            if current_user.get("uid") != expected_uid:
                raise AssertionError(
                    "restricted diagnose_access did not run as the restricted user: "
                    f"{access}"
                )
            metadata_used = access.get("metadata_used", {})
            if metadata_used.get("sudo") or metadata_used.get("impersonation"):
                raise AssertionError(
                    f"restricted diagnose_access used sudo/impersonation: {access}"
                )
            diagnosis = access.get("diagnosis", {})
            codes = [
                str(item.get("code"))
                for item in diagnosis.get("codes", [])
                if isinstance(item, dict)
            ]
            if not codes:
                raise AssertionError(f"restricted diagnose_access had no codes: {access}")
            return {
                "transport": transport,
                "uid": current_user.get("uid"),
                "group_field": current_user.get("group_field"),
                "all_group_field": current_user.get("all_group_field"),
                "metadata_error_count": len(access.get("metadata_errors", [])),
                "diagnosis_codes": codes,
                "actual_count": access.get("actual_count"),
            }


async def mcp_complex_record_rule_smoke(
    target: VersionTarget,
    *,
    fixture: dict[str, Any],
    transport: str = "xmlrpc",
    api_key: str | None = None,
    username: str = RULE_AUDITOR_LOGIN,
    password: str = RULE_AUDITOR_PASSWORD,
) -> dict[str, Any]:
    env = mcp_env(
        target,
        transport=transport,
        api_key=api_key,
        username=username,
        password=password,
    )
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odoo_mcp"],
        env=env,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert_tool_surface(tool_names)
            record_ids = [int(record_id) for record_id in fixture["record_ids"]]
            access = decode_tool_json(
                await session.call_tool(
                    "diagnose_access",
                    arguments={
                        "model": "res.partner",
                        "operation": "read",
                        "record_ids": record_ids,
                        "expected_count": int(fixture["expected_count"]),
                        "include_rules": True,
                    },
                ),
                "diagnose_access",
            )
            if not access.get("success"):
                raise AssertionError(f"complex-rule diagnose_access failed: {access}")
            current_user = access.get("current_user", {})
            if current_user.get("uid") != int(fixture["uid"]):
                raise AssertionError(
                    "complex-rule diagnose_access did not run as fixture user: "
                    f"{access}"
                )
            if access.get("actual_count") != int(fixture["visible_count"]):
                raise AssertionError(
                    "complex-rule diagnose_access did not observe rule-filtered "
                    f"count: {access}"
                )
            metadata_errors = access.get("metadata_errors", [])
            metadata_error_stages = [
                str(error.get("stage"))
                for error in metadata_errors
                if isinstance(error, dict)
            ]
            unexpected_metadata_errors = [
                error
                for error in metadata_errors
                if not (
                    isinstance(error, dict)
                    and error.get("stage") == "res.users.read"
                )
            ]
            if unexpected_metadata_errors:
                raise AssertionError(
                    "complex-rule diagnose_access had unexpected metadata errors: "
                    f"{access}"
                )
            metadata_used = access.get("metadata_used", {})
            if not metadata_used.get("acl") or not metadata_used.get("rules"):
                raise AssertionError(
                    "complex-rule diagnose_access did not read ACL/rule metadata: "
                    f"{access}"
                )
            if metadata_used.get("sudo") or metadata_used.get("impersonation"):
                raise AssertionError(
                    f"complex-rule diagnose_access used sudo/impersonation: {access}"
                )
            diagnosis = access.get("diagnosis", {})
            codes = [
                str(item.get("code"))
                for item in diagnosis.get("codes", [])
                if isinstance(item, dict)
            ]
            if "record_rule_filter_likely" not in codes:
                raise AssertionError(
                    f"complex-rule diagnose_access missed rule diagnosis: {access}"
                )
            rule_names = [
                str(rule.get("name"))
                for rule in access.get("rules", {}).get("active", [])
                if isinstance(rule, dict)
            ]
            if fixture["rule_name"] not in rule_names:
                raise AssertionError(
                    f"complex-rule diagnose_access did not report seeded rule: {access}"
                )
            return {
                "transport": transport,
                "uid": current_user.get("uid"),
                "record_ids": record_ids,
                "expected_count": access.get("expected_count"),
                "actual_count": access.get("actual_count"),
                "diagnosis_codes": codes,
                "rule_name": fixture["rule_name"],
                "rule_id": fixture["rule_id"],
                "domain_force": fixture["domain_force"],
                "active_rule_names": rule_names,
                "group_field": current_user.get("group_field"),
                "all_group_field": current_user.get("all_group_field"),
                "metadata_error_count": len(metadata_errors),
                "metadata_error_stages": metadata_error_stages,
            }


async def mcp_streamable_http_smoke(
    target: VersionTarget,
    *,
    transport: str = "xmlrpc",
    api_key: str | None = None,
    inspector_smoke: bool = False,
) -> dict[str, Any]:
    env = mcp_env(target, transport=transport, api_key=api_key)
    env.update(
        {
            "MCP_TRANSPORT": "streamable-http",
            "MCP_HTTP_HOST": "127.0.0.1",
            "MCP_HTTP_PORT": str(target.mcp_port),
            "MCP_HTTP_PATH": "/mcp",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "odoo_mcp",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(target.mcp_port),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        url = f"http://127.0.0.1:{target.mcp_port}/mcp"
        deadline = time.monotonic() + 60
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                async with streamable_http_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        tool_names = {tool.name for tool in tools.tools}
                        assert_tool_surface(tool_names)
                        result = await session.call_tool(
                            "search_records",
                            arguments={
                                "model": "res.partner",
                                "domain": [["id", ">", 0]],
                                "fields": ["id", "name"],
                                "limit": 1,
                            },
                        )
                        content = result.content[0]
                        if not isinstance(content, TextContent):
                            raise AssertionError(
                                "MCP HTTP search_records did not return text content"
                            )
                        payload = json.loads(content.text)
                        if not payload.get("success"):
                            raise AssertionError(
                                f"MCP HTTP search_records failed: {payload}"
                            )
                        inspector_result = (
                            run_inspector_http_tools_list(target)
                            if inspector_smoke
                            else None
                        )
                        response: dict[str, Any] = {
                            "transport": "streamable-http",
                            "odoo_transport": transport,
                            "tools": sorted(tool_names),
                            "http_partner_sample_count": payload.get("count"),
                        }
                        if inspector_result:
                            response["inspector_http"] = inspector_result
                        return response
            except Exception as exc:
                last_error = exc
                if process.poll() is not None:
                    _, stderr = process.communicate(timeout=5)
                    raise AssertionError(
                        f"MCP HTTP server exited early with {process.returncode}: {stderr}"
                    ) from exc
                await asyncio.sleep(1)
        raise TimeoutError(f"MCP HTTP smoke did not become ready: {last_error}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)


def run_inspector_stdio_tools_list(
    target: VersionTarget, *, transport: str = "xmlrpc", api_key: str | None = None
) -> dict[str, Any]:
    env = mcp_env(target, transport=transport, api_key=api_key)
    completed = run(
        [
            "npx",
            "--yes",
            "@modelcontextprotocol/inspector@2",
            "--cli",
            sys.executable,
            "-m",
            "odoo_mcp",
            "--",
            "--method",
            "tools/list",
            "--transport",
            "stdio",
        ],
        env=env,
        timeout=120,
        capture_output=True,
    )
    payload = parse_inspector_json(completed.stdout)
    tool_names = {tool["name"] for tool in payload.get("tools", [])}
    assert_tool_surface(tool_names)
    return {"transport": "stdio", "tool_count": len(tool_names)}


def run_inspector_http_tools_list(target: VersionTarget) -> dict[str, Any]:
    completed = run(
        [
            "npx",
            "--yes",
            "@modelcontextprotocol/inspector@2",
            "--cli",
            "--method",
            "tools/list",
            "--transport",
            "http",
            "--server-url",
            f"http://127.0.0.1:{target.mcp_port}/mcp",
        ],
        env=os.environ.copy(),
        timeout=120,
        capture_output=True,
    )
    payload = parse_inspector_json(completed.stdout)
    tool_names = {tool["name"] for tool in payload.get("tools", [])}
    assert_tool_surface(tool_names)
    return {"transport": "streamable-http", "tool_count": len(tool_names)}


async def mcp_locale_smoke(
    target: VersionTarget,
    *,
    locale: str = "en_US",
    transport: str = "xmlrpc",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Verify ODOO_LOCALE plumbing does not break basic MCP operations."""
    env = mcp_env(target, transport=transport, api_key=api_key, locale=locale)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odoo_mcp"],
        env=env,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            profile = decode_tool_json(
                await session.call_tool(
                    "get_odoo_profile",
                    arguments={"include_modules": False, "module_limit": 5},
                ),
                "get_odoo_profile",
            )
            if not profile.get("success"):
                raise AssertionError(
                    f"get_odoo_profile failed under ODOO_LOCALE={locale}: {profile}"
                )
            record = decode_tool_json(
                await session.call_tool(
                    "read_record",
                    arguments={"model": "res.partner", "record_id": 1},
                ),
                "read_record",
            )
            if not record.get("success"):
                raise AssertionError(
                    f"read_record failed under ODOO_LOCALE={locale}: {record}"
                )
    return {
        "locale": locale,
        "transport": transport,
        "profile_ok": True,
        "read_record_ok": True,
    }


def smoke_one(
    target: VersionTarget,
    keep_stack: bool,
    timeout_seconds: int,
    inspector_smoke: bool,
    http_smoke: bool,
) -> dict[str, Any]:
    env = compose_env(target)
    run(compose_cmd(target, "down", "-v", "--remove-orphans"), env=env, check=False)
    try:
        run(compose_cmd(target, "up", "-d", "db"), env=env, timeout=300, attempts=3)
        run(compose_cmd(target, "pull", "odoo"), env=env, timeout=900, attempts=3)
        init_database(target, env)
        restricted_user = create_restricted_user(target, env)
        json2_api_key = (
            generate_json2_api_key(target, env)
            if target.version.startswith("19.")
            else None
        )
        restricted_json2_api_key = (
            generate_json2_api_key(target, env, login=RESTRICTED_LOGIN)
            if target.version.startswith("19.")
            else None
        )
        run(compose_cmd(target, "up", "-d", "odoo"), env=env, timeout=300)
        wait_for_http(target.port, timeout_seconds)
        uid = wait_for_xmlrpc(target, timeout_seconds)
        direct = direct_xmlrpc_smoke(target, uid)
        direct_json2 = (
            direct_json2_smoke(target, json2_api_key) if json2_api_key else None
        )
        mcp_result = asyncio.run(mcp_stdio_smoke(target, transport="xmlrpc"))
        restricted_access = asyncio.run(
            mcp_restricted_access_smoke(
                target,
                expected_uid=int(restricted_user["uid"]),
                transport="xmlrpc",
            )
        )
        mcp_json2_result = (
            asyncio.run(
                mcp_stdio_smoke(target, transport="json2", api_key=json2_api_key)
            )
            if json2_api_key
            else None
        )
        restricted_access_json2 = (
            asyncio.run(
                mcp_restricted_access_smoke(
                    target,
                    expected_uid=int(restricted_user["uid"]),
                    transport="json2",
                    api_key=restricted_json2_api_key,
                )
            )
            if restricted_json2_api_key
            else None
        )
        locale_result = asyncio.run(
            mcp_locale_smoke(target, locale="en_US", transport="xmlrpc")
        )
        mcp_http_result = (
            asyncio.run(
                mcp_streamable_http_smoke(
                    target,
                    transport="json2" if json2_api_key else "xmlrpc",
                    api_key=json2_api_key,
                    inspector_smoke=inspector_smoke,
                )
            )
            if http_smoke
            else None
        )
        inspector_stdio_result = (
            run_inspector_stdio_tools_list(
                target,
                transport="json2" if json2_api_key else "xmlrpc",
                api_key=json2_api_key,
            )
            if inspector_smoke
            else None
        )
        result = {
            "version": target.version,
            "project": target.project,
            "port": target.port,
            "mcp_port": target.mcp_port,
            "database": target.database,
            "uid": uid,
            "direct_xmlrpc": direct,
            "mcp_stdio": mcp_result,
            "restricted_user": restricted_user,
            "restricted_access_xmlrpc": restricted_access,
            "status": "passed",
        }
        if direct_json2:
            result["direct_json2"] = direct_json2
        if mcp_json2_result:
            result["mcp_stdio_json2"] = mcp_json2_result
        if restricted_access_json2:
            result["restricted_access_json2"] = restricted_access_json2
        if mcp_http_result:
            result["mcp_streamable_http"] = mcp_http_result
        if inspector_stdio_result:
            result["inspector_stdio"] = inspector_stdio_result
        if locale_result:
            result["locale_smoke"] = locale_result

        complex_rule_fixture = create_complex_record_rule_fixture(target, env)
        complex_rule_access = asyncio.run(
            mcp_complex_record_rule_smoke(
                target,
                fixture=complex_rule_fixture,
                transport="xmlrpc",
            )
        )
        result["complex_record_rule_fixture"] = complex_rule_fixture
        result["complex_record_rule_xmlrpc"] = complex_rule_access
        if target.version.startswith("19."):
            rule_auditor_json2_api_key = generate_json2_api_key(
                target, env, login=RULE_AUDITOR_LOGIN
            )
            result["complex_record_rule_json2"] = asyncio.run(
                mcp_complex_record_rule_smoke(
                    target,
                    fixture=complex_rule_fixture,
                    transport="json2",
                    api_key=rule_auditor_json2_api_key,
                )
            )
        deactivate_record_rule(target, env, RULE_SMOKE_RULE_NAME)

        packaged_addon_lifecycle = run_packaged_addon_lifecycle(target, env)
        packaged_rule_fixture = create_packaged_addon_rule_fixture(target, env)
        packaged_json2_api_key = (
            generate_json2_api_key(target, env, login=PACKAGED_AUDITOR_LOGIN)
            if target.version.startswith("19.")
            else None
        )
        run(compose_cmd(target, "restart", "odoo"), env=env, timeout=300)
        wait_for_http(target.port, timeout_seconds)
        wait_for_xmlrpc(target, timeout_seconds)
        packaged_rule_access = asyncio.run(
            mcp_complex_record_rule_smoke(
                target,
                fixture=packaged_rule_fixture,
                transport="xmlrpc",
                username=PACKAGED_AUDITOR_LOGIN,
                password=PACKAGED_AUDITOR_PASSWORD,
            )
        )
        result["packaged_addon_lifecycle"] = packaged_addon_lifecycle
        result["packaged_record_rule_fixture"] = packaged_rule_fixture
        result["packaged_record_rule_xmlrpc"] = packaged_rule_access
        if packaged_json2_api_key:
            result["packaged_record_rule_json2"] = asyncio.run(
                mcp_complex_record_rule_smoke(
                    target,
                    fixture=packaged_rule_fixture,
                    transport="json2",
                    api_key=packaged_json2_api_key,
                    username=PACKAGED_AUDITOR_LOGIN,
                    password=PACKAGED_AUDITOR_PASSWORD,
                )
            )
        return result
    finally:
        if not keep_stack:
            run(
                compose_cmd(target, "down", "-v", "--remove-orphans"),
                env=env,
                check=False,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live Docker Compose Odoo smoke tests across versions."
    )
    parser.add_argument(
        "--versions",
        nargs="+",
        default=list(DEFAULT_VERSIONS),
        help="Odoo Docker tags to test, default: %(default)s",
    )
    parser.add_argument(
        "--keep-stack",
        action="store_true",
        help="Keep Compose services and volumes after the run for debugging.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=240,
        help="Readiness timeout per Odoo service in seconds.",
    )
    parser.add_argument(
        "--skip-http-smoke",
        action="store_true",
        help="Skip Streamable HTTP MCP smoke. By default Odoo 19.0 includes it.",
    )
    parser.add_argument(
        "--inspector-smoke",
        action="store_true",
        help="Run MCP Inspector CLI tools/list checks in addition to Python SDK smoke.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not COMPOSE_FILE.exists():
        raise FileNotFoundError(COMPOSE_FILE)

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, version in enumerate(args.versions):
        target = make_target(version, index)
        print(f"\n=== Odoo {version} real smoke ===", flush=True)
        try:
            http_smoke = version.startswith("19.") and not args.skip_http_smoke
            result = smoke_one(
                target,
                args.keep_stack,
                args.timeout,
                args.inspector_smoke,
                http_smoke,
            )
            results.append(result)
            print(json.dumps(result, indent=2, sort_keys=True), flush=True)
        except Exception as exc:
            failures.append({"version": version, "error": repr(exc)})
            print(f"FAILED {version}: {exc!r}", file=sys.stderr, flush=True)
            if args.keep_stack:
                print(
                    f"Stack kept for debugging: COMPOSE_PROJECT_NAME={target.project}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                run(
                    compose_cmd(target, "down", "-v", "--remove-orphans"),
                    env=compose_env(target),
                    check=False,
                )

    summary = {"passed": results, "failed": failures}
    print("\n=== smoke summary ===")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
