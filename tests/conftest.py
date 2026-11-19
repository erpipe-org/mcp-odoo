import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_developer_odoo_config(monkeypatch, tmp_path):
    """Pin every test to the CI baseline: no Odoo config present on the box.

    A developer machine legitimately carries a real instances config at a
    default discovery path (~/.config/odoo/config.json) plus ODOO_* variables.
    With one present, load_instances_config() switches instance resolution
    from the permissive no-config fallback to validated named instances, and
    the suite starts depending on whatever the developer configured (e.g.
    index_knowledge resolving the developer's default instance while
    search_knowledge resolves 'default'). Pointing ODOO_CONFIG_FILE at a
    missing file makes _config_file_paths() raise FileNotFoundError exactly
    like a bare CI runner. Tests that exercise real configs set the variable
    (or clear it and chdir into a tmp dir) themselves, which overrides this.
    """
    for var in ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ODOO_CONFIG_FILE", str(tmp_path / "no-odoo-config.json"))


@pytest.fixture
def odoo_client_module():
    module = importlib.import_module("odoo_mcp.odoo_client")
    return importlib.reload(module)


@pytest.fixture(autouse=True)
def _reset_model_policy_cache():
    """The model ACL policy is cached per process; never leak it across tests."""
    from odoo_mcp.model_policy import reset_model_policy

    reset_model_policy()
    yield
    reset_model_policy()
