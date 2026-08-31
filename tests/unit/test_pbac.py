"""PBAC engine: deny-by-default, boot-fatal validation."""

import json

import pytest

from insurance.api.auth import Identity
from insurance.api.pbac import PolicyEngine, PolicyError


def _write_policy(tmp_path, doc, name="test.policy.json"):
    (tmp_path / name).write_text(json.dumps(doc))


def _doc(policies):
    return {"version": "1.0", "policies": policies}


def _identity(roles=("officer",), tenant="", clearance=""):
    return Identity(subject="s", roles=frozenset(roles), tenant=tenant, clearance=clearance)


def test_allow_and_deny(tmp_path):
    _write_policy(tmp_path, _doc([
        {"name": "r1", "roles": ["officer"], "resource": "assessment", "action": "create",
         "classification": ["INTERNAL"]},
    ]))
    engine = PolicyEngine.load(str(tmp_path))
    assert engine.allow(_identity(), "assessment", "create", "INTERNAL")
    assert not engine.allow(_identity(), "assessment", "approve", "INTERNAL")
    assert not engine.allow(_identity(), "assessment", "create", "CONFIDENTIAL")
    assert not engine.allow(_identity(roles=("other",)), "assessment", "create", "INTERNAL")


def test_wildcards_and_tenant(tmp_path):
    _write_policy(tmp_path, _doc([
        {"name": "r1", "roles": ["*"], "resource": "*", "action": "read", "classification": ["*"]},
        {"name": "r2", "roles": ["admin"], "resource": "ops", "action": "*", "tenant": "firs",
         "classification": ["*"]},
    ]))
    engine = PolicyEngine.load(str(tmp_path))
    assert engine.allow(_identity(roles=("nobody",)), "anything", "read", "RESTRICTED")
    assert not engine.allow(_identity(roles=("nobody",)), "anything", "write", "PUBLIC")
    assert engine.allow(_identity(roles=("admin",), tenant="firs"), "ops", "write", "PUBLIC")
    assert not engine.allow(_identity(roles=("admin",), tenant="nsc"), "ops", "write", "PUBLIC")
    assert not engine.allow(_identity(roles=("admin",)), "ops", "write", "PUBLIC")  # no tenant claim


def test_clearance_matching(tmp_path):
    _write_policy(tmp_path, _doc([
        {"name": "r1", "roles": ["officer"], "resource": "stamp", "action": "void",
         "clearance": ["high"], "classification": ["*"]},
    ]))
    engine = PolicyEngine.load(str(tmp_path))
    assert engine.allow(_identity(clearance="high"), "stamp", "void", "CONFIDENTIAL")
    assert not engine.allow(_identity(clearance="low"), "stamp", "void", "CONFIDENTIAL")
    assert not engine.allow(_identity(), "stamp", "void", "CONFIDENTIAL")


def test_boot_fatal_conditions(tmp_path):
    with pytest.raises(PolicyError):
        PolicyEngine.load(str(tmp_path / "nonexistent"))
    with pytest.raises(PolicyError):
        PolicyEngine.load(str(tmp_path))  # empty dir
    _write_policy(tmp_path, {"version": "2.0", "policies": []})
    with pytest.raises(PolicyError):
        PolicyEngine.load(str(tmp_path))
    _write_policy(tmp_path, _doc([]), name="test.policy.json")
    with pytest.raises(PolicyError):
        PolicyEngine.load(str(tmp_path))


def test_schema_violations(tmp_path):
    base = {"name": "r", "roles": ["a"], "resource": "x", "action": "y"}
    for mutation in [
        lambda d: d.pop("roles"),
        lambda d: d.update(roles=[]),
        lambda d: d.update(unknown_field=1),
        lambda d: d.update(classification=["MARS"]),
        lambda d: d.update(resource="bad ident!"),
    ]:
        doc = _doc([json.loads(json.dumps(base))])
        mutation(doc["policies"][0])
        _write_policy(tmp_path, doc)
        with pytest.raises(PolicyError):
            PolicyEngine.load(str(tmp_path))


def test_duplicate_rule_names(tmp_path):
    rule = {"name": "r", "roles": ["a"], "resource": "x", "action": "y"}
    _write_policy(tmp_path, _doc([rule, rule]))
    with pytest.raises(PolicyError):
        PolicyEngine.load(str(tmp_path))


def test_shipped_policy_file_loads():
    engine = PolicyEngine.load("policies")
    assert engine.allow(_identity(roles=("underwriter",)), "quote", "create", "CONFIDENTIAL")
    assert engine.allow(_identity(roles=("underwriter-approver",)), "quote", "bind-decide", "CONFIDENTIAL")
    assert engine.allow(_identity(roles=("insurer-aggregator",)), "aggregate", "read", "INTERNAL")
    assert engine.allow(_identity(roles=("auditor",)), "audit", "verify", "CONFIDENTIAL")
    # deny-by-default: auditor cannot create quotes; aggregator cannot bind.
    assert not engine.allow(_identity(roles=("auditor",)), "quote", "create", "CONFIDENTIAL")
    assert not engine.allow(_identity(roles=("insurer-aggregator",)), "quote", "bind", "CONFIDENTIAL")
    assert not engine.allow(_identity(), "quote", "create", "CONFIDENTIAL")
