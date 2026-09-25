import pytest

from screening_agent.approvals import PendingApprovals
from screening_agent.gateway import GatewayError
from screening_agent.graph import NODE_MAP, NODES, STEP_ORDER, process_application
from screening_agent.prompts import DISCLOSURE
from screening_agent.scheduler import RetryTracker, Scheduler

EXPECTED_STEPS = [NODE_MAP[n][0] for n in NODES]  # gather x4, assess x2, decide x2
EXPECTED_TURNS = [NODE_MAP[n][1] for n in NODES]  # t1 x5, t2 x3


def _run(settings, fake, tmp_path):
    gw = fake.gateway(settings)
    store = PendingApprovals(tmp_path)
    result = process_application(gw, settings, store, "APP-20260923-001")
    return gw, store, result


def _assert_single_run(fake, run_id, *, close_attempts=1):
    """Every governed call shares the run id; only the last call(s) carry the close.
    close_attempts=2 covers the approval hold: the held call carried the close
    header, answered 202, and the note that follows closes the run instead."""
    calls = [c for c in fake.calls if c.kind in ("llm", "mcp", "a2a")]
    assert calls, "no gateway calls recorded"
    assert {c.run_id for c in calls} == {run_id}, "all calls of a run share the run id"
    assert all(c.headers["x-tenant-id"] == "default" for c in calls)
    assert all("traceparent" in c.headers for c in calls)
    for c in calls[:-close_attempts]:
        assert "x-brutor-run-end" not in c.headers
    last = calls[-1]
    assert last.headers["x-brutor-run-end"] == "completed"
    assert "x-brutor-run-outcome" in last.headers
    return calls, last


def test_happy_path_one_run_eight_steps(settings, fake, tmp_path):
    gw, store, result = _run(settings, fake, tmp_path)
    assert result.state == "completed" and result.outcome == "resolved"
    assert result.run_id.startswith("bds-APP-20260923-001-") and len(result.run_id) <= 128
    calls, last = _assert_single_run(fake, result.run_id)
    # steps are phases: exactly 3 distinct ids, in order gather, assess, decide
    steps = [c.headers["x-brutor-step-id"] for c in calls]
    assert steps == EXPECTED_STEPS == ["gather"] * 4 + ["assess"] * 2 + ["decide"] * 2
    assert list(dict.fromkeys(steps)) == STEP_ORDER
    assert [c.headers["x-brutor-step-name"] for c in calls][::4] == ["Gather the application facts", "Assess risk and fraud"]
    assert calls[-1].headers["x-brutor-step-name"] == "Decide and record"
    # turns are model passes: exactly 2 distinct ids, seq 1 then 2
    turns = [c.headers["x-brutor-turn-id"] for c in calls]
    assert turns == EXPECTED_TURNS == ["t1"] * 5 + ["t2"] * 3
    assert [c.headers["x-brutor-turn-seq"] for c in calls] == ["1"] * 5 + ["2"] * 3
    assert len(set(turns)) == 2 and len(set(steps)) == 3
    # no node name leaks into a header ("decide" is both a node and a phase, by design)
    node_only = set(NODES) - set(STEP_ORDER)
    assert not any(c.headers["x-brutor-step-id"] in node_only for c in calls)
    assert [c.kind for c in calls] == ["mcp", "mcp", "mcp", "mcp", "llm", "a2a", "llm", "mcp"]
    assert [c.tool for c in calls if c.kind == "mcp"] == ["applications_get", "bureau_verify_identity", "bureau_get_report", "skills__run_script", "applications_set_recommendation"]
    assert last.headers["x-brutor-run-outcome"] == "resolved"
    # models: classifier then drafter, JSON mode, no sampling params
    llm = [c for c in calls if c.kind == "llm"]
    assert [c.body["model"] for c in llm] == ["gpt-5.2", "gpt-5.5"]
    # the record call carries amount_eur explicitly and the disclosure
    rec = fake.recorded[0]["args"]
    assert rec["amount_eur"] == 12000 and rec["recommendation"] == "approve"
    assert DISCLOSURE in rec["customer_letter"]
    assert "Rule applied: none" in rec["rationale"]
    assert rec["risk_band"] == "low" and rec["affordability_class"] == "comfortable" and rec["fraud_verdict"] == "clear"
    # header close took, so no POST /v1/runs/.../end
    assert fake.run_ends == []
    assert store.count() == 0
    assert gw.ctx is None


