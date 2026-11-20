"""The approval window in provisioning: every approval hold on the demo waits a day.

A window carried by the recommendation tool alone leaves every other hold
(skill runs, notes and every hold raised while a drift response has demoted
the system) on the platform's default, and those expire unanswered. Setup
states the window once, on the organisation; both AI Systems inherit it."""

import pytest

import setup
from test_release_identity import FakeCP, StrictCP, provisioner

ORG = "rg-org"
PATH = f"/v1/admin/resource-groups/{ORG}/approval-window"


def test_the_window_is_a_day():
    assert setup.APPROVAL_WINDOW_SECONDS == 86400


def test_setup_puts_the_window_on_the_organisation():
    cp = FakeCP({("PUT", PATH): lambda body: {"approval_window_seconds": body["approval_window_seconds"],
                                               "effective_seconds": body["approval_window_seconds"],
                                               "effective_source": "resource_group"}})
    provisioner(cp)._approval_window(ORG)
    assert cp.writes() == [("PUT", PATH, {"approval_window_seconds": 86400})]


def test_a_window_the_control_plane_did_not_keep_is_an_error():
    cp = FakeCP({("PUT", PATH): {"approval_window_seconds": 86400, "effective_seconds": 300}})
    with pytest.raises(setup.StepError, match="did not keep"):
        provisioner(cp)._approval_window(ORG)


def test_a_window_that_cannot_be_set_stops_the_script():
    """The window is a step like any other: a Control Plane that does not
    answer the PUT ends the run, it is not skipped with a warning."""
    with pytest.raises(setup.StepError, match="approval-window -> HTTP 404"):
        provisioner(StrictCP({}))._approval_window(ORG)


def test_binding_models_removes_nothing():
    """The classifier reaches both AI Systems by inheritance from the
    organisation and setup binds only the drafter directly, so a run has no
    binding of its own to take back."""
    eff = {"direct_models": [{"id": "m-cls", "model_name": "gpt-5.2"}], "inherited_models": [{"id": "m-cls", "model_name": "gpt-5.2"}]}
    cp = FakeCP({("POST", "/v1/admin/resource-groups/rg-demo/llm-models"): {},
                 ("GET", "/v1/admin/resource-groups/rg-demo/effective-llm-models"): eff,
                 ("GET", "/v1/admin/resource-groups/rg-fraud/effective-llm-models"): eff})
    prov = provisioner(cp)
    prov.ids.update({"fraud_group_id": "rg-fraud", "classifier_model_id": "m-cls", "classifier_model_name": "gpt-5.2",
                     "drafter_model_id": "m-drf", "drafter_model_name": "gpt-5.5"})
    prov.bind_models()
    assert cp.writes() == [("POST", "/v1/admin/resource-groups/rg-demo/llm-models", {"llm_model_id": "m-drf", "portal_visible": False})]


def test_existing_guardrail_configs_are_found_by_name_and_left_as_they_are():
    """A re-run finds both configs by name and writes nothing: what setup
    creates is already what it wants."""
    rows = [{"id": "gc-base", "name": setup.BASELINE_GUARDRAIL_NAME}, {"id": "gc-sys", "name": setup.GUARDRAIL_NAME}]
    cp = FakeCP({("GET", "/v1/admin/guardrails/configs"): {"configs": rows},
                 ("GET", "/v1/admin/guardrails/configs/gc-sys"): {"config": {"builtin": {"prompt_injection": {"enabled": True}}}}})
    prov = provisioner(cp)
    prov.ids["org_group_id"] = ORG
    prov._guardrails(["rg-demo", "rg-fraud"])
    assert cp.writes() == []
    assert cp.calls == [("GET", "/v1/admin/guardrails/configs", None)]


def test_no_tool_carries_a_second_window():
    """A per-tool window overrides the group's in both directions; one
    statement of the window, not two that can diverge."""
    for tool in ("applications_set_recommendation", "applications_add_note", "credit_report"):
        row = setup._capability_row(tool)
        assert "approval_timeout_seconds" not in row
        assert row["access"] == "enabled"


def test_the_org_step_sets_it(monkeypatch):
    calls = []
    prov = provisioner(FakeCP({}))
    prov.ids.update({"classifier_model_id": "m", "classifier_model_name": "gpt-5.2"})
    monkeypatch.setattr(setup.Provisioner, "_approval_window", lambda self, gid: calls.append(gid))
    prov._org_resources(ORG)
    assert calls == [ORG]


def test_the_dry_run_plan_names_it(capsys):
    setup.dry_run(residency=False, skip_lifecycle=False)
    assert "approval-window 86400" in capsys.readouterr().out
