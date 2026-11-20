"""Scheduler behaviour under failures (DESIGN.md section 4, "Failure classes").

- System-class failures (gateway, provider, governance) never count against an
  application and never skip it; the tick stops and the scheduler backs off.
- Application-class failures skip after three attempts, and the skip hands the
  application off to the terminal status needs_manual_review.
- Held and skipped applications cannot starve new ones (the listing pages on).
- An operator can re-queue (origination system and the agent's skip list).
- A tracker file in any other format is refused and left as it is.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from screening_agent.__main__ import main as agent_main
from screening_agent.approvals import PendingApprovals
from screening_agent.failures import Failure, FailureClass
from screening_agent.scheduler import RetryFileError, RetryTracker, Scheduler
from tests.fake_gateway import POLICY_UNAVAILABLE, RUN_ABORTED_MESSAGE, SAMPLE_APPLICATION, skill_policy_denied

CREDITS_429 = {"error": {"code": "credit_balance_exhausted", "message": "You have no credits remaining.", "type": "insufficient_quota"}}
RESILIENCE_503 = {"error": {"message": "primary gated by resilience layer", "type": "gateway_resilience_gated"}}
AUTONOMY_403 = {"error": {"message": "autonomy_denied: AI System 'Brutor Demo System' is at approval_required"}}
RUN_ABORTED_403 = {"error": {"message": RUN_ABORTED_MESSAGE}}
GUARDRAIL_403 = {
    "jsonrpc": "2.0",
    "id": 1,
    "error": {"code": -32000, "message": "Blocked by guardrail", "data": {"code": "guardrail_blocked", "check": "prompt_injection", "direction": "output"}},
}
BASE = datetime(2026, 9, 27, tzinfo=UTC)


def _rpc_error(text: str) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": True}})


def _apps(fake, n: int, start: int = 1) -> list[str]:
    """n origination records, oldest first; the fake's sample application is
    marked screened so only these are pending."""
    fake.statuses[SAMPLE_APPLICATION["application_id"]] = "screened"
    ids = []
    for i in range(start, start + n):
        app_id = f"APP-20260927-{i:04d}"
        received = (BASE + timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
        fake.add_application(app_id, received)
        ids.append(app_id)
    return ids


def _scheduler(settings, fake, tmp_path) -> Scheduler:
    return Scheduler(settings, fake.gateway(settings), PendingApprovals(tmp_path), RetryTracker(tmp_path, max_attempts=3))


def _guardrail_on(app_id: str):
    return lambda tool, args, call: httpx.Response(403, json=GUARDRAIL_403) if tool == "applications_get" and args.get("application_id") == app_id else None


# -- system-class failures ------------------------------------------------------
SYSTEM_OUTAGES = {
    "llm-429-credit-balance-exhausted": lambda fake: setattr(fake, "llm_failure", lambda: httpx.Response(429, json=CREDITS_429)),
    "llm-503-gateway-resilience-gated": lambda fake: setattr(fake, "llm_failure", lambda: httpx.Response(503, json=RESILIENCE_503)),
    "skill-202-approval-required": lambda fake: setattr(
        fake, "intercept", lambda tool, args, call: _rpc_error("skill_error_202: approval_required") if tool == "skills__run_script" else None
    ),
    "mcp-403-autonomy-denied": lambda fake: setattr(
        fake, "intercept", lambda tool, args, call: httpx.Response(403, json=AUTONOMY_403) if tool == "applications_get" else None
    ),
    "mcp-403-run-aborted": lambda fake: setattr(
        fake, "intercept", lambda tool, args, call: httpx.Response(403, json=RUN_ABORTED_403) if tool == "bureau_get_report" else None
    ),
    # A stopped application run, refused on the skills system server as on any other.
    "skill-server-403-run-aborted": lambda fake: setattr(fake, "run_stopped", lambda run_id: None if run_id.startswith("bds-tick-") else "aborted"),
    "skill-server-429-run-over-budget": lambda fake: setattr(fake, "run_stopped", lambda run_id: None if run_id.startswith("bds-tick-") else "over_budget"),
    # The group's argument policies cannot be read: every call of the surface is refused.
    "skill-403-input-policy-unavailable": lambda fake: setattr(
        fake, "intercept", lambda tool, args, call: skill_policy_denied(**POLICY_UNAVAILABLE) if tool == "skills__run_script" else None
    ),
    "gateway-down-dns": lambda fake: setattr(fake, "down", True),
}
# With BACKOFF_MAX_TICKS=6: probe, skip 0, 1, 3, then 6 (capped) ticks between probes.
EXPECTED_PROBE_TICKS = [1, 2, 4, 8, 15, 22, 29, 36]


def _heal(fake) -> None:
    fake.llm_failure = None
    fake.intercept = None
    fake.run_stopped = None
    fake.down = False


@pytest.mark.parametrize("outage", list(SYSTEM_OUTAGES), ids=list(SYSTEM_OUTAGES))
def test_system_failures_never_skip_and_back_off(settings, fake, tmp_path, outage):
    ids = _apps(fake, 3)
    settings.backoff_max_ticks = 6
    sched = _scheduler(settings, fake, tmp_path)
    SYSTEM_OUTAGES[outage](fake)

    probe_ticks = []
    for tick in range(1, 41):
        before = len(fake.calls)
        summary = sched.tick()
        if len(fake.calls) > before:
            probe_ticks.append(tick)
            # the tick stops at the first system failure: at most one application run
            assert len(summary["processed"]) <= 1, summary
            assert summary["backoff"]["streak"] == len(probe_ticks)
        else:
            assert summary["backoff"]["active"] or summary["backoff"]["ticks_left"] == 0
        assert sched.backoff["ticks_left"] <= settings.backoff_max_ticks
    assert probe_ticks == EXPECTED_PROBE_TICKS

    # nothing was ever the applications' fault
    assert sched.tracker.skipped() == {}
    assert all(sched.tracker.attempts(a) == 0 for a in ids)
    assert fake.handed_off == [] and fake.notes == []
    assert not any(c.tool in ("applications_hand_off", "applications_add_note") for c in fake.calls)
    assert all(fake.status_of(a) == "received" for a in ids)
    assert sched.status["system_failures"] == len(EXPECTED_PROBE_TICKS)
    assert sched.status["ticks_backed_off"] == 40 - len(EXPECTED_PROBE_TICKS)

    # the system recovers: the next probe screens everything and clears the backoff
    _heal(fake)
    for _ in range(settings.backoff_max_ticks + 1):
        summary = sched.tick()
        if summary["processed"]:
            break
    assert sorted(p["application_id"] for p in summary["processed"]) == ids
    assert {p["state"] for p in summary["processed"]} == {"completed"}
    assert sched.backoff["streak"] == 0 and sched.backoff["ticks_left"] == 0


@pytest.mark.parametrize("outage", ["llm-503-gateway-resilience-gated", "llm-429-credit-balance-exhausted"])
def test_an_outage_probe_is_a_no_work_poll_beside_a_failed_screening(settings, fake, tmp_path, outage):
    """With declared outcomes the poll is its own run, closed completed with a
    declared outcome that does NOT count toward the baseline
    (applications_dispatched), and the screening is a run of its own that
    errors at the model call and reports no outcome. The gateway's success
    rate reads only the screening: the system is failing, not half working."""
    ids = _apps(fake, 2)
    sched = _scheduler(settings, fake, tmp_path)
    SYSTEM_OUTAGES[outage](fake)
    before = len(fake.calls)
    summary = sched.tick()
    assert summary["tick_outcome"] == "applications_dispatched"
    assert [(p["application_id"], p["state"], p["outcome"]) for p in summary["processed"]] == [(ids[0], "errored", None)]
    probe = fake.calls[before:]
    runs: dict[str, list] = {}
    for c in probe:
        runs.setdefault(c.run_id, []).append(c)
    tick_id, app_id = list(runs)
    # the tick run: page + closing call, nothing else, closed before the screening started
    assert tick_id.startswith("bds-tick-")
    assert [c.tool for c in runs[tick_id]] == ["applications_list_pending"] * 2
    assert probe[: len(runs[tick_id])] == runs[tick_id]
    assert runs[tick_id][-1].headers["x-brutor-run-end"] == "completed"
    assert runs[tick_id][-1].outcome_id == "applications_dispatched"
    # the screening run: its own id, failed at the model call, no close, no outcome
    assert app_id == summary["processed"][0]["run_id"] and app_id.startswith(f"bds-{ids[0]}-")
    assert [c.kind for c in runs[app_id]] == ["mcp", "mcp", "mcp", "mcp", "llm"]
    assert not any("x-brutor-run-end" in c.headers or c.outcome_id for c in runs[app_id])
    assert fake.run_ends == []  # the model never answered: no derived root to close with


def test_a_failing_listing_errors_the_tick_run(settings, fake, tmp_path):
    """applications_list_pending answers 500: the tick run's only action failed
    and nothing closes it, so the gateway sweeps it as errored (its last action
    failed) with no outcome. No application run is opened."""
    _apps(fake, 2)
    sched = _scheduler(settings, fake, tmp_path)
    fake.fail_tool = "applications_list_pending"
    summary = sched.tick()
    assert "error" in summary and summary["processed"] == []
    assert "tick_outcome" not in summary
    assert summary["backoff"]["last_failure"]["kind"] == "system/server_error"
    [call] = fake.calls
    assert call.tool == "applications_list_pending" and call.run_id.startswith("bds-tick-")
    assert "x-brutor-run-end" not in call.headers and call.outcome_id is None
    assert fake.run_ends == []
    assert sched.gw.ctx is None


def test_a_successful_empty_poll_reports_no_applications_available(settings, fake, tmp_path):
    """Nothing pending: one page and the closing call, in one run, closed
    completed / no_applications_available; no model call, no write."""
    fake.statuses[SAMPLE_APPLICATION["application_id"]] = "screened"
    sched = _scheduler(settings, fake, tmp_path)
    summary = sched.tick()
    assert summary["processed"] == [] and "error" not in summary
    assert summary["tick_outcome"] == "no_applications_available"
    page, close = fake.calls
    assert page.run_id == close.run_id and page.run_id.startswith("bds-tick-")
    assert page.body["params"]["arguments"] == {"limit": settings.list_limit}
    assert "x-brutor-run-end" not in page.headers and page.outcome_id is None
    assert close.body["params"]["arguments"] == {"limit": 1}
    assert close.headers["x-brutor-run-end"] == "completed"
    assert close.outcome_id == "no_applications_available"
    assert {c.kind for c in fake.calls} == {"mcp"}
    # a second tick is a second run
    sched.tick()
    assert len({c.run_id for c in fake.calls}) == 2


def test_a_tick_that_finds_work_reports_applications_dispatched_and_closes_first(settings, fake, tmp_path):
    ids = _apps(fake, 3)
    settings.max_per_tick = 2
    sched = _scheduler(settings, fake, tmp_path)
    summary = sched.tick()
    assert summary["tick_outcome"] == "applications_dispatched"
    assert [p["application_id"] for p in summary["processed"]] == ids[:2]
    assert [p["outcome"] for p in summary["processed"]] == ["application_processed.loan_approved"] * 2
    tick_calls = [c for c in fake.calls if c.run_id.startswith("bds-tick-")]
    assert fake.calls[:2] == tick_calls  # the tick run closed before any screening began
    assert tick_calls[-1].outcome_id == "applications_dispatched"
    assert {p["run_id"].split("-")[1] for p in summary["processed"]} == {"APP"}
    assert len({p["run_id"] for p in summary["processed"]}) == 2


def test_system_failure_stops_the_tick_and_the_tripping_application_goes_last(settings, fake, tmp_path):
    ids = _apps(fake, 3)
    sched = _scheduler(settings, fake, tmp_path)
    fake.llm_failure = lambda: httpx.Response(429, json=CREDITS_429)
    first = sched.tick()
    assert [p["application_id"] for p in first["processed"]] == [ids[0]]
    assert first["processed"][0]["failure"] == "system/rate_limited"
    assert "stopped" in first and first["backoff"]["streak"] == 1 and first["backoff"]["ticks_left"] == 0
    second = sched.tick()  # the next probe starts with the next application
    assert [p["application_id"] for p in second["processed"]] == [ids[1]]


def test_system_failures_between_application_failures_do_not_count(settings, fake, tmp_path):
    ids = _apps(fake, 1)
    settings.backoff_max_ticks = 1
    sched = _scheduler(settings, fake, tmp_path)
    fake.intercept = _guardrail_on(ids[0])
    sched.tick()
    assert sched.tracker.attempts(ids[0]) == 1
    fake.down = True
    for _ in range(6):
        sched.tick()
    fake.down = False
    for _ in range(3):  # wait out the backoff
        if sched.tick()["processed"]:
            break
    assert sched.tracker.attempts(ids[0]) == 2
    assert not sched.tracker.is_skipped(ids[0])


def test_tick_level_outage_backs_off(settings, fake, tmp_path):
    """applications_list_pending itself failing (DNS during a gateway restart)."""
    _apps(fake, 1)
    sched = _scheduler(settings, fake, tmp_path)
    fake.down = True
    summary = sched.tick()
    assert "error" in summary and summary["backoff"]["last_failure"]["kind"] == "system/unreachable"
    assert sched.status["tick_errors"] == 1


# -- application-class failures -------------------------------------------------
def test_application_failures_skip_after_three_and_hand_off(settings, fake, tmp_path):
    ids = _apps(fake, 3)
    bad = ids[0]
    sched = _scheduler(settings, fake, tmp_path)
    fake.intercept = _guardrail_on(bad)

    first = sched.tick()
    # an application-class failure does not stop the tick: the others are screened
    assert [(p["application_id"], p["state"]) for p in first["processed"]] == [(bad, "errored"), (ids[1], "completed"), (ids[2], "completed")]
    assert sched.status["runs_blocked"] == 1 and sched.status["runs_errored"] == 0  # a block is counted as blocked
    assert first["processed"][0]["failure"] == "application/content_blocked"
    assert sched.tracker.attempts(bad) == 1
    sched.tick()
    assert sched.tracker.attempts(bad) == 2 and not sched.tracker.is_skipped(bad)
    sched.tick()
    entry = sched.tracker.skipped()[bad]
    assert entry["handed_off"] is True and entry["failure_kind"] == "content_blocked" and entry["attempts"] == 3
    assert fake.status_of(bad) == "needs_manual_review"
    [hand_off] = [c for c in fake.calls if c.tool == "applications_hand_off"]
    assert hand_off.run_id.startswith(f"bds-{bad}-giveup-")
    assert hand_off.headers["x-brutor-step-id"] == "give_up"
    assert hand_off.headers["x-brutor-run-end"] == "completed"
    assert hand_off.outcome_id == "application_processed.sent_to_manual_review"
    assert "gave up after 3 attempts (content_blocked)" in fake.handed_off[0]["reason"]
    assert sched.backoff["streak"] == 0 and sched.status["system_failures"] == 0

    # it left the pending set: the next tick screens nothing and closes its
    # tick run on one more read-only listing call as no_applications_available
    before = len(fake.calls)
    summary = sched.tick()
    assert summary["processed"] == [] and summary["skipped"] == []
    assert [c.tool for c in fake.calls[before:]] == ["applications_list_pending"] * 2
    assert fake.calls[-1].headers["x-brutor-run-end"] == "completed"
    assert fake.calls[-1].outcome_id == "no_applications_available"


def test_a_skill_input_an_argument_policy_denies_is_the_applications(settings, fake, tmp_path):
    """The denial arrives as `skill_error_403: Argument policy (...)` in a tool
    result. It is a verdict on this application's figures, not a missing grant:
    the tick goes on, and the application is handed off after three attempts."""
    ids = _apps(fake, 2)
    bad = ids[0]
    fake.applications[bad]["requested_amount_eur"] = 58000
    fake.skill_policy = lambda args: "deny" if args["args"]["requested_amount_eur"] > 50000 else None
    sched = _scheduler(settings, fake, tmp_path)

    first = sched.tick()
    assert [(p["application_id"], p["state"]) for p in first["processed"]] == [(bad, "errored"), (ids[1], "completed")]
    assert first["processed"][0]["failure"] == "application/content_blocked"
    assert sched.tracker.attempts(bad) == 1
    assert sched.backoff["streak"] == 0 and sched.status["system_failures"] == 0
    sched.tick()
    sched.tick()
    entry = sched.tracker.skipped()[bad]
    assert entry["handed_off"] is True and entry["failure_kind"] == "content_blocked" and entry["attempts"] == 3
    assert fake.status_of(bad) == "needs_manual_review"


def test_a_held_skill_input_is_a_governance_hold_and_names_the_approval(settings, fake, tmp_path):
    """`skill_error_202: approval_required {envelope}`: the system's, never the
    application's, and the failure keeps the approval the gateway raised."""
    ids = _apps(fake, 2)
    fake.skill_policy = lambda args: "hold"
    sched = _scheduler(settings, fake, tmp_path)

    summary = sched.tick()
    assert [(p["application_id"], p["failure"]) for p in summary["processed"]] == [(ids[0], "system/approval_hold")]
    assert "apr-0001" in sched.backoff["last_failure"]["detail"]
    assert sched.tracker.attempts(ids[0]) == 0 and sched.tracker.skipped() == {}


