"""Starlette app: health, agent card, message:send (both spellings)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Settings
from .llm import DELEGATION_PREFIX, fraud_indicators
from .screening import verdict_for

log = logging.getLogger("fraud_screener.app")

SKILL_ID = "screening.fraud_sanctions"
AGENT_NAME = "Brutor Demo Fraud Screener"


def build_agent_card(public_url: str) -> dict[str, Any]:
    return {
        "name": AGENT_NAME,
        "description": (
            "Mock fraud and sanctions screening for consumer loan applications: "
            "synthetic sanctions list match, velocity heuristics and one model "
            "call for fraud indicators in the stated purpose. Part of the Brutor "
            "Demo System; not a real fraud detector."
        ),
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "url": public_url,
        "supportedInterfaces": [
            {"url": public_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "capabilities": {"streaming": False},
        "data_classification": ["PII"],
        "skills": [
            {
                "id": SKILL_ID,
                "name": SKILL_ID,
                "description": (
                    "Screen an applicant for sanctions-list matches and fraud indicators. "
                    "Input: one text part holding JSON {applicant_id, full_name, date_of_birth, "
                    "country, purpose, amount_eur, bureau: {inquiries_6m, delinquencies_24m, "
                    "open_credit_lines}}. Output: JSON {verdict: clear|review|hit, "
                    "sanctions_match, reasons[], model_used}."
                ),
                "tags": ["screening", "fraud", "sanctions"],
                "data_classification": ["PII"],
                "inputModes": ["text"],
                "outputModes": ["text"],
            }
        ],
    }


def extract_message(body: Any) -> dict[str, Any]:
    """body["message"], falling back to body["params"]["message"]."""
    if not isinstance(body, dict):
        return {}
    message = body.get("message")
    if not isinstance(message, dict):
        params = body.get("params")
        message = params.get("message") if isinstance(params, dict) else None
    return message if isinstance(message, dict) else {}


def first_text_part(message: dict[str, Any]) -> str:
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            return text
        if part.get("kind") == "text" and isinstance(part.get("text"), str):
            return part["text"]
    return ""


def parse_payload(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"purpose": text}
    return parsed if isinstance(parsed, dict) else {"purpose": text}


def task_response(verdict: dict[str, Any], *, state: str = "TASK_STATE_COMPLETED") -> dict[str, Any]:
    return {
        "task": {
            "id": str(uuid.uuid4()),
            "contextId": str(uuid.uuid4()),
            "status": {
                "state": state,
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "ROLE_AGENT",
                    "parts": [{"kind": "text", "text": json.dumps(verdict)}],
                },
            },
        }
    }


def create_app(settings: Settings | None = None, *, http_client: httpx.AsyncClient | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    card = build_agent_card(settings.public_url)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": "brutor-demo-fraud-screener-agent", "skill": SKILL_ID})

    async def agent_card(_request: Request) -> JSONResponse:
        return JSONResponse(card)

    async def message_send(request: Request) -> JSONResponse:
        correlation = request.headers.get("x-correlation-id", "-")
        depth = request.headers.get(f"{DELEGATION_PREFIX}depth", "-")
        chain_present = any(k.lower().startswith(DELEGATION_PREFIX) for k in request.headers.keys())
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            log.warning("message:send correlation=%s depth=%s error=invalid_json", correlation, depth)
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        message = extract_message(body)
        text = first_text_part(message)
        if not text:
            log.warning("message:send correlation=%s depth=%s error=no_text_part", correlation, depth)
            return JSONResponse({"error": "message has no text part"}, status_code=400)
        payload = parse_payload(text)

        llm_result, llm_error = await fraud_indicators(settings, payload, request.headers, client=http_client)
        model_used = settings.classifier_model if llm_result is not None else None
        verdict = verdict_for(payload, llm_result, llm_error, model_used)
        log.info(
            "message:send correlation=%s depth=%s chain=%s applicant=%s verdict=%s sanctions=%s model=%s",
            correlation,
            depth,
            "yes" if chain_present else "no",
            payload.get("applicant_id", "-"),
            verdict["verdict"],
            verdict["sanctions_match"],
            model_used or "heuristics-only",
        )
        return JSONResponse(task_response(verdict))

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/message:send", message_send, methods=["POST"]),
        Route("/message%3Asend", message_send, methods=["POST"]),
    ]
    return Starlette(routes=routes)
