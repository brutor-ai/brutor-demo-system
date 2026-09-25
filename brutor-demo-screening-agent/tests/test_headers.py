import re

import pytest

from screening_agent.gateway import (
    OUTCOMES,
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


def test_headers_outside_run_have_auth_only(settings):
    gw = Gateway(settings)
    h = gw.headers()
    assert h["Authorization"] == f"Bearer {settings.api_key}"
    assert h["X-Tenant-ID"] == "default"
    assert not any(k.lower().startswith("x-brutor-") for k in h)
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
        assert "X-Brutor-Run-End" not in h and "X-Brutor-Run-Outcome" not in h
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
    h = gw.headers("record", "Record", run_end="completed", outcome="resolved")
    assert h["X-Brutor-Run-End"] == "completed"
    assert h["X-Brutor-Run-Outcome"] == "resolved"
    h = gw.headers("record", "Record", run_end="completed", outcome="escalated")
    assert h["X-Brutor-Run-Outcome"] == "escalated"
    for state in TERMINAL_STATES:
        assert gw.headers("record", "Record", run_end=state)["X-Brutor-Run-End"] == state
    for outcome in OUTCOMES:
        assert gw.headers("record", "Record", run_end="completed", outcome=outcome)["X-Brutor-Run-Outcome"] == outcome


@pytest.mark.parametrize("bad", ["true", "True", "TRUE", "done", "", "1"])
def test_run_end_never_true_or_unknown(settings, bad):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError):
        gw.headers("record", "Record", run_end=bad)


def test_outcome_requires_run_end(settings):
    gw = Gateway(settings)
    gw.begin_run("bds-APP-1-X")
    with pytest.raises(ValueError):
        gw.headers("record", "Record", outcome="resolved")
    with pytest.raises(ValueError):
        gw.headers("record", "Record", run_end="completed", outcome="finished")


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
    h = gw.headers("apply_approved_decision", "Apply", run_end="completed", outcome="resolved", approval_token="tok-123")
    assert h["X-Approval-Token"] == "tok-123"
    assert "X-Approval-Token" not in gw.headers("apply_approved_decision", "Apply")


def test_run_context_manager_resets(settings):
    gw = Gateway(settings)
    with gw.run("bds-APP-1-X") as ctx:
        assert gw.ctx is ctx
    assert gw.ctx is None
