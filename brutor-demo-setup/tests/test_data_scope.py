"""Run data scope provisioning (setup.py step 12, DESIGN.md section 5.7)."""

import copy

import setup
from tests.test_release_identity import FakeCP, provisioner

PATH = "/v1/admin/ai-systems/rg-demo/data-scope"


def _stored(**overrides):
    row = copy.deepcopy(setup.DATA_SCOPE)
    row.update({"ai_system_id": "rg-demo", "version": 3, "updated_at": "2026-11-09T08:00:00Z", "warnings": []})
    row.update(overrides)
    return row


def test_a_system_without_a_scope_gets_the_demo_scope():
    cp = FakeCP({("PUT", PATH): _stored(version=1)})
    provisioner(cp).data_scope()
    assert cp.writes() == [("PUT", PATH, setup.DATA_SCOPE)]


def test_an_unchanged_scope_is_not_written_again():
    # The scope is in the contract closure: a re-run must not drift it.
    stored = _stored()
    stored["rules"] = list(reversed(stored["rules"]))  # order is not a difference
    cp = FakeCP({("GET", PATH): stored})
    provisioner(cp).data_scope()
    assert cp.writes() == []


def test_the_operators_mode_is_kept():
    cp = FakeCP({("GET", PATH): _stored(mode="enforce")})
    provisioner(cp).data_scope()
    assert cp.writes() == []


def test_a_drifted_rule_set_is_converged_keeping_the_mode():
    cp = FakeCP({("GET", PATH): _stored(mode="enforce", rules=setup.DATA_SCOPE["rules"][:2]),
                 ("PUT", PATH): _stored(mode="enforce")})
    provisioner(cp).data_scope()
    ((method, path, body),) = cp.writes()
    assert (method, path) == ("PUT", PATH)
    assert body == dict(setup.DATA_SCOPE, mode="enforce")


def test_the_rules_cover_every_tool_that_names_a_record():
    rules = {(r["tool"], r["argument"], r["key"]) for r in setup.DATA_SCOPE["rules"]}
    assert rules == {
        ("applications_get", "application_id", "application"),
        ("applications_set_recommendation", "application_id", "application"),
        ("applications_add_note", "application_id", "application"),
        ("applications_hand_off", "application_id", "application"),
        ("bureau_verify_identity", "applicant_id", "applicant"),
        ("bureau_get_report", "applicant_id", "applicant"),
        (f"a2a:{setup.FRAUD_CARD_NAME}", "applicant_id", "applicant"),
    }
    assert set(setup.DATA_SCOPE["unscoped_tools"]) == {"applications_list_pending", "applications_stats"}
