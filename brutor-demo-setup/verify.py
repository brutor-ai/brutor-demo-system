#!/usr/bin/env python3
"""verify.py: read back what the Brutor Demo System has produced.

Reads health, runs, assurance, contract, lifecycle gate, obligations, evidence,
transparency, oversight, pending approvals and the Annex IV documentation
export for the demo AI System, and checks the expectations in DESIGN.md
section 9 against the most recent runs. It also reads back the agents'
implementation identity: each identity's declared implementation
name, approved versions and require_release as setup.py declares them, the
demo system's empty intended_client_labels, and the latest release the
gateway observed per identity with its trust. And the declared outcomes:
each AI System's stored catalogue against its agent's
outcomes.json, and the dependency's expected_outcomes. And workload identity:
the demo's SPIRE as an identity provider with the audience `brutor`, and each
agent identity's subject the agent's SPIFFE ID.

Usage:
    python verify.py                 full report, exit 1 if an expectation fails
    python verify.py --brief         one line per surface (used by demo.sh status)
    python verify.py --period-hours 72
    python verify.py --open-incident open a demo Art 73 serious incident linked
                                     to the latest run (never done automatically)
    python verify.py --json          print the raw payloads as one JSON document
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from setup import (  # noqa: E402  (shares the client and config with setup.py)
    CP_URL, DEMO_SYSTEM_NAME, FRAUD_DIST, FRAUD_IDENTITY, FRAUD_SPIFFE_ID,
    FRAUD_SYSTEM_NAME, GW_URL, SCREENING_DIST, SCREENING_IDENTITY, SCREENING_SPIFFE_ID,
    SPIRE_JWKS_URI, SPIRE_OIDC_ISSUER, TENANT_ID, WORKLOAD_AUDIENCE, Client,
    StepError, _stored_catalogue, expected_delegate_outcomes, find, implementation_declaration, listing,
    admin_login, normalize_catalogue, outcome_catalogue, read_demo_env,
)

# (label, identity name, component distribution) per agent.
AGENT_RELEASES = (
    ("screening", SCREENING_IDENTITY, SCREENING_DIST),
    ("fraud", FRAUD_IDENTITY, FRAUD_DIST),
)

# A full run declares 3 steps (gather, assess, decide) and 3 turns (t1 and t2
# of the screening agent plus the fraud screener's own turn at depth 1, which
# declares no step) over 9 actions (10 when held for an underwriter: the note): llm 3, a2a 1,
# skill 1, tools 4 (DESIGN.md section 4).
EXPECT_STEPS = 3
EXPECT_TURNS = 3
EXPECT_ACTIONS = 9
EXPECT_LLM_CALLS = 3
GOOD_CHAIN = ("intact", "client_asserted")


def is_full_run(row: Dict[str, Any]) -> bool:
    """A row of the runs list that declared the three steps of a screening.
    The agent's housekeeping runs (tick poll, hand-off, approval follow-up)
    declare one."""
    return row.get("step_count") == EXPECT_STEPS


def run_counts_met(row: Dict[str, Any], detail: Dict[str, Any]) -> bool:
    """A full run's counts, from its row in the runs list and its detail:
    intact chain, three model calls, one skill call, one A2A call, three
    turns, nine actions (ten when held: the note). A count that was not
    answered is not met."""
    turns, actions = row.get("turn_count"), row.get("action_count")
    return (row.get("chain_integrity") in GOOD_CHAIN
            and (row.get("llm_call_count") or 0) >= EXPECT_LLM_CALLS
            and detail.get("skill_call_count") == 1
            and detail.get("a2a_call_count") == 1
            and turns is not None and turns >= EXPECT_TURNS
            and actions is not None and actions >= EXPECT_ACTIONS)


def login() -> Client:
    cp = Client(CP_URL)
    admin_login(cp)
    return cp


def resolve_group(cp: Client, env: Dict[str, str], key: str, name: str) -> Optional[str]:
    if env.get(key):
        return env[key]
    _, resp = cp.get("/v1/admin/resource-groups")
    row = find(listing(resp, "groups"), name=name)
    return row["id"] if row else None


class Report:
    def __init__(self, brief: bool):
        self.brief = brief
        self.failures: List[str] = []
        self.payloads: Dict[str, Any] = {}

    def line(self, label: str, text: str, good: Optional[bool] = None) -> None:
        mark = "✓" if good else ("⚠" if good is None else "✗")
        print(f"{mark} {label}: {text}")
        if good is False:
            self.failures.append(f"{label}: {text}")

    def detail(self, text: str) -> None:
        if not self.brief:
            print(f"    {text}")


def health_summary(health: Dict[str, Any]) -> Tuple[str, str]:
    """(state, per-signal summary) of GET /v1/admin/ai-systems/{id}/health.

    The Control Plane answers `health_state` (learning, healthy, ...) with a
    `health_score` and `signal_levels` = {signal: {"score", "level"}}. A
    payload without a state says so; the line never prints `None`."""
    state = health.get("health_state") or "state not reported"
    score = health.get("health_score")
    status = f"{state}" + (f" score={score}" if score is not None else "")
    signals = health.get("signal_levels")
    signals = signals if isinstance(signals, dict) else {}

    def one(value: Any) -> str:
        score = value.get("score") if isinstance(value, dict) else None
        return "unmeasured" if score is None else f"{score}"

    summary = ", ".join(f"{k}={one(v)}" for k, v in signals.items())
    return status, summary


def check_agent_releases(cp: Client, rep: "Report", gid: str) -> None:
    """Agent release read-back. Declarations must be exactly what setup.py declares
    (✗ otherwise). The latest observed release is ⚠ while none has been seen
    (no run yet), ✓ when approved under the
    declared name, ✗ when it is unapproved or names other software."""
    cp.step = "client_labels"
    _, group = cp.get(f"/v1/admin/resource-groups/{gid}", tolerate=(404,))
    group = group if isinstance(group, dict) else {}
    labels = group.get("intended_client_labels")
    rep.line("client_labels", f"{DEMO_SYSTEM_NAME} intended_client_labels={labels} "
                              "(its own agent is judged on its release)", labels == [])

    cp.step = "agent_release"
    _, resp = cp.get("/v1/admin/agent-identities")
    agents = listing(resp, "agents")
    for label, name, dist in AGENT_RELEASES:
        want = implementation_declaration(dist)
        row = find(agents, name=name)
        if not row:
            rep.line(f"identity.{label}", f"{name} not found; run setup.py", False)
            continue
        aid = row["id"]
        _, detail = cp.get(f"/v1/admin/agent-identities/{aid}", tolerate=(404,))
        detail = detail if isinstance(detail, dict) else {}
        have = {k: detail.get(k) for k in want}
        rep.line(f"identity.{label}", f"{name} implementation={have['implementation_name']} "
                                      f"approved={have['approved_versions']} "
                                      f"require_release={have['require_release']}", have == want)
        if have != want:
            rep.detail(f"setup.py declares {want}")

        _, rel = cp.get(f"/v1/admin/agent-identities/{aid}/releases", tolerate=(404,))
        rel = rel if isinstance(rel, dict) else {}
        releases = listing(rel, "releases")
        current = next((r for r in releases if r.get("current")), releases[0] if releases else None)
        if current is None:
            rep.line(f"release.{label}", "none observed yet (no run yet)", None)
            continue
        approved = current.get("approved")
        name_ok = current.get("name_matches")
        good = None if approved is None else (approved is True and name_ok is not False)
        rep.line(f"release.{label}", f"{current.get('label')} trust={current.get('trust')} "
                                     f"approved={approved} runs={current.get('run_count')} "
                                     f"last_seen={current.get('last_seen_at')}", good)
        for r in releases[1:5]:
            rep.detail(f"earlier {r.get('label')} trust={r.get('trust')} approved={r.get('approved')} "
                       f"runs={r.get('run_count')} first_seen={r.get('first_seen_at')}")


def check_workload_identity(cp: Client, rep: "Report") -> None:
    """The IdP the gateway verifies the agents' JWT-SVIDs against, and each
    identity's subject: ✗ when either is not what setup.py registers."""
    cp.step = "workload_idp"
    _, resp = cp.get("/v1/admin/idps")
    idp = find(listing(resp, "idps"), issuer=SPIRE_OIDC_ISSUER)
    if not idp:
        rep.line("workload_idp", f"no identity provider with issuer {SPIRE_OIDC_ISSUER}; run setup.py", False)
        return
    audiences = idp.get("workload_audiences") or []
    good = bool(idp.get("enabled")) and WORKLOAD_AUDIENCE in audiences and idp.get("jwks_uri") == SPIRE_JWKS_URI
    rep.line("workload_idp", f"{idp.get('name')} issuer={SPIRE_OIDC_ISSUER} enabled={idp.get('enabled')} "
                             f"audiences={audiences} jwks_uri={idp.get('jwks_uri')}", good)
    cp.step = "workload_subject"
    _, resp = cp.get("/v1/admin/agent-identities")
    agents = listing(resp, "agents")
    for label, name, spiffe_id in (("screening", SCREENING_IDENTITY, SCREENING_SPIFFE_ID),
                                   ("fraud", FRAUD_IDENTITY, FRAUD_SPIFFE_ID)):
        row = find(agents, name=name) or {}
        _, detail = cp.get(f"/v1/admin/agent-identities/{row['id']}", tolerate=(404,)) if row else (404, {})
        detail = detail if isinstance(detail, dict) else {}
        have = (detail.get("idp_server_id"), detail.get("idp_subject"))
        rep.line(f"workload.{label}", f"{name} subject={have[1]}", have == (idp.get("id"), spiffe_id))


