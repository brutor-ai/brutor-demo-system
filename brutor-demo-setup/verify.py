#!/usr/bin/env python3
"""verify.py: read back what the Brutor Demo System has produced.

Reads health, runs, assurance, contract, lifecycle gate, obligations, evidence,
transparency, oversight, pending approvals and the Annex IV documentation
export for the demo AI System, and checks the expectations in DESIGN.md
section 9 against the most recent runs.

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
from typing import Any, Dict, List, Optional

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from setup import (  # noqa: E402  (shares the client and config with setup.py)
    ADMIN_PASSWORD, ADMIN_USER, CP_URL, DEMO_SYSTEM_NAME, FRAUD_SYSTEM_NAME, GW_URL,
    TENANT_ID, Client, StepError, find, listing, read_demo_env,
)

# A full run declares 3 steps (gather, assess, decide) and 3 turns (t1 and t2
# of the screening agent plus the fraud screener's own turn at depth 1, which
# declares no step) over 9 actions (10 when escalated: the note): llm 3, a2a 1,
# skill 1, tools 4 (DESIGN.md section 4). Runs recorded before the 2026-09-24
# granularity fix declared one step per node (8 or 9), so completeness is
# judged as step_count >= EXPECT_STEPS.
EXPECT_STEPS = 3
EXPECT_TURNS = 3
EXPECT_ACTIONS = 9
GOOD_CHAIN = ("intact", "client_asserted")


def login() -> Client:
    cp = Client(CP_URL)
    cp.step = "login"
    status, resp = cp.post("/v1/admin-users/tenant/login",
                           {"username": ADMIN_USER, "password": ADMIN_PASSWORD, "tenant_id": TENANT_ID},
                           tolerate=(401, 403))
    if status != 200 or not isinstance(resp, dict) or not resp.get("access_token"):
        raise StepError("login", f"HTTP {status}: {resp}")
    cp.token = resp["access_token"]
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
        status = health.get("status") or health.get("health") or health.get("overall")
        signals = health.get("signals") or health.get("dimensions") or {}
        summary = ", ".join(f"{k}={v.get('status') if isinstance(v, dict) else v}"
                            for k, v in (signals.items() if isinstance(signals, dict) else []))
        rep.line("health", f"{status}" + (f" ({summary})" if summary and not args.brief else ""), True)
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
    # Anything at or above EXPECT_STEPS is complete (older runs in the ledger
    # declared a step per node; see the EXPECT_* comment).
    complete = [r for r in rows if (r.get("step_count") or 0) >= EXPECT_STEPS]
    rep.line("runs", f"{len(rows)} in {ph}h, {len(complete)} with >= {EXPECT_STEPS} steps", len(rows) >= 1)
    checked = False
    for r in complete:
        _, detail = cp.get(f"/v1/admin/runs/{r['id']}", tolerate=(404,))
        d = detail if isinstance(detail, dict) else {}
        chain = r.get("chain_integrity")
        llm = r.get("llm_call_count") or 0
        skill = d.get("skill_call_count")
        a2a = d.get("a2a_call_count")
        tools = d.get("tool_call_count") or r.get("tool_call_count") or 0
        # Core images before the run_ledger `skill_run` fix (2026-09-23) count
        # a skill executed through the skills MCP server as a tool call, so
        # skill reads 0 and tool_call_count is one higher than it should be.
        # That is a known ledger gap, not a demo failure: accept it with a note.
        skill_ok = skill is None or skill == 1 or (skill == 0 and tools >= 5)
        skill_note = " (skill counted as a tool: core image predates the skill_run ledger fix)" \
            if skill == 0 and tools >= 5 else ""
        turns = r.get("turn_count") if r.get("turn_count") is not None else d.get("turn_count")
        actions = r.get("action_count") if r.get("action_count") is not None else d.get("action_count")
        turns_ok = turns is None or turns >= EXPECT_TURNS
        actions_ok = actions is None or actions >= EXPECT_ACTIONS  # 9 resolved, 10 escalated
        good = (chain in GOOD_CHAIN and llm >= 3 and skill_ok and (a2a is None or a2a == 1) and turns_ok and actions_ok)
        rep.line("run", f"{r['id']} state={r.get('terminal_state')} outcome={r.get('client_outcome')} "
                        f"steps={r.get('step_count')} turns={turns} actions={actions} llm={llm} skill={skill} tools={tools} a2a={a2a} "
                        f"chain={chain} cost={r.get('total_cost_usd')}{skill_note}", good)
        checked = True
        break
    if rows and not checked:
        latest = rows[0]
        rep.line("run", f"latest {latest['id']} has {latest.get('step_count')} steps "
                        f"(state={latest.get('terminal_state')}); no run with >= {EXPECT_STEPS} steps in {ph}h", None)

    cp.step = "assurance"
    _, assurance = cp.get(f"/v1/admin/ai-systems/{gid}/assurance", params=hours, tolerate=(404,))
    rep.payloads["assurance"] = assurance
    if isinstance(assurance, dict):
        keys = [k for k in ("status", "coverage", "checks", "inbox", "findings") if k in assurance]
        rep.line("assurance", ", ".join(f"{k}={_brief(assurance[k])}" for k in keys) or "readable", True)
    else:
        rep.line("assurance", "not readable", None)

    cp.step = "approvals"
    _, approvals = cp.get("/v1/admin/tool-approvals",
                          params={"status_filter": "pending", "group_id": gid, "page_size": 20}, tolerate=(404,))
    rep.payloads["approvals"] = approvals
    pending = listing(approvals, "items")
    rep.line("approvals", f"{len(pending)} pending for an underwriter"
                          + (": " + ", ".join(p.get("capability_name", "?") for p in pending[:5]) if pending else ""),
             True)
    for p in pending[:5]:
        rep.detail(f"{p['id']} {p.get('capability_name')} requested {p.get('created_at')} expires {p.get('expires_at')}")

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
