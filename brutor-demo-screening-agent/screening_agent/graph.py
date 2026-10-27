"""The LangGraph graph: eight fixed nodes, one run per application.

Node order (DESIGN.md section 4), node name = step id:

    intake -> verify_identity -> credit_report -> affordability -> classify
           -> fraud_screen -> decide -> record

Ledger vocabulary (ADR 0003, DESIGN.md section 4): run > step > turn > action.
Steps are phases of the task, turns are model passes, and each node makes one
action:

    node             step     turn
    intake           gather   t1
    verify_identity  gather   t1
    credit_report    gather   t1
    affordability    gather   t1
    classify         assess   t1   (the classifier call the four reads feed)
    fraud_screen     assess   t2
    decide           decide   t2
    record           decide   t2   (last action carries X-Brutor-Run-End)

A completed run reads 3 steps, 3 turns, 9 actions (10 when held): the
ninth action is the fraud screener's own model call, joining at depth 1 with
a turn of its own and no step (steps are the orchestrating agent's phases).
process_application() is the driver: it opens the run context, invokes the
graph and closes the run via POST /v1/runs/{root}/end when a node raised.

Declared outcomes (RFC 0025, outcomes.py): the record step closes the run with
X-Brutor-Outcome-Id application_processed.<variant> — loan_approved,
loan_rejected or loan_referred from the final recommendation, or
held_for_underwriter when the gateway held the recommendation for a human.
A run that fails reports no outcome. The fraud verdict the graph branches on
is the variant of the delegate's outcome as the gateway checked it, not the
body's `verdict` field.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, TypedDict

from .approvals import PendingApprovals
from .config import Settings
from .failures import Failure, classify_failure
from .gateway import ApprovalRequired, DelegateDeviated, Gateway, PolicyBlocked, ToolError, new_ulid
from .outcomes import FRAUD_OUTCOME, HELD_FOR_UNDERWRITER, RECOMMENDATION_OUTCOMES, expected_delegate_outcomes
from .prompts import classifier_messages, drafter_messages, ensure_disclosure
from .rules import allowed_recommendations, final_recommendation, normalize_recommendation, policy_floor

log = logging.getLogger("screening_agent.graph")

NODES: list[str] = ["intake", "verify_identity", "credit_report", "affordability", "classify", "fraud_screen", "decide", "record"]

STEP_NAMES: dict[str, str] = {
    "gather": "Gather the application facts",
    "assess": "Assess risk and fraud",
    "decide": "Decide and record",
}
TURNS: dict[str, tuple[str, int]] = {"t1": ("t1", 1), "t2": ("t2", 2)}

# node -> (step id, turn id)
NODE_MAP: dict[str, tuple[str, str]] = {
    "intake": ("gather", "t1"),
    "verify_identity": ("gather", "t1"),
    "credit_report": ("gather", "t1"),
    "affordability": ("gather", "t1"),
    "classify": ("assess", "t1"),
    "fraud_screen": ("assess", "t2"),
    "decide": ("decide", "t2"),
    "record": ("decide", "t2"),
}
STEP_ORDER = ["gather", "assess", "decide"]


def node_headers(node: str) -> dict[str, Any]:
    """The header kwargs (step id, step name, turn, node label) for one node."""
    step, turn = NODE_MAP[node]
    return {"step_id": step, "step_name": STEP_NAMES[step], "turn": TURNS[turn], "node": node}

RISK_BANDS = ("low", "medium", "high")
FRAUD_VERDICTS = ("clear", "review", "hit")
AFFORDABILITY_CLASSES = ("comfortable", "tight", "unaffordable")


class ScreeningState(TypedDict, total=False):
    application_id: str
    application: dict[str, Any]
    identity: dict[str, Any]
    report: dict[str, Any]
    affordability: dict[str, Any]
    classification: dict[str, Any]
    fraud: dict[str, Any]
    draft: dict[str, Any]
    final: dict[str, Any]
    tool_args: dict[str, Any]
    outcome_id: str
    approval_id: str


@dataclass
class RunResult:
    application_id: str
    run_id: str
    state: str  # completed | errored
    outcome: str | None  # the declared outcome id (outcomes.py); None when the run failed
    error: str | None = None
    approval_id: str | None = None
    duration_ms: int = 0
    # Whose failure this was (failures.py): the application's, or the system's
    # (gateway, provider, governance). None when the run completed.
    failure: Failure | None = None
    # The run errored because the gateway refused a call (guardrail, policy):
    # the gateway records the error cause blocked_by_policy.
    blocked_by_policy: bool = False


def _require_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(f"{what} returned no JSON object: {str(value)[:200]}")
    return value


def _number(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{what} is not a number: {value!r}") from exc


def build_graph(gw: Gateway, settings: Settings, store: PendingApprovals):
    """Compile the graph. Nodes are closures over the gateway client."""
    from langgraph.graph import END, START, StateGraph

    apps = settings.applications_mcp_server_id
    bureau = settings.bureau_mcp_server_id

    hdr = node_headers
    fraud_expected = expected_delegate_outcomes()

    def intake(state: ScreeningState) -> ScreeningState:
        app = _require_dict(
            gw.mcp_call(apps, "applications_get", {"application_id": state["application_id"]}, **hdr("intake")),
            "applications_get",
        )
        if app.get("application_id") and app["application_id"] != state["application_id"]:
            raise ToolError(f"applications_get returned {app['application_id']} for {state['application_id']}")
        return {"application": app}

    def verify_identity(state: ScreeningState) -> ScreeningState:
        applicant = state["application"].get("applicant") or {}
        identity = _require_dict(
            gw.mcp_call(
                bureau,
                "bureau_verify_identity",
                {
                    "applicant_id": applicant.get("applicant_id"),
                    "full_name": applicant.get("full_name"),
                    "date_of_birth": applicant.get("date_of_birth"),
                },
                **hdr("verify_identity"),
            ),
            "bureau_verify_identity",
        )
        return {"identity": identity}

    def credit_report(state: ScreeningState) -> ScreeningState:
        applicant = state["application"].get("applicant") or {}
        report = _require_dict(
            gw.mcp_call(bureau, "bureau_get_report", {"applicant_id": applicant.get("applicant_id")}, **hdr("credit_report")),
            "bureau_get_report",
        )
        return {"report": report}

    def affordability(state: ScreeningState) -> ScreeningState:
        app = state["application"]
        args = {
            "monthly_income_eur": _number(app.get("monthly_income_eur"), "monthly_income_eur"),
            "monthly_expenses_eur": _number(app.get("monthly_expenses_eur"), "monthly_expenses_eur"),
            "existing_debt_monthly_eur": _number(app.get("existing_debt_monthly_eur", 0), "existing_debt_monthly_eur"),
            "requested_amount_eur": _number(app.get("requested_amount_eur"), "requested_amount_eur"),
            "term_months": int(_number(app.get("term_months"), "term_months")),
        }
        result = _require_dict(gw.skill_run(settings.skill_name, settings.skill_script, args, **hdr("affordability")), "affordability skill")
        cls = str(result.get("affordability_class", "")).lower()
        if cls not in AFFORDABILITY_CLASSES:
            raise ToolError(f"affordability skill returned unknown class {cls!r}")
        result["affordability_class"] = cls
        return {"affordability": result}

    def classify(state: ScreeningState) -> ScreeningState:
        out = gw.llm(
            settings.classifier_model,
            classifier_messages(state["application"], state["report"], state["affordability"]),
            json=True,
            api=settings.classifier_api,
            **hdr("classify"),
        )
        band = str(out.get("risk_band", "")).strip().lower()
        factors = out.get("key_factors")
        if band not in RISK_BANDS:
            # An unreadable classification is treated as high risk (forces at least refer).
            log.warning("classifier returned unknown risk_band %r; treating as high", band)
            factors = list(factors or []) + [f"classifier output unreadable ({band or 'empty'}); treated as high"]
            band = "high"
        return {
            "classification": {
                "risk_band": band,
                "key_factors": [str(f) for f in (factors or [])][:8],
                "model": settings.classifier_model,
            }
        }

    def fraud_screen(state: ScreeningState) -> ScreeningState:
        app = state["application"]
        applicant = app.get("applicant") or {}
        report = state["report"]
        payload = {
            "applicant_id": applicant.get("applicant_id"),
            "full_name": applicant.get("full_name"),
            "date_of_birth": applicant.get("date_of_birth"),
            "country": applicant.get("country"),
            "purpose": app.get("purpose"),
            "amount_eur": app.get("requested_amount_eur"),
            "bureau": {
                "inquiries_6m": report.get("inquiries_6m"),
                "delinquencies_24m": report.get("delinquencies_24m"),
                "open_credit_lines": report.get("open_credit_lines"),
            },
        }
        answer = gw.a2a_delegate(
            settings.fraud_card_id,
            settings.fraud_capability,
            payload,
            expected_outcomes=fraud_expected,
            **hdr("fraud_screen"),
        )
        # Branch on the outcome the gateway checked against the screener's
        # contract (screening_completed.<verdict>), never on the body's word.
        head, _, v = answer.outcome_id.partition(".")
        if head != FRAUD_OUTCOME or v not in FRAUD_VERDICTS:
            raise DelegateDeviated(f"fraud screener outcome {answer.outcome_id!r} names no verdict")
        verdict = dict(answer.verdict)
        body_v = str(verdict.get("verdict", "")).strip().lower()
        if body_v != v:
            log.warning("fraud screener body says verdict %r, its checked outcome says %r; the outcome wins", body_v, v)
        verdict["verdict"] = v
        return {"fraud": verdict}

    def decide(state: ScreeningState) -> ScreeningState:
        identity_ok = bool(state["identity"].get("verified"))
        aff_class = state["affordability"]["affordability_class"]
        fraud_v = state["fraud"]["verdict"]
        band = state["classification"]["risk_band"]
        floor, floor_rule = policy_floor(identity_ok, aff_class, fraud_v, band)
        draft = gw.llm(
            settings.drafter_model,
            drafter_messages(
                state["application"],
                state["identity"],
                state["report"],
                state["affordability"],
                state["classification"],
                state["fraud"],
                floor,
                floor_rule,
                allowed_recommendations(floor),
            ),
            json=True,
            api=settings.drafter_api,
            **hdr("decide"),
        )
        model_reco = normalize_recommendation(draft.get("recommendation"))
        recommendation, rule = final_recommendation(draft.get("recommendation"), identity_ok, aff_class, fraud_v, band)
        rationale = str(draft.get("rationale") or "").strip()
        if rule:
            rationale = (rationale + " " if rationale else "") + f"Rule applied: {rule} -> {recommendation}."
            if model_reco and model_reco != recommendation:
                rationale += f" (Drafter proposed {model_reco}; overridden by policy.)"
        else:
            rationale = (rationale + " " if rationale else "") + "Rule applied: none; drafter recommendation stands."
        letter = ensure_disclosure(str(draft.get("customer_letter") or ""))
        return {
            "draft": draft,
            "final": {
                "recommendation": recommendation,
                "rule_fired": rule,
                "rationale": rationale,
                "customer_letter": letter,
                "model": settings.drafter_model,
            },
        }

    def record(state: ScreeningState) -> ScreeningState:
        app = state["application"]
        final = state["final"]
        tool_args = {
            "application_id": state["application_id"],
            "recommendation": final["recommendation"],
            "amount_eur": app.get("requested_amount_eur"),
            "rationale": final["rationale"],
            "risk_band": state["classification"]["risk_band"],
            "affordability_class": state["affordability"]["affordability_class"],
            "fraud_verdict": state["fraud"]["verdict"],
            "customer_letter": final["customer_letter"],
        }
        outcome_id = RECOMMENDATION_OUTCOMES.get(final["recommendation"])
        if outcome_id is None:
            raise ToolError(f"no declared outcome for recommendation {final['recommendation']!r}")
        try:
            gw.mcp_call(
                settings.applications_mcp_server_id,
                "applications_set_recommendation",
                tool_args,
                run_end="completed",
                outcome_id=outcome_id,
                **hdr("record"),
            )
        except ApprovalRequired as held:
            note = (
                f"Held for underwriter, approval id {held.approval_id}: recommendation "
                f"'{final['recommendation']}' for EUR {tool_args['amount_eur']} awaits human approval "
                f"(rule: {final.get('rule_fired') or 'none'})."
            )
            gw.mcp_call(
                settings.applications_mcp_server_id,
                "applications_add_note",
                {"application_id": state["application_id"], "note": note},
                run_end="completed",
                outcome_id=HELD_FOR_UNDERWRITER,
                **hdr("record"),
            )
            store.add(state["application_id"], held.approval_id, tool_args, gw.ctx.run_id if gw.ctx else "")
            return {"tool_args": tool_args, "outcome_id": HELD_FOR_UNDERWRITER, "approval_id": held.approval_id}
        return {"tool_args": tool_args, "outcome_id": outcome_id}

    graph = StateGraph(ScreeningState)
    nodes = {
        "intake": intake,
        "verify_identity": verify_identity,
        "credit_report": credit_report,
        "affordability": affordability,
        "classify": classify,
        "fraud_screen": fraud_screen,
        "decide": decide,
        "record": record,
    }
    previous = START
    for node in NODES:
        graph.add_node(node, nodes[node])
        graph.add_edge(previous, node)
        previous = node
    graph.add_edge(previous, END)
    return graph.compile()


def process_application(
    gw: Gateway,
    settings: Settings,
    store: PendingApprovals,
    application_id: str,
    *,
    graph=None,
) -> RunResult:
    """Run the graph for one application inside its own run and always close it."""
    compiled = graph or build_graph(gw, settings, store)
    run_id = f"bds-{application_id}-{new_ulid()}"
    started = time.monotonic()
    with gw.run(run_id) as ctx:
        try:
            final_state = compiled.invoke({"application_id": application_id})
            outcome = final_state["outcome_id"]
            result = RunResult(
                application_id,
                run_id,
                "completed",
                outcome,
                approval_id=final_state.get("approval_id"),
            )
            if not ctx.closed:
                # The header close did not take (for example the last call answered 202);
                # close explicitly so the ledger never has to infer.
                gw.end_run("completed", outcome)
        except PolicyBlocked as exc:
            # RFC 0025: a refusal is an errored run (cause blocked_by_policy), not a terminal state.
            gw.end_run("errored")
            result = RunResult(
                application_id,
                run_id,
                "errored",
                None,
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
                failure=classify_failure(exc),
                blocked_by_policy=True,
            )
        except Exception as exc:  # noqa: BLE001 - any node failure ends the run errored
            gw.end_run("errored")
            result = RunResult(
                application_id,
                run_id,
                "errored",
                None,
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
                failure=classify_failure(exc),
            )
        result.duration_ms = int((time.monotonic() - started) * 1000)
    level = logging.INFO if result.state == "completed" else logging.ERROR
    log.log(
        level,
        "run=%s state=%s outcome=%s application=%s ms=%d%s%s",
        run_id,
        result.state,
        result.outcome or "-",
        application_id,
        result.duration_ms,
        f" failure={result.failure.label()}" if result.failure else "",
        f" error={result.error}" if result.error else "",
    )
    return result
