import re

import pytest

from screening_agent import outcomes
from screening_agent.gateway import (
    RUN_ID_MAX,
    STEP_ID_MAX,
    STEP_NAME_MAX,
    TERMINAL_STATES,
    TURN_ID_MAX,
    Gateway,
    RunContext,
    new_ulid,
)


def test_ulid_shape():
    ids = {new_ulid() for _ in range(50)}
    assert len(ids) == 50
    for u in ids:
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", u)


def test_headers_outside_run_have_auth_and_release_only(settings):
    gw = Gateway(settings)
    h = gw.headers()
    assert h["Authorization"] == f"Bearer {settings.api_key}"
    assert h["X-Tenant-ID"] == "default"
    # the agent release (RFC 0023) is on every call; no run correlation outside a run
    assert h["X-Brutor-Agent-Name"] == "brutor-demo-screening-agent"
    assert {k for k in h if k.lower().startswith("x-brutor-")} <= {"X-Brutor-Agent-Name", "X-Brutor-Agent-Version", "X-Brutor-Agent-Build"}
    with pytest.raises(ValueError):
        gw.headers("intake", "Load")


def test_headers_inside_run(settings):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-20260923-001-01ARZ3NDEKTSV4RRFFQ69G5FAV")
    h1 = gw.headers("gather", "Gather the application facts", turn=("t1", 1))
    h2 = gw.headers("gather", "Gather the application facts", turn=("t1", 1))
    h3 = gw.headers("assess", "Assess risk and fraud", turn=("t1", 1))
    h4 = gw.headers("assess", "Assess risk and fraud", turn=("t2", 2))
    h5 = gw.headers("decide", "Decide and record", turn=("t2", 2))
    for h in (h1, h2, h3, h4, h5):
        assert h["x-brutor-run-id"] == "bds-APP-20260923-001-01ARZ3NDEKTSV4RRFFQ69G5FAV"
        assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", h["traceparent"])
        assert "X-Brutor-Run-End" not in h and "X-Brutor-Outcome-Id" not in h
    # same trace per run, new span per call
    assert h1["traceparent"][3:35] == h5["traceparent"][3:35]
    assert h1["traceparent"] != h2["traceparent"]
    # steps are phases and turns are model passes, both declared per call
    assert [h["X-Brutor-Step-Id"] for h in (h1, h2, h3, h4, h5)] == ["gather", "gather", "assess", "assess", "decide"]
    assert [h["X-Brutor-Turn-Id"] for h in (h1, h2, h3, h4, h5)] == ["t1", "t1", "t1", "t2", "t2"]
    assert [h["X-Brutor-Turn-Seq"] for h in (h1, h2, h3, h4, h5)] == ["1", "1", "1", "2", "2"]
    assert h1["X-Brutor-Step-Name"] == "Gather the application facts"
    assert h5["X-Brutor-Step-Name"] == "Decide and record"


def test_turn_defaults_to_one_per_step(settings):
    """Housekeeping runs give no turn: one turn per step, starting at t1."""
    gw = Gateway(settings)
    gw.begin_run("bds-tick-X")
    h1 = gw.headers("poll_pending", "Tick: poll pending applications")
    h2 = gw.headers("poll_pending", "Tick: poll pending applications")
    h3 = gw.headers("other", "Other")
    assert (h1["X-Brutor-Turn-Id"], h1["X-Brutor-Turn-Seq"]) == ("t1", "1")
    assert (h2["X-Brutor-Turn-Id"], h2["X-Brutor-Turn-Seq"]) == ("t1", "1")
    assert (h3["X-Brutor-Turn-Id"], h3["X-Brutor-Turn-Seq"]) == ("t2", "2")


def test_turn_validation(settings):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError):
        gw.headers("gather", "Gather", turn=("t" * 70, 1))
    with pytest.raises(ValueError):
        gw.headers("gather", "Gather", turn=("t0", 0))
    with pytest.raises(ValueError):
        gw.headers("gather", "Gather", turn=("", 1))


def test_close_headers_are_literal_states(settings):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    h = gw.headers("record", "Record", run_end="completed", outcome_id="application_processed.loan_approved")
    assert h["X-Brutor-Run-End"] == "completed"
    assert h["X-Brutor-Outcome-Id"] == "application_processed.loan_approved"
    assert "X-Brutor-Run-Outcome" not in h  # RFC 0025: removed, not aliased
    for state in TERMINAL_STATES:
        h = gw.headers("record", "Record", run_end=state)
        assert h["X-Brutor-Run-End"] == state and "X-Brutor-Outcome-Id" not in h
    for full in outcomes.full_ids():
        h = gw.headers("record", "Record", run_end="completed", outcome_id=full)
        assert h["X-Brutor-Outcome-Id"] == full
    assert gw.headers("r", "R", run_end="completed_degraded", outcome_id="no_applications_available")["X-Brutor-Outcome-Id"] == "no_applications_available"


def test_terminal_states_are_the_rfc_0025_vocabulary():
    """blocked_policy is gone: a refusal is an errored run with a cause."""
    assert TERMINAL_STATES == {"completed", "completed_degraded", "exhausted", "errored", "abandoned", "cancelled"}


@pytest.mark.parametrize("bad", ["true", "True", "TRUE", "done", "", "1"])
def test_run_end_never_true_or_unknown(settings, bad):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError):
        gw.headers("record", "Record", run_end=bad)


