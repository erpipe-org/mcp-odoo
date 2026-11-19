"""Model/method access control policy (``model_acl``).

Opt-in, per-instance allow/deny rules over ``(model, method)`` pairs, enforced
at the single RPC choke point (:meth:`OdooClient._execute`) and surfaced early
by the tools for friendlier refusals.

Design mirrors :mod:`field_policy`: env var -> JSON file -> validated, cached
structure. No ``model_acl`` key means no behavior change. A malformed policy
fails closed (raises at load, aborting startup) rather than silently running
unprotected.

Policy shape (a ``model_acl`` key in the shared policy file, or a dedicated
file via ``ODOO_MCP_MODEL_POLICY_FILE``)::

    {
      "model_acl": {
        "default": {
          "default": "deny",
          "rules": [
            {"allow": "project.*:@read"},
            {"allow": "project.task:@write"},
            {"allow": "project.project:message_subscribe"},
            {"deny":  "project.project:unlink", "reason": "never via agents"}
          ]
        }
      }
    }

Patterns are ``<model-glob>[:<method>]``; a missing method means ``*``. The
method part is an exact method name, a glob (``action_*``), or a group
(``@read``, ``@write``). The most specific matching rule wins (see
:func:`_specificity`); ties go to ``deny``; no match falls back to the
instance's ``default``.

The server's own metadata lookups (model list, ``fields_get``, user context,
installed modules, ACL diagnostics) run inside :func:`metadata_scope` and are
exempt unless the instance sets ``"metadata": "enforce"``.
"""

from __future__ import annotations

import contextvars
import fnmatch
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Optional, Tuple

from .diagnostics import DESTRUCTIVE_METHODS, READ_ONLY_METHODS

MODEL_POLICY_FILE_ENV = "ODOO_MCP_MODEL_POLICY_FILE"
MODEL_ACL_KEY = "model_acl"

READ_GROUP: FrozenSet[str] = frozenset(
    set(READ_ONLY_METHODS)
    | {
        "read_group",
        "formatted_read_group",
        "web_search_read",
        "web_read",
        "web_read_group",
        "search_fetch",
        "fetch",
        "default_get",
        "check_access_rights",
        "check_access",
        "has_access",
    }
)
WRITE_GROUP: FrozenSet[str] = frozenset(DESTRUCTIVE_METHODS)
METHOD_GROUPS: Dict[str, FrozenSet[str]] = {"@read": READ_GROUP, "@write": WRITE_GROUP}

_MODEL_GLOB_RE = re.compile(r"^[A-Za-z0-9_.*?]+$")
_METHOD_GLOB_RE = re.compile(r"^[A-Za-z0-9_*?]+$")
_WILDCARDS = "*?"
_RULE_META_KEYS = {"reason", "reviewed_by", "date", "comment"}

_metadata_flag: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "odoo_mcp_model_acl_metadata", default=False
)


class ModelPolicyError(ValueError):
    """Raised when a model policy is present but malformed (fail closed)."""


class ModelAccessDenied(PermissionError):
    """Raised when the model ACL refuses an Odoo call."""

    def __init__(self, decision: "Decision") -> None:
        self.decision = decision
        super().__init__(decision.message())


@contextmanager
def metadata_scope() -> Iterator[None]:
    """Mark Odoo calls inside this block as server-internal metadata lookups."""
    token = _metadata_flag.set(True)
    try:
        yield
    finally:
        _metadata_flag.reset(token)


def in_metadata_scope() -> bool:
    return _metadata_flag.get()


def _literal_len(pattern: str) -> int:
    return sum(1 for ch in pattern if ch not in _WILDCARDS)


