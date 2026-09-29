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
the bodies come from the Brutor core proxy). Every kind is covered by
tests/test_failures.py, including the exact error strings seen live on
2026-09-27/28.

| class | kind | what the agent sees |
|---|---|---|
| system | unreachable | GatewayUnreachable: DNS failure, connection refused/reset, timeout (no HTTP answer) |
| system | server_error | HTTP >= 500, e.g. 503 `gateway_resilience_gated`, 502 from a delegate, `skill_error_5xx` |
| system | rate_limited | HTTP 429, e.g. provider `credit_balance_exhausted`, gateway limits and budgets, `run_cap_reached`, `skill_error_429` |
| system | approval_hold | a hold outside the record step: `skill_error_202: approval_required` (autonomy demoted to approval_required), an unexpected 202/428 |
| system | governance_refusal | `autonomy_denied: ...`, `run_aborted` / "run aborted by an operator", HTTP 401, a 403 that carries no content verdict (missing grant or access), `skill_error_401/403` |
| system | unavailable | other 4xx the gateway answers about the target, not the content: 404, 405, 408, 409, `skill_error_404` |
| system | delegate_failed | the A2A delegate's task ended FAILED / REJECTED / CANCELED |
| application | content_blocked | a content verdict: `guardrail_blocked`, `semantic_policy_blocked`, an argument-policy deny (`argument_policy`, JSON-RPC -32000) |
| application | rejected_request | HTTP 400 / 413 / 422, `skill_error_400/413/422`: the request built from this application was refused |
| application | tool_error | a tool answered with an error about this application (not found, invalid record, unknown class or verdict) |
| application | malformed_output | an answer without a status the agent could not use (non-JSON model or tool output) |
| application | unexpected | any other exception while processing this application (a malformed record) |

Order of the rules: no answer at all, then governance refusals (an
`autonomy_denied` refusal is the system's even when it names the
`approval_required` level or quotes a policy), then approval holds, then
content verdicts (a guardrail block is a 403 and still the application's),
then the HTTP status, then the exception family.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from .gateway import ApprovalRequired, DelegateFailed, GatewayError, GatewayUnreachable, PolicyBlocked, ToolError


class FailureClass(StrEnum):
    APPLICATION = "application"
    SYSTEM = "system"


SYSTEM_KINDS: dict[str, str] = {
    "unreachable": "no HTTP answer: DNS failure, connection refused or reset, timeout",
    "server_error": "HTTP 5xx from the gateway, a provider or a delegate",
    "rate_limited": "HTTP 429: provider credits or quota, gateway limits, budgets, run caps",
    "approval_hold": "a governance hold outside the record step (autonomy demoted to approval_required)",
    "governance_refusal": "autonomy denied, run aborted, missing grant or access, bad credentials",
    "unavailable": "the gateway could not find or route the target (404, 405, 408, 409)",
    "delegate_failed": "the A2A delegate's task ended FAILED, REJECTED or CANCELED",
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
GOVERNANCE_MARKERS = ("autonomy_denied", "run_aborted", "run aborted", "run_cap_reached")
CONTENT_MARKERS = ("guardrail_blocked", "semantic_policy_blocked", "argument_policy")
JSONRPC_POLICY_DENIED = -32000  # the core's JSON-RPC code for a guardrail or argument-policy deny
# Transport failures as older builds (and the pre-v2 tracker file) wrote them:
# a plain GatewayError whose message is the httpx / socket error text.
TRANSPORT_MARKERS = (
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "connection refused",
    "connection reset",
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "timed out",
    "server disconnected",
    "remoteprotocolerror",
)
APPLICATION_STATUSES = frozenset({400, 413, 422})
SKILL_STATUS = re.compile(r"skill_error_(\d{3})")
MESSAGE_STATUS = re.compile(r"(?:\(|returned |failed \()(\d{3})\)?")


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
    approval, delegate, policy, tool, gateway or other."""
    low = text.lower()
    detail = text
    skill = SKILL_STATUS.search(low)
    skill_status = int(skill.group(1)) if skill else None

    if hint == "unreachable":
        return _system("unreachable", detail)
    if any(m in low for m in GOVERNANCE_MARKERS):
        # run_cap_reached is a 429 (the run spent its allowance); abort and
        # autonomy denial are 403s. All three are about the run or the system.
        # Checked before approval markers: an autonomy denial names the level
        # (`approval_required`) it refuses at.
        kind = "rate_limited" if "run_cap_reached" in low else "governance_refusal"
        return _system(kind, detail)
    if hint == "approval" or skill_status == 202 or any(m in low for m in APPROVAL_MARKERS):
        return _system("approval_hold", detail)
    if any(m in low for m in CONTENT_MARKERS) or _jsonrpc_policy_denied(body):
        return _application("content_blocked", detail)
    if hint == "delegate":
        return _system("delegate_failed", detail)

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
        if any(m in low for m in TRANSPORT_MARKERS):
            return _system("unreachable", detail)
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
    if isinstance(exc, (GatewayUnreachable, httpx.TransportError)):
        hint = "unreachable"
    elif isinstance(exc, ApprovalRequired):
        hint = "approval"
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


_REASON_HINTS = {
    "GatewayUnreachable": "unreachable",
    "ApprovalRequired": "approval",
    "DelegateFailed": "delegate",
    "PolicyBlocked": "policy",
    "ToolError": "tool",
    "GatewayError": "gateway",
}


def classify_reason(reason: str) -> Failure:
    """Classify a failure from its recorded text alone.

    Used for skip entries written before failures were classified (tracker
    file v1), whose reason reads `<run state>: <ExcType>: <message>` or
    `blocked_policy: blocked by policy: <body>`."""
    text = str(reason or "")
    state, _, rest = text.partition(": ")
    hint = "other"
    status: int | None = None
    if state == "blocked_policy":
        hint, status = "policy", 403
    else:
        exc_type, _, _message = rest.partition(": ")
        hint = _REASON_HINTS.get(exc_type.strip(), "other")
    if status is None:
        match = MESSAGE_STATUS.search(text)
        if match:
            status = int(match.group(1))
    return _classify(hint, status, text)
