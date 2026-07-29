"""Static AST/XML scanner for local Odoo addon source."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

WRITE_OPERATIONS = {"create", "write", "unlink"}


def scan_addons_source_report(
    *,
    addons_paths: list[str] | None = None,
    max_files: int = 200,
    max_file_bytes: int = 300_000,
) -> dict[str, Any]:
    """Scan local Odoo addon source without importing addon code."""
    return _scan_addons_source_report(
        paths=_normalize_scan_paths(addons_paths),
        max_files=max_files,
        max_file_bytes=max_file_bytes,
    )


def _scan_addons_source_report(
    *,
    paths: list[str],
    max_files: int,
    max_file_bytes: int,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    scanned_files = 0
    skipped_files = 0

    for root in paths:
        if scanned_files >= max_files:
            break
        root_path = Path(root).expanduser()
        if not root_path.exists():
            findings.append(
                {
                    "code": "addons_path_missing",
                    "severity": "warning",
                    "evidence": str(root_path),
                    "recommendation": "Check ODOO_ADDONS_PATHS or pass addons_paths explicitly.",
                }
            )
            continue
        for file_path in root_path.rglob("*"):
            if scanned_files >= max_files:
                break
            if not file_path.is_file() or file_path.is_symlink():
                continue
            if (
                file_path.suffix not in {".py", ".xml", ".csv"}
                and file_path.name != "__manifest__.py"
            ):
                continue
            try:
                if file_path.stat().st_size > max_file_bytes:
                    skipped_files += 1
                    continue
            except OSError:
                skipped_files += 1
                continue
            scanned_files += 1
            relative = str(file_path)
            if file_path.name == "__manifest__.py":
                manifest = _read_manifest(file_path)
                if manifest:
                    modules.append(manifest)
                    if manifest.get("installable") is False:
                        findings.append(
                            {
                                "code": "non_installable_module",
                                "severity": "info",
                                "evidence": relative,
                                "recommendation": "Confirm whether this module should be considered during upgrade.",
                            }
                        )
            elif file_path.suffix == ".py":
                findings.extend(_scan_python_file(file_path))
            elif file_path.suffix == ".xml":
                findings.extend(_scan_xml_file(file_path))
            elif file_path.suffix == ".csv" and "security" in file_path.parts:
                findings.append(
                    {
                        "code": "security_rule_file",
                        "severity": "info",
                        "evidence": relative,
                        "recommendation": "Review access CSV rules during upgrade testing.",
                    }
                )

    from .diagnostics import annotate_finding_actions

    action_summary = annotate_finding_actions(findings)
    return {
        "success": True,
        "tool": "scan_addons_source",
        "paths": paths,
        "summary": {
            "modules": len(modules),
            "findings": len(findings),
            "scanned_files": scanned_files,
            "skipped_files": skipped_files,
            "max_files_reached": scanned_files >= max_files,
            "actions": action_summary,
        },
        "modules": modules,
        "source_findings": findings,
        "metadata_used": {"source_scan": True},
    }


def _normalize_scan_paths(addons_paths: list[str] | None) -> list[str]:
    if addons_paths:
        return [path for path in addons_paths if path]
    env_value = os.environ.get("ODOO_ADDONS_PATHS", "")
    return [path for path in env_value.split(os.pathsep) if path]


def _read_manifest(file_path: Path) -> dict[str, Any] | None:
    try:
        raw = file_path.read_text(encoding="utf-8")
        parsed = ast.literal_eval(raw)
    except (OSError, SyntaxError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    module_dir = file_path.parent
    return {
        "name": str(parsed.get("name", module_dir.name)),
        "module": module_dir.name,
        "version": str(parsed.get("version", "")),
        "depends": (
            list(parsed.get("depends", []))
            if isinstance(parsed.get("depends", []), list)
            else []
        ),
        "installable": parsed.get("installable", True),
        "path": str(file_path),
        "custom": not str(module_dir.name).startswith(("base", "web", "mail")),
    }


def _scan_python_file(file_path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        raw = file_path.read_text(encoding="utf-8")
        tree = ast.parse(raw)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return [
            {
                "code": "python_parse_error",
                "severity": "warning",
                "evidence": f"{file_path}: {exc}",
                "recommendation": "Inspect this file manually before upgrade.",
            }
        ]

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            inherit_names = [_expr_name(base) for base in node.bases]
            if any(
                name.endswith(("models.Model", "Model", "TransientModel"))
                for name in inherit_names
            ):
                findings.append(
                    {
                        "code": "custom_model_class",
                        "severity": "info",
                        "evidence": f"{file_path}:{node.lineno} class {node.name}",
                        "recommendation": "Review model fields, constraints, computes, and overrides.",
                    }
                )
                findings.extend(_scan_model_class(file_path, node))
        elif isinstance(node, ast.FunctionDef):
            if node.name in {"create", "write", "unlink"} or node.name.startswith(
                "action_"
            ):
                findings.append(
                    {
                        "code": "custom_method",
                        "severity": (
                            "warning"
                            if node.name in {"create", "write", "unlink"}
                            else "info"
                        ),
                        "evidence": f"{file_path}:{node.lineno} def {node.name}",
                        "recommendation": "Review side effects and JSON-2 named-argument compatibility.",
                    }
                )
    if re.search(r"\.sudo\(\)", raw):
        findings.append(
            {
                "code": "sudo_usage",
                "severity": "warning",
                "evidence": str(file_path),
                "recommendation": "Review privilege escalation and record-rule assumptions.",
            }
        )
    return findings


def _scan_model_class(file_path: Path, node: ast.ClassDef) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    computed_fields = _computed_fields_by_method(node)
    functions = {
        item.name: item for item in node.body if isinstance(item, ast.FunctionDef)
    }
    for method_name, field_names in computed_fields.items():
        method_node = functions.get(method_name)
        field_list = ", ".join(sorted(field_names))
        if method_node is None:
            findings.append(
                {
                    "code": "computed_method_missing",
                    "severity": "warning",
                    "evidence": f"{file_path}:{node.lineno} compute={method_name!r}",
                    "recommendation": (
                        f"Define {method_name} or update computed fields: {field_list}."
                    ),
                }
            )
            continue

        depends = _api_depends_arguments(method_node)
        if not depends:
            findings.append(
                {
                    "code": "computed_method_missing_depends",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_name}",
                    "recommendation": (
                        f"Add @api.depends(...) for computed fields: {field_list}."
                    ),
                }
            )
            continue

        read_fields = _record_field_reads(method_node)
        declared_roots = {dependency.split(".", 1)[0] for dependency in depends}
        ignored = set(field_names) | {"env", "id", "ids", "display_name"}
        missing = sorted(read_fields - declared_roots - ignored)
        if missing:
            findings.append(
                {
                    "code": "computed_depends_missing_fields",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_name}",
                    "recommendation": (
                        "Review @api.depends for fields read in the compute method: "
                        + ", ".join(missing)
                        + "."
                    ),
                }
            )

    for method_node in functions.values():
        if method_node.name not in WRITE_OPERATIONS:
            continue
        super_call = _super_method_call(method_node, method_node.name)
        if super_call is None:
            findings.append(
                {
                    "code": "crud_override_missing_super",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_node.name}",
                    "recommendation": (
                        f"Call super().{method_node.name}(...) unless this override "
                        "intentionally replaces the full ORM chain."
                    ),
                }
            )
        elif not _super_call_returned(method_node, method_node.name):
            findings.append(
                {
                    "code": "crud_override_super_not_returned",
                    "severity": "warning",
                    "evidence": f"{file_path}:{method_node.lineno} def {method_node.name}",
                    "recommendation": (
                        f"Return the result from super().{method_node.name}(...) "
                        "or document why the ORM return contract is intentionally changed."
                    ),
                }
            )
    return findings


def _computed_fields_by_method(node: ast.ClassDef) -> dict[str, set[str]]:
    computed_fields: dict[str, set[str]] = {}
    for statement in node.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        if value is None:
            continue
        compute_method = _field_compute_method(value)
        if compute_method is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                computed_fields.setdefault(compute_method, set()).add(target.id)
    return computed_fields


def _field_compute_method(expr: ast.expr) -> str | None:
    if not isinstance(expr, ast.Call):
        return None
    if not _expr_name(expr.func).startswith("fields."):
        return None
    for keyword in expr.keywords:
        if keyword.arg == "compute":
            if isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, str
            ):
                return keyword.value.value
            return "<dynamic-compute>"
    return None


def _api_depends_arguments(node: ast.FunctionDef) -> set[str]:
    depends: set[str] = set()
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if _expr_name(decorator.func) != "api.depends":
            continue
        for arg in decorator.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                depends.add(arg.value)
    return depends


def _record_field_reads(node: ast.FunctionDef) -> set[str]:
    record_names = {"self"}
    for child in ast.walk(node):
        if (
            isinstance(child, ast.For)
            and isinstance(child.target, ast.Name)
            and isinstance(child.iter, ast.Name)
            and child.iter.id == "self"
        ):
            record_names.add(child.target.id)

    reads: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute) or not isinstance(child.ctx, ast.Load):
            continue
        if isinstance(child.value, ast.Name) and child.value.id in record_names:
            reads.add(child.attr)
    return reads


def _super_method_call(node: ast.FunctionDef, method_name: str) -> ast.Call | None:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _is_super_method_call(child, method_name):
            return child
    return None


def _super_call_returned(node: ast.FunctionDef, method_name: str) -> bool:
    assigned_super_vars: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and _contains_super_method_call(
            child.value, method_name
        ):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    assigned_super_vars.add(target.id)
        elif isinstance(child, ast.AnnAssign) and _contains_super_method_call(
            child.value, method_name
        ):
            if isinstance(child.target, ast.Name):
                assigned_super_vars.add(child.target.id)

    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        if _contains_super_method_call(child.value, method_name):
            return True
        if isinstance(child.value, ast.Name) and child.value.id in assigned_super_vars:
            return True
    return False


def _contains_super_method_call(expr: ast.AST | None, method_name: str) -> bool:
    if expr is None:
        return False
    return any(_is_super_method_call(child, method_name) for child in ast.walk(expr))


def _is_super_method_call(node: ast.AST, method_name: str) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != method_name:
        return False
    value = func.value
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "super"
    )


def _scan_xml_file(file_path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        tree = ElementTree.parse(file_path)
    except (OSError, ElementTree.ParseError) as exc:
        return [
            {
                "code": "xml_parse_error",
                "severity": "warning",
                "evidence": f"{file_path}: {exc}",
                "recommendation": "Inspect this XML file manually before upgrade.",
            }
        ]
    for record in tree.findall(".//record"):
        model = record.attrib.get("model", "")
        if model in {"ir.cron", "base.automation", "ir.actions.server"}:
            findings.append(
                {
                    "code": "automated_action",
                    "severity": "warning",
                    "evidence": f"{file_path}: record model={model}",
                    "recommendation": "Verify automated actions and server actions on staging.",
                }
            )
        elif model.startswith("ir.ui.view"):
            findings.append(
                {
                    "code": "custom_view",
                    "severity": "info",
                    "evidence": f"{file_path}: record model={model}",
                    "recommendation": "Review view inheritance after Odoo upgrades.",
                }
            )
    return findings


def _expr_name(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        value = _expr_name(expr.value)
        return f"{value}.{expr.attr}" if value else expr.attr
    return ""
