"""Scheduler behaviour under failures (DESIGN.md section 4, "Failure classes").

- System-class failures (gateway, provider, governance) never count against an
  application and never skip it; the tick stops and the scheduler backs off.
- Application-class failures skip after three attempts, and the skip hands the
  application off to the terminal status needs_manual_review.
- Held and skipped applications cannot starve new ones (the listing pages on).
- An operator can re-queue (origination system and the agent's skip list).
- A v1 tracker file is migrated on read without losing anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from screening_agent.__main__ import main as agent_main
from screening_agent.approvals import PendingApprovals
from screening_agent.scheduler import RETRIES_V1_BACKUP, RetryTracker, Scheduler
from tests.fake_gateway import SAMPLE_APPLICATION

CREDITS_429 = {"error": {"code": "credit_balance_exhausted", "message": "You have no credits remaining.", "type": "insufficient_quota"}}
RESILIENCE_503 = {"error": {"message": "primary gated by resilience layer", "type": "gateway_resilience_gated"}}
AUTONOMY_403 = {"error": {"message": "autonomy_denied: AI System 'Brutor Demo System' is at approval_required"}}
RUN_ABORTED_403 = {"error": {"message": "run aborted by an operator — refusing the next step"}}
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
    "gateway-down-dns": lambda fake: setattr(fake, "down", True),
}
# With BACKOFF_MAX_TICKS=6: probe, skip 0, 1, 3, then 6 (capped) ticks between probes.
EXPECTED_PROBE_TICKS = [1, 2, 4, 8, 15, 22, 29, 36]


def _heal(fake) -> None:
    fake.llm_failure = None
    fake.intercept = None
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
    assert [(p["application_id"], p["state"]) for p in first["processed"]] == [(bad, "blocked_policy"), (ids[1], "completed"), (ids[2], "completed")]
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
    assert hand_off.headers["x-brutor-run-end"] == "completed" and hand_off.headers["x-brutor-run-outcome"] == "handed_off"
    assert "gave up after 3 attempts (content_blocked)" in fake.handed_off[0]["reason"]
    assert sched.backoff["streak"] == 0 and sched.status["system_failures"] == 0

    # it left the pending set: the next tick lists nothing and opens no run
    before = len(fake.calls)
    summary = sched.tick()
    assert summary["processed"] == [] and summary["skipped"] == []
    assert [c.tool for c in fake.calls[before:]] == ["applications_list_pending"]


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
        tracker.skip(app_id, "blocked_policy: guardrail_blocked", "content_blocked")
    # hand-offs keep failing on the application's side (not a system failure)
    fake.intercept = lambda tool, args, call: _rpc_error("applications_hand_off: in_manual_review_conflict") if tool == "applications_hand_off" else None

    sched = Scheduler(settings, fake.gateway(settings), store, tracker)
    summary = sched.tick()
    assert [p["application_id"] for p in summary["processed"]] == new
    assert {p["state"] for p in summary["processed"]} == {"completed"}
    assert len(summary["held"]) == 120 and len(summary["skipped"]) == 30
    listing = [c for c in fake.calls if c.tool == "applications_list_pending"]
    assert [c.body["params"]["arguments"].get("after") for c in listing] == [None, held[99]]
    assert all(c.body["params"]["arguments"]["limit"] == 100 for c in listing)
    assert all(c.run_id.startswith("bds-tick-") and c.headers["x-brutor-run-end"] == "completed" for c in listing)


def test_paging_stops_when_the_server_ignores_the_cursor(settings, fake, tmp_path):
    """An applications MCP older than 0.2.0 ignores `after`: paging stops after
    one repeated page instead of looping (degraded, logged)."""
    settings.list_limit = 100
    store = PendingApprovals(tmp_path)
    for app_id in _apps(fake, 120):
        _hold(store, fake, app_id)
    _apps(fake, 1, start=121)
    fake.supports_cursor = False
    sched = Scheduler(settings, fake.gateway(settings), store, RetryTracker(tmp_path))
    summary = sched.tick()
    assert summary["processed"] == [] and "error" not in summary
    assert len([c for c in fake.calls if c.tool == "applications_list_pending"]) == 2


def test_paging_is_bounded(settings, fake, tmp_path):
    settings.list_limit = 10
    settings.list_max_pages = 3
    store = PendingApprovals(tmp_path)
    for app_id in _apps(fake, 50):
        _hold(store, fake, app_id)
    sched = Scheduler(settings, fake.gateway(settings), store, RetryTracker(tmp_path))
    sched.tick()
    assert len([c for c in fake.calls if c.tool == "applications_list_pending"]) == 3


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
    sched.tracker.skip(ids[0], "blocked_policy: guardrail_blocked", "content_blocked")
    sched.tracker.skip(ids[1], "blocked_policy: guardrail_blocked", "content_blocked")
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
V1_FILE = {
    "attempts": {"APP-20260928-0001": 2},
    "skipped": {
        "APP-20260927-0101": {"reason": "errored: ToolError: skills__run_script: skill_error_202: approval_required", "attempts": 3, "at": "2026-09-28T01:10:00Z"},
        "APP-20260927-0102": {
            "reason": 'errored: GatewayError: llm call failed (429): {"error": {"code": "credit_balance_exhausted", "message": "You have no credits remaining."}}',
            "attempts": 3,
            "at": "2026-09-27T22:00:00Z",
        },
        "APP-20260927-0103": {
            "reason": 'errored: GatewayError: llm call failed (503): {"error": {"message": "primary gated by resilience layer", "type": "gateway_resilience_gated"}}',
            "attempts": 3,
            "at": "2026-09-27T23:00:00Z",
        },
        "APP-20260927-0104": {
            "reason": 'blocked_policy: blocked by policy: {"error": {"check": "prompt_injection", "code": "guardrail_blocked", "direction": "input"}}',
            "attempts": 3,
            "at": "2026-09-27T20:00:00Z",
        },
    },
}


def test_v1_tracker_file_is_migrated_on_read(settings, fake, tmp_path):
    raw = json.dumps(V1_FILE, indent=2)
    (tmp_path / "retries.json").write_text(raw, encoding="utf-8")
    tracker = RetryTracker(tmp_path)

    # system-class skips released (screened again), the guardrail one kept for hand-off
    assert list(tracker.skipped()) == ["APP-20260927-0104"]
    kept = tracker.skipped()["APP-20260927-0104"]
    assert kept["handed_off"] is False and kept["failure_kind"] == "content_blocked" and kept["attempts"] == 3
    released = {e["application_id"]: e for e in tracker.released()}
    assert set(released) == {"APP-20260927-0101", "APP-20260927-0102", "APP-20260927-0103"}
    assert released["APP-20260927-0101"]["why"].startswith("migrated: system/approval_hold")
    assert released["APP-20260927-0102"]["reason"] == V1_FILE["skipped"]["APP-20260927-0102"]["reason"]
    assert tracker.attempts("APP-20260928-0001") == 0
    # nothing lost: the original is kept byte for byte, the new file is v2
    assert (tmp_path / RETRIES_V1_BACKUP).read_text(encoding="utf-8") == raw
    assert json.loads((tmp_path / "retries.json").read_text())["version"] == 2
    # idempotent: reading the v2 file again changes nothing
    assert RetryTracker(tmp_path).skipped() == tracker.skipped()

    # the next tick hands the kept one off
    fake.statuses[SAMPLE_APPLICATION["application_id"]] = "screened"
    fake.add_application("APP-20260927-0104", "2026-09-27T20:00:00Z")
    sched = Scheduler(settings, fake.gateway(settings), PendingApprovals(tmp_path), tracker)
    summary = sched.tick()
    assert summary["handed_off"] == ["APP-20260927-0104"]
    assert fake.status_of("APP-20260927-0104") == "needs_manual_review"


def test_record_failure_refuses_system_failures(tmp_path):
    from screening_agent.failures import classify_reason

    tracker = RetryTracker(tmp_path)
    with pytest.raises(ValueError):
        tracker.record_failure("APP-1", classify_reason("errored: ToolError: skills__run_script: skill_error_202: approval_required"))
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
