import json

from screening_agent.approvals import PendingApprovals, resolve_pending


def test_store_roundtrip(tmp_path):
    store = PendingApprovals(tmp_path)
    assert store.all() == [] and store.count() == 0
    args = {"application_id": "APP-1", "recommendation": "decline", "amount_eur": 9000}
    entry = store.add("APP-1", "apr-1", args, "bds-APP-1-X")
    assert entry["apply_attempts"] == 0
    store.add("APP-2", "apr-2", {"application_id": "APP-2"}, "bds-APP-2-X")
    assert store.count() == 2
    assert store.application_ids() == {"APP-1", "APP-2"}
    on_disk = json.loads((tmp_path / "pending_approvals.json").read_text())
    assert on_disk[0]["tool_args"] == args
    # re-adding the same approval id replaces, never duplicates
    store.add("APP-1", "apr-1", args, "bds-APP-1-Y")
    assert store.count() == 2
    # one hold per application: a new approval id for APP-1 replaces the old entry
    store.add("APP-1", "apr-1b", args, "bds-APP-1-Z")
    assert store.count() == 2 and [e["approval_id"] for e in store.all() if e["application_id"] == "APP-1"] == ["apr-1b"]
    store.add("APP-1", "apr-1", args, "bds-APP-1-X")
    store.remove("apr-1")
    assert [e["approval_id"] for e in store.all()] == ["apr-2"]
    store.remove("missing")
    assert store.count() == 1


def test_store_survives_corrupt_file(tmp_path):
    (tmp_path / "pending_approvals.json").write_text("{not json")
    store = PendingApprovals(tmp_path)
    assert store.all() == []
    store.add("APP-1", "apr-1", {}, "r")
    assert store.count() == 1


ARGS = {"application_id": "APP-20260923-001", "recommendation": "decline", "amount_eur": 12000, "rationale": "r", "risk_band": "low", "affordability_class": "comfortable", "fraud_verdict": "clear", "customer_letter": "l"}


def _args(app_id):
    return dict(ARGS, application_id=app_id)


def _hold_on_decline(args):
    return args["recommendation"] == "decline" or args["amount_eur"] > 25000


def test_approved_applies_and_rejected_drops(settings, fake, tmp_path):
    store = PendingApprovals(tmp_path)
    store.add("APP-20260923-001", "apr-approved", ARGS, "bds-a")
    store.add("APP-20260923-002", "apr-rejected", _args("APP-20260923-002"), "bds-b")
    store.add("APP-20260923-003", "apr-pending", _args("APP-20260923-003"), "bds-d")
    fake.approval_rule = _hold_on_decline
    fake.approval_statuses = {
        "apr-approved": {"status": "approved", "approval_token": "tok-abc"},
        "apr-rejected": {"status": "rejected"},
        "apr-pending": {"status": "pending"},
    }
    gw = fake.gateway(settings)
    counts = resolve_pending(gw, settings, store)
    assert counts == {"approved": 1, "rejected": 1, "reraised": 0, "applied_without_hold": 0, "handed_off": 0, "pending": 1, "errored": 0}
    assert [e["approval_id"] for e in store.all()] == ["apr-pending"]

    # approved -> identical call re-issued with the token in its own run, closed resolved
    assert len(fake.recorded) == 1
    assert fake.recorded[0]["args"] == ARGS and fake.recorded[0]["token"] == "tok-abc"
    apply_calls = [c for c in fake.calls if c.tool == "applications_set_recommendation"]
    assert len(apply_calls) == 1
    h = apply_calls[0].headers
    assert h["x-brutor-run-id"].startswith("bds-APP-20260923-001-approval-")
    assert h["x-brutor-step-id"] == "apply_approved_decision"
    assert h["x-brutor-run-end"] == "completed" and h["x-brutor-run-outcome"] == "resolved"
    assert h["x-approval-token"] == "tok-abc"

    # rejected -> note in a short run, handed_off, dropped
    note_calls = [c for c in fake.calls if c.tool == "applications_add_note"]
    assert len(note_calls) == 1
    assert note_calls[0].headers["x-brutor-run-end"] == "completed"
    assert note_calls[0].headers["x-brutor-run-outcome"] == "handed_off"
    assert note_calls[0].headers["x-brutor-run-id"].startswith("bds-APP-20260923-002-approval-")
    assert "rejected" in fake.notes[0]["note"]
    assert gw.ctx is None