def test_failed_hand_off_is_retried_next_tick(settings, fake, tmp_path):
    ids = _apps(fake, 1)
    bad = ids[0]
    sched = _scheduler(settings, fake, tmp_path)
    fake.intercept = _guardrail_on(bad)
    for _ in range(2):
        sched.tick()
    guard = fake.intercept
    fake.intercept = lambda tool, args, call: httpx.Response(503, json=RESILIENCE_503) if tool == "applications_hand_off" else guard(tool, args, call)
    summary = sched.tick()
    assert sched.tracker.is_skipped(bad) and sched.tracker.awaiting_hand_off() == [bad]
    assert summary["backoff"]["last_failure"]["kind"] == "system/server_error"  # the hand-off outage backs off
    fake.intercept = guard
    summary = sched.tick()
    assert summary["handed_off"] == [bad] and sched.tracker.awaiting_hand_off() == []
    assert fake.status_of(bad) == "needs_manual_review"


# -- starvation -----------------------------------------------------------------
def _hold(store: PendingApprovals, fake, app_id: str) -> None:
    store.add(app_id, f"apr-{app_id}", {"application_id": app_id, "recommendation": "decline", "amount_eur": 9000}, f"bds-{app_id}")
    fake.approval_statuses[f"apr-{app_id}"] = {"status": "pending"}