@dataclass(frozen=True)
class Rule:
    effect: str  # "allow" or "deny"
    model_glob: str
    method: str  # exact name, glob, "*", or "@group"
    reason: Optional[str] = None

    @property
    def pattern(self) -> str:
        return f"{self.model_glob}:{self.method}"

    def describe(self) -> str:
        return f"{self.effect} {self.pattern}"

    def matches(self, model: str, method: str) -> bool:
        if not fnmatch.fnmatchcase(model, self.model_glob):
            return False
        group = METHOD_GROUPS.get(self.method)
        if group is not None:
            return method in group
        return fnmatch.fnmatchcase(method, self.method)

    def specificity(self) -> Tuple[int, int, int]:
        """Rank: model literal chars, then method kind, then method literal chars.

        Method kind: exact name 3 > glob with literal chars 2 > group 1 > ``*`` 0.
        """
        if self.method in METHOD_GROUPS:
            rank, method_len = 1, 0
        elif not any(ch in self.method for ch in _WILDCARDS):
            rank, method_len = 3, len(self.method)
        elif _literal_len(self.method):
            rank, method_len = 2, _literal_len(self.method)
        else:
            rank, method_len = 0, 0
        return (_literal_len(self.model_glob), rank, method_len)


@dataclass(frozen=True)
class InstanceACL:
    default: str  # "allow" or "deny"
    rules: Tuple[Rule, ...]
    metadata: str = "exempt"  # "exempt" or "enforce"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    instance: str
    model: str
    method: str
    source: str  # "no_policy", "metadata", "rule", "default"
    rule: Optional[Rule] = None

    def matched(self) -> str:
        if self.rule is not None:
            return f"rule '{self.rule.describe()}'"
        if self.source == "default":
            return "instance default"
        return self.source

    def message(self) -> str:
        reason = f" Reason: {self.rule.reason}." if self.rule and self.rule.reason else ""
        return (
            f"Model ACL denies {self.model}:{self.method} on instance "
            f"'{self.instance}' (matched {self.matched()}).{reason} This is a "
            "deployment policy, not a transient error: do not retry or work "
            "around it; ask an administrator to change model_acl in the policy "
            "file if this access is needed."
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "instance": self.instance,
            "model": self.model,
            "method": self.method,
            "matched": self.matched(),
        }


