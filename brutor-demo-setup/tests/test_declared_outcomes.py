"""RFC 0025 in provisioning: setup.py declares both AI Systems' outcome
catalogues from the agents' own outcomes.json files and the screening agent's
expected delegate outcomes on its dependency edge, idempotently, before the
contracts are minted; verify.py reads them back."""

import copy
import json
from pathlib import Path

import pytest

import setup
import verify
from test_release_identity import FakeCP, provisioner

SYSTEM_ROOT = Path(setup.SYSTEM_ROOT)
SCREENING_FILE = SYSTEM_ROOT / "brutor-demo-screening-agent" / "screening_agent" / "outcomes.json"
FRAUD_FILE = SYSTEM_ROOT / "brutor-demo-fraud-screener-agent" / "fraud_screener" / "outcomes.json"
SCREENING_IDS = {
    "application_processed.loan_approved", "application_processed.loan_rejected",
    "application_processed.loan_referred", "application_processed.held_for_underwriter",
    "application_processed.underwriter_decision_applied", "application_processed.underwriter_hold_renewed",
    "application_processed.underwriter_hold_closed", "application_processed.sent_to_manual_review",
    "no_applications_available", "applications_dispatched",
}
FRAUD_IDS = ["screening_completed.clear", "screening_completed.hit", "screening_completed.review"]
STARTER = [
    {"id": "nothing_to_do", "label": "Nothing to do", "counts_toward_baseline": False,
     "expect": {"max_actions": 2, "max_model_calls": 0, "max_cost_usd": 0}},
    {"id": "task_completed", "label": "Task completed", "counts_toward_baseline": True},
]


def _ids(catalogue):
    return {f"{o['id']}.{v['id']}" if v else o["id"] for o in catalogue for v in (o.get("variants") or [None])}


# --------------------------------------------------------------------------- #
# One source: the agents' files
# --------------------------------------------------------------------------- #
def test_the_catalogues_are_the_agents_files_verbatim():
    assert setup.outcome_catalogue(setup.SCREENING_DIST) == json.loads(SCREENING_FILE.read_text())["outcomes"]
    assert setup.outcome_catalogue(setup.FRAUD_DIST) == json.loads(FRAUD_FILE.read_text())["outcomes"]
    assert _ids(setup.outcome_catalogue(setup.SCREENING_DIST)) == SCREENING_IDS
    assert _ids(setup.outcome_catalogue(setup.FRAUD_DIST)) == set(FRAUD_IDS)


def test_the_screening_agent_expects_exactly_what_the_fraud_screener_declares():
    """Cross-component: the dependency's expected_outcomes (screening agent's
    file) are the fraud screener's declared ids (its own file)."""
    expected = setup.expected_delegate_outcomes(setup.SCREENING_DIST, setup.FRAUD_DIST)
    assert expected == FRAUD_IDS == sorted(_ids(setup.outcome_catalogue(setup.FRAUD_DIST)))


def test_normalize_ignores_order_and_echoed_defaults():
    want = setup.outcome_catalogue(setup.SCREENING_DIST)
    echoed = copy.deepcopy(list(reversed(want)))
    for o in echoed:
        o.setdefault("description", None)
        o["variants"] = list(reversed(o.get("variants") or []))
        for v in o["variants"]:
            v.setdefault("expect", None)
            v.setdefault("description", None)
        if isinstance(o.get("expect"), dict) and "forbids_capabilities" in o["expect"]:
            o["expect"]["forbids_capabilities"] = list(reversed(o["expect"]["forbids_capabilities"]))
    assert setup.normalize_catalogue(echoed) == setup.normalize_catalogue(want)
    changed = copy.deepcopy(want)
    changed[0]["variants"][0]["label"] = "Recommended: yes"
    assert setup.normalize_catalogue(changed) != setup.normalize_catalogue(want)
    flipped = copy.deepcopy(want)
    flipped[1]["counts_toward_baseline"] = True
    assert setup.normalize_catalogue(flipped) != setup.normalize_catalogue(want)