def test_disclosure_appended_when_model_omits_it(settings, fake, tmp_path):
    fake.drafter_includes_disclosure = False
    _run(settings, fake, tmp_path)
    assert fake.recorded[0]["args"]["customer_letter"].endswith(DISCLOSURE)


@pytest.mark.parametrize(
    "setup, expected, rule",
    [
        (dict(identity_verified=False), "refer", "identity_unverified"),
        (dict(affordability_class="unaffordable"), "decline", "affordability_unaffordable"),
        (dict(fraud_verdict="hit"), "decline", "fraud_hit"),
        (dict(fraud_verdict="review"), "refer", "fraud_review"),
        (dict(risk_band="high"), "refer", "risk_band_high"),
    ],
)
def test_rules_override_the_drafter(settings, fake, tmp_path, setup, expected, rule):
    for k, v in setup.items():
        setattr(fake, k, v)
    fake.drafter_recommendation = "approve"
    _, _, result = _run(settings, fake, tmp_path)
    assert result.state == "completed"
    rec = fake.recorded[0]["args"]
    assert rec["recommendation"] == expected
    assert f"Rule applied: {rule}" in rec["rationale"]
    # the drafter was told the floor
    drafter_call = [c for c in fake.calls if c.kind == "llm"][1]
    assert drafter_call.llm_api == "responses"
    assert "Deterministic policy rules have already fixed the minimum outcome" in drafter_call.body["input"][1]["content"]


def test_approval_hold_escalates_and_persists(settings, fake, tmp_path):
    fake.approval_rule = lambda args: args["recommendation"] == "decline" or args["amount_eur"] > 25000
    fake.fraud_verdict = "hit"  # forces decline -> held
    gw, store, result = _run(settings, fake, tmp_path)
    assert result.state == "completed" and result.outcome == "escalated"
    assert result.approval_id == "apr-0001"
    calls, last = _assert_single_run(fake, result.run_id, close_attempts=2)
    # 9 calls: 8 steps + the note; the held call has no close header, the note closes the run
    assert len(calls) == 9
    held, note = calls[-2], calls[-1]
    assert held.tool == "applications_set_recommendation" and note.tool == "applications_add_note"
    assert held.headers["x-brutor-step-id"] == note.headers["x-brutor-step-id"] == "decide"
    assert held.headers["x-brutor-turn-id"] == note.headers["x-brutor-turn-id"] == "t2"
    assert [c.headers["x-brutor-step-id"] for c in calls] == EXPECTED_STEPS + ["decide"]
    assert [c.headers["x-brutor-turn-id"] for c in calls] == EXPECTED_TURNS + ["t2"]
    assert held.headers["x-brutor-run-end"] == "completed" and held.headers["x-brutor-run-outcome"] == "resolved"
    assert note.headers["x-brutor-run-end"] == "completed" and note.headers["x-brutor-run-outcome"] == "escalated"
    assert "apr-0001" in fake.notes[0]["note"] and "Held for underwriter" in fake.notes[0]["note"]
    assert fake.recorded == []
    pending = store.all()
    assert len(pending) == 1
    assert pending[0]["approval_id"] == "apr-0001"
    assert pending[0]["application_id"] == "APP-20260923-001"
    assert pending[0]["tool_args"] == held.body["params"]["arguments"]
    assert pending[0]["run_id"] == result.run_id
    assert fake.run_ends == []


def test_approval_resolved_on_later_tick(settings, fake, tmp_path):
    fake.approval_rule = lambda args: args["amount_eur"] > 25000
    fake.application["requested_amount_eur"] = 30000
    gw, store, result = _run(settings, fake, tmp_path)
    assert result.outcome == "escalated"
    fake.approval_statuses["apr-0001"]["status"] = "approved"
    fake.approval_statuses["apr-0001"]["approval_token"] = "tok-1"
    fake.pending_list = []
    sched = Scheduler(settings, gw, store, RetryTracker(tmp_path))
    summary = sched.tick()
    assert summary["approvals"]["approved"] == 1
    assert store.count() == 0
    assert fake.recorded[0]["token"] == "tok-1"
    assert fake.recorded[0]["args"]["amount_eur"] == 30000
    apply_call = [c for c in fake.calls if c.tool == "applications_set_recommendation" and c.headers.get("x-approval-token")][0]
    assert apply_call.run_id != result.run_id
    assert apply_call.run_id.startswith("bds-APP-20260923-001-approval-")
    assert apply_call.headers["x-brutor-step-id"] == "apply_approved_decision"
    assert apply_call.headers["x-brutor-run-end"] == "completed"
    assert apply_call.headers["x-brutor-run-outcome"] == "resolved"
    assert sched.status["approvals_applied"] == 1 and sched.status["pending_approvals"] == 0