class ModelPolicy:
    """Validated per-instance model/method rules."""

    def __init__(self, by_instance: Dict[str, InstanceACL]) -> None:
        self._by_instance = by_instance

    def active(self) -> bool:
        return bool(self._by_instance)

    def instances(self) -> List[str]:
        return sorted(self._by_instance)

    def instance_acl(self, instance: str) -> Optional[InstanceACL]:
        return self._by_instance.get(instance)

    def decide(
        self, instance: str, model: str, method: str, *, metadata: bool = False
    ) -> Decision:
        acl = self._by_instance.get(instance)
        if acl is None:
            return Decision(True, instance, model, method, "no_policy")
        if metadata and acl.metadata == "exempt":
            return Decision(True, instance, model, method, "metadata")
        best: Optional[Rule] = None
        best_key: Optional[Tuple[int, int, int, int]] = None
        for rule in acl.rules:
            if not rule.matches(model, method):
                continue
            # deny outranks allow at equal specificity
            key = (*rule.specificity(), 1 if rule.effect == "deny" else 0)
            if best_key is None or key > best_key:
                best, best_key = rule, key
        if best is not None:
            return Decision(best.effect == "allow", instance, model, method, "rule", best)
        return Decision(acl.default == "allow", instance, model, method, "default")

    def check(
        self, instance: str, model: str, method: str, *, metadata: bool = False
    ) -> None:
        """Raise :class:`ModelAccessDenied` when the call is not allowed."""
        decision = self.decide(instance, model, method, metadata=metadata)
        if not decision.allowed:
            raise ModelAccessDenied(decision)

    def explicitly_allowed(self, instance: str, model: str, method: str) -> bool:
        """True when an exact ``model:method`` allow rule decides this call.

        Counts as the human review that execute_method otherwise requires for
        side-effect/unknown methods. Wildcards, groups, ``*`` and the instance
        default never count: they cover models and methods nobody looked at.
        """
        decision = self.decide(instance, model, method)
        rule = decision.rule
        return (
            decision.allowed
            and rule is not None
            and rule.model_glob == model
            and rule.method == method
        )

    def model_visible(self, instance: str, model: str) -> bool:
        """True when at least one method on ``model`` could be allowed.

        Probes every group member, every method named in a rule matching the
        model (glob patterns are probed literally, which fnmatch matches), and
        a sentinel that only wildcard method patterns match.
        """
        acl = self._by_instance.get(instance)
        if acl is None:
            return True
        probes: set[str] = set(READ_GROUP | WRITE_GROUP)
        probes.add("__odoo_mcp_any_other_method__")
        for rule in acl.rules:
            if rule.method not in METHOD_GROUPS and fnmatch.fnmatchcase(
                model, rule.model_glob
            ):
                probes.add(rule.method)
        return any(self.decide(instance, model, probe).allowed for probe in probes)

    def filter_models(self, instance: str, models: Iterable[str]) -> List[str]:
        return [name for name in models if self.model_visible(instance, name)]

    def refusal(
        self, instance: str, model: str, method: str, **extra: Any
    ) -> Optional[Dict[str, Any]]:
        """Tool-shaped refusal dict, or None when allowed."""
        decision = self.decide(instance, model, method)
        if decision.allowed:
            return None
        return {"success": False, **extra, "error": decision.message(), "acl": decision.as_dict()}

    def visibility_refusal(
        self, instance: str, model: str, **extra: Any
    ) -> Optional[Dict[str, Any]]:
        """Refusal for schema/metadata tools when every method on ``model`` is denied."""
        if self.model_visible(instance, model):
            return None
        decision = self.decide(instance, model, "*")
        return {
            "success": False,
            **extra,
            "error": (
                f"Model ACL denies every method on {model} for instance "
                f"'{instance}', so its schema is not exposed either. Do not "
                "retry; ask an administrator to change model_acl if needed."
            ),
            "acl": {**decision.as_dict(), "method": "*"},
        }