def test_a_full_window_of_held_and_skipped_applications_does_not_starve_new_ones(settings, fake, tmp_path):
    """120 held + 30 skipped (hand-off not through yet) are older than the two
    new applications and fill the server's 100-row page: the tick pages on."""
    settings.list_limit = 100
    settings.max_per_tick = 2
    store = PendingApprovals(tmp_path)
    tracker = RetryTracker(tmp_path, max_attempts=3)
    held = _apps(fake, 120)
    skipped = _apps(fake, 30, start=121)
    new = _apps(fake, 2, start=151)
    for app_id in held:
        _hold(store, fake, app_id)
    for app_id in skipped:
        tracker.skip(app_id, "errored: PolicyBlocked: blocked by policy: guardrail_blocked", "content_blocked")
    # hand-offs keep failing on the application's side (not a system failure)
    fake.intercept = lambda tool, args, call: _rpc_error("applications_hand_off: in_manual_review_conflict") if tool == "applications_hand_off" else None

    sched = Scheduler(settings, fake.gateway(settings), store, tracker)
    summary = sched.tick()
    assert [p["application_id"] for p in summary["processed"]] == new
    assert {p["state"] for p in summary["processed"]} == {"completed"}
    assert len(summary["held"]) == 120 and len(summary["skipped"]) == 30
    listing = [c for c in fake.calls if c.tool == "applications_list_pending"]
    pages, close = listing[:-1], listing[-1]
    assert [c.body["params"]["arguments"].get("after") for c in pages] == [None, held[99]]
    assert all(c.body["params"]["arguments"]["limit"] == 100 for c in pages)
    # both pages and the closing call are the tick's own run; each new
    # application is screened in a run of its own
    assert len({c.run_id for c in listing}) == 1 and listing[0].run_id.startswith("bds-tick-")
    assert not any("x-brutor-run-end" in c.headers for c in pages)
    assert close.body["params"]["arguments"] == {"limit": 1}
    assert close.headers["x-brutor-run-end"] == "completed" and close.outcome_id == "applications_dispatched"
    assert [p["run_id"].startswith(f"bds-{a}-") for p, a in zip(summary["processed"], new)] == [True, True]