# --------------------------------------------------------------------------- #
# Step 9c: PUT the catalogues, only when they differ
# --------------------------------------------------------------------------- #
def _outcomes_cp(stored, *, keep=True, envelope=True):
    """A control plane holding one catalogue per AI System (rg-demo, rg-fraud)."""
    state = {gid: copy.deepcopy(cat) for gid, cat in stored.items()}

    def get(gid):
        return lambda _b: ({"ai_system_id": gid, "outcomes": copy.deepcopy(state[gid])} if envelope
                           else copy.deepcopy(state[gid]))

    def put(gid):
        def answer(body):
            if keep:
                state[gid] = copy.deepcopy(body["outcomes"])
            return {"ai_system_id": gid, "outcomes": copy.deepcopy(state[gid])}
        return answer

    routes = {}
    for gid in stored:
        routes[("GET", f"/v1/admin/ai-systems/{gid}/outcomes")] = get(gid)
        routes[("PUT", f"/v1/admin/ai-systems/{gid}/outcomes")] = put(gid)
    cp = FakeCP(routes)
    cp.state = state
    return cp


def _prov(cp):
    prov = provisioner(cp)
    prov.ids["fraud_group_id"] = "rg-fraud"
    return prov


def test_a_fresh_system_gets_both_catalogues_declared_once(capsys):
    cp = _outcomes_cp({"rg-demo": STARTER, "rg-fraud": STARTER})
    _prov(cp).declared_outcomes()
    assert cp.writes() == [
        ("PUT", "/v1/admin/ai-systems/rg-fraud/outcomes", {"outcomes": setup.outcome_catalogue(setup.FRAUD_DIST)}),
        ("PUT", "/v1/admin/ai-systems/rg-demo/outcomes", {"outcomes": setup.outcome_catalogue(setup.SCREENING_DIST)}),
    ]
    out = capsys.readouterr().out
    assert "✓ outcomes.fraud: declared screening_completed.clear, screening_completed.hit, screening_completed.review" in out
    assert "✓ outcomes.demo: declared application_processed.held_for_underwriter" in out
    # a re-run finds them stored and writes nothing (the contract does not drift)
    _prov(cp).declared_outcomes()
    assert len(cp.writes()) == 2
    assert "10 declared id(s) unchanged" in capsys.readouterr().out


def test_a_stored_catalogue_echoed_with_defaults_and_another_order_is_not_rewritten():
    demo = setup.outcome_catalogue(setup.SCREENING_DIST)
    echoed = [dict(o, description=o.get("description")) for o in reversed(demo)]
    cp = _outcomes_cp({"rg-demo": echoed, "rg-fraud": setup.outcome_catalogue(setup.FRAUD_DIST)})
    _prov(cp).declared_outcomes()
    assert cp.writes() == []


def test_a_bare_list_answer_is_tolerated():
    cp = _outcomes_cp({"rg-demo": setup.outcome_catalogue(setup.SCREENING_DIST),
                       "rg-fraud": setup.outcome_catalogue(setup.FRAUD_DIST)}, envelope=False)
    _prov(cp).declared_outcomes()
    assert cp.writes() == []


def test_only_the_system_whose_catalogue_differs_is_written():
    cp = _outcomes_cp({"rg-demo": STARTER, "rg-fraud": setup.outcome_catalogue(setup.FRAUD_DIST)})
    _prov(cp).declared_outcomes()
    assert [w[1] for w in cp.writes()] == ["/v1/admin/ai-systems/rg-demo/outcomes"]


def test_a_catalogue_the_control_plane_did_not_keep_is_an_error():
    cp = _outcomes_cp({"rg-demo": STARTER, "rg-fraud": STARTER}, keep=False)
    with pytest.raises(setup.StepError, match="did not keep"):
        _prov(cp).declared_outcomes()


def test_a_platform_without_the_endpoint_is_an_error_not_a_warning():
    with pytest.raises(setup.StepError, match="predates RFC 0025"):
        _prov(FakeCP({})).declared_outcomes()


def test_the_outcomes_are_declared_before_the_contracts_are_minted(monkeypatch):
    order = []
    names = ("login", "preflight", "models", "org_unit", "ai_systems", "bind_models", "mcp_servers", "skill",
             "agent_card", "identities_and_keys", "composition", "declared_outcomes", "portal_underwriter",
             "governance", "assurance", "compliance", "contract_and_gate", "write_env", "print_next")
    for name in names:
        monkeypatch.setattr(setup.Provisioner, name, lambda self, n=name: order.append(n))
    prov = provisioner(FakeCP({}))
    prov.residency = False
    prov.run()
    assert order.index("composition") < order.index("declared_outcomes") < order.index("contract_and_gate")


# --------------------------------------------------------------------------- #
# Step 9b: the dependency carries expected_outcomes
# --------------------------------------------------------------------------- #
DEPS = "/v1/admin/ai-systems/rg-demo/dependencies"


