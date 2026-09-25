"""A fake Brutor gateway behind httpx.MockTransport.

It records every request (method, path, headers, JSON body) so tests can
assert the run/turn/step/close headers, and it answers the routes the agent
uses with canned, configurable responses.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from screening_agent.config import Settings
from screening_agent.gateway import Gateway
from screening_agent.prompts import DISCLOSURE

SAMPLE_APPLICATION: dict[str, Any] = {
    "application_id": "APP-20260923-001",
    "received_at": "2026-09-23T08:00:00Z",
    "applicant": {
        "applicant_id": "CUST-1001",
        "full_name": "Maja Lindholm",
        "date_of_birth": "1987-04-12",
        "country": "SE",
        "email": "maja.lindholm@example.se",
        "employment_status": "employed",
    },
    "requested_amount_eur": 12000,
    "term_months": 48,
    "purpose": "Kitchen renovation",
    "monthly_income_eur": 4200,
    "monthly_expenses_eur": 1900,
    "existing_debt_monthly_eur": 250,
    "status": "received",
    "recommendation": None,
    "notes": [],
    "screened_at": None,
}


@dataclass
class Call:
    method: str
    path: str
    headers: dict[str, str]
    body: Any

    @property
    def run_id(self) -> str | None:
        return self.headers.get("x-brutor-run-id")

    @property
    def tool(self) -> str | None:
        if isinstance(self.body, dict) and self.body.get("method") == "tools/call":
            return self.body["params"]["name"]
        return None

    @property
    def llm_api(self) -> str | None:
        if self.path == "/v1/proxy/llm/chat/completions":
            return "chat"
        if self.path == "/v1/proxy/llm/responses":
            return "responses"
        return None

    @property
    def kind(self) -> str:
        if "/v1/proxy/llm/" in self.path:
            return "llm"
        if "/v1/proxy/mcp/" in self.path:
            return "mcp"
        if "/v1/proxy/a2a/" in self.path:
            return "a2a"
        if "/v1/runs/" in self.path:
            return "run_end"
        if "/v1/portal/approvals/" in self.path:
            return "approval_poll"
        return "other"


def settings_for_tests(tmp_path) -> Settings:
    return Settings(
        gateway_url="http://gateway.test",
        api_key="sk_brutor_api_testkey0123456789",
        tenant_id="default",
        applications_mcp_server_id="mcp-apps",
        bureau_mcp_server_id="mcp-bureau",
        skills_mcp_server_id="system-agent-skill-server-default",
        fraud_card_id="agentcard-fraud",
        data_dir=tmp_path,
        tick_seconds=1,
        max_per_tick=5,
    )


@dataclass
class FakeGateway:
    application: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(SAMPLE_APPLICATION)))
    identity_verified: bool = True
    affordability_class: str = "comfortable"
    risk_band: str = "low"
    fraud_verdict: str = "clear"
    drafter_recommendation: str = "approve"
    drafter_includes_disclosure: bool = True
    approval_rule: Callable[[dict[str, Any]], bool] | None = None  # args -> hold?
    block_tool: str | None = None  # tool name that answers 403
    fail_tool: str | None = None  # tool name that answers 500
    fail_a2a: bool = False
    approval_statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_list: list[dict[str, Any]] | None = None
    derived_root: str = "root-derived-0001"
    responses_only_models: set[str] = field(default_factory=set)  # chat answers 400 for these
    calls: list[Call] = field(default_factory=list)
    approval_counter: int = 0
    recorded: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    run_ends: list[dict[str, Any]] = field(default_factory=list)

    # -- helpers ---------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def gateway(self, settings: Settings) -> Gateway:
        return Gateway(settings, http=httpx.Client(transport=self.transport()))

    def runs(self) -> dict[str, list[Call]]:
        out: dict[str, list[Call]] = {}
        for c in self.calls:
            if c.run_id:
                out.setdefault(c.run_id, []).append(c)
        return out

    def calls_of(self, kind: str) -> list[Call]:
        return [c for c in self.calls if c.kind == kind]

    # -- handler ---------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = raw.decode("utf-8", "replace")
        call = Call(request.method, request.url.path, {k.lower(): v for k, v in request.headers.items()}, body)
        self.calls.append(call)
        assert call.headers.get("authorization", "").startswith("Bearer sk_brutor_api_"), "missing Brutor bearer key"
        assert call.headers.get("x-brutor-run-end", "").lower() != "true", "X-Brutor-Run-End: true is banned"
        path = request.url.path
        if path == "/v1/proxy/llm/chat/completions":
            return self._llm(body)
        if path == "/v1/proxy/llm/responses":
            return self._responses(body)
        if path.startswith("/v1/proxy/mcp/"):
            return self._mcp(path.rsplit("/", 1)[1], body, call)
        if path == "/v1/proxy/a2a/outbound":
            return self._a2a(body)
        if path.startswith("/v1/runs/") and path.endswith("/end"):
            self.run_ends.append({"root": path.split("/")[3], "body": body})
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/v1/portal/approvals/") and path.endswith("/poll"):
            approval_id = path.split("/")[4]
            status = self.approval_statuses.get(approval_id)
            if status is None:
                return httpx.Response(404, json={"error": "Approval request not found"})
            return httpx.Response(200, json={"id": approval_id, **status})
        return httpx.Response(404, json={"error": f"no route {path}"})

    def _model_content(self, system: str) -> str:
        if "risk classification step" in system:
            content = json.dumps({"risk_band": self.risk_band, "key_factors": ["stable income", "moderate DTI"]})
        else:
            letter = "Dear Maja Lindholm, thank you for your application. Our pre-screening outcome and next step follow."
            if self.drafter_includes_disclosure:
                letter += " " + DISCLOSURE
            content = json.dumps(
                {
                    "recommendation": self.drafter_recommendation,
                    "rationale": "Income comfortably covers the installment.",
                    "customer_letter": letter,
                }
            )
        return content

    def _llm(self, body: dict[str, Any]) -> httpx.Response:
        assert "temperature" not in body and "top_p" not in body, "sampling params must not be sent"
        assert body.get("response_format") == {"type": "json_object"}
        if body["model"] in self.responses_only_models:
            return httpx.Response(
                400,
                json={
                    "error": "chat_completions_blocked",
                    "message": f"Model '{body['model']}' only supports the Responses endpoint. POST to /v1/proxy/llm/responses with this model_name instead.",
                },
            )
        content = self._model_content(body["messages"][0]["content"])
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }
        return httpx.Response(200, json=payload, headers={"x-brutor-run-id": self.derived_root})

    def _responses(self, body: dict[str, Any]) -> httpx.Response:
        """The Responses route as the proxy exposes it: model, input (string or
        [{role, content}]), optional instructions/max_output_tokens/stream, other
        fields passed through. Answers the OpenAI Responses object shape."""
        assert "temperature" not in body and "top_p" not in body, "sampling params must not be sent"
        assert "messages" not in body and "response_format" not in body, "chat fields on the responses route"
        assert body.get("text") == {"format": {"type": "json_object"}}
        items = body["input"]
        assert isinstance(items, list) and items[0]["role"] == "system"
        content = self._model_content(items[0]["content"])
        payload = {
            "id": "resp_fake",
            "object": "response",
            "created_at": 1,
            "model": body["model"],
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "msg_fake",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        }
        return httpx.Response(200, json=payload, headers={"x-brutor-run-id": self.derived_root})

    @staticmethod
    def _rpc(text: Any, *, is_error: bool = False, status: int = 200) -> httpx.Response:
        if not isinstance(text, str):
            text = json.dumps(text)
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})

    def _mcp(self, server: str, body: dict[str, Any], call: Call) -> httpx.Response:
        tool = body["params"]["name"]
        args = body["params"].get("arguments") or {}
        if tool == self.block_tool:
            return httpx.Response(403, json={"error": "guardrail_blocked", "guardrail": "prompt_injection", "surface": "mcp_output", "message": "Blocked by guardrail"})
        if tool == self.fail_tool:
            return httpx.Response(500, json={"error": "upstream failure"})
        if tool == "applications_list_pending":
            rows = self.pending_list
            if rows is None:
                rows = [{"application_id": self.application["application_id"], "received_at": "2026-09-23T08:00:00Z", "amount_eur": self.application["requested_amount_eur"], "purpose_short": "Kitchen"}]
            return self._rpc(rows[: int(args.get("limit", 10))])
        if tool == "applications_get":
            if args.get("application_id") != self.application["application_id"]:
                return self._rpc(f"unknown application {args.get('application_id')}", is_error=True)
            return self._rpc(self.application)
        if tool == "applications_set_recommendation":
            if self.approval_rule and self.approval_rule(args) and not call.headers.get("x-approval-token"):
                self.approval_counter += 1
                approval_id = f"apr-{self.approval_counter:04d}"
                self.approval_statuses.setdefault(approval_id, {"status": "pending"})
                return httpx.Response(
                    202,
                    json={
                        "approval_required": True,
                        "approval_request_id": approval_id,
                        "capability_type": "tool",
                        "capability_name": tool,
                        "poll_url": f"/v1/portal/approvals/{approval_id}/poll",
                        "message": "Tool requires approval.",
                    },
                )
            self.recorded.append({"args": args, "token": call.headers.get("x-approval-token")})
            return self._rpc({"ok": True, "application_id": args.get("application_id"), "status": "screened"})
        if tool == "applications_add_note":
            self.notes.append(args)
            return self._rpc({"ok": True, "notes_count": len(self.notes)})
        if tool == "bureau_verify_identity":
            return self._rpc({"verified": self.identity_verified, "match_score": 0.97 if self.identity_verified else 0.31, "checked_at": "2026-09-23T08:01:00Z"})
        if tool == "bureau_get_report":
            return self._rpc({"score": 712, "open_credit_lines": 2, "total_debt_eur": 5400, "delinquencies_24m": 0, "inquiries_6m": 1, "report_date": "2026-09-23", "bureau": "Borealis Demo Bureau"})
        if tool == "skills__run_script":
            assert server == "system-agent-skill-server-default"
            assert args["skill_name"] == "affordability-check" and args["script"] == "affordability.py"
            assert "requested_amount_eur" in args["args"]
            return self._rpc(
                {
                    "monthly_installment_eur": 301.2,
                    "dti_before": 0.06,
                    "dti_after": 0.13,
                    "disposable_after_eur": 1748.8,
                    "affordability_class": self.affordability_class,
                    "flags": [],
                    "policy_version": "2026-09",
                }
            )
        return self._rpc(f"unknown tool {tool}", is_error=True)

    def _a2a(self, body: dict[str, Any]) -> httpx.Response:
        assert body["target_card_id"] == "agentcard-fraud"
        assert body["capability"] == "screening.fraud_sanctions"
        payload = json.loads(body["message"]["parts"][0]["text"])
        assert "full_name" in payload and "bureau" in payload
        if self.fail_a2a:
            return httpx.Response(502, json={"error": "remote agent unreachable"})
        verdict = {
            "verdict": self.fraud_verdict,
            "sanctions_match": self.fraud_verdict == "hit",
            "reasons": [] if self.fraud_verdict == "clear" else ["mock reason"],
            "model_used": "gpt-5.2",
        }
        return httpx.Response(
            200,
            json={
                "task": {
                    "id": str(uuid.uuid4()),
                    "contextId": str(uuid.uuid4()),
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"kind": "text", "text": json.dumps(verdict)}]},
                    },
                }
            },
        )