def test_paging_is_bounded(settings, fake, tmp_path):
    settings.list_limit = 10
    settings.list_max_pages = 3
    store = PendingApprovals(tmp_path)
    for app_id in _apps(fake, 50):
        _hold(store, fake, app_id)
    sched = Scheduler(settings, fake.gateway(settings), store, RetryTracker(tmp_path))
    sched.tick()
    # three pages (the bound), then the closing call (nothing screenable)
    listing = [c for c in fake.calls if c.tool == "applications_list_pending"]
    assert len(listing) == 4 and listing[-1].headers["x-brutor-run-end"] == "completed"
    assert listing[-1].outcome_id == "no_applications_available"
    assert len({c.run_id for c in listing}) == 1


# -- requeue --------------------------------------------------------------------
def _hand_off_one(settings, fake, tmp_path):
    ids = _apps(fake, 1)
    bad = ids[0]
    sched = _scheduler(settings, fake, tmp_path)
    fake.intercept = _guardrail_on(bad)
    for _ in range(3):
        sched.tick()
    assert fake.status_of(bad) == "needs_manual_review"
    return sched, bad


def test_requeue_in_the_origination_system_is_picked_up(settings, fake, tmp_path):
    """`python -m applications_mcp.requeue` moves it back to received; the agent
    sees a handed-off application pending again, releases it and screens it."""
    sched, bad = _hand_off_one(settings, fake, tmp_path)
    fake.intercept = None
    fake.statuses[bad] = "received"
    summary = sched.tick()
    assert summary["released"] == [bad]
    assert [(p["application_id"], p["state"]) for p in summary["processed"]] == [(bad, "completed")]
    assert sched.tracker.skipped() == {} and sched.tracker.attempts(bad) == 0
    assert sched.tracker.released()[-1]["why"].startswith("pending again after hand-off")


