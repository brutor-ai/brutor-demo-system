import json

import httpx
import pytest
from starlette.testclient import TestClient

from fraud_screener.app import create_app
from fraud_screener.config import Settings

GATEWAY = "http://gateway.test"
LLM_URL = f"{GATEWAY}/v1/proxy/llm/chat/completions"


@pytest.fixture
def settings():
    return Settings(
        gateway_url=GATEWAY,
        api_key="sk_brutor_api_fraudkey0123456789",
        tenant_id="default",
        classifier_model="gpt-5.2",
        public_url="http://brutor-demo-fraud-screener-agent:9200",
        llm_timeout_seconds=2.0,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def llm_response(indicators=None, suspicious=False):
    content = json.dumps({"fraud_indicators": indicators or [], "suspicious": suspicious})
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-5.2",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        },
        headers={"x-brutor-run-id": "root-of-caller"},
    )


def payload(**overrides):
    base = {
        "applicant_id": "CUST-1001",
        "full_name": "Maja Lindholm",
        "date_of_birth": "1987-04-12",
        "country": "SE",
        "purpose": "Kitchen renovation",
        "amount_eur": 12000,
        "bureau": {"inquiries_6m": 1, "delinquencies_24m": 0, "open_credit_lines": 2},
    }
    base.update(overrides)
    return base


def send_body(p, *, wrap_params=False, rpc=False):
    message = {"messageId": "m-1", "role": "user", "parts": [{"kind": "text", "text": json.dumps(p)}]}
    if wrap_params:
        return {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": message}}
    return {"message": message}


def verdict_of(response):
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["status"]["message"]["role"] == "ROLE_AGENT"
    part = task["status"]["message"]["parts"][0]
    assert part["kind"] == "text"
    return json.loads(part["text"])