def test_outcome_requires_run_end(settings):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError, match="run-closing call"):
        gw.headers("record", "Record", outcome_id="application_processed.loan_approved")


@pytest.mark.parametrize("state", ["errored", "exhausted", "abandoned", "cancelled"])
def test_a_failed_run_reports_no_outcome(settings, state):
    """RFC 0025 P2: errors are execution, not outcomes."""
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError, match="completed run only"):
        gw.headers("record", "Record", run_end=state, outcome_id="application_processed.loan_approved")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "Application_processed",  # upper case
        "application processed",  # space
        "application_processed.",  # empty variant
        ".loan_approved",  # empty outcome
        "a.b.c",  # two dots
        "x" * 49,  # part longer than 48
        "application_processed." + "v" * 49,
        "resolved!",
        "application_processed/loan_approved",
    ],
)
def test_malformed_outcome_ids_are_refused_before_the_call(settings, bad):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError, match="malformed outcome id"):
        gw.headers("record", "Record", run_end="completed", outcome_id=bad)


@pytest.mark.parametrize(
    "undeclared",
    [
        "resolved",  # the removed vocabulary is not declared
        "escalated",
        "handed_off",
        "abandoned_by_user",
        "application_processed",  # declares variants: bare is not reportable
        "application_processed.loan_maybe",  # unknown variant
        "no_applications_available.today",  # declares no variants
        "screening_completed.clear",  # the fraud screener's, not ours
        "task_completed",  # the starter catalogue's, not ours
    ],
)
def test_ids_outside_the_shipped_catalogue_are_refused(settings, undeclared):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(outcomes.UndeclaredOutcome):
        gw.headers("record", "Record", run_end="completed", outcome_id=undeclared)


def test_grammar_boundaries():
    assert outcomes.check_grammar("a") == "a"
    assert outcomes.check_grammar("x" * 48 + "." + "y" * 48)
    assert outcomes.check_grammar("a-b_c.0-9")
    for bad in (None, 3, "x" * 49, "a." + "y" * 49, "A"):
        with pytest.raises(ValueError):
            outcomes.check_grammar(bad)


def test_run_end_requires_open_run(settings):
    gw = Gateway(settings)
    with pytest.raises(ValueError):
        gw.headers(run_end="completed")


def test_lengths(settings):
    gw = Gateway(settings)
    with pytest.raises(ValueError):
        RunContext(run_id="x" * (RUN_ID_MAX + 1))
    RunContext(run_id="x" * RUN_ID_MAX)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError):
        gw.headers("s" * (STEP_ID_MAX + 1), "name")
    h = gw.headers("s" * STEP_ID_MAX, "n" * (STEP_NAME_MAX + 50))
    assert len(h["X-Brutor-Step-Id"]) == STEP_ID_MAX
    assert len(h["X-Brutor-Step-Name"]) == STEP_NAME_MAX
    assert len(h["X-Brutor-Turn-Id"]) <= TURN_ID_MAX
    assert len(h["x-brutor-run-id"]) <= RUN_ID_MAX


def test_approval_token_header(settings):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-approval-X")
    h = gw.headers("apply_approved_decision", "Apply", run_end="completed", outcome_id=outcomes.UNDERWRITER_DECISION_APPLIED, approval_token="tok-123")
    assert h["X-Approval-Token"] == "tok-123"
    assert "X-Approval-Token" not in gw.headers("apply_approved_decision", "Apply")


def test_run_context_manager_resets(settings):
    gw = Gateway(settings)
    with gw.run("bds-APP-1-X") as ctx:
        assert gw.ctx is ctx
    assert gw.ctx is None


def test_the_poll_fold_plumbing_is_gone():
    """Spec section 8: the tick is its own run again; nothing continues it."""
    assert not hasattr(Gateway, "resume")


# -- end_run body ----------------------------------------------------------------
def _closing_gateway(settings, fake):
    gw = fake.gateway(settings)
    gw.begin_run("bds-APP-1-X")
    gw.ctx.derived_root = "root-derived-0001"
    return gw


def test_end_run_sends_state_and_outcome_id(settings, fake):
    gw = _closing_gateway(settings, fake)
    assert gw.end_run("completed", "application_processed.held_for_underwriter") is True
    assert fake.run_ends == [{"root": "root-derived-0001", "body": {"state": "completed", "outcome_id": "application_processed.held_for_underwriter"}}]
    assert gw.ctx.closed


def test_end_run_without_outcome_sends_state_only(settings, fake):
    gw = _closing_gateway(settings, fake)
    assert gw.end_run("errored") is True
    assert fake.run_ends == [{"root": "root-derived-0001", "body": {"state": "errored"}}]


@pytest.mark.parametrize(
    "state,outcome_id,error",
    [
        ("completed", "resolved", outcomes.UndeclaredOutcome),
        ("completed", "Bad Id", ValueError),
        ("errored", "application_processed.loan_approved", ValueError),
        ("blocked_policy", None, ValueError),
    ],
)
def test_end_run_refuses_before_the_call(settings, fake, state, outcome_id, error):
    gw = _closing_gateway(settings, fake)
    with pytest.raises(error):
        gw.end_run(state, outcome_id)
    assert fake.calls == [] and not gw.ctx.closed