def test_requeue_cli_clears_the_skip_list_of_the_running_scheduler(settings, fake, tmp_path, monkeypatch, capsys):
    ids = _apps(fake, 2)
    sched = _scheduler(settings, fake, tmp_path)
    # skipped locally, hand-off never went through (still pending upstream)
    sched.tracker.skip(ids[0], "errored: PolicyBlocked: blocked by policy: guardrail_blocked", "content_blocked")
    sched.tracker.skip(ids[1], "errored: PolicyBlocked: blocked by policy: guardrail_blocked", "content_blocked")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    assert agent_main(["--requeue", ids[0], "APP-19700101-0001"]) == 0
    out = capsys.readouterr().out
    assert f"{ids[0]}: released from the skip list" in out and "APP-19700101-0001: was not skipped" in out
    # the running scheduler's tracker re-reads the file
    assert not sched.tracker.is_skipped(ids[0]) and sched.tracker.is_skipped(ids[1])

    assert agent_main(["--requeue-all-skipped"]) == 0
    assert sched.tracker.skipped() == {}
    assert [e["why"] for e in sched.tracker.released()] == ["requeued by an operator"] * 2

    summary = sched.tick()
    assert sorted(p["application_id"] for p in summary["processed"]) == ids


# -- tracker file ---------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        {"attempts": {"APP-1": 2}, "skipped": {"APP-2": {"reason": "errored: ToolError: applications_get: unknown application APP-2", "attempts": 3}}},
        {"version": 1, "attempts": {}, "skipped": {}},
        {"version": 3, "attempts": {}, "skipped": {}, "released": []},
        ["APP-1"],
    ],
    ids=["no-version", "version-1", "version-3", "not-an-object"],
)
def test_a_tracker_file_in_another_format_is_refused(tmp_path, content):
    """One format. Anything else stops the agent with a sentence that names
    the file; the file is left byte for byte and nothing is written beside it."""
    raw = json.dumps(content, indent=2)
    path = tmp_path / "retries.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(RetryFileError) as refused:
        RetryTracker(tmp_path)
    assert str(path) in str(refused.value) and "format 2" in str(refused.value)
    assert path.read_text(encoding="utf-8") == raw
    assert sorted(p.name for p in tmp_path.iterdir()) == ["retries.json"]


