"""Run data scope (DESIGN.md section 5.7).

Every application run declares the records it is for in X-Brutor-Run-Scope,
from the record it serves; the fraud screener message names its subject in a
data part; a referred application's referral check reads a record outside the
scope (the deliberate breach) and, when the gateway refuses it, the screening
still completes.
"""

import json

import httpx
import pytest

from screening_agent.approvals import PendingApprovals, note_run
from screening_agent.gateway import RunContext
from screening_agent.graph import process_application

APP = "APP-20260923-001"
APPLICANT = "CUST-1001"
REFERRER = "CUST-2002"


def _run(settings, fake, tmp_path):
    gw = fake.gateway(settings)
    return process_application(gw, settings, PendingApprovals(tmp_path), APP)


def _governed(fake):
    return [c for c in fake.calls if c.kind in ("llm", "mcp", "a2a")]


def test_every_call_declares_the_application_and_the_applicant_after_intake(settings, fake, tmp_path):
    result = _run(settings, fake, tmp_path)
    assert result.state == "completed"
    calls = _governed(fake)
    assert calls[0].tool == "applications_get"
    # Intake knows only the application; it reads the applicant from the record.
    assert calls[0].headers["x-brutor-run-scope"] == f"application={APP}"
    for c in calls[1:]:
        assert c.headers["x-brutor-run-scope"] == f"application={APP}; applicant={APPLICANT}", c.path
    # The values are the record's, so no call names an applicant outside the scope.
    named = [c.body["params"]["arguments"]["applicant_id"] for c in calls
             if c.kind == "mcp" and "applicant_id" in c.body["params"]["arguments"]]
    assert named == [APPLICANT, APPLICANT]


def test_the_fraud_message_names_its_subject_in_a_data_part(settings, fake, tmp_path):
    _run(settings, fake, tmp_path)
    (a2a,) = fake.calls_of("a2a")
    text, data = a2a.body["message"]["parts"]
    # The payload stays in the text part: the screener reads it and the
    # gateway's A2A guardrails scan text parts only.
    assert text["kind"] == "text" and json.loads(text["text"])["applicant_id"] == APPLICANT
    assert data == {"kind": "data", "data": {"applicant_id": APPLICANT}}


def test_a_referred_application_reads_the_referrers_credit_file(settings, fake, tmp_path):
    fake.application["referred_by"] = {"applicant_id": REFERRER}
    result = _run(settings, fake, tmp_path)
    assert result.state == "completed"
    reports = [c for c in _governed(fake) if c.tool == "bureau_get_report"]
    assert [c.body["params"]["arguments"]["applicant_id"] for c in reports] == [APPLICANT, REFERRER]
    # Same step and turn as the applicant's report; the scope does not widen.
    assert {c.headers["x-brutor-step-id"] for c in reports} == {"gather"}
    assert all(c.headers["x-brutor-run-scope"] == f"application={APP}; applicant={APPLICANT}" for c in reports)
    assert len(_governed(fake)) == 9


def test_a_refused_referral_check_does_not_fail_the_screening(settings, fake, tmp_path):
    fake.application["referred_by"] = {"applicant_id": REFERRER}

    def refuse_the_referrer(tool, args, call):
        if tool == "bureau_get_report" and args.get("applicant_id") == REFERRER:
            return httpx.Response(403, json={
                "jsonrpc": "2.0", "id": call.body.get("id"),
                "error": {"code": -32003, "message": "data scope: applicant out_of_scope",
                          "data": {"data_scope": {"tool": "bureau_get_report", "argument": "applicant_id",
                                                  "key": "applicant", "verdict": "out_of_scope"}}},
            })
        return None

    fake.intercept = refuse_the_referrer
    result = _run(settings, fake, tmp_path)
    assert result.state == "completed" and not result.blocked_by_policy
    assert result.outcome == "application_processed.loan_approved"
    assert fake.recorded and fake.recorded[0]["args"]["recommendation"] == "approve"


def test_short_application_runs_declare_their_application(settings, fake, tmp_path):
    gw = fake.gateway(settings)
    note_run(gw, settings, APP, "a note", suffix="note", step_id="note", step_name="Note")
    (call,) = _governed(fake)
    assert call.headers["x-brutor-run-scope"] == f"application={APP}"


def test_the_scheduler_tick_declares_nothing(settings, fake, tmp_path):
    from screening_agent.scheduler import Scheduler

    gw = fake.gateway(settings)
    with gw.run("bds-tick-0001"):
        Scheduler(settings, gw).list_pending(5)
    (call,) = _governed(fake)
    assert call.tool == "applications_list_pending"
    assert "x-brutor-run-scope" not in call.headers


@pytest.mark.parametrize("key,value", [("", "x"), ("applicant", ""), ("a;b", "x"), ("k", "a,b"), ("k", "a=b"), ("k", "a b")])
def test_a_malformed_scope_is_refused_before_it_is_sent(key, value):
    with pytest.raises(ValueError):
        RunContext(run_id="r").declare(key, value)


def test_a_bound_key_cannot_be_redeclared():
    ctx = RunContext(run_id="r")
    ctx.declare("applicant", APPLICANT)
    ctx.declare("applicant", APPLICANT)  # the same value again is fine
    with pytest.raises(ValueError, match="already declared"):
        ctx.declare("applicant", REFERRER)
    assert ctx.scope_header() == f"applicant={APPLICANT}"
