"""The one gateway client the screening agent uses.

Everything the agent does that is governed goes through here: model calls,
MCP tool calls, skill runs, A2A delegation, run close and approval polling.
The client owns the run context and builds the correlation headers described
in ../DESIGN.md section 8, so a node never assembles a header by hand.

Header contract (every call inside a run):

    Authorization        Bearer sk_brutor_api_...
    X-Tenant-ID          tenant id (the key resolves the tenant; sent anyway)
    x-brutor-run-id      bds-<application_id>-<ulid>, identical on every call
    X-Brutor-Turn-Id     one turn per node that calls the gateway
    X-Brutor-Turn-Seq    turn ordinal within the run (1, 2, 3, ...)
    X-Brutor-Step-Id     the node name (intake, verify_identity, ...)
    X-Brutor-Step-Name   human label for the step
    traceparent          00-<trace id per run>-<span id per call>-01

Closing headers, only on the last call of the run:

    X-Brutor-Run-End     a literal terminal state, never "true"
    X-Brutor-Run-Outcome resolved | escalated | handed_off | abandoned_by_user

The fallback close is POST /v1/runs/{root}/end, where root is the
x-brutor-run-id RESPONSE header the gateway returns on LLM calls.

LLM calls use one of two OpenAI-shaped routes, chosen per model:

    chat       POST {gw}/v1/proxy/llm/chat/completions  (messages, response_format)
    responses  POST {gw}/v1/proxy/llm/responses         (input, text.format)

Both carry the same headers and both return the x-brutor-run-id header. A
model the gateway flags requires_responses_api answers the chat route with
HTTP 400 "only supports the Responses endpoint"; the client retries that
call once on the responses route and remembers the model as responses-only.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from .config import Settings

log = logging.getLogger("screening_agent.gateway")

RUN_ID_HEADER = "x-brutor-run-id"
TURN_ID_HEADER = "X-Brutor-Turn-Id"
TURN_SEQ_HEADER = "X-Brutor-Turn-Seq"
STEP_ID_HEADER = "X-Brutor-Step-Id"
STEP_NAME_HEADER = "X-Brutor-Step-Name"
RUN_END_HEADER = "X-Brutor-Run-End"
RUN_OUTCOME_HEADER = "X-Brutor-Run-Outcome"
APPROVAL_TOKEN_HEADER = "X-Approval-Token"
TRACEPARENT_HEADER = "traceparent"

TERMINAL_STATES = frozenset(
    {
        "completed",
        "completed_degraded",
        "errored",
        "blocked_policy",
        "cancelled",
        "exhausted",
        "abandoned",
    }
)
OUTCOMES = frozenset({"resolved", "escalated", "handed_off", "abandoned_by_user"})

LLM_APIS = ("chat", "responses")
RESPONSES_ONLY_MARKER = "only supports the responses endpoint"

RUN_ID_MAX = 128
TURN_ID_MAX = 64
STEP_ID_MAX = 64
STEP_NAME_MAX = 128

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A 26-character Crockford base32 ULID (48-bit ms timestamp + 80 random bits)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class GatewayError(RuntimeError):
    """Any non-success answer from the gateway that is not one of the typed cases."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ApprovalRequired(GatewayError):
    """HTTP 202 with approval_required: the action is held for a human."""

    def __init__(self, approval_id: str, poll_url: str | None, body: Any):
        super().__init__(f"approval required: {approval_id}", status=202, body=body)
        self.approval_id = approval_id
        self.poll_url = poll_url


class PolicyBlocked(GatewayError):
    """HTTP 403 with a guardrail or policy body: the run ends blocked_policy."""

    def __init__(self, body: Any, *, status: int = 403):
        super().__init__(f"blocked by policy: {_short(body)}", status=status, body=body)


class ToolError(GatewayError):
    """The tool ran and reported an error (JSON-RPC error or result.isError)."""