def test_policy_block_ends_run_blocked(settings, fake, tmp_path):
    fake.block_tool = "applications_get"
    gw, store, result = _run(settings, fake, tmp_path)
    assert result.state == "blocked_policy" and result.outcome is None
    assert "guardrail" in result.error
    # no LLM answered, so no derived root: nothing to close explicitly
    assert fake.run_ends == []
    assert fake.recorded == [] and fake.notes == []


def test_node_error_closes_run_errored_via_derived_root(settings, fake, tmp_path):
    fake.fail_a2a = True
    gw, store, result = _run(settings, fake, tmp_path)
    assert result.state == "errored" and result.outcome is None
    assert "502" in result.error
    assert fake.run_ends == [{"root": "root-derived-0001", "body": {"state": "errored"}}]
    end_call = fake.calls_of("run_end")[0]
    assert end_call.headers["x-brutor-run-id"] == result.run_id
    assert "x-brutor-run-end" not in end_call.headers
    assert fake.recorded == []


def test_block_after_llm_closes_blocked_policy(settings, fake, tmp_path):
    fake.block_tool = "applications_set_recommendation"
    _, _, result = _run(settings, fake, tmp_path)
    assert result.state == "blocked_policy"
    assert fake.run_ends == [{"root": "root-derived-0001", "body": {"state": "blocked_policy"}}]


def test_scheduler_retries_then_skips(settings, fake, tmp_path):
    fake.block_tool = "applications_get"
    gw = fake.gateway(settings)
    store = PendingApprovals(tmp_path)
    sched = Scheduler(settings, gw, store, RetryTracker(tmp_path, max_attempts=3))
    for i in range(1, 4):
        summary = sched.tick()
        assert [p["state"] for p in summary["processed"]] == ["blocked_policy"], f"tick {i}"
    assert sched.tracker.is_skipped("APP-20260923-001")
    assert sched.status["runs_blocked"] == 3
    # the give-up note was added in its own closed run
    give_up = [c for c in fake.calls if c.tool == "applications_add_note"]
    assert len(give_up) == 1
    assert give_up[0].headers["x-brutor-run-end"] == "completed"
    assert give_up[0].headers["x-brutor-run-outcome"] == "handed_off"
    assert give_up[0].headers["x-brutor-step-id"] == "give_up"
    # fourth tick: listed but skipped, no run
    before = len(fake.calls)
    summary = sched.tick()
    assert summary["processed"] == [] and summary["skipped"] == ["APP-20260923-001"]
    assert [c.tool for c in fake.calls[before:]] == ["applications_list_pending"]
    # the listing runs in its own short run, closed on that single call
    listing = [c for c in fake.calls if c.tool == "applications_list_pending"]
    assert len(listing) == 4
    app_runs = {c.run_id for c in fake.calls if c.run_id and not c.run_id.startswith("bds-tick-")}
    for c in listing:
        assert c.run_id.startswith("bds-tick-") and len(c.run_id) <= 128
        assert c.run_id not in app_runs
        assert c.headers["x-brutor-step-id"] == "poll_pending"
        assert c.headers["x-brutor-step-name"] == "Tick: poll pending applications"
        assert c.headers["x-brutor-turn-seq"] == "1"
        assert c.headers["x-brutor-run-end"] == "completed"
        assert c.headers["x-brutor-run-outcome"] == "resolved"
    assert len({c.run_id for c in listing}) == 4  # a fresh run per tick
    # the tracker persists across restarts
    assert RetryTracker(tmp_path).is_skipped("APP-20260923-001")