def _composition_cp(edges):
    state = {"edges": copy.deepcopy(edges)}

    def post(body):
        state["edges"].append(dict(body, id="dep-1"))
        return {"id": "dep-1"}

    def patch(body):
        state["edges"][0].update(body)
        return dict(state["edges"][0])

    cp = FakeCP({("GET", DEPS): lambda _b: {"dependencies": copy.deepcopy(state["edges"])},
                 ("POST", DEPS): post, ("PATCH", f"{DEPS}/dep-1"): patch})
    return cp


def _comp_prov(cp):
    prov = _prov(cp)
    prov.ids["fraud_card_id"] = "card-1"
    return prov


def _edge(**over):
    edge = {"id": "dep-1", "role": "shared_service", "depends_on_system_id": "rg-fraud", "agent_card_id": "card-1",
            "criticality": "critical", "contributes_to_decision": True, "contract_pin": None,
            "supplier_legal_entity": None, "expected_outcomes": FRAUD_IDS}
    edge.update(over)
    return edge


def test_a_new_edge_is_declared_with_the_expected_outcomes():
    cp = _composition_cp([])
    _comp_prov(cp).composition()
    [(method, path, body)] = cp.writes()
    assert (method, path) == ("POST", DEPS)
    assert body["expected_outcomes"] == FRAUD_IDS
    # re-run: nothing differs
    _comp_prov(cp).composition()
    assert len(cp.writes()) == 1


def test_an_edge_without_expected_outcomes_gets_only_that_patched():
    cp = _composition_cp([_edge(expected_outcomes=None)])
    _comp_prov(cp).composition()
    assert cp.writes() == [("PATCH", f"{DEPS}/dep-1", {"expected_outcomes": FRAUD_IDS})]
    _comp_prov(cp).composition()
    assert len(cp.writes()) == 1


def test_expected_outcomes_in_another_order_are_not_rewritten():
    cp = _composition_cp([_edge(expected_outcomes=list(reversed(FRAUD_IDS)))])
    _comp_prov(cp).composition()
    assert cp.writes() == []


def test_a_narrower_stored_list_is_converged():
    cp = _composition_cp([_edge(expected_outcomes=["screening_completed.clear"])])
    _comp_prov(cp).composition()
    assert cp.writes() == [("PATCH", f"{DEPS}/dep-1", {"expected_outcomes": FRAUD_IDS})]


# --------------------------------------------------------------------------- #
# Dry run and verify.py
# --------------------------------------------------------------------------- #
def test_the_dry_run_plan_names_both_catalogues_and_the_expected_outcomes(capsys):
    setup.dry_run(residency=False, skip_lifecycle=False)
    out = capsys.readouterr().out
    assert "9c. declared_outcomes: PUT /v1/admin/ai-systems/{fraud,demo}/outcomes" in out
    assert "screening_agent/outcomes.json" in out and "fraud_screener/outcomes.json" in out
    assert f"expected_outcomes {FRAUD_IDS}" in out


def _verify_cp(demo, fraud, expected):
    cp = _outcomes_cp({"rg-demo": demo, "rg-fraud": fraud})
    cp.routes[("GET", DEPS)] = {"dependencies": [_edge(expected_outcomes=expected)]}
    return cp


def _run_verify(cp, capsys):
    rep = verify.Report(brief=True)
    verify.check_declared_outcomes(cp, rep, "rg-demo", "rg-fraud")
    return rep, capsys.readouterr().out


def test_verify_reads_back_what_setup_declares(capsys):
    cp = _verify_cp(setup.outcome_catalogue(setup.SCREENING_DIST), setup.outcome_catalogue(setup.FRAUD_DIST),
                    list(reversed(FRAUD_IDS)))
    rep, out = _run_verify(cp, capsys)
    assert rep.failures == []
    assert "✓ outcomes.demo: 3 outcome(s) declared" in out and "✓ outcomes.fraud: 1 outcome(s) declared" in out
    assert "✓ outcomes.dependency:" in out


@pytest.mark.parametrize("which", ["demo", "fraud", "dependency"])
def test_verify_flags_a_catalogue_or_edge_that_differs(capsys, which):
    demo = STARTER if which == "demo" else setup.outcome_catalogue(setup.SCREENING_DIST)
    fraud = STARTER if which == "fraud" else setup.outcome_catalogue(setup.FRAUD_DIST)
    expected = None if which == "dependency" else FRAUD_IDS
    rep, _ = _run_verify(_verify_cp(demo, fraud, expected), capsys)
    assert rep.failures, which