def check_declared_outcomes(cp: Client, rep: "Report", gid: str, fraud_gid: Optional[str]) -> None:
    """Declared outcomes read-back: the stored catalogues are the agents' outcomes.json
    files, and the demo system's dependency on the fraud screener expects
    exactly the screener's three verdicts."""
    cp.step = "outcomes"
    for label, sys_gid, dist in (("demo", gid, SCREENING_DIST), ("fraud", fraud_gid, FRAUD_DIST)):
        if not sys_gid:
            rep.line(f"outcomes.{label}", "AI System not found; run setup.py", False)
            continue
        status, resp = cp.get(f"/v1/admin/ai-systems/{sys_gid}/outcomes", tolerate=(404,))
        if status == 404:
            rep.line(f"outcomes.{label}", "not readable (HTTP 404)", False)
            continue
        have = normalize_catalogue(_stored_catalogue(resp))
        same = have == normalize_catalogue(outcome_catalogue(dist))
        rep.line(f"outcomes.{label}", f"{len(have)} outcome(s) declared"
                                      + ("" if same else f"; differs from {dist}'s outcomes.json, re-run setup.py"), same)
    _, deps = cp.get(f"/v1/admin/ai-systems/{gid}/dependencies", tolerate=(404,))
    edge = find(listing(deps, "dependencies"), depends_on_system_id=fraud_gid) if fraud_gid else None
    want = expected_delegate_outcomes(SCREENING_DIST, FRAUD_DIST)
    have_expected = sorted((edge or {}).get("expected_outcomes") or [])
    rep.line("outcomes.dependency", f"expected_outcomes={have_expected}", bool(edge) and have_expected == want)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read back the Brutor Demo System's surfaces.")
    ap.add_argument("--brief", action="store_true", help="one line per surface")
    ap.add_argument("--period-hours", type=int, default=24)
    ap.add_argument("--open-incident", action="store_true",
                    help="open a demo serious incident (Art 73) linked to the latest run")
    ap.add_argument("--json", action="store_true", help="dump the raw payloads as JSON")
    args = ap.parse_args()

    rep = Report(args.brief)
    env = read_demo_env()
    try:
        cp = login()
        gid = resolve_group(cp, env, "DEMO_SYSTEM_GROUP_ID", DEMO_SYSTEM_NAME)
        fraud_gid = resolve_group(cp, env, "FRAUD_SYSTEM_GROUP_ID", FRAUD_SYSTEM_NAME)
    except StepError as exc:
        print(f"✗ {exc.step}: {exc.reason}")
        return 1
    if not gid:
        print(f"✗ ai_system: {DEMO_SYSTEM_NAME} not found; run setup.py first")
        return 1
    ph = args.period_hours
    hours = {"period_hours": ph}

    # Gateway health (unauthenticated).
    try:
        gw = requests.get(GW_URL + "/health", timeout=5)
        rep.line("gateway", f"{GW_URL} HTTP {gw.status_code}", gw.status_code == 200)
    except requests.RequestException as exc:
        rep.line("gateway", f"{GW_URL} unreachable: {exc}", False)

    cp.step = "health"
    _, health = cp.get(f"/v1/admin/ai-systems/{gid}/health", params={"refresh": "true"}, tolerate=(404, 500))
    rep.payloads["health"] = health
    if isinstance(health, dict):
        status, summary = health_summary(health)
        rep.line("health", status + (f" ({summary})" if summary and not args.brief else ""), True)
    else:
        rep.line("health", "not readable", False)

    cp.step = "lifecycle"
    _, life = cp.get(f"/v1/admin/ai-systems/{gid}/lifecycle", tolerate=(404,))
    rep.payloads["lifecycle"] = life
    stage = (life or {}).get("current_stage") if isinstance(life, dict) else None
    rep.line("lifecycle", f"stage={stage}", stage == "active")
    if isinstance(life, dict) and not args.brief:
        for t in life.get("transitions") or []:
            unmet = ", ".join(u.get("key", "?") for u in t.get("unmet") or [])
            rep.detail(f"-> {t.get('to_stage')}: {'allowed' if t.get('allowed') else 'blocked'}"
                       + (f" (unmet: {unmet})" if unmet else ""))

    cp.step = "contracts"
    _, contracts = cp.get(f"/v1/admin/ai-systems/{gid}/contracts", tolerate=(404,))
    rep.payloads["contracts"] = contracts
    if isinstance(contracts, dict):
        active_v = contracts.get("active_version")
        drifted = contracts.get("config_drifted")
        rep.line("contract", f"active v{active_v}, {len(contracts.get('contracts') or [])} version(s), "
                             f"drifted={drifted}", active_v is not None and not drifted)
    else:
        rep.line("contract", "not readable", False)

    cp.step = "runs"
    _, runs = cp.get(f"/v1/admin/ai-systems/{gid}/runs", params=dict(hours, limit=50), tolerate=(404,))
    rep.payloads["runs"] = runs
    rows = listing(runs, "runs")
    complete = [r for r in rows if is_full_run(r)]
    rep.line("runs", f"{len(rows)} in {ph}h, {len(complete)} with {EXPECT_STEPS} steps", len(rows) >= 1)
    checked = False
    for r in complete:
        _, detail = cp.get(f"/v1/admin/runs/{r['id']}", tolerate=(404,))
        d = detail if isinstance(detail, dict) else {}
        chain = r.get("chain_integrity")
        llm = r.get("llm_call_count") or 0
        skill = d.get("skill_call_count")
        a2a = d.get("a2a_call_count")
        tools = r.get("tool_call_count") or 0
        turns = r.get("turn_count")
        actions = r.get("action_count")
        good = run_counts_met(r, d)
        rep.line("run", f"{r['id']} state={r.get('terminal_state')} outcome={r.get('outcome_id')} "
                        f"conformance={r.get('outcome_conformance')} "
                        f"steps={r.get('step_count')} turns={turns} actions={actions} llm={llm} skill={skill} tools={tools} a2a={a2a} "
                        f"chain={chain} cost={r.get('total_cost_usd')}", good)
        checked = True
        break
    if rows and not checked:
        latest = rows[0]
        rep.line("run", f"latest {latest['id']} has {latest.get('step_count')} steps "
                        f"(state={latest.get('terminal_state')}); no run with {EXPECT_STEPS} steps in {ph}h", None)

    cp.step = "assurance"
    _, assurance = cp.get(f"/v1/admin/ai-systems/{gid}/assurance", params=hours, tolerate=(404,))
    rep.payloads["assurance"] = assurance
    if isinstance(assurance, dict):
        keys = [k for k in ("status", "coverage", "checks", "inbox", "findings") if k in assurance]
        rep.line("assurance", ", ".join(f"{k}={_brief(assurance[k])}" for k in keys) or "readable", True)
    else:
        rep.line("assurance", "not readable", None)

    cp.step = "approvals"
    _, approvals = cp.get("/v1/admin/approvals",
                          params={"status_filter": "pending", "group_id": gid, "page_size": 20}, tolerate=(404,))
    rep.payloads["approvals"] = approvals
    pending = listing(approvals, "items")
    rep.line("approvals", f"{len(pending)} pending for an underwriter"
                          + (": " + ", ".join(p.get("capability_name", "?") for p in pending[:5]) if pending else ""),
             True)
    for p in pending[:5]:
        rep.detail(f"{p['id']} {p.get('capability_name')} requested {p.get('created_at')} expires {p.get('expires_at')}")
    if pending:
        rep.detail("decide them in the Admin Console (Mission Control -> Approvals) or with ./demo.sh approve")

    cp.step = "obligations"
    _, obligations = cp.get(f"/v1/admin/ai-systems/{gid}/obligations", tolerate=(404,))
    rep.payloads["obligations"] = obligations
    obl_rows = listing(obligations, "items", "obligations")
    if obl_rows:
        by_status: Dict[str, int] = {}
        for o in obl_rows:
            s = str(o.get("status") or o.get("state") or "?")
            by_status[s] = by_status.get(s, 0) + 1
        rep.line("obligations", f"{len(obl_rows)} ({', '.join(f'{k}={v}' for k, v in sorted(by_status.items()))})", True)
    else:
        rep.line("obligations", "none listed (is eu-ai-act enabled?)", None)

    cp.step = "evidence"
    _, records = cp.get("/v1/admin/evidence/records", params={"ai_system_id": gid, "limit": 20}, tolerate=(404,))
    _, summary = cp.get("/v1/admin/evidence/summary", params={"ai_system_id": gid}, tolerate=(404,))
    rep.payloads["evidence_records"] = records
    rep.payloads["evidence_summary"] = summary
    rec_rows = listing(records, "items", "records")
    total = (records or {}).get("total") if isinstance(records, dict) else None
    rep.line("evidence", f"{total if total is not None else len(rec_rows)} sealed record(s)"
                         + (f"; summary {_brief(summary)}" if isinstance(summary, dict) and not args.brief else ""),
             None if not rec_rows else True)
    _, files = cp.get(f"/v1/admin/ai-systems/{gid}/evidence", tolerate=(404,))
    file_rows = listing(files, "evidence")
    rep.line("evidence_files", ", ".join(f"{e.get('kind')}{' (expired)' if e.get('is_expired') else ''}"
                                         for e in file_rows) or "none",
             any(e.get("kind") == "impact_assessment" and not e.get("is_expired") for e in file_rows))

    cp.step = "transparency"
    _, transparency = cp.get("/v1/admin/compliance/transparency", params={"ai_system_id": gid}, tolerate=(404,))
    rep.payloads["transparency"] = transparency
    if isinstance(transparency, dict):
        cfg = transparency.get("config") or {}
        nt = transparency.get("notice_text") or {}
        rep.line("transparency", f"notice enabled={cfg.get('enabled')} surface={cfg.get('surface')} "
                                 f"status={nt.get('status')}", bool(cfg.get("enabled")))
    else:
        rep.line("transparency", "not readable", None)

    cp.step = "oversight"
    _, oversight = cp.get("/v1/admin/compliance/oversight", params={"ai_system_id": gid}, tolerate=(404,))
    rep.payloads["oversight"] = oversight
    if isinstance(oversight, dict):
        rep.line("oversight", f"approvals {_brief(oversight.get('approvals'))}; "
                              f"review queue {_brief(oversight.get('review_queue'))}", True)
    else:
        rep.line("oversight", "not readable", None)

    cp.step = "documentation"
    _, doc = cp.get(f"/v1/admin/ai-systems/{gid}/documentation",
                    params={"framework": "eu-ai-act", "format": "json"}, tolerate=(404, 422))
    rep.payloads["documentation"] = doc
    if isinstance(doc, dict):
        sections = doc.get("sections") or doc.get("annex_iv") or doc
        n = len(sections) if isinstance(sections, (list, dict)) else 0
        rep.line("documentation", f"Annex IV export readable ({n} section(s))", True)
    else:
        rep.line("documentation", "not readable", None)

    check_agent_releases(cp, rep, gid)
    check_workload_identity(cp, rep)
    check_declared_outcomes(cp, rep, gid, fraud_gid)

    if fraud_gid:
        _, fl = cp.get(f"/v1/admin/ai-systems/{fraud_gid}/lifecycle", tolerate=(404,))
        fs = (fl or {}).get("current_stage") if isinstance(fl, dict) else None
        rep.line("fraud_screener", f"stage={fs}", fs == "active")

    if args.open_incident:
        cp.step = "incident"
        run_ids = [rows[0]["id"]] if rows else []
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        status, inc = cp.post("/v1/admin/compliance/incidents", {
            "title": "Demo: adverse recommendation recorded without underwriter review",
            "description": "Demonstration serious incident (Art 73). Synthetic scenario: a decline was recorded "
                           "while the approval hold was bypassed. Opened by verify.py --open-incident; not real.",
            "classification": "serious_incident", "severity": "high",
            "became_aware_at": now, "frameworks": ["eu-ai-act"],
            "ai_system_id": gid, "linked_run_ids": run_ids,
        }, tolerate=(400, 404, 409, 422))
        rep.payloads["incident"] = inc
        if status < 400:
            incident = inc.get("incident") if isinstance(inc, dict) and isinstance(inc.get("incident"), dict) else inc
            deadlines = (incident or {}).get("deadline_status") or []
            rep.line("incident", f"opened {(incident or {}).get('id')} with {len(deadlines)} deadline(s)", True)
            for d in deadlines:
                rep.detail(f"{d.get('framework_id')} {d.get('rule')} due {d.get('due_at')} ({d.get('status')})")
        else:
            rep.line("incident", f"HTTP {status}: {json.dumps(inc)[:200]}", False)

    if args.json:
        print(json.dumps(rep.payloads, indent=2, default=str))

    if rep.failures and not args.brief:
        print(f"\n{len(rep.failures)} expectation(s) not met:")
        for f in rep.failures:
            print(f"  - {f}")
        return 1
    return 0


def _brief(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "..."


if __name__ == "__main__":
    sys.exit(main())