def test_held_applications_are_excluded_from_the_batch(settings, fake, tmp_path):
    """Held ids never re-enter the batch, and never crowd it out either."""
    settings.max_per_tick = 2
    store = PendingApprovals(tmp_path)
    for n in range(1, 6):
        store.add(f"APP-HELD-{n}", f"apr-held-{n}", dict(fake.application, application_id=f"APP-HELD-{n}"), f"bds-{n}")
        fake.approval_statuses[f"apr-held-{n}"] = {"status": "pending"}
    # oldest-first listing: the five held ones come first, then the screenable one
    fake.pending_list = [{"application_id": f"APP-HELD-{n}"} for n in range(1, 6)] + [{"application_id": "APP-20260923-001"}, {"application_id": "APP-LATER"}]
    gw = fake.gateway(settings)
    sched = Scheduler(settings, gw, store, RetryTracker(tmp_path))
    summary = sched.tick()
    assert summary["held"] == [f"APP-HELD-{n}" for n in range(1, 6)]
    assert [p["application_id"] for p in summary["processed"]] == ["APP-20260923-001"] + ["APP-LATER"]
    listing = [c for c in fake.calls if c.tool == "applications_list_pending"][0]
    assert listing.body["params"]["arguments"]["limit"] >= 50
    # no run was opened for a held application
    assert not any(c.run_id and "APP-HELD" in c.run_id for c in fake.calls)
    assert sched.status["held_applications"] == 5 and sched.status["pending_approvals"] == 5
    # the pending holds were polled but not re-raised (still pending)
    assert len(fake.calls_of("approval_poll")) == 5
    assert not any(c.headers.get("x-brutor-step-id") == "reraise_approval" for c in fake.calls)


def test_scheduler_tick_survives_gateway_outage(settings, tmp_path):
    import httpx

    from screening_agent.gateway import Gateway

    def down(request):
        raise httpx.ConnectError("connection refused")

    gw = Gateway(settings, http=httpx.Client(transport=httpx.MockTransport(down)))
    sched = Scheduler(settings, gw, PendingApprovals(tmp_path), RetryTracker(tmp_path))
    summary = sched.tick()
    assert "error" in summary
    assert sched.status["tick_errors"] == 1 and sched.status["ticks"] == 1


def test_health_status_endpoints(settings, fake, tmp_path):
    from starlette.testclient import TestClient

    gw = fake.gateway(settings)
    sched = Scheduler(settings, gw, PendingApprovals(tmp_path), RetryTracker(tmp_path))
    sched.tick()
    client = TestClient(sched.health_app())
    assert client.get("/health").json()["status"] == "ok"
    status = client.get("/status").json()
    assert status["ticks"] == 1 and status["applications_processed"] == 1
    assert status["runs_completed"] == 1 and status["last_tick_at"]
    assert status["pending_approvals"] == 0


def test_llm_non_json_is_an_error(settings, fake, tmp_path):
    import httpx

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "not json"}, "finish_reason": "stop"}], "id": "x", "object": "chat.completion", "created": 1, "model": "m"})
        return fake.handler(request)

    from screening_agent.gateway import Gateway

    gw = Gateway(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    result = process_application(gw, settings, PendingApprovals(tmp_path), "APP-20260923-001")
    assert result.state == "errored" and "JSON" in result.error


def test_cli_refuses_to_rescreen_a_held_application(settings, fake, tmp_path, monkeypatch, capsys):
    from screening_agent import __main__ as cli

    store = PendingApprovals(tmp_path)
    store.add("APP-20260923-001", "apr-held", {"application_id": "APP-20260923-001"}, "bds-x")
    for name, value in {
        "BRUTOR_API_KEY": settings.api_key, "APPLICATIONS_MCP_SERVER_ID": "mcp-apps", "BUREAU_MCP_SERVER_ID": "mcp-bureau",
        "FRAUD_CARD_ID": "agentcard-fraud", "DATA_DIR": str(tmp_path), "BRUTOR_GATEWAY_URL": "http://gateway.test",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "Gateway", lambda s: fake.gateway(s))
    assert cli.main(["--application", "APP-20260923-001"]) == 3
    assert "held for an underwriter" in capsys.readouterr().out
    assert fake.calls == []
    assert cli.main(["--application", "APP-20260923-001", "--force"]) == 0
    assert fake.recorded and fake.recorded[0]["args"]["application_id"] == "APP-20260923-001"
