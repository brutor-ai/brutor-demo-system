"""The agent's credential: a JWT-SVID from the Workload API, refreshed before it
expires, sent as the Bearer of every gateway call; the API key only when
BRUTOR_CREDENTIAL=api_key (development stage)."""

import logging

import pytest

from screening_agent import credential as credential_module
from screening_agent.config import Settings
from screening_agent.credential import ApiKey, CredentialError, Svid, WorkloadToken, from_settings

SPIFFE_ID = "spiffe://borealis.example/brutor-demo/screening-agent"
MSGS = [
    {"role": "system", "content": "You are the risk classification step. Respond with JSON."},
    {"role": "user", "content": "classify"},
]


def jwt(n: int) -> str:
    """A token-shaped placeholder; nothing verifies it here."""
    return f"eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.sig{n}"


class FakeWorkloadApi:
    """Hands out JWT-SVIDs that live `ttl` seconds from the clock's now."""

    def __init__(self, clock, ttl=300.0):
        self.clock, self.ttl, self.audiences = clock, ttl, []

    def __call__(self, audience):
        self.audiences.append(audience)
        return Svid(jwt(len(self.audiences)), self.clock.now + self.ttl, SPIFFE_ID)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_the_token_is_reused_until_half_its_lifetime_has_passed():
    clock = Clock()
    api = FakeWorkloadApi(clock)
    token = WorkloadToken(fetch=api, clock=clock)
    first = token.bearer()
    clock.now += 149
    assert token.bearer() == first and len(api.audiences) == 1
    clock.now += 1  # 150 s of 300: refreshed, never close to expiry
    second = token.bearer()
    assert second != first and len(api.audiences) == 2
    assert api.audiences == ["brutor", "brutor"]
    assert token.spiffe_id == SPIFFE_ID


def test_an_expired_svid_is_refused():
    clock = Clock()
    token = WorkloadToken(fetch=lambda aud: Svid(jwt(1), clock.now - 1, SPIFFE_ID), clock=clock)
    with pytest.raises(CredentialError, match="expired"):
        token.bearer()


def test_a_workload_api_failure_is_a_credential_error(monkeypatch):
    class Refusing:
        def __init__(self, socket_path=None, default_timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch_jwt_svid(self, audience):
            raise RuntimeError("no identity issued")

    import spiffe

    monkeypatch.setattr(spiffe, "WorkloadApiClient", Refusing)
    with pytest.raises(CredentialError, match="no identity issued"):
        credential_module.fetch_from_spire("brutor")


def test_neither_token_nor_key_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    token = WorkloadToken(fetch=FakeWorkloadApi(clock), clock=clock)
    value = token.bearer()
    assert SPIFFE_ID in caplog.text
    assert value not in caplog.text


def test_every_call_carries_the_svid_including_the_llm_sdk_path(settings, fake):
    clock = Clock()
    api = FakeWorkloadApi(clock)
    gw = fake.gateway(settings, credential=WorkloadToken(fetch=api, clock=clock))
    with gw.run("bds-APP-1-X"):
        gw.llm("gpt-5.2", MSGS, api="chat", step_id="assess", turn=("t1", 1))
        clock.now += 200  # past half the lifetime: the next call carries a fresh token
        gw.llm("gpt-5.2", MSGS, api="responses", step_id="assess", turn=("t1", 1))
    sent = [c.headers["authorization"] for c in fake.calls_of("llm")]
    assert sent == [f"Bearer {jwt(1)}", f"Bearer {jwt(2)}"]
    assert all(settings.api_key not in s for s in sent)


def test_workload_is_the_default_and_needs_the_socket_not_a_key(monkeypatch):
    for name in ("BRUTOR_CREDENTIAL", "BRUTOR_API_KEY", "SPIFFE_ENDPOINT_SOCKET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"APPLICATIONS_MCP_SERVER_ID": "a", "BUREAU_MCP_SERVER_ID": "b", "FRAUD_CARD_ID": "c"}.items():
        monkeypatch.setenv(name, value)
    s = Settings.from_env()
    assert s.credential == "workload"
    assert s.missing() == ["SPIFFE_ENDPOINT_SOCKET"]
    monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", "unix:///run/spire/sockets/api.sock")
    assert Settings.from_env().missing() == []
    assert isinstance(from_settings("workload", ""), WorkloadToken)


def test_the_api_key_is_the_development_stage_choice(monkeypatch):
    monkeypatch.setenv("BRUTOR_CREDENTIAL", "API_KEY")
    monkeypatch.delenv("BRUTOR_API_KEY", raising=False)
    s = Settings.from_env()
    assert s.credential == "api_key" and "BRUTOR_API_KEY" in s.missing()
    assert "SPIFFE_ENDPOINT_SOCKET" not in s.missing()
    assert isinstance(from_settings("api_key", "sk_brutor_api_x"), ApiKey)
    with pytest.raises(CredentialError):
        ApiKey("").bearer()
    monkeypatch.setenv("BRUTOR_CREDENTIAL", "password")
    with pytest.raises(ValueError, match="BRUTOR_CREDENTIAL"):
        Settings.from_env()
