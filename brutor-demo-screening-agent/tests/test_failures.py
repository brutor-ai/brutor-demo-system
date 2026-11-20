"""Failure classification (failures.py): every kind, from the shapes the agent sees.

Each case drives the real gateway client against a canned HTTP answer (or a
transport error) and classifies what it raises, so the test pins the whole
path: HTTP answer -> gateway.py exception -> class and kind. The response
bodies are the Brutor Core Gateway's (error_envelope.rs, mcp.rs
guardrail_violation_body and handle_system_server, system_server.rs
err_result, skill_orchestrator.rs, run_controls.rs, run_intervention.rs,
run_budget.rs, argument_policy/enforce.rs) and ones recorded from a live gateway.
The shapes a system server answers with (a stopped run, an
argument policy on a skill's input) are built by tests/fake_gateway.py, so the
fake and these cases cannot drift apart.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from screening_agent import failures
from screening_agent.failures import (
    APPLICATION_KINDS,
    SYSTEM_KINDS,
    FailureClass,
    classify_failure,
)
from screening_agent.credential import ApiKey
from screening_agent.gateway import Gateway, GatewayError, ToolError
from tests.fake_gateway import (
    POLICY_UNAVAILABLE,
    RUN_ABORTED_MESSAGE,
    approval_envelope,
    argument_policy_denied_403,
    run_stopped_response,
    skill_policy_denied,
    skill_policy_held,
)

SYSTEM = FailureClass.SYSTEM
APPLICATION = FailureClass.APPLICATION

# -- live bodies ----------------------------------------------------------------
CREDITS_429 = {
    "error": {
        "code": "credit_balance_exhausted",
        "message": "You have no credits remaining. Add credits to continue using the API at https://platform.openai.com/settings/organization/billing/.",
        "param": None,
        "type": "insufficient_quota",
    }
}
RESILIENCE_503 = {"error": {"message": "primary gated by resilience layer", "type": "gateway_resilience_gated"}}
LLM_GUARDRAIL_403 = {
    "error": {
        "check": "prompt_injection",
        "code": "guardrail_blocked",
        "direction": "input",
        "guardrail": "Borealis Baseline Guardrails",
        "message": "guardrail hit on `prompt_injection` (1 match)",
    }
}
# -- core shapes ----------------------------------------------------------------
MCP_GUARDRAIL_403 = {
    "jsonrpc": "2.0",
    "id": 1,
    "error": {
        "code": -32000,
        "message": "Blocked by guardrail: prompt_injection",
        "data": {"code": "guardrail_blocked", "check": "prompt_injection", "guardrail": "Borealis Baseline Guardrails", "direction": "output"},
    },
}
ARGUMENT_POLICY_DENY_403 = {
    "jsonrpc": "2.0",
    "id": 1,
    "error": {
        "code": -32000,
        "message": "argument policy denied the call",
        "data": {"argument_policy": {"policy": "Adverse or large decisions need an underwriter", "tool": "applications_set_recommendation", "argument": "*", "analyzer": "json", "rule": "schema_invalid", "facts": {}}},
    },
}
JSONRPC_POLICY_ERROR_200 = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "denied by policy"}}
AUTONOMY_403 = {"error": {"message": "autonomy_denied: AI System 'Brutor Demo System' is at approval_required"}}
# A stopped run, as every governed surface refuses it (run_controls.rs through
# error_envelope.rs): the operator-facing sentence, no machine marker. The
# status says which control it was.
RUN_ABORTED_403 = {"error": {"message": RUN_ABORTED_MESSAGE}}
RUN_CAP_429 = run_stopped_response("over_budget").json()
# A run stopped between the skills server admitting the call and the skill
# starting: the refusal arrives inside the tool result, marked `run_aborted`.
SKILL_RUN_ABORTED = f"skill_error_403: run_aborted: {RUN_ABORTED_MESSAGE}"
# An argument policy the gateway cannot decode refuses every call of the group
# on that surface: the rule is `policy_unavailable`, the severity deny.
POLICY_UNAVAILABLE_403 = argument_policy_denied_403(
    "applications_set_recommendation", POLICY_UNAVAILABLE["policy"], POLICY_UNAVAILABLE["rule"], POLICY_UNAVAILABLE["message"]
)
# A compared field that is present but not a value the rule can compare.
NOT_COMPARABLE_403 = argument_policy_denied_403(
    "applications_set_recommendation",
    "Adverse or large decisions need an underwriter",
    "field_gt",
    "Loans over 25,000 EUR are reviewed by an underwriter. (refused: field 'amount_eur' is present but is not a value this rule can compare)",
)
ARGUMENTS_TRUNCATED_403 = argument_policy_denied_403(
    "applications_set_recommendation",
    "Adverse or large decisions need an underwriter",
    "arguments_truncated",
    "Refused: the arguments are too large or too deeply nested to be checked against this policy",
)
SEMANTIC_403 = {"error": {"message": "semantic_policy_blocked: lending decisions based on nationality"}}
NO_GRANT_403 = {"error": {"message": "agent brutor-demo-screening-worker has no grant for mcp_tool applications_get"}}
APPROVAL_202 = {"approval_required": True, "approval_request_id": "apr-x", "poll_url": "/v1/approvals/apr-x/poll"}


def _rpc_tool_error(text: str) -> httpx.Response:
    """system_server.rs err_result / a tool's result.isError, HTTP 200."""
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": True}})


