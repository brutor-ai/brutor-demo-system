"""a193 in provisioning: every approval hold on the demo waits a day.

Live 2026-09-29: 76 holds expired in 24 h (skill runs, notes and every hold
raised while a drift response had demoted the system), because only the
recommendation tool carried a window. Setup now states the window once, on
the organisation; both AI Systems inherit it."""

import pytest

import setup
from test_release_identity import FakeCP, provisioner

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


def test_an_older_platform_warns_and_goes_on(capsys):
    cp = FakeCP({})  # 404: no approval-window endpoint before a193
    provisioner(cp)._approval_window(ORG)
    assert "older than a193" in capsys.readouterr().out


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
