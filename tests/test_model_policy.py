"""Unit tests for the model/method ACL policy (parsing, matching, precedence)."""

import json

import pytest

from odoo_mcp.model_policy import (
    ModelAccessDenied,
    ModelPolicyError,
    _parse_model_policy,
    enforce_model_acl,
    get_model_policy,
    load_model_policy,
    metadata_scope,
    model_policy_posture,
    reset_model_policy,
)


def policy(instance_spec, instance="default"):
    return _parse_model_policy({"model_acl": {instance: instance_spec}})


def allowed(p, model, method, instance="default", **kw):
    return p.decide(instance, model, method, **kw).allowed


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def test_deny_all_except():
    p = policy({"default": "deny", "rules": [{"allow": "project.*"}]})
    assert allowed(p, "project.project", "read")
    assert allowed(p, "project.task", "message_subscribe")
    assert not allowed(p, "res.partner", "read")


def test_allow_all_except():
    p = policy(
        {"default": "allow", "rules": [{"deny": "project.project:message_subscribe"}]}
    )
    assert not allowed(p, "project.project", "message_subscribe")
    assert allowed(p, "project.project", "write")
    assert allowed(p, "project.task", "message_subscribe")


def test_no_policy_passes_everything():
    p = _parse_model_policy({})
    assert not p.active()
    decision = p.decide("default", "res.partner", "unlink")
    assert decision.allowed and decision.source == "no_policy"


def test_instances_are_isolated():
    p = policy({"default": "deny"}, instance="alpha")
    assert not allowed(p, "res.partner", "read", instance="alpha")
    assert allowed(p, "res.partner", "read", instance="default")


# --------------------------------------------------------------------------
# Pattern grammar
# --------------------------------------------------------------------------


def test_bare_model_means_all_methods():
    p = policy({"default": "deny", "rules": [{"allow": "project.project"}]})
    assert allowed(p, "project.project", "action_archive")


def test_model_glob_covers_dotted_submodels():
    """`project.*` matches project.project.stage (a real Odoo model)."""
    p = policy({"default": "deny", "rules": [{"allow": "project.*:@read"}]})
    assert allowed(p, "project.project.stage", "search_read")


def test_exact_model_does_not_match_submodel():
    p = policy({"default": "deny", "rules": [{"allow": "project.project:*"}]})
    assert allowed(p, "project.project", "read")
    assert not allowed(p, "project.project.stage", "read")


def test_method_groups():
    p = policy(
        {
            "default": "deny",
            "rules": [
                {"allow": "res.partner:@read"},
                {"allow": "project.task:@write"},
            ],
        }
    )
    for method in ("search_read", "read", "read_group", "fields_get", "search_count"):
        assert allowed(p, "res.partner", method), method
    assert not allowed(p, "res.partner", "write")
    assert not allowed(p, "res.partner", "message_subscribe")
    for method in ("create", "write", "unlink"):
        assert allowed(p, "project.task", method), method
    assert not allowed(p, "project.task", "read")


def test_method_glob():
    p = policy({"default": "deny", "rules": [{"allow": "sale.order:action_*"}]})
    assert allowed(p, "sale.order", "action_confirm")
    assert not allowed(p, "sale.order", "button_validate")


def test_list_of_patterns_in_one_rule():
    p = policy(
        {"default": "deny", "rules": [{"allow": ["res.partner:read", "res.users:read"]}]}
    )
    assert allowed(p, "res.partner", "read")
    assert allowed(p, "res.users", "read")


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------


def test_more_specific_model_wins():
    p = policy(
        {
            "default": "deny",
            "rules": [
                {"deny": "project.*:@write"},
                {"allow": "project.task:@write"},
            ],
        }
    )
    assert allowed(p, "project.task", "write")
    assert not allowed(p, "project.project", "write")


def test_exception_inside_exception():
    p = policy(
        {
            "default": "deny",
            "rules": [
                {"allow": "project.*"},
                {"deny": "project.project:unlink"},
            ],
        }
    )
    assert allowed(p, "project.project", "write")
    assert not allowed(p, "project.project", "unlink")
    assert allowed(p, "project.task", "unlink")


def test_exact_method_beats_group_on_same_model():
    p = policy(
        {
            "default": "allow",
            "rules": [
                {"deny": "res.partner:@write"},
                {"allow": "res.partner:write"},
            ],
        }
    )
    assert allowed(p, "res.partner", "write")
    assert not allowed(p, "res.partner", "unlink")


def test_group_beats_star_on_same_model():
    p = policy(
        {
            "default": "allow",
            "rules": [
                {"deny": "res.partner:*"},
                {"allow": "res.partner:@read"},
            ],
        }
    )
    assert allowed(p, "res.partner", "read")
    assert not allowed(p, "res.partner", "write")


def test_tie_goes_to_deny():
    p = policy(
        {
            "default": "allow",
            "rules": [
                {"allow": "res.partner:read"},
                {"deny": "res.partner:read"},
            ],
        }
    )
    assert not allowed(p, "res.partner", "read")


def test_decision_names_the_matched_rule():
    p = policy(
        {
            "default": "deny",
            "rules": [{"deny": "project.project:unlink", "reason": "never via agents"}],
        }
    )
    decision = p.decide("default", "project.project", "unlink")
    assert decision.rule is not None
    message = decision.message()
    assert "deny project.project:unlink" in message
    assert "never via agents" in message
    default_decision = p.decide("default", "res.partner", "read")
    assert "instance default" in default_decision.message()