def _parse_pattern(raw: Any, where: str) -> Tuple[str, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise ModelPolicyError(f"{where}: pattern must be a non-empty string")
    text = raw.strip()
    model_glob, sep, method = text.partition(":")
    model_glob = model_glob.strip()
    method = method.strip() if sep else "*"
    if not model_glob or not _MODEL_GLOB_RE.match(model_glob):
        raise ModelPolicyError(
            f"{where}: invalid model pattern {model_glob!r} in {text!r} "
            "(letters, digits, '_', '.', '*', '?' only)"
        )
    if method.startswith("@"):
        if method not in METHOD_GROUPS:
            raise ModelPolicyError(
                f"{where}: unknown method group {method!r}; known groups: "
                f"{sorted(METHOD_GROUPS)}"
            )
    elif not method or not _METHOD_GLOB_RE.match(method):
        raise ModelPolicyError(
            f"{where}: invalid method pattern {method!r} in {text!r}"
        )
    return model_glob, method


def _parse_rule(entry: Any, where: str) -> List[Rule]:
    if not isinstance(entry, dict):
        raise ModelPolicyError(f"{where} must be an object like {{\"allow\": \"model:method\"}}")
    has_allow = "allow" in entry
    has_deny = "deny" in entry
    if has_allow == has_deny:
        raise ModelPolicyError(f"{where} needs exactly one of 'allow' or 'deny'")
    effect = "allow" if has_allow else "deny"
    unknown = set(entry) - {effect} - _RULE_META_KEYS
    if unknown:
        raise ModelPolicyError(f"{where}: unknown keys {sorted(unknown)}")
    reason = entry.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ModelPolicyError(f"{where}: 'reason' must be a string")
    raw = entry[effect]
    patterns = raw if isinstance(raw, list) else [raw]
    if not patterns:
        raise ModelPolicyError(f"{where}: '{effect}' must not be empty")
    rules: List[Rule] = []
    for pattern in patterns:
        model_glob, method = _parse_pattern(pattern, where)
        rules.append(Rule(effect, model_glob, method, reason))
    return rules


def _parse_model_policy(data: Dict[str, Any]) -> ModelPolicy:
    acl = data.get(MODEL_ACL_KEY, {})
    if not isinstance(acl, dict):
        raise ModelPolicyError(f"'{MODEL_ACL_KEY}' must be an object")
    by_instance: Dict[str, InstanceACL] = {}
    for instance, spec in acl.items():
        where = f"model_acl['{instance}']"
        if not isinstance(spec, dict):
            raise ModelPolicyError(f"{where} must be an object")
        unknown = set(spec) - {"default", "rules", "metadata", "comment"}
        if unknown:
            raise ModelPolicyError(f"{where}: unknown keys {sorted(unknown)}")
        default = spec.get("default")
        if default not in ("allow", "deny"):
            raise ModelPolicyError(f"{where}['default'] must be 'allow' or 'deny'")
        metadata = spec.get("metadata", "exempt")
        if metadata not in ("exempt", "enforce"):
            raise ModelPolicyError(f"{where}['metadata'] must be 'exempt' or 'enforce'")
        raw_rules = spec.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ModelPolicyError(f"{where}['rules'] must be a list")
        rules: List[Rule] = []
        for index, entry in enumerate(raw_rules):
            rules.extend(_parse_rule(entry, f"{where}['rules'][{index}]"))
        by_instance[str(instance)] = InstanceACL(default, tuple(rules), metadata)
    return ModelPolicy(by_instance)


def model_policy_file_path() -> Optional[str]:
    """Dedicated model-policy file, else the shared policy file."""
    explicit = os.environ.get(MODEL_POLICY_FILE_ENV, "").strip()
    if explicit:
        return explicit
    # Lazy: odoo_client imports this module, and write_policy -> tool_helpers
    # imports odoo_client.
    from .write_policy import policy_file_path

    return policy_file_path()


def load_model_policy() -> ModelPolicy:
    """Load and validate the model policy; raise ModelPolicyError if malformed."""
    path = model_policy_file_path()
    if path is None:
        return ModelPolicy({})
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelPolicyError(f"Cannot read model policy file {path}: {exc}")
    if not isinstance(data, dict):
        raise ModelPolicyError(f"Model policy file {path} must contain an object")
    return _parse_model_policy(data)


_policy: Optional[ModelPolicy] = None
_policy_lock = threading.Lock()


def get_model_policy() -> ModelPolicy:
    """Process-wide model policy, loaded lazily and cached."""
    global _policy
    with _policy_lock:
        if _policy is None:
            _policy = load_model_policy()
        return _policy


def reset_model_policy() -> None:
    """Drop the cached policy (intended for tests and lifespan reload)."""
    global _policy
    with _policy_lock:
        _policy = None


def enforce_model_acl(instance: str, model: str, method: str) -> None:
    """Choke-point check used by OdooClient._execute (fails closed on bad policy)."""
    get_model_policy().check(instance, model, method, metadata=in_metadata_scope())


def model_policy_posture() -> Dict[str, Any]:
    """Non-secret health posture: whether model ACL is active and rule counts."""
    try:
        policy = get_model_policy()
    except ModelPolicyError as exc:
        return {"active": False, "error": str(exc)}
    posture: Dict[str, Any] = {
        "active": policy.active(),
        "instances_with_rules": len(policy.instances()),
    }
    if policy.active():
        posture["instances"] = {
            name: {
                "default": acl.default,
                "rule_count": len(acl.rules),
                "metadata": acl.metadata,
            }
            for name in policy.instances()
            if (acl := policy.instance_acl(name)) is not None
        }
    return posture