def _json(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


def _raise(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _mcp(gw: Gateway) -> None:
    gw.mcp_call("mcp-apps", "applications_get", {"application_id": "APP-1"})


def _skill(gw: Gateway) -> None:
    gw.skill_run("affordability-check", "affordability.py", {"requested_amount_eur": 12000})


def _llm(gw: Gateway) -> None:
    gw.llm("gpt-5.2", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], json=True, api="chat")


def _a2a(gw: Gateway) -> None:
    gw.a2a_delegate("agentcard-fraud", "screening.fraud_sanctions", {"full_name": "x"})


def _chat_text(text: str) -> Callable[[httpx.Request], httpx.Response]:
    body = {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5.2",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
    return lambda request: httpx.Response(200, json=body)


def _a2a_expecting(gw: Gateway) -> None:
    gw.a2a_delegate("agentcard-fraud", "screening.fraud_sanctions", {"full_name": "x"}, expected_outcomes={"screening_completed.clear"})


def _a2a_answer(headers: dict[str, str], metadata: dict[str, str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    """A completed task whose checked outcome is in `headers` / `metadata`."""
    task: dict[str, Any] = {"id": "t", "contextId": "c", "status": {"state": "TASK_STATE_COMPLETED", "message": {"role": "ROLE_AGENT", "parts": [{"kind": "text", "text": '{"verdict": "clear"}'}]}}}
    if metadata is not None:
        task["metadata"] = metadata
    return lambda request: httpx.Response(200, json={"task": task}, headers=headers)


def _a2a_task(state: str) -> Callable[[httpx.Request], httpx.Response]:
    body = {"task": {"id": "t", "contextId": "c", "status": {"state": state, "message": {"role": "ROLE_AGENT", "parts": [{"kind": "text", "text": "failed"}]}}}}
    return lambda request: httpx.Response(200, json=body)


# (case id, call, handler, expected class, expected kind)
def _without_credential(gw: Gateway) -> None:
    """The Workload API gave nothing: the call is never sent."""
    gw.credential = ApiKey("")
    _llm(gw)


CREDENTIAL_403 = {
    "error": {
        "code": "CREDENTIAL_ASSURANCE_INSUFFICIENT",
        "message": "AI System brutor-demo-system requires workload identity; the call presented an API key",
        "required": "workload",
        "presented": "key",
    }
}


CASES: list[tuple[str, Callable[[Gateway], None], Callable[[httpx.Request], httpx.Response], FailureClass, str]] = [
    # ---- system: unreachable
    ("llm-no-workload-credential", _without_credential, _raise(AssertionError("no call without a credential")), SYSTEM, "no_credential"),
    # The refusal of a credential below the AI System's identity policy (a copied API key on
    # a system in production): a 403 with no content verdict, so the system's, never the application's.
    ("llm-403-credential-assurance", _llm, _json(403, CREDENTIAL_403), SYSTEM, "governance_refusal"),
    ("mcp-dns", _mcp, _raise(httpx.ConnectError("[Errno -2] Name or service not known")), SYSTEM, "unreachable"),
    ("mcp-refused", _mcp, _raise(httpx.ConnectError("[Errno 111] Connection refused")), SYSTEM, "unreachable"),
    ("mcp-timeout", _mcp, _raise(httpx.ReadTimeout("timed out")), SYSTEM, "unreachable"),
    ("llm-connect", _llm, _raise(httpx.ConnectError("[Errno -2] Name or service not known")), SYSTEM, "unreachable"),
    ("a2a-connect", _a2a, _raise(httpx.ConnectError("[Errno 111] Connection refused")), SYSTEM, "unreachable"),
    # ---- system: server_error
    ("llm-503-resilience-gated", _llm, _json(503, RESILIENCE_503), SYSTEM, "server_error"),
    ("mcp-500", _mcp, _json(500, {"error": {"message": "upstream failure"}}), SYSTEM, "server_error"),
    ("mcp-503-resilience-gated", _mcp, _json(503, RESILIENCE_503), SYSTEM, "server_error"),
    ("a2a-502", _a2a, _json(502, {"error": {"message": "remote agent unreachable"}}), SYSTEM, "server_error"),
    ("skill-500", _mcp, lambda r: _rpc_tool_error("skill_error_500: runner crashed"), SYSTEM, "server_error"),
    # ---- system: rate_limited
    ("llm-429-credit-balance-exhausted", _llm, _json(429, CREDITS_429), SYSTEM, "rate_limited"),
    ("mcp-429-limit", _mcp, _json(429, {"error": {"message": "mcp calls per hour exceeded"}}), SYSTEM, "rate_limited"),
    ("mcp-429-run-cap", _mcp, _json(429, RUN_CAP_429), SYSTEM, "rate_limited"),
    ("skill-server-429-run-cap", _skill, lambda r: run_stopped_response("over_budget"), SYSTEM, "rate_limited"),
    ("skill-429-quota", _mcp, lambda r: _rpc_tool_error("skill_error_429: skill_quota_exceeded"), SYSTEM, "rate_limited"),
    # ---- system: approval_hold
    ("skill-202-approval-required", _mcp, lambda r: _rpc_tool_error("skill_error_202: approval_required"), SYSTEM, "approval_hold"),
    ("mcp-202-hold", _mcp, _json(202, APPROVAL_202), SYSTEM, "approval_hold"),
    ("skill-202-input-policy-hold-with-envelope", _skill, lambda r: skill_policy_held("apr-0007"), SYSTEM, "approval_hold"),
    ("skill-server-202-tool-policy-hold", _skill, _json(202, approval_envelope("apr-0008", "skills__run_script")), SYSTEM, "approval_hold"),
    # ---- system: governance_refusal
    ("mcp-403-autonomy-denied", _mcp, _json(403, AUTONOMY_403), SYSTEM, "governance_refusal"),
    ("llm-403-autonomy-denied", _llm, _json(403, AUTONOMY_403), SYSTEM, "governance_refusal"),
    ("mcp-403-run-aborted", _mcp, _json(403, RUN_ABORTED_403), SYSTEM, "governance_refusal"),
    ("skill-403-run-aborted-in-tool-result", _mcp, lambda r: _rpc_tool_error(SKILL_RUN_ABORTED), SYSTEM, "governance_refusal"),
    ("skill-server-403-run-aborted", _skill, lambda r: run_stopped_response("aborted"), SYSTEM, "governance_refusal"),
    ("mcp-403-argument-policy-unavailable", _mcp, _json(403, POLICY_UNAVAILABLE_403), SYSTEM, "governance_refusal"),
    ("skill-403-input-policy-unavailable", _skill, lambda r: skill_policy_denied(**POLICY_UNAVAILABLE), SYSTEM, "governance_refusal"),
    ("mcp-403-no-grant", _mcp, _json(403, NO_GRANT_403), SYSTEM, "governance_refusal"),
    ("mcp-401", _mcp, _json(401, {"error": {"message": "invalid API key"}}), SYSTEM, "governance_refusal"),
    ("skill-403-not-authorized", _mcp, lambda r: _rpc_tool_error("skill_error_403: agent_not_authorized"), SYSTEM, "governance_refusal"),
    # ---- system: unavailable
    ("mcp-404-server", _mcp, _json(404, {"error": {"message": "MCP server not found"}}), SYSTEM, "unavailable"),
    ("mcp-409", _mcp, _json(409, {"error": {"message": "conflict"}}), SYSTEM, "unavailable"),
    ("skill-404", _mcp, lambda r: _rpc_tool_error("skill_error_404: skill not found"), SYSTEM, "unavailable"),
    # ---- system: delegate_failed
    ("a2a-task-failed", _a2a, _a2a_task("TASK_STATE_FAILED"), SYSTEM, "delegate_failed"),
    ("a2a-task-rejected", _a2a, _a2a_task("TASK_STATE_REJECTED"), SYSTEM, "delegate_failed"),
    # ---- system: delegate_deviated (the gateway's checked outcome)
    ("a2a-outcome-undeclared", _a2a, _a2a_answer({"x-brutor-outcome-id": "screening_completed.maybe", "x-brutor-outcome-conformance": "undeclared"}), SYSTEM, "delegate_deviated"),
    ("a2a-outcome-unreported", _a2a, _a2a_answer({"x-brutor-outcome-conformance": "unreported"}), SYSTEM, "delegate_deviated"),
    ("a2a-outcome-not-judged", _a2a, _a2a_answer({}), SYSTEM, "delegate_deviated"),
    ("a2a-outcome-unexpected", _a2a_expecting, _a2a_answer({"x-brutor-outcome-id": "screening_completed.hit", "x-brutor-outcome-conformance": "declared"}), SYSTEM, "delegate_deviated"),
    ("a2a-outcome-metadata-undeclared", _a2a, _a2a_answer({}, {"brutor.outcome.id": "screening_completed.maybe", "brutor.outcome.conformance": "undeclared"}), SYSTEM, "delegate_deviated"),
    # ---- application: content_blocked
    ("llm-403-guardrail", _llm, _json(403, LLM_GUARDRAIL_403), APPLICATION, "content_blocked"),
    ("mcp-403-guardrail-jsonrpc", _mcp, _json(403, MCP_GUARDRAIL_403), APPLICATION, "content_blocked"),
    ("mcp-403-argument-policy-deny", _mcp, _json(403, ARGUMENT_POLICY_DENY_403), APPLICATION, "content_blocked"),
    ("mcp-403-argument-policy-field-not-comparable", _mcp, _json(403, NOT_COMPARABLE_403), APPLICATION, "content_blocked"),
    ("mcp-403-argument-policy-arguments-truncated", _mcp, _json(403, ARGUMENTS_TRUNCATED_403), APPLICATION, "content_blocked"),
    ("skill-403-input-policy-deny", _skill, lambda r: skill_policy_denied(), APPLICATION, "content_blocked"),
    (
        "skill-403-input-policy-arguments-truncated",
        _skill,
        lambda r: skill_policy_denied(rule="arguments_truncated", message="Refused: the arguments are too large or too deeply nested to be checked against this policy"),
        APPLICATION,
        "content_blocked",
    ),
    ("mcp-200-jsonrpc-policy-error", _mcp, _json(200, JSONRPC_POLICY_ERROR_200), APPLICATION, "content_blocked"),
    ("mcp-403-semantic-policy", _mcp, _json(403, SEMANTIC_403), APPLICATION, "content_blocked"),
    ("mcp-403-guardrail-flat", _mcp, _json(403, {"error": "guardrail_blocked", "guardrail": "prompt_injection", "message": "Blocked by guardrail"}), APPLICATION, "content_blocked"),
    # ---- application: rejected_request
    ("mcp-400", _mcp, _json(400, {"error": {"message": "invalid arguments"}}), APPLICATION, "rejected_request"),
    ("llm-400-context", _llm, _json(400, {"error": {"message": "maximum context length exceeded"}}), APPLICATION, "rejected_request"),
    ("mcp-422", _mcp, _json(422, {"error": {"message": "unprocessable"}}), APPLICATION, "rejected_request"),
    ("skill-400", _mcp, lambda r: _rpc_tool_error("skill_error_400: invalid input_params"), APPLICATION, "rejected_request"),
    # ---- application: tool_error
    ("mcp-tool-error-unknown-application", _mcp, lambda r: _rpc_tool_error("unknown application APP-1"), APPLICATION, "tool_error"),
    # ---- application: malformed_output
    ("llm-non-json-content", _llm, _chat_text("I cannot help with that."), APPLICATION, "malformed_output"),
    ("mcp-non-json-body", _mcp, lambda r: httpx.Response(200, text="<html>oops</html>"), APPLICATION, "malformed_output"),
]


@pytest.mark.parametrize("case_id, call, handler, expected_class, expected_kind", CASES, ids=[c[0] for c in CASES])
def test_gateway_answers_classify(settings, case_id, call, handler, expected_class, expected_kind):
    gw = Gateway(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(Exception) as info:
        call(gw)
    failure = classify_failure(info.value)
    assert (failure.failure_class, failure.kind) == (expected_class, expected_kind), f"{case_id}: {type(info.value).__name__}: {info.value}"
    assert failure.is_system is (expected_class is SYSTEM)


def test_exceptions_raised_by_the_agent_itself():
    assert classify_failure(ToolError("affordability skill returned unknown class 'maybe'")).kind == "tool_error"
    for exc in (KeyError("applicant"), ValueError("bad date"), TypeError("NoneType")):
        failure = classify_failure(exc)
        assert failure.failure_class is APPLICATION and failure.kind == "unexpected"


def test_every_kind_is_covered_and_documented():
    """A new kind must get a case above and a row in failures.py's table."""
    covered = {(c[3], c[4]) for c in CASES} | {(APPLICATION, "unexpected")}
    declared = {(SYSTEM, k) for k in SYSTEM_KINDS} | {(APPLICATION, k) for k in APPLICATION_KINDS}
    assert covered == declared
    for cls, kind in declared:
        assert f"| {cls.value} | {kind} |" in failures.__doc__, kind


def test_the_governance_markers_are_the_ones_the_gateway_writes():
    """`autonomy_denied: <reason>`, `run_aborted: <reason>` inside a skill's
    tool result, and the argument-policy rule `policy_unavailable`."""
    assert set(failures.GOVERNANCE_MARKERS) == {"autonomy_denied", "run_aborted", "policy_unavailable"}


@pytest.mark.parametrize(
    "text",
    ["applications_get: run aborted early for APP-1", "applications_get: field run_cap_reached is not a number"],
)
def test_a_tool_error_is_not_read_as_a_run_control_by_its_wording(text):
    failure = classify_failure(ToolError(text))
    assert (failure.failure_class, failure.kind) == (APPLICATION, "tool_error")


def test_an_unusable_answer_is_not_read_as_an_outage_by_its_wording():
    """No answer at all is GatewayUnreachable; a GatewayError is an answer the
    agent could not use, whatever words it quotes."""
    failure = classify_failure(GatewayError("model m did not return a JSON object: 'the connection timed out'"))
    assert (failure.failure_class, failure.kind) == (APPLICATION, "malformed_output")