def _short(value: Any, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------- #
# Run context
# --------------------------------------------------------------------------- #
@dataclass
class RunContext:
    """Correlation state for one run: the asserted run id, the trace, and the
    current turn/step. One turn per node; the turn seq increments per node."""

    run_id: str
    trace_id: str = field(default_factory=lambda: secrets.token_hex(16))
    turn_seq: int = 0
    turn_id: str | None = None
    step: str | None = None
    derived_root: str | None = None
    closed: bool = False
    calls: int = 0

    def __post_init__(self) -> None:
        if not self.run_id or len(self.run_id) > RUN_ID_MAX:
            raise ValueError(f"run id must be 1..{RUN_ID_MAX} characters: {self.run_id!r}")
        if len(self.trace_id) != 32:
            raise ValueError("trace id must be 32 hex characters")

    def enter(self, step_id: str, turn: tuple[str, int] | None = None) -> None:
        """Record the step and turn of the next call. An explicit `turn`
        (id, seq) wins; otherwise a new turn starts whenever the step changes."""
        if turn is not None:
            turn_id, turn_seq = turn
            if not turn_id or len(turn_id) > TURN_ID_MAX:
                raise ValueError(f"turn id must be 1..{TURN_ID_MAX} characters: {turn_id!r}")
            if int(turn_seq) < 1:
                raise ValueError("turn seq starts at 1")
            self.turn_id, self.turn_seq = turn_id, int(turn_seq)
        elif step_id != self.step or self.turn_id is None:
            self.turn_seq += 1
            self.turn_id = f"t{self.turn_seq}"
        self.step = step_id

    def new_traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-01"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class Gateway:
    """Synchronous client for the Brutor Core Proxy.

    `http` may be injected (tests use an httpx.MockTransport); the OpenAI SDK
    client shares it so the LLM path is exercised the same way.
    """

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        self.base = settings.gateway_url.rstrip("/")
        self.http = http or httpx.Client(timeout=settings.http_timeout_seconds)
        self.ctx: RunContext | None = None
        self._rpc_id = 0
        self._openai = None
        # Models the gateway told us are Responses-only; learned once per process.
        self.responses_only: set[str] = set()

    # -- run lifecycle ------------------------------------------------------
    def begin_run(self, run_id: str) -> RunContext:
        if self.ctx is not None and not self.ctx.closed:
            log.warning("run=%s replaced an open run %s", run_id, self.ctx.run_id)
        self.ctx = RunContext(run_id=run_id)
        return self.ctx

    @contextmanager
    def run(self, run_id: str) -> Iterator[RunContext]:
        ctx = self.begin_run(run_id)
        try:
            yield ctx
        finally:
            self.ctx = None

    # -- headers ------------------------------------------------------------
    def headers(
        self,
        step_id: str | None = None,
        step_name: str | None = None,
        *,
        turn: tuple[str, int] | None = None,
        run_end: str | None = None,
        outcome: str | None = None,
        approval_token: str | None = None,
    ) -> dict[str, str]:
        """Build the per-call header set. Outside a run (ctx is None) only the
        auth headers are produced (approval polls are not proxied actions)."""
        h = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "X-Tenant-ID": self.settings.tenant_id,
        }
        ctx = self.ctx
        if ctx is not None:
            h[RUN_ID_HEADER] = ctx.run_id
            h[TRACEPARENT_HEADER] = ctx.new_traceparent()
            if step_id:
                if len(step_id) > STEP_ID_MAX:
                    raise ValueError(f"step id longer than {STEP_ID_MAX}: {step_id!r}")
                ctx.enter(step_id, turn)
                h[TURN_ID_HEADER] = ctx.turn_id or ""
                h[TURN_SEQ_HEADER] = str(ctx.turn_seq)
                h[STEP_ID_HEADER] = step_id
                h[STEP_NAME_HEADER] = (step_name or step_id)[:STEP_NAME_MAX]
        elif step_id:
            raise ValueError("step headers require an open run")

        if run_end is not None:
            if ctx is None:
                raise ValueError("a run-end header requires an open run")
            if run_end not in TERMINAL_STATES:
                raise ValueError(
                    f"X-Brutor-Run-End must be a literal terminal state, got {run_end!r}"
                )
            h[RUN_END_HEADER] = run_end
            if outcome is not None:
                if outcome not in OUTCOMES:
                    raise ValueError(f"unknown run outcome {outcome!r}")
                h[RUN_OUTCOME_HEADER] = outcome
        elif outcome is not None:
            raise ValueError("X-Brutor-Run-Outcome is only kept alongside X-Brutor-Run-End")

        if approval_token:
            h[APPROVAL_TOKEN_HEADER] = approval_token
        return h

    # -- helpers --------------------------------------------------------------
    def _note_response(self, response: httpx.Response | Any, *, run_end: str | None) -> None:
        """Remember the derived root the gateway returns and whether the run
        has been closed by a header."""
        ctx = self.ctx
        if ctx is None:
            return
        ctx.calls += 1
        headers = getattr(response, "headers", None)
        if headers is not None and ctx.derived_root is None:
            root = headers.get(RUN_ID_HEADER)
            if root:
                ctx.derived_root = root
        status = getattr(response, "status_code", 0)
        if run_end is not None and 200 <= int(status) < 300 and int(status) != 202:
            ctx.closed = True

    def _log_call(self, kind: str, target: str, status: int | str, step: str | None, ms: float, node: str | None = None) -> None:
        ctx = self.ctx
        log.info(
            "gateway kind=%s target=%s status=%s node=%s step=%s turn=%s run=%s ms=%.0f",
            kind,
            target,
            status,
            node or "-",
            step or "-",
            ctx.turn_id if ctx and step else "-",
            ctx.run_id if ctx else "-",
            ms,
        )

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        ctype = response.headers.get("content-type", "")
        text = response.text
        if "text/event-stream" in ctype:
            last: Any = None
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if not chunk:
                        continue
                    try:
                        last = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
            return last if last is not None else text
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return text

    @staticmethod
    def _approval_info(payload: Any) -> tuple[str, str | None] | None:
        candidates = []
        if isinstance(payload, dict):
            candidates.append(payload)
            err = payload.get("error")
            if isinstance(err, dict) and isinstance(err.get("data"), dict):
                candidates.append(err["data"])
        for cand in candidates:
            if cand.get("approval_required"):
                approval_id = (
                    cand.get("approval_request_id")
                    or cand.get("approval_id")
                    or cand.get("request_id")
                    or cand.get("id")
                )
                if approval_id:
                    return str(approval_id), cand.get("poll_url")
        return None

    def _raise_for_status(self, response: httpx.Response, payload: Any) -> None:
        status = response.status_code
        if status == 202:
            info = self._approval_info(payload)
            if info:
                raise ApprovalRequired(info[0], info[1], payload)
        if status == 403:
            raise PolicyBlocked(payload, status=status)
        if status == 428:
            info = self._approval_info(payload)
            if info:
                raise ApprovalRequired(info[0], info[1], payload)
        if status >= 400:
            raise GatewayError(f"gateway returned {status}: {_short(payload)}", status=status, body=payload)

    @staticmethod
    def _loads_tolerant(text: str) -> Any:
        """Parse JSON out of a model or tool text; tolerate code fences."""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.lower().startswith("json"):
                stripped = stripped[4:]
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            start, end = stripped.find("{"), stripped.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(stripped[start : end + 1])
                except json.JSONDecodeError:
                    return None
            return None

    # -- LLM -----------------------------------------------------------------
    @property
    def openai(self):
        if self._openai is None:
            from openai import OpenAI

            self._openai = OpenAI(
                base_url=f"{self.base}/v1/proxy/llm",
                api_key=self.settings.api_key,
                http_client=self.http,
                max_retries=0,
                timeout=self.settings.http_timeout_seconds,
            )
        return self._openai

    def llm(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        json: bool = True,
        api: str | None = None,
        step_id: str | None = None,
        step_name: str | None = None,
        turn: tuple[str, int] | None = None,
        node: str | None = None,
        run_end: str | None = None,
        outcome: str | None = None,
    ) -> Any:
        """Model call through the gateway on the chat or the responses route.

        `api` is "chat" or "responses" (None = chat unless the model is known
        to be responses-only). Returns the parsed JSON object when `json` is
        true (raises GatewayError if the model returned none), else the raw
        text. No temperature or top_p is ever sent on either route."""
        if api is None:
            api = "responses" if model in self.responses_only else "chat"
        if api not in LLM_APIS:
            raise ValueError(f"unknown LLM api {api!r}; expected one of {LLM_APIS}")
        if api == "chat" and model in self.responses_only:
            api = "responses"
        hdr = self.headers(step_id, step_name, turn=turn, run_end=run_end, outcome=outcome)
        if api == "chat":
            try:
                content = self._chat(model, messages, json, hdr, step_id, node)
            except GatewayError as exc:
                if exc.status == 400 and RESPONSES_ONLY_MARKER in _short(exc.body, 2000).lower():
                    log.warning(
                        "model=%s is Responses-only per the gateway; retrying on /responses and remembering it "
                        "(set DRAFTER_API/CLASSIFIER_API=responses to skip this round trip)",
                        model,
                    )
                    self.responses_only.add(model)
                    content = self._responses(model, messages, json, hdr, step_id, node)
                else:
                    raise
        else:
            content = self._responses(model, messages, json, hdr, step_id, node)
        if not json:
            return content
        parsed = self._loads_tolerant(content)
        if not isinstance(parsed, dict):
            raise GatewayError(f"model {model} did not return a JSON object: {_short(content)}")
        return parsed

    def _chat(self, model: str, messages: list[dict[str, Any]], json: bool, hdr: dict[str, str], step_id: str | None, node: str | None = None) -> str:
        """POST /v1/proxy/llm/chat/completions via the OpenAI SDK."""
        import openai as openai_sdk

        kwargs: dict[str, Any] = {"model": model, "messages": messages, "extra_headers": hdr}
        if json:
            kwargs["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        try:
            raw = self.openai.chat.completions.with_raw_response.create(**kwargs)
        except openai_sdk.APIStatusError as exc:
            self._log_call("llm", model, exc.status_code, step_id, (time.monotonic() - started) * 1000, node)
            raise self._llm_status_error(exc) from exc
        except openai_sdk.APIError as exc:
            self._log_call("llm", model, "error", step_id, (time.monotonic() - started) * 1000, node)
            raise GatewayError(f"llm call failed: {exc}") from exc
        self._log_call("llm", model, raw.status_code, step_id, (time.monotonic() - started) * 1000, node)
        self._note_response(raw, run_end=hdr.get(RUN_END_HEADER))
        completion = raw.parse()
        return completion.choices[0].message.content or ""

    def _responses(self, model: str, messages: list[dict[str, Any]], json: bool, hdr: dict[str, str], step_id: str | None, node: str | None = None) -> str:
        """POST /v1/proxy/llm/responses via the OpenAI SDK.

        The proxy accepts `model`, `input` (a string or a list of {role,
        content} items), `instructions`, `max_output_tokens`, `stream` and
        passes other fields (such as `text.format`) through to the upstream.
        Messages are sent as `input` items so the system prompt stays a system
        role on the governed chat view. The text is read from `output_text`
        when present, else from output[].content[] items of type output_text."""
        import openai as openai_sdk

        kwargs: dict[str, Any] = {"model": model, "input": messages, "extra_headers": hdr}
        if json:
            kwargs["text"] = {"format": {"type": "json_object"}}
        started = time.monotonic()
        try:
            raw = self.openai.responses.with_raw_response.create(**kwargs)
        except openai_sdk.APIStatusError as exc:
            self._log_call("llm_responses", model, exc.status_code, step_id, (time.monotonic() - started) * 1000, node)
            raise self._llm_status_error(exc) from exc
        except openai_sdk.APIError as exc:
            self._log_call("llm_responses", model, "error", step_id, (time.monotonic() - started) * 1000, node)
            raise GatewayError(f"llm responses call failed: {exc}") from exc
        self._log_call("llm_responses", model, raw.status_code, step_id, (time.monotonic() - started) * 1000, node)
        self._note_response(raw, run_end=hdr.get(RUN_END_HEADER))
        try:
            data = raw.http_response.json()
        except ValueError as exc:
            raise GatewayError(f"responses route returned non-JSON: {_short(raw.http_response.text)}") from exc
        return self._responses_text(data)

    @staticmethod
    def _responses_text(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        text = data.get("output_text")
        if isinstance(text, str) and text:
            return text
        chunks: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") not in (None, "message"):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        return "".join(chunks)

    @staticmethod
    def _llm_status_error(exc: Any) -> GatewayError:
        # The SDK keeps only body["error"] when that key exists, which for the
        # gateway's {"error": "<code>", "message": "..."} drops the message.
        # Prefer the full response JSON.
        body: Any = None
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                body = None
        if body is None:
            body = exc.body if exc.body is not None else str(exc)
        if exc.status_code == 403:
            return PolicyBlocked(body)
        return GatewayError(f"llm call failed ({exc.status_code}): {_short(body)}", status=exc.status_code, body=body)

    # -- MCP -----------------------------------------------------------------
    def mcp_call(
        self,
        server_id: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        step_id: str | None = None,
        step_name: str | None = None,
        turn: tuple[str, int] | None = None,
        node: str | None = None,
        run_end: str | None = None,
        outcome: str | None = None,
        approval_token: str | None = None,
    ) -> Any:
        """Raw JSON-RPC tools/call. Returns the parsed JSON from
        result.content[0].text when it parses, else the raw text."""
        hdr = self.headers(step_id, step_name, turn=turn, run_end=run_end, outcome=outcome, approval_token=approval_token)
        hdr["Content-Type"] = "application/json"
        hdr["Accept"] = "application/json, text/event-stream"
        self._rpc_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        started = time.monotonic()
        try:
            response = self.http.post(f"{self.base}/v1/proxy/mcp/{server_id}", json=body, headers=hdr)
        except httpx.HTTPError as exc:
            self._log_call("mcp", tool, "error", step_id, (time.monotonic() - started) * 1000, node)
            raise GatewayError(f"mcp call {tool} failed: {exc}") from exc
        self._log_call("mcp", tool, response.status_code, step_id, (time.monotonic() - started) * 1000, node)
        payload = self._parse_body(response)
        self._raise_for_status(response, payload)
        self._note_response(response, run_end=run_end)
        if not isinstance(payload, dict):
            raise GatewayError(f"mcp call {tool}: non-JSON response: {_short(payload)}", body=payload)
        if payload.get("error"):
            err = payload["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise ToolError(f"{tool}: {msg}", status=response.status_code, body=payload)
        result = payload.get("result") or {}
        content = result.get("content") or []
        text = ""
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text = part["text"]
                break
        if result.get("isError"):
            raise ToolError(f"{tool}: {_short(text or result)}", status=response.status_code, body=payload)
        parsed = self._loads_tolerant(text) if text else None
        return parsed if parsed is not None else text

    def skill_run(
        self,
        skill_name: str,
        script: str,
        args: dict[str, Any],
        **hdr: Any,
    ) -> Any:
        """Run one skill script on the skills MCP server (tool skills__run_script)."""
        return self.mcp_call(
            self.settings.skills_mcp_server_id,
            "skills__run_script",
            {"skill_name": skill_name, "script": script, "args": args},
            **hdr,
        )

    # -- A2A -----------------------------------------------------------------
    def a2a_delegate(
        self,
        card_id: str,
        capability: str,
        payload: dict[str, Any],
        *,
        step_id: str | None = None,
        step_name: str | None = None,
        turn: tuple[str, int] | None = None,
        node: str | None = None,
        run_end: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        """Delegate to a remote agent via the outbound A2A proxy. Returns the
        verdict JSON found in task.status.message.parts[0].text."""
        import uuid

        hdr = self.headers(step_id, step_name, turn=turn, run_end=run_end, outcome=outcome)
        hdr["Content-Type"] = "application/json"
        body = {
            "target_card_id": card_id,
            "capability": capability,
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": json.dumps(payload)}],
            },
        }
        started = time.monotonic()
        try:
            response = self.http.post(f"{self.base}/v1/proxy/a2a/outbound", json=body, headers=hdr)
        except httpx.HTTPError as exc:
            self._log_call("a2a", capability, "error", step_id, (time.monotonic() - started) * 1000, node)
            raise GatewayError(f"a2a delegation failed: {exc}") from exc
        self._log_call("a2a", capability, response.status_code, step_id, (time.monotonic() - started) * 1000, node)
        out = self._parse_body(response)
        self._raise_for_status(response, out)
        self._note_response(response, run_end=run_end)
        if not isinstance(out, dict):
            raise GatewayError(f"a2a: non-JSON response: {_short(out)}", body=out)
        task = out.get("task") or out.get("result") or {}
        if not isinstance(task, dict):
            raise GatewayError(f"a2a: unexpected response shape: {_short(out)}", body=out)
        status = task.get("status") or {}
        state = str(status.get("state", "")).upper()
        if "FAILED" in state or "REJECTED" in state or "CANCELED" in state or "CANCELLED" in state:
            raise ToolError(f"a2a task ended {state}: {_short(status)}", body=out)
        parts = (status.get("message") or {}).get("parts") or []
        text = ""
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text = part["text"]
                break
        verdict = self._loads_tolerant(text) if text else None
        if not isinstance(verdict, dict):
            raise GatewayError(f"a2a: remote agent returned no JSON verdict: {_short(text or out)}", body=out)
        return verdict

    # -- run close and approvals -------------------------------------------------
    def end_run(self, state: str, outcome: str | None) -> bool:
        """Close the run via POST /v1/runs/{root}/end. Requires the derived root
        captured from an earlier response; otherwise a warning and no-op."""
        if state not in TERMINAL_STATES:
            raise ValueError(f"unknown terminal state {state!r}")
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError(f"unknown run outcome {outcome!r}")
        ctx = self.ctx
        if ctx is None:
            log.warning("end_run(%s) called outside a run", state)
            return False
        if ctx.closed:
            log.info("run=%s already closed by header; end_run(%s) skipped", ctx.run_id, state)
            return False
        if not ctx.derived_root:
            log.warning(
                "run=%s cannot be closed as %s: no derived root captured yet "
                "(no LLM call answered); the gateway will sweep it after the idle window",
                ctx.run_id,
                state,
            )
            return False
        body: dict[str, Any] = {"state": state}
        if outcome:
            body["outcome"] = outcome
        hdr = self.headers()
        hdr["Content-Type"] = "application/json"
        started = time.monotonic()
        try:
            response = self.http.post(f"{self.base}/v1/runs/{ctx.derived_root}/end", json=body, headers=hdr)
        except httpx.HTTPError as exc:
            self._log_call("run_end", state, "error", None, (time.monotonic() - started) * 1000)
            log.error("run=%s end_run(%s) failed: %s", ctx.run_id, state, exc)
            return False
        self._log_call("run_end", state, response.status_code, None, (time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            log.error("run=%s end_run(%s) rejected: %s %s", ctx.run_id, state, response.status_code, _short(response.text))
            return False
        ctx.closed = True
        return True

    def poll_approval(self, approval_id: str) -> dict[str, Any]:
        """GET /v1/portal/approvals/{id}/poll -> {status, approval_token?}."""
        hdr = self.headers()
        hdr["Accept"] = "application/json"
        started = time.monotonic()
        try:
            response = self.http.get(f"{self.base}/v1/portal/approvals/{approval_id}/poll", headers=hdr)
        except httpx.HTTPError as exc:
            self._log_call("approval_poll", approval_id, "error", None, (time.monotonic() - started) * 1000)
            raise GatewayError(f"approval poll failed: {exc}") from exc
        self._log_call("approval_poll", approval_id, response.status_code, None, (time.monotonic() - started) * 1000)
        if response.status_code == 404:
            return {"id": approval_id, "status": "not_found"}
        payload = self._parse_body(response)
        if response.status_code >= 400:
            raise GatewayError(f"approval poll returned {response.status_code}: {_short(payload)}", status=response.status_code, body=payload)
        if not isinstance(payload, dict):
            raise GatewayError(f"approval poll: non-JSON response: {_short(payload)}", body=payload)
        return payload

    def close(self) -> None:
        self.http.close()
