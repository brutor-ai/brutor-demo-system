"""Declared outcomes (RFC 0025; DESIGN.md section 8, "Declared outcomes").

- The shipped catalogue (screening_agent/outcomes.json) is a valid section-5
  document, and its ids are exactly the ones below (hand-listed).
- Every exit path of the graph, the approvals and the scheduler closes with a
  declared id; walked together, the exits report EVERY declared id (no
  declared outcome is unreachable) and nothing else. Failed runs report none.
- Each exit's run satisfies the `expect` its variant declares (the evidence
  the gateway will check).
- The fraud screener's outcome, as the gateway checked it, decides the fraud
  verdict; anything but a declared, expected outcome fails the run.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from screening_agent import outcomes
from screening_agent.approvals import PendingApprovals, resolve_pending
from screening_agent.gateway import DelegateDeviated, Gateway
from screening_agent.graph import process_application
from screening_agent.scheduler import RetryTracker, Scheduler
from tests.fake_gateway import SAMPLE_APPLICATION, FakeGateway, settings_for_tests

DECLARED = {
    "application_processed.loan_approved",
    "application_processed.loan_rejected",
    "application_processed.loan_referred",
    "application_processed.held_for_underwriter",
    "application_processed.underwriter_decision_applied",
    "application_processed.underwriter_hold_renewed",
    "application_processed.underwriter_hold_closed",
    "application_processed.sent_to_manual_review",
    "no_applications_available",
    "applications_dispatched",
}
FRAUD_EXPECTED = {"screening_completed.clear", "screening_completed.review", "screening_completed.hit"}
ID = re.compile(r"^[a-z0-9_-]{1,48}$")
EXPECT_KEYS = {"max_actions", "max_model_calls", "max_cost_usd", "requires_capabilities", "forbids_capabilities"}


# --------------------------------------------------------------------------- #
# The catalogue document
# --------------------------------------------------------------------------- #
def test_the_catalogue_is_the_hand_listed_set():
    assert outcomes.full_ids() == DECLARED
    assert set(outcomes.REPORTED) == DECLARED and len(outcomes.REPORTED) == len(DECLARED)
    assert outcomes.expected_delegate_outcomes() == FRAUD_EXPECTED


def _check_expect(expect):
    assert set(expect) <= EXPECT_KEYS, expect
    for key in ("max_actions", "max_model_calls"):
        if key in expect:
            assert isinstance(expect[key], int) and expect[key] >= 0
    if "max_cost_usd" in expect:
        assert isinstance(expect["max_cost_usd"], (int, float)) and expect["max_cost_usd"] >= 0
    for key in ("requires_capabilities", "forbids_capabilities"):
        if key in expect:
            assert isinstance(expect[key], list) and all(isinstance(c, str) and c for c in expect[key])


def test_the_catalogue_is_a_valid_section_5_document():
    doc = json.loads(outcomes.CATALOGUE_FILE.read_text(encoding="utf-8"))
    assert set(doc) == {"outcomes", "dependencies"}
    cat = doc["outcomes"]
    assert 1 <= len(cat) <= 32
    assert len({o["id"] for o in cat}) == len(cat)
    for o in cat:
        assert set(o) <= {"id", "label", "description", "counts_toward_baseline", "expect", "variants"}, o
        assert ID.fullmatch(o["id"]) and 1 <= len(o["label"]) <= 128
        assert isinstance(o["counts_toward_baseline"], bool)
        _check_expect(o.get("expect") or {})
        variants = o.get("variants") or []
        assert len(variants) <= 32 and len({v["id"] for v in variants}) == len(variants)
        for v in variants:
            assert set(v) <= {"id", "label", "description", "expect"}, v
            assert ID.fullmatch(v["id"]) and 1 <= len(v["label"]) <= 128
            _check_expect(v.get("expect") or {})
    [dep] = doc["dependencies"]
    assert dep["delegate"] == "brutor-demo-fraud-screener-agent" and dep["capability"] == "screening.fraud_sanctions"


def test_only_application_processed_counts_toward_the_baseline():
    """The two tick outcomes are polls (no work); every application exit is work."""
    counts = {o["id"]: o["counts_toward_baseline"] for o in outcomes.catalogue()}
    assert counts == {"application_processed": True, "no_applications_available": False, "applications_dispatched": False}
    assert outcomes.counts_toward_baseline(outcomes.LOAN_APPROVED) is True
    assert outcomes.counts_toward_baseline(outcomes.NO_APPLICATIONS_AVAILABLE) is False
    with pytest.raises(outcomes.UndeclaredOutcome):
        outcomes.counts_toward_baseline("resolved")


def test_recommendations_map_onto_the_three_loan_variants():
    from screening_agent.rules import RECOMMENDATIONS

    assert set(outcomes.RECOMMENDATION_OUTCOMES) == set(RECOMMENDATIONS)
    assert outcomes.RECOMMENDATION_OUTCOMES == {
        "approve": "application_processed.loan_approved",
        "decline": "application_processed.loan_rejected",
        "refer": "application_processed.loan_referred",
    }


def test_validate():
    for full in DECLARED:
        assert outcomes.validate(full) == full
    with pytest.raises(outcomes.UndeclaredOutcome):
        outcomes.validate("application_processed")
    with pytest.raises(ValueError, match="malformed"):
        outcomes.validate("Application_processed.loan_approved")


# --------------------------------------------------------------------------- #
# The exit walk
# --------------------------------------------------------------------------- #
ARGS = {"application_id": "APP-20260923-001", "recommendation": "decline", "amount_eur": 12000, "rationale": "r",
        "risk_band": "low", "affordability_class": "comfortable", "fraud_verdict": "clear", "customer_letter": "l"}


def _env(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    settings = settings_for_tests(d)
    return settings, FakeGateway(), PendingApprovals(d), d


def _screen(fake, settings, store):
    return process_application(fake.gateway(settings), settings, store, "APP-20260923-001")


def _graph_exit(tmp_path, name, **fake_attrs):
    settings, fake, store, _ = _env(tmp_path, name)
    for k, v in fake_attrs.items():
        setattr(fake, k, v)
    result = _screen(fake, settings, store)
    assert result.state == "completed"
    return fake, result.outcome


def _approval_exit(tmp_path, name, status, *, intercept=None, apply_failures=1):
    settings, fake, store, _ = _env(tmp_path, name)
    store.add("APP-20260923-001", "apr-1", ARGS, "bds-x")
    fake.approval_rule = lambda args: args["recommendation"] == "decline"
    fake.approval_statuses = {"apr-1": status}
    fake.intercept = intercept
    gw = fake.gateway(settings)
    for _ in range(apply_failures):
        resolve_pending(gw, settings, store)
    return fake


def _scheduler_exit(tmp_path, name, *, pending):
    settings, fake, store, d = _env(tmp_path, name)
    if not pending:
        fake.statuses[SAMPLE_APPLICATION["application_id"]] = "screened"
    summary = Scheduler(settings, fake.gateway(settings), store, RetryTracker(d)).tick()
    return fake, summary


def _give_up_exit(tmp_path):
    settings, fake, store, d = _env(tmp_path, "giveup")
    fake.block_tool = "applications_get"
    sched = Scheduler(settings, fake.gateway(settings), store, RetryTracker(d, max_attempts=3))
    for _ in range(3):
        sched.tick()
    return fake


def _unknown_tool(tool):
    return lambda t, args, call: (
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": f"Unknown tool: {t}"}], "isError": True}})
        if t == tool else None
    )


def _invalid(tool):
    return lambda t, args, call: httpx.Response(422, json={"error": "invalid"}) if t == tool else None


def walk_exits(tmp_path) -> dict[str, list[FakeGateway]]:
    """Run every exit path once; map each reported outcome id to the fakes
    (gateways) whose calls reported it."""
    seen: dict[str, list[FakeGateway]] = {}

    def note(fake):
        for c in fake.calls:
            if c.outcome_id and c.headers.get("x-brutor-run-end") or (c.kind == "run_end" and c.outcome_id):
                seen.setdefault(c.outcome_id, []).append(fake)

    # graph: the three recommendations and the hold
    for name, attrs in {
        "approve": {},
        "decline": {"fraud_verdict": "hit"},
        "refer": {"identity_verified": False},
        "hold": {"approval_rule": lambda a: a["recommendation"] == "decline", "affordability_class": "unaffordable"},
    }.items():
        fake, _ = _graph_exit(tmp_path, name, **attrs)
        note(fake)
    # approvals: applied, rejected -> hand-off, expired -> renewed, expired past
    # RERAISE_MAX -> hand-off, hand-off unavailable -> note, apply refused 3x -> note
    note(_approval_exit(tmp_path, "applied", {"status": "approved", "approval_token": "t"}))
    note(_approval_exit(tmp_path, "rejected", {"status": "rejected"}))
    note(_approval_exit(tmp_path, "renewed", {"status": "expired"}))
    note(_approval_exit(tmp_path, "noted", {"status": "rejected"}, intercept=_unknown_tool("applications_hand_off")))
    note(_approval_exit(tmp_path, "unapplied", {"status": "approved", "approval_token": "t"},
                        intercept=_invalid("applications_set_recommendation"), apply_failures=3))
    # scheduler: empty poll, poll that found work, give-up hand-off
    note(_scheduler_exit(tmp_path, "empty", pending=False)[0])
    note(_scheduler_exit(tmp_path, "work", pending=True)[0])
    note(_give_up_exit(tmp_path))
    return seen


def test_every_exit_reports_a_declared_id_and_every_declared_id_is_reachable(tmp_path):
    seen = walk_exits(tmp_path)
    assert set(seen) == DECLARED == outcomes.full_ids()


@pytest.mark.parametrize(
    "attrs, expected",
    [
        ({}, "application_processed.loan_approved"),
        ({"fraud_verdict": "hit"}, "application_processed.loan_rejected"),
        ({"affordability_class": "unaffordable"}, "application_processed.loan_rejected"),
        ({"drafter_recommendation": "decline"}, "application_processed.loan_rejected"),
        ({"identity_verified": False}, "application_processed.loan_referred"),
        ({"fraud_verdict": "review"}, "application_processed.loan_referred"),
        ({"risk_band": "high"}, "application_processed.loan_referred"),
        ({"drafter_recommendation": "refer"}, "application_processed.loan_referred"),
        ({"approval_rule": lambda a: True}, "application_processed.held_for_underwriter"),
    ],
)
def test_graph_exits(tmp_path, attrs, expected):
    fake, outcome = _graph_exit(tmp_path, "g", **attrs)
    assert outcome == expected
    closing = [c for c in fake.calls if c.outcome_id]
    # the record call carries the outcome; when held, the note closes the run
    assert closing[-1].outcome_id == expected and closing[-1] is fake.calls[-1]
    assert fake.run_ends == []


def test_approval_exits(tmp_path):
    def closes(fake):
        return [(c.tool, c.outcome_id) for c in fake.calls if c.outcome_id]

    assert closes(_approval_exit(tmp_path, "a", {"status": "approved", "approval_token": "t"})) == [
        ("applications_set_recommendation", "application_processed.underwriter_decision_applied")]
    assert closes(_approval_exit(tmp_path, "b", {"status": "rejected"})) == [
        ("applications_hand_off", "application_processed.sent_to_manual_review")]
    assert closes(_approval_exit(tmp_path, "c", {"status": "expired"})) == [
        ("applications_set_recommendation", "application_processed.underwriter_hold_renewed")]
    assert closes(_approval_exit(tmp_path, "d", {"status": "rejected"}, intercept=_unknown_tool("applications_hand_off"))) == [
        ("applications_hand_off", "application_processed.sent_to_manual_review"),  # attempted, refused
        ("applications_add_note", "application_processed.underwriter_hold_closed")]
    # past RERAISE_MAX the lapsed hold is handed off
    settings, fake, store, _ = _env(tmp_path, "e")
    settings.reraise_max = 1
    store.add("APP-20260923-001", "apr-1", dict(ARGS), "bds-x")
    store.update(dict(store.all()[0], reraise_count=1))
    fake.approval_statuses = {"apr-1": {"status": "expired"}}
    resolve_pending(fake.gateway(settings), settings, store)
    assert closes(fake) == [("applications_hand_off", "application_processed.sent_to_manual_review")]


def test_failed_runs_report_no_outcome(tmp_path):
    for name, attrs in {
        "block": {"block_tool": "applications_get"},
        "late-block": {"block_tool": "applications_set_recommendation"},
        "a2a": {"fail_a2a": True},
        "tool": {"fail_tool": "bureau_get_report"},
        "model": {"llm_failure": lambda: httpx.Response(503, json={"error": "down"})},
        "deviated": {"fraud_conformance": "undeclared", "fraud_outcome_id": "screening_completed.maybe"},
    }.items():
        settings, fake, store, _ = _env(tmp_path, name)
        for k, v in attrs.items():
            setattr(fake, k, v)
        result = _screen(fake, settings, store)
        assert result.state == "errored" and result.outcome is None, name
        # a call may carry the close it intended (the failing record call); none was answered
        answered_closes = [c for c in fake.calls if c.outcome_id and c.tool != attrs.get("block_tool")]
        assert answered_closes == [], name
        assert all("outcome_id" not in (e["body"] or {}) for e in fake.run_ends), name
        assert all(e["body"]["state"] == "errored" for e in fake.run_ends), name


# --------------------------------------------------------------------------- #
# Evidence: each exit's run is what its variant's `expect` says
# --------------------------------------------------------------------------- #
def _merged_expect(full_id):
    head, _, variant = full_id.partition(".")
    [o] = [o for o in outcomes.catalogue() if o["id"] == head]
    expect = dict(o.get("expect") or {})
    for v in o.get("variants") or []:
        if v["id"] == variant:
            expect.update(v.get("expect") or {})
    return expect


def test_every_exit_satisfies_its_declared_evidence(tmp_path):
    seen = walk_exits(tmp_path)
    checked = 0
    for full_id, fakes in seen.items():
        expect = _merged_expect(full_id)
        for fake in fakes:
            runs = fake.runs()
            for run_id, calls in runs.items():
                closing = [c for c in calls if c.outcome_id == full_id and c.headers.get("x-brutor-run-end")]
                if not closing:
                    continue
                actions = [c for c in calls if c.kind in ("llm", "mcp", "a2a")]
                tools = {c.tool for c in actions if c.tool}
                model_calls = sum(1 for c in actions if c.kind == "llm")
                if "max_actions" in expect:
                    assert len(actions) <= expect["max_actions"], (full_id, run_id)
                if "max_model_calls" in expect:
                    assert model_calls <= expect["max_model_calls"], (full_id, run_id)
                if "max_cost_usd" in expect and expect["max_cost_usd"] == 0:
                    assert model_calls == 0, (full_id, run_id)  # only model calls cost money here
                assert set(expect.get("requires_capabilities", [])) <= tools, (full_id, run_id, tools)
                assert not set(expect.get("forbids_capabilities", [])) & tools, (full_id, run_id, tools)
                checked += 1
    assert checked >= len(DECLARED)


def test_a_paging_tick_stays_within_its_evidence(tmp_path):
    """No max_actions on the tick outcomes: a tick pages (LIST_MAX_PAGES) and
    then closes, so it may take pages + 1 actions, but never a model call or a write."""
    settings, fake, store, d = _env(tmp_path, "paging")
    settings.list_limit = 10
    settings.list_max_pages = 3
    for i in range(30):
        app_id = f"APP-H-{i:03d}"
        fake.add_application(app_id, f"2026-09-01T00:{i:02d}:00Z")
        store.add(app_id, f"apr-{i}", {"application_id": app_id}, "bds-x")
        fake.approval_statuses[f"apr-{i}"] = {"status": "pending"}
    fake.statuses[SAMPLE_APPLICATION["application_id"]] = "screened"
    Scheduler(settings, fake.gateway(settings), store, RetryTracker(d)).tick()
    [tick] = [calls for rid, calls in fake.runs().items() if rid.startswith("bds-tick-")]
    assert len(tick) == settings.list_max_pages + 1
    assert tick[-1].outcome_id == "no_applications_available"
    expect = _merged_expect("no_applications_available")
    assert "max_actions" not in expect and expect["max_model_calls"] == 0
    assert {c.tool for c in tick} == {"applications_list_pending"}


# --------------------------------------------------------------------------- #
# Delegated outcomes (A2A, RFC 0025 section 7a)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("carrier", ["header", "metadata", "both"])
@pytest.mark.parametrize("verdict, recommendation", [("clear", "approve"), ("review", "refer"), ("hit", "decline")])
def test_a_declared_delegate_outcome_decides_the_verdict(settings, fake, tmp_path, carrier, verdict, recommendation):
    fake.fraud_outcome_carrier = carrier
    fake.fraud_verdict = verdict
    result = process_application(fake.gateway(settings), settings, PendingApprovals(tmp_path), "APP-20260923-001")
    assert result.state == "completed"
    assert fake.recorded[0]["args"]["fraud_verdict"] == verdict
    assert fake.recorded[0]["args"]["recommendation"] == recommendation


@pytest.mark.parametrize("carrier", ["header", "metadata"])
def test_the_checked_outcome_wins_over_the_body(settings, fake, tmp_path, carrier):
    """The body says clear; the gateway-checked outcome says hit: hit decides."""
    fake.fraud_outcome_carrier = carrier
    fake.fraud_verdict = "clear"
    fake.fraud_outcome_id = "screening_completed.hit"
    result = process_application(fake.gateway(settings), settings, PendingApprovals(tmp_path), "APP-20260923-001")
    assert result.state == "completed" and result.outcome == "application_processed.loan_rejected"
    assert fake.recorded[0]["args"]["fraud_verdict"] == "hit"


DEVIATIONS = {
    # the gateway judged the delegate's own answer against its contract
    "undeclared": dict(fraud_outcome_id="screening_completed.maybe", fraud_conformance="undeclared"),
    "unreported": dict(fraud_outcome_id="", fraud_conformance="unreported"),
    # the gateway did not judge it at all (no conformance on either carrier)
    "not-judged": dict(fraud_conformance=None),
    "no-carrier": dict(fraud_outcome_carrier="none"),
    # declared by the screener's contract, but not one this agent handles
    "unexpected": dict(fraud_outcome_id="screening_completed.escalate", fraud_conformance="declared"),
    "other-outcome": dict(fraud_outcome_id="screening_skipped", fraud_conformance="declared"),
    # a value outside the vocabulary
    "bad-conformance": dict(fraud_conformance="maybe"),
    "malformed-id": dict(fraud_outcome_id="Screening Completed", fraud_conformance="declared"),
    # header and metadata disagree
    "disagree-id": dict(fraud_outcome_carrier="both", fraud_metadata_override={"brutor.outcome.id": "screening_completed.clear"}, fraud_verdict="hit"),
    "disagree-conformance": dict(fraud_outcome_carrier="both", fraud_metadata_override={"brutor.outcome.conformance": "undeclared"}),
}


@pytest.mark.parametrize("carrier", ["header", "metadata"])
@pytest.mark.parametrize("case", list(DEVIATIONS), ids=list(DEVIATIONS))
def test_a_deviating_delegate_fails_the_run_like_an_unreachable_one(settings, fake, tmp_path, carrier, case):
    fake.fraud_outcome_carrier = carrier
    for k, v in DEVIATIONS[case].items():
        setattr(fake, k, v)
    result = process_application(fake.gateway(settings), settings, PendingApprovals(tmp_path), "APP-20260923-001")
    assert result.state == "errored" and result.outcome is None
    assert result.failure.label() == "system/delegate_deviated", result.error
    assert result.error.startswith("DelegateDeviated:")
    # nothing recorded; the run is closed errored via the derived root (the classifier answered)
    assert fake.recorded == [] and fake.notes == []
    assert fake.run_ends == [{"root": "root-derived-0001", "body": {"state": "errored"}}]
    # the graph stopped at fraud_screen: no drafter call
    assert [c.kind for c in fake.calls if c.kind in ("llm", "a2a")] == ["llm", "a2a"]


def test_a_deviating_delegate_backs_the_scheduler_off_and_never_skips(settings, fake, tmp_path):
    fake.fraud_outcome_id = "screening_completed.maybe"
    fake.fraud_conformance = "undeclared"
    sched = Scheduler(settings, fake.gateway(settings), PendingApprovals(tmp_path), RetryTracker(tmp_path))
    summary = sched.tick()
    assert summary["processed"][0]["failure"] == "system/delegate_deviated"
    assert summary["backoff"]["streak"] == 1 and sched.tracker.attempts("APP-20260923-001") == 0


def test_a2a_delegate_returns_the_checked_answer(settings, fake):
    gw = fake.gateway(settings)
    fake.fraud_verdict = "review"
    with gw.run("bds-APP-1-X"):
        answer = gw.a2a_delegate("agentcard-fraud", "screening.fraud_sanctions", {"full_name": "A", "bureau": {}},
                                 expected_outcomes=FRAUD_EXPECTED, step_id="assess")
    assert (answer.outcome_id, answer.conformance, answer.variant) == ("screening_completed.review", "declared", "review")
    assert answer.verdict["verdict"] == "review"
    # the delegation call never closes the run and never carries an outcome
    [call] = fake.calls_of("a2a")
    assert "x-brutor-run-end" not in call.headers and call.outcome_id is None


def test_without_expected_outcomes_any_declared_outcome_is_accepted(settings, fake):
    gw = fake.gateway(settings)
    fake.fraud_outcome_id = "screening_completed.escalate"
    with gw.run("bds-APP-1-X"):
        answer = gw.a2a_delegate("agentcard-fraud", "screening.fraud_sanctions", {"full_name": "A", "bureau": {}})
    assert answer.outcome_id == "screening_completed.escalate"
    with gw.run("bds-APP-1-Y"), pytest.raises(DelegateDeviated, match="not one of the outcomes"):
        gw.a2a_delegate("agentcard-fraud", "screening.fraud_sanctions", {"full_name": "A", "bureau": {}}, expected_outcomes=FRAUD_EXPECTED)


def test_a_failed_task_is_delegate_failed_before_any_outcome_check(settings, fake):
    """A FAILED task with no outcome is the old delegate_failed, not a deviation."""
    from screening_agent.failures import classify_failure
    from screening_agent.gateway import DelegateFailed

    def failed(request):
        return httpx.Response(200, json={"task": {"status": {"state": "TASK_STATE_FAILED"}}})

    gw = Gateway(settings, http=httpx.Client(transport=httpx.MockTransport(failed)))
    with gw.run("bds-APP-1-X"), pytest.raises(DelegateFailed) as info:
        gw.a2a_delegate("agentcard-fraud", "screening.fraud_sanctions", {"full_name": "A"})
    assert not isinstance(info.value, DelegateDeviated)
    assert classify_failure(info.value).label() == "system/delegate_failed"