def test_explicitly_allowed_only_for_exact_rules():
    p = policy(
        {
            "default": "allow",
            "rules": [
                {"allow": "sale.order:action_confirm"},
                {"allow": "sale.*:action_cancel"},
                {"allow": "*:message_subscribe"},
                {"allow": "stock.picking:button_*"},
                {"allow": "project.task:@write"},
                {"allow": "res.partner:message_post"},
                {"deny": "res.partner:message_post"},
            ],
        }
    )
    assert p.explicitly_allowed("default", "sale.order", "action_confirm")
    assert not p.explicitly_allowed("default", "sale.order", "action_cancel")
    assert not p.explicitly_allowed("default", "project.task", "message_subscribe")
    assert not p.explicitly_allowed("default", "stock.picking", "button_validate")
    assert not p.explicitly_allowed("default", "project.task", "write")
    assert not p.explicitly_allowed("default", "account.move", "action_post")  # default
    assert not p.explicitly_allowed("default", "res.partner", "message_post")  # deny tie
    assert not p.explicitly_allowed("other", "sale.order", "action_confirm")  # no policy


# --------------------------------------------------------------------------
# Visibility + metadata exemption
# --------------------------------------------------------------------------


def test_model_visible():
    p = policy(
        {
            "default": "deny",
            "rules": [
                {"allow": "project.*:@read"},
                {"allow": "res.partner:message_subscribe"},
                {"allow": "sale.order:action_*"},
            ],
        }
    )
    assert p.model_visible("default", "project.task")
    assert p.model_visible("default", "res.partner")
    assert p.model_visible("default", "sale.order")
    assert not p.model_visible("default", "account.move")
    assert p.filter_models("default", ["account.move", "project.task"]) == [
        "project.task"
    ]


def test_model_hidden_under_allow_default_when_fully_denied():
    p = policy({"default": "allow", "rules": [{"deny": "hr.*"}]})
    assert not p.model_visible("default", "hr.employee")
    assert p.model_visible("default", "res.partner")


def test_metadata_exempt_by_default():
    p = policy({"default": "deny"})
    assert allowed(p, "ir.model", "search_read", metadata=True)
    assert not allowed(p, "ir.model", "search_read")


def test_metadata_enforce():
    p = policy({"default": "deny", "metadata": "enforce"})
    assert not allowed(p, "ir.model", "search_read", metadata=True)


# --------------------------------------------------------------------------
# Validation fails closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "not-an-object",
        {},  # missing default
        {"default": "maybe"},
        {"default": "deny", "rules": "project.*"},
        {"default": "deny", "rules": [{"allow": "a", "deny": "b"}]},
        {"default": "deny", "rules": [{"alow": "project.*"}]},
        {"default": "deny", "rules": [{"allow": "project.*:@unknown"}]},
        {"default": "deny", "rules": [{"allow": "project project"}]},
        {"default": "deny", "rules": [{"allow": "project.*:do.thing"}]},
        {"default": "deny", "rules": [{"allow": ""}]},
        {"default": "deny", "rules": [{"allow": []}]},
        {"default": "deny", "rules": [{"allow": "x", "reason": 3}]},
        {"default": "deny", "metadata": "sometimes"},
        {"default": "deny", "unexpected": True},
    ],
)
def test_malformed_policy_raises(spec):
    with pytest.raises(ModelPolicyError):
        policy(spec)


def test_model_acl_must_be_object():
    with pytest.raises(ModelPolicyError):
        _parse_model_policy({"model_acl": []})


# --------------------------------------------------------------------------
# Loading, caching, choke point, posture
# --------------------------------------------------------------------------


@pytest.fixture
def policy_file(monkeypatch, tmp_path):
    def write(data):
        path = tmp_path / "policy.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        monkeypatch.setenv("ODOO_MCP_MODEL_POLICY_FILE", str(path))
        reset_model_policy()
        return path

    return write


def test_load_from_shared_policy_file(monkeypatch, tmp_path):
    path = tmp_path / "odoo_mcp_policy.json"
    path.write_text(json.dumps({"model_acl": {"default": {"default": "deny"}}}))
    monkeypatch.delenv("ODOO_MCP_MODEL_POLICY_FILE", raising=False)
    monkeypatch.setenv("ODOO_MCP_POLICY_FILE", str(path))
    assert load_model_policy().active()


def test_policy_file_without_model_acl_is_inactive(policy_file):
    policy_file({"allowed_side_effect_methods": ["sale.order.action_confirm"]})
    assert not get_model_policy().active()


def test_broken_json_fails_closed(policy_file):
    policy_file("{broken")
    with pytest.raises(ModelPolicyError):
        get_model_policy()
    with pytest.raises(ModelPolicyError):
        enforce_model_acl("default", "res.partner", "read")
    assert model_policy_posture()["active"] is False
    assert "error" in model_policy_posture()


def test_enforce_raises_and_respects_metadata_scope(policy_file):
    policy_file({"model_acl": {"default": {"default": "deny"}}})
    with pytest.raises(ModelAccessDenied) as excinfo:
        enforce_model_acl("default", "ir.model", "search_read")
    assert excinfo.value.decision.model == "ir.model"
    with metadata_scope():
        enforce_model_acl("default", "ir.model", "search_read")
    with pytest.raises(ModelAccessDenied):
        enforce_model_acl("default", "ir.model", "search_read")


def test_posture_reports_counts_not_rules(policy_file):
    policy_file(
        {
            "model_acl": {
                "default": {
                    "default": "deny",
                    "rules": [{"allow": ["project.*", "res.partner:read"]}],
                }
            }
        }
    )
    posture = model_policy_posture()
    assert posture["active"] is True
    assert posture["instances"]["default"] == {
        "default": "deny",
        "rule_count": 2,
        "metadata": "exempt",
    }
    assert "project" not in json.dumps(posture)
