"""The screener's credential: a JWT-SVID from the Workload API on its model
call, fetched once and reused until half its lifetime has passed; no credential
means no model call (the verdict degrades to llm_unavailable)."""

import logging

import pytest
import respx
from starlette.testclient import TestClient

from fraud_screener.app import create_app
from fraud_screener.config import Settings
from fraud_screener.credential import ApiKey, CredentialError, Svid, WorkloadToken
from tests.conftest import LLM_URL, llm_response, payload, send_body, verdict_of

SPIFFE_ID = "spiffe://borealis.example/brutor-demo/fraud-screener-agent"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def svids(clock, ttl=300.0):
    issued = []

    def fetch(audience):
        assert audience == "brutor"
        issued.append(f"eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJmcmF1ZCJ9.sig{len(issued) + 1}")
        return Svid(issued[-1], clock.now + ttl, SPIFFE_ID)

    return fetch, issued


def test_the_model_call_carries_the_svid_and_reuses_it(settings, caplog):
    caplog.set_level(logging.DEBUG)
    settings.credential = "workload"
    clock = Clock()
    fetch, issued = svids(clock)
    app = create_app(settings, credential=WorkloadToken(fetch=fetch, clock=clock))
    with TestClient(app) as c, respx.mock(assert_all_called=True) as mock:
        route = mock.post(LLM_URL).mock(return_value=llm_response())
        verdict_of(c.post("/message:send", json=send_body(payload())))
        verdict_of(c.post("/message:send", json=send_body(payload())))
        clock.now += 151
        verdict_of(c.post("/message:send", json=send_body(payload())))
    sent = [call.request.headers["authorization"] for call in route.calls]
    assert sent == [f"Bearer {issued[0]}", f"Bearer {issued[0]}", f"Bearer {issued[1]}"]
    assert all(token not in caplog.text for token in issued)


def test_no_svid_means_no_model_call(settings):
    def refuse(audience):
        raise CredentialError("the Workload API gave no JWT-SVID: no identity issued")

    app = create_app(settings, credential=WorkloadToken(fetch=refuse))
    with TestClient(app) as c, respx.mock(assert_all_called=False) as mock:
        route = mock.post(LLM_URL).mock(return_value=llm_response())
        v = verdict_of(c.post("/message:send", json=send_body(payload())))
    assert not route.called
    assert v["model_used"] is None and any("llm_unavailable" in r for r in v["reasons"])


def test_workload_is_the_default_credential(monkeypatch):
    monkeypatch.delenv("BRUTOR_CREDENTIAL", raising=False)
    assert Settings.from_env().credential == "workload"
    monkeypatch.setenv("BRUTOR_CREDENTIAL", "api_key")
    assert Settings.from_env().credential == "api_key"
    monkeypatch.setenv("BRUTOR_CREDENTIAL", "password")
    with pytest.raises(ValueError, match="BRUTOR_CREDENTIAL"):
        Settings.from_env()
    with pytest.raises(CredentialError, match="FRAUD_BRUTOR_API_KEY"):
        ApiKey("").bearer()