def test_expired_hold_is_reraised_not_dropped(settings, fake, tmp_path):
    store = PendingApprovals(tmp_path)
    store.add("APP-20260923-001", "apr-expired", ARGS, "bds-c")
    store.add("APP-20260923-002", "apr-gone", _args("APP-20260923-002"), "bds-e")  # 404 on poll
    fake.approval_rule = _hold_on_decline
    fake.approval_statuses = {"apr-expired": {"status": "expired"}}
    gw = fake.gateway(settings)
    counts = resolve_pending(gw, settings, store)
    assert counts["reraised"] == 2 and counts["handed_off"] == 0 and counts["applied_without_hold"] == 0
    entries = sorted(store.all(), key=lambda e: e["previous_approval_id"])
    assert [e["previous_approval_id"] for e in entries] == ["apr-expired", "apr-gone"]
    assert {e["approval_id"] for e in entries} == {"apr-0001", "apr-0002"}  # fresh ids from the gateway
    for e in entries:
        assert e["tool_args"] == _args(e["application_id"]) and e["reraise_count"] == 1
    # nothing recorded, no notes, both applications are still held
    assert fake.recorded == [] and fake.notes == []
    assert store.application_ids() == {"APP-20260923-001", "APP-20260923-002"}
    # the rehold runs: identical args, own run id, step, closed escalated on that one call
    rehold = [c for c in fake.calls if c.tool == "applications_set_recommendation"]
    assert len(rehold) == 2
    for c in rehold:
        app = c.body["params"]["arguments"]["application_id"]
        assert c.body["params"]["arguments"] == _args(app)
        assert "x-approval-token" not in c.headers
        assert c.run_id.startswith(f"bds-{app}-rehold-")
        assert c.headers["x-brutor-step-id"] == "reraise_approval"
        assert c.headers["x-brutor-step-name"] == "Re-raise underwriter approval"
        assert c.headers["x-brutor-turn-seq"] == "1"
        assert c.headers["x-brutor-run-end"] == "completed"
        assert c.headers["x-brutor-run-outcome"] == "escalated"
    assert len({c.run_id for c in rehold}) == 2
    assert fake.run_ends == []

    # the human approves the re-raised hold: it applies with the token as usual
    fake.approval_statuses["apr-0001"] = {"status": "approved", "approval_token": "tok-new"}
    fake.approval_statuses["apr-0002"] = {"status": "pending"}
    counts = resolve_pending(gw, settings, store)
    assert counts["approved"] == 1 and counts["pending"] == 1
    assert fake.recorded[0]["args"] == ARGS and fake.recorded[0]["token"] == "tok-new"
    assert [e["approval_id"] for e in store.all()] == ["apr-0002"]

    # a second expiry re-raises again and counts up
    fake.approval_statuses["apr-0002"] = {"status": "expired"}
    counts = resolve_pending(gw, settings, store)
    assert counts["reraised"] == 1
    assert store.all()[0]["reraise_count"] == 2 and store.all()[0]["approval_id"] == "apr-0003"


def test_reraise_recorded_without_hold_is_applied(settings, fake, tmp_path):
    store = PendingApprovals(tmp_path)
    store.add("APP-20260923-001", "apr-expired", ARGS, "bds-c")
    fake.approval_rule = None  # the policy no longer holds declines
    fake.approval_statuses = {"apr-expired": {"status": "expired"}}
    gw = fake.gateway(settings)
    counts = resolve_pending(gw, settings, store)
    assert counts["applied_without_hold"] == 1 and counts["reraised"] == 0
    assert store.all() == []
    assert fake.recorded[0]["args"] == ARGS and fake.recorded[0]["token"] is None


def test_reraise_max_hands_off(settings, fake, tmp_path):
    settings.reraise_max = 1
    store = PendingApprovals(tmp_path)
    store.add("APP-20260923-001", "apr-expired", ARGS, "bds-c")
    fake.approval_rule = _hold_on_decline
    fake.approval_statuses = {"apr-expired": {"status": "expired"}}
    gw = fake.gateway(settings)
    assert resolve_pending(gw, settings, store)["reraised"] == 1
    fake.approval_statuses["apr-0001"] = {"status": "expired"}
    counts = resolve_pending(gw, settings, store)
    assert counts["handed_off"] == 1 and counts["reraised"] == 0
    assert store.all() == []
    assert len(fake.notes) == 1 and "expired 2 times" in fake.notes[0]["note"]
    note_call = [c for c in fake.calls if c.tool == "applications_add_note"][0]
    assert note_call.headers["x-brutor-run-outcome"] == "handed_off"


def test_reraise_failure_keeps_entry(settings, fake, tmp_path):
    store = PendingApprovals(tmp_path)
    store.add("APP-20260923-001", "apr-expired", ARGS, "bds-c")
    fake.fail_tool = "applications_set_recommendation"
    fake.approval_statuses = {"apr-expired": {"status": "expired"}}
    gw = fake.gateway(settings)
    counts = resolve_pending(gw, settings, store)
    assert counts["errored"] == 1
    assert [e["approval_id"] for e in store.all()] == ["apr-expired"]
    assert store.application_ids() == {"APP-20260923-001"}