def test_a_tracker_file_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "retries.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RetryFileError):
        RetryTracker(tmp_path)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_the_tracker_reads_back_what_it_wrote(tmp_path):
    tracker = RetryTracker(tmp_path)
    tracker.skip("APP-1", "errored: ToolError: applications_get: unknown application APP-1", "tool_error")
    on_disk = json.loads((tmp_path / "retries.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == 2 and set(on_disk) == {"version", "attempts", "skipped", "released"}
    assert RetryTracker(tmp_path).skipped() == tracker.skipped()


def test_requeue_cli_reports_a_refused_tracker_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "retries.json").write_text(json.dumps({"attempts": {}, "skipped": {}}), encoding="utf-8")
    assert agent_main(["--requeue-all-skipped"]) == 2
    assert "retries.json" in capsys.readouterr().err


def test_record_failure_refuses_system_failures(tmp_path):
    tracker = RetryTracker(tmp_path)
    with pytest.raises(ValueError):
        tracker.record_failure("APP-1", Failure(FailureClass.SYSTEM, "approval_hold", "skill_error_202: approval_required"))
    assert tracker.attempts("APP-1") == 0


def test_status_endpoint_reports_backoff(settings, fake, tmp_path):
    from starlette.testclient import TestClient

    _apps(fake, 1)
    sched = _scheduler(settings, fake, tmp_path)
    fake.llm_failure = lambda: httpx.Response(429, json=CREDITS_429)
    sched.tick()
    body = TestClient(sched.health_app()).get("/status").json()
    assert body["backoff"]["streak"] == 1 and body["backoff"]["last_failure"]["kind"] == "system/rate_limited"
    assert body["system_failures"] == 1 and body["skipped"] == {}
