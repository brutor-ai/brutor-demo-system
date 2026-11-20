"""Who a failed run is attributable to (DESIGN.md section 4, "Failure classes").

This is the one place that decides whether a failure is the APPLICATION's or
the SYSTEM's. The scheduler acts on the answer:

- An **application** failure is about this application's content or record: a
  guardrail or policy refused its content, a tool answered with an error about
  it, a request built from it was rejected. It counts toward the per-application
  attempt limit; after `MAX_ATTEMPTS_PER_APPLICATION` (3) the application is
  handed off to manual review.
- A **system** failure is the condition of the gateway, a provider, a delegate
  or the governance around the agent: no answer at all, 5xx, 429, a governance
  hold or refusal. It never counts against the application. The tick stops and
  the scheduler backs off.

The shapes below are the ones the agent actually sees (gateway.py raises them;
the bodies come from the Brutor Core Gateway). Every kind is covered by
tests/test_failures.py, including error strings recorded from a live gateway.

| class | kind | what the agent sees |
|---|---|---|
| system | unreachable | GatewayUnreachable: DNS failure, connection refused/reset, timeout (no HTTP answer) |
| system | server_error | HTTP >= 500, e.g. 503 `gateway_resilience_gated`, 502 from a delegate, `skill_error_5xx` |
| system | rate_limited | HTTP 429, e.g. provider `credit_balance_exhausted`, gateway limits and budgets, a run over its ceiling ("This run has made ... reaching its ceiling", on a system server's tools too), `skill_error_429` |
| system | approval_hold | a hold outside the record step: `skill_error_202: approval_required {envelope}` (an argument policy on the skill's input, or autonomy demoted to approval_required), an unexpected 202/428 |
| system | governance_refusal | `autonomy_denied: ...`, a run an operator stopped (HTTP 403 "This run was stopped by an operator", on a system server's tools too), HTTP 401, a 403 that carries no content verdict (missing grant or access), `skill_error_401/403`, an argument policy the gateway cannot apply (rule `policy_unavailable`) |
| system | no_credential | the agent has no credential to send: the SPIRE Agent's Workload API gave no JWT-SVID (CredentialError) |
| system | unavailable | other 4xx the gateway answers about the target, not the content: 404, 405, 408, 409, `skill_error_404` |
| system | delegate_failed | the A2A delegate's task ended FAILED / REJECTED / CANCELED |
| system | delegate_deviated | the delegate's outcome, as the gateway checked it, is not `declared`, is missing, or is not one the agent handles (DelegateDeviated) |
| application | content_blocked | a content verdict: `guardrail_blocked`, `semantic_policy_blocked`, an argument-policy deny (`argument_policy`, JSON-RPC -32000; on a skill's input `skill_error_403: Argument policy (<policy>/<rule>): ...`), including a compared field of the wrong type and `arguments_truncated` |
| application | rejected_request | HTTP 400 / 413 / 422, `skill_error_400/413/422`: the request built from this application was refused |
| application | tool_error | a tool answered with an error about this application (not found, invalid record, unknown class or verdict) |
| application | malformed_output | an answer without a status the agent could not use (non-JSON model or tool output) |
| application | unexpected | any other exception while processing this application (a malformed record) |

Order of the rules: no answer at all, then governance refusals (an
`autonomy_denied` refusal is the system's even when it names the
`approval_required` level or quotes a policy; so is `policy_unavailable`,
which arrives in the shape of an argument-policy deny but says the policy
could not be read, not that this application broke it), then approval holds,
then content verdicts (a guardrail block is a 403 and still the
application's; an argument-policy deny inside a skill run reads
`skill_error_403` and is still the application's), then the HTTP status,
then the exception family.

A stopped run carries no marker: the gateway answers every surface, the
system servers (`skills__*`, `search__*`) included, with HTTP 403 or 429 and
the operator-facing sentence, so the status classifies it. One refusal is
marked: a run stopped after the skills server admitted the call and before
the skill started answers inside the tool result,
`skill_error_403: run_aborted: <sentence>`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from .credential import CredentialError
from .gateway import ApprovalRequired, DelegateDeviated, DelegateFailed, GatewayError, GatewayUnreachable, PolicyBlocked, ToolError


class FailureClass(StrEnum):
    APPLICATION = "application"
    SYSTEM = "system"


SYSTEM_KINDS: dict[str, str] = {
    "unreachable": "no HTTP answer: DNS failure, connection refused or reset, timeout",
    "server_error": "HTTP 5xx from the gateway, a provider or a delegate",
    "rate_limited": "HTTP 429: provider credits or quota, gateway limits, budgets, run caps",
    "approval_hold": "a governance hold outside the record step (autonomy demoted to approval_required)",
    "governance_refusal": "autonomy denied, run stopped by an operator, missing grant or access, bad credentials, an argument policy the gateway cannot apply",
    "no_credential": "the Workload API gave no JWT-SVID, so there was nothing to authenticate with",
    "unavailable": "the gateway could not find or route the target (404, 405, 408, 409)",
    "delegate_failed": "the A2A delegate's task ended FAILED, REJECTED or CANCELED",
    "delegate_deviated": "the A2A delegate answered with an outcome that is not declared, missing or not one the agent handles",
}

APPLICATION_KINDS: dict[str, str] = {
    "content_blocked": "a guardrail, semantic policy or argument policy refused this application's content",
    "rejected_request": "HTTP 400, 413 or 422: the request built from this application was refused",
    "tool_error": "a tool answered with an error about this application",
    "malformed_output": "an answer the agent could not use (non-JSON model or tool output)",
    "unexpected": "any other exception while processing this application",
}

# Markers are matched on the lower-cased message plus the JSON body.
APPROVAL_MARKERS = ("approval_required",)
# `policy_unavailable` is the rule of the refusal for argument policies that
# exist but cannot be applied: every call of the group on that surface is
# refused, whatever its arguments.
GOVERNANCE_MARKERS = ("autonomy_denied", "run_aborted", "policy_unavailable")
# `argument policy (` opens the text of a deny on a skill's input
# (`skill_error_403: Argument policy (<policy>/<rule>): <message>`).
CONTENT_MARKERS = ("guardrail_blocked", "semantic_policy_blocked", "argument_policy", "argument policy (")
JSONRPC_POLICY_DENIED = -32000  # the core's JSON-RPC code for a guardrail or argument-policy deny
APPLICATION_STATUSES = frozenset({400, 413, 422})
SKILL_STATUS = re.compile(r"skill_error_(\d{3})")


@dataclass(frozen=True)
class Failure:
    failure_class: FailureClass
    kind: str
    detail: str = ""

    @property
    def is_system(self) -> bool:
        return self.failure_class is FailureClass.SYSTEM

    def label(self) -> str:
        return f"{self.failure_class.value}/{self.kind}"


def _system(kind: str, detail: str) -> Failure:
    return Failure(FailureClass.SYSTEM, kind, detail[:500])


def _application(kind: str, detail: str) -> Failure:
    return Failure(FailureClass.APPLICATION, kind, detail[:500])


def _jsonrpc_policy_denied(body: Any) -> bool:
    return isinstance(body, dict) and isinstance(body.get("error"), dict) and body["error"].get("code") == JSONRPC_POLICY_DENIED


def _classify(hint: str, status: int | None, text: str, body: Any = None) -> Failure:
    """The rules, in order. `hint` is the exception family: unreachable,
    approval, delegate, deviated, policy, tool, gateway or other."""
    low = text.lower()
    detail = text
    skill = SKILL_STATUS.search(low)
    skill_status = int(skill.group(1)) if skill else None

    if hint == "unreachable":
        return _system("unreachable", detail)
    if any(m in low for m in GOVERNANCE_MARKERS):
        # All three are about the run or the system. Checked before approval
        # markers: an autonomy denial names the level (`approval_required`)
        # it refuses at.
        return _system("governance_refusal", detail)
    if hint == "approval" or skill_status == 202 or any(m in low for m in APPROVAL_MARKERS):
        return _system("approval_hold", detail)
    if any(m in low for m in CONTENT_MARKERS) or _jsonrpc_policy_denied(body):
        return _application("content_blocked", detail)
    if hint == "delegate":
        return _system("delegate_failed", detail)
    if hint == "deviated":
        return _system("delegate_deviated", detail)

    effective = status if status not in (None, 200) else skill_status
    if effective is not None:
        if effective >= 500:
            return _system("server_error", detail)
        if effective == 429:
            return _system("rate_limited", detail)
        if effective in (401, 403):
            return _system("governance_refusal", detail)
        if effective in (202, 428):
            return _system("approval_hold", detail)
        if effective in APPLICATION_STATUSES:
            return _application("rejected_request", detail)
        if effective >= 400:
            return _system("unavailable", detail)

    if hint == "tool":
        return _application("tool_error", detail)
    if hint == "gateway":
        return _application("malformed_output", detail)
    return _application("unexpected", detail)


def _body_text(body: Any) -> str:
    if body is None:
        return ""
    try:
        text = body if isinstance(body, str) else json.dumps(body, default=str)
    except (TypeError, ValueError):
        text = str(body)
    return text[:4000]


def classify_failure(exc: BaseException) -> Failure:
    """Classify an exception raised while processing an application or a tick."""
    if isinstance(exc, CredentialError):
        return _system("no_credential", f"{type(exc).__name__}: {exc}")
    if isinstance(exc, (GatewayUnreachable, httpx.TransportError)):
        hint = "unreachable"
    elif isinstance(exc, ApprovalRequired):
        hint = "approval"
    elif isinstance(exc, DelegateDeviated):
        hint = "deviated"
    elif isinstance(exc, DelegateFailed):
        hint = "delegate"
    elif isinstance(exc, PolicyBlocked):
        hint = "policy"
    elif isinstance(exc, ToolError):
        hint = "tool"
    elif isinstance(exc, GatewayError):
        hint = "gateway"
    else:
        hint = "other"
    status = getattr(exc, "status", None) if isinstance(exc, GatewayError) else None
    body = getattr(exc, "body", None) if isinstance(exc, GatewayError) else None
    text = f"{type(exc).__name__}: {exc}"
    body_text = _body_text(body)
    return _classify(hint, status, f"{text} {body_text}".strip() if body_text else text, body)
