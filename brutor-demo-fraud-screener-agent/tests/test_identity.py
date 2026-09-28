"""RFC 0023: the fraud screener names its implementation and release on its own
outbound gateway call (the model call); the version comes from the installed
distribution, the build from BRUTOR_AGENT_BUILD, and the caller's agent
headers are never copied."""

import tomllib
from importlib import metadata
from pathlib import Path

import pytest
import respx
from starlette.testclient import TestClient

from fraud_screener import identity
from fraud_screener.app import create_app
from fraud_screener.config import Settings
from fraud_screener.identity import (
    AGENT_BUILD_HEADER,
    AGENT_NAME_HEADER,
    AGENT_VERSION_HEADER,
    DISTRIBUTION,
    AgentRelease,
    current_release,
)
from tests.conftest import LLM_URL, llm_response, payload, send_body, verdict_of

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
BUILD = "sha256:" + "0123456789abcdef" * 4


def _pyproject_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _sent_headers(client, inbound=None):
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(LLM_URL).mock(return_value=llm_response())
        verdict_of(client.post("/message:send", json=send_body(payload()), headers=inbound or {}))
    assert route.call_count == 1
    return {k.lower(): v for k, v in route.calls[0].request.headers.items()}


def test_wire_contract_is_pinned():
    assert AGENT_NAME_HEADER == "X-Brutor-Agent-Name"
    assert AGENT_VERSION_HEADER == "X-Brutor-Agent-Version"
    assert AGENT_BUILD_HEADER == "X-Brutor-Agent-Build"
    assert identity.BUILD_ENV == "BRUTOR_AGENT_BUILD"


def test_version_comes_from_installed_metadata():
    release = current_release()
    assert release.name == DISTRIBUTION == "brutor-demo-fraud-screener-agent"
    assert release.version == metadata.version(DISTRIBUTION) == _pyproject_version()


def test_llm_call_carries_name_and_version(client):
    h = _sent_headers(client)
    assert h["x-brutor-agent-name"] == DISTRIBUTION
    assert h["x-brutor-agent-version"] == metadata.version(DISTRIBUTION)


def test_llm_call_version_follows_the_installed_distribution(settings, monkeypatch):
    monkeypatch.setattr(identity.metadata, "version", lambda dist: "2.0.0" if dist == DISTRIBUTION else "0")
    with TestClient(create_app(settings)) as c:
        h = _sent_headers(c)
    assert h["x-brutor-agent-version"] == "2.0.0"


def test_build_from_env(monkeypatch):
    monkeypatch.setenv("BRUTOR_AGENT_BUILD", BUILD)
    monkeypatch.setenv("FRAUD_BRUTOR_API_KEY", "sk_brutor_api_fraudkey0123456789")
    monkeypatch.setenv("BRUTOR_GATEWAY_URL", "http://gateway.test")
    settings = Settings.from_env()
    assert settings.agent_build == BUILD
    with TestClient(create_app(settings)) as c:
        h = _sent_headers(c)
        health = c.get("/health").json()
    assert h["x-brutor-agent-build"] == BUILD
    assert health["agent"] == DISTRIBUTION and health["build"] == BUILD


@pytest.mark.parametrize("value", [None, ""])
def test_no_build_means_no_build_header(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("BRUTOR_AGENT_BUILD", raising=False)
    else:
        monkeypatch.setenv("BRUTOR_AGENT_BUILD", value)
    monkeypatch.setenv("FRAUD_BRUTOR_API_KEY", "sk_brutor_api_fraudkey0123456789")
    monkeypatch.setenv("BRUTOR_GATEWAY_URL", "http://gateway.test")
    with TestClient(create_app(Settings.from_env())) as c:
        h = _sent_headers(c)
    assert "x-brutor-agent-build" not in h
    assert h["x-brutor-agent-name"] == DISTRIBUTION


def test_callers_agent_headers_are_not_copied(settings):
    """The inbound request is the gateway relaying the screening agent's call;
    the screener declares its own release, never the caller's."""
    inbound = {
        "X-Brutor-Agent-Name": "brutor-demo-screening-agent",
        "X-Brutor-Agent-Version": "0.0.1",
        "X-Brutor-Agent-Build": "caller-build",
        "x-brutor-delegation-depth": "1",
    }
    release = AgentRelease(DISTRIBUTION, "1.2.3")
    with TestClient(create_app(settings, release=release)) as c:
        h = _sent_headers(c, inbound)
    assert h["x-brutor-agent-name"] == DISTRIBUTION
    assert h["x-brutor-agent-version"] == "1.2.3"
    assert "x-brutor-agent-build" not in h
    assert h["x-brutor-delegation-depth"] == "1"


@pytest.mark.parametrize("bad", ["has space", "x" * 129])
def test_out_of_bounds_build_refuses_to_start(settings, bad):
    settings.agent_build = bad
    with pytest.raises(ValueError, match="BRUTOR_AGENT_BUILD"):
        create_app(settings)


def test_not_installed_is_refused(monkeypatch):
    def missing(_dist):
        raise metadata.PackageNotFoundError(DISTRIBUTION)

    monkeypatch.setattr(identity.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="not installed"):
        current_release()


def test_served_card_version_is_the_distribution_version(client):
    """One source: the A2A card, the version header and approved_versions all
    name the installed distribution's version (pyproject.toml)."""
    card = client.get("/.well-known/agent-card.json").json()
    assert card["version"] == metadata.version(DISTRIBUTION) == _pyproject_version()


def test_served_card_version_follows_the_release(settings):
    with TestClient(create_app(settings, release=AgentRelease(DISTRIBUTION, "3.1.4"))) as c:
        assert c.get("/.well-known/agent-card.json").json()["version"] == "3.1.4"
