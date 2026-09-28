"""RFC 0023: the agent declares its implementation name and release on every
gateway call (Brutor headers on HTTP, clientInfo on MCP), the version comes
from the installed distribution and the build from BRUTOR_AGENT_BUILD."""

import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from screening_agent import identity
from screening_agent.approvals import PendingApprovals
from screening_agent.config import Settings
from screening_agent.gateway import Gateway
from screening_agent.graph import process_application
from screening_agent.identity import (
    AGENT_BUILD_HEADER,
    AGENT_NAME_HEADER,
    AGENT_VERSION_HEADER,
    DISTRIBUTION,
    MCP_CLIENT_INFO_META_KEY,
    AgentRelease,
    current_release,
)

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
BUILD = "3f9c2e1d0b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e"


def _pyproject_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_wire_contract_is_pinned():
    # Must match the gateway's constants (RFC 0023 section 5.1, both repos pin them).
    assert AGENT_NAME_HEADER == "X-Brutor-Agent-Name"
    assert AGENT_VERSION_HEADER == "X-Brutor-Agent-Version"
    assert AGENT_BUILD_HEADER == "X-Brutor-Agent-Build"
    assert MCP_CLIENT_INFO_META_KEY == "io.modelcontextprotocol/clientInfo"
    assert identity.BUILD_ENV == "BRUTOR_AGENT_BUILD"


def test_name_is_the_distribution_and_version_comes_from_metadata(settings):
    gw = Gateway(settings)
    assert gw.release.name == DISTRIBUTION == "brutor-demo-screening-agent"
    assert gw.release.version == metadata.version(DISTRIBUTION)
    # single source: the installed metadata is the pyproject version
    assert gw.release.version == _pyproject_version()


def test_version_follows_the_installed_distribution(settings, monkeypatch):
    """A new release is a pyproject bump: nothing else in the code carries a version."""
    monkeypatch.setattr(identity.metadata, "version", lambda dist: "9.8.7" if dist == DISTRIBUTION else "0")
    h = Gateway(settings).headers()
    assert h[AGENT_VERSION_HEADER] == "9.8.7"


def test_not_installed_is_refused(monkeypatch):
    def missing(_dist):
        raise metadata.PackageNotFoundError(DISTRIBUTION)

    monkeypatch.setattr(identity.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="not installed"):
        current_release()


def test_build_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRUTOR_AGENT_BUILD", BUILD)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    assert settings.agent_build == BUILD
    h = Gateway(settings).headers()
    assert h[AGENT_BUILD_HEADER] == BUILD


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_build_means_no_build_header(monkeypatch, tmp_path, value):
    if value is None:
        monkeypatch.delenv("BRUTOR_AGENT_BUILD", raising=False)
    else:
        monkeypatch.setenv("BRUTOR_AGENT_BUILD", value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    gw = Gateway(Settings.from_env())
    assert gw.release.build is None
    h = gw.headers()
    assert AGENT_BUILD_HEADER not in h
    assert h[AGENT_NAME_HEADER] == DISTRIBUTION and h[AGENT_VERSION_HEADER]


@pytest.mark.parametrize("bad", ["has space", "semi;colon", "x" * 129, "ünïcode"])
def test_out_of_bounds_build_refuses_to_start(settings, bad):
    settings.agent_build = bad
    with pytest.raises(ValueError, match="BRUTOR_AGENT_BUILD"):
        Gateway(settings)


def test_release_bounds():
    AgentRelease("brutor-demo-screening-agent", "0.1.0+local.1", "sha256:" + "a" * 64)
    with pytest.raises(ValueError):
        AgentRelease("Brutor Demo", "0.1.0")
    with pytest.raises(ValueError):
        AgentRelease("brutor-demo-screening-agent", "0.1.0 beta")


def _gateway_with_build(settings, fake):
    settings.agent_build = BUILD
    return fake.gateway(settings)


def _assert_release_headers(call, version):
    assert call.headers["x-brutor-agent-name"] == DISTRIBUTION, call.path
    assert call.headers["x-brutor-agent-version"] == version, call.path
    assert call.headers["x-brutor-agent-build"] == BUILD, call.path


def test_every_call_type_carries_the_release(settings, fake):
    """LLM (both routes), MCP tool, skill, A2A, run end and approval poll."""
    fake.responses_only_models = set()
    fake.approval_statuses["apr-1"] = {"status": "pending"}
    gw = _gateway_with_build(settings, fake)
    version = gw.release.version
    gw.begin_run("bds-APP-1-X")
    gw.llm("gpt-5.2", [{"role": "system", "content": "risk classification step"}, {"role": "user", "content": "x"}], api="chat", step_id="assess")
    gw.llm("gpt-5.5", [{"role": "system", "content": "draft"}, {"role": "user", "content": "x"}], api="responses", step_id="decide")
    gw.mcp_call("mcp-apps", "applications_get", {"application_id": "APP-20260923-001"}, step_id="gather")
    gw.skill_run("affordability-check", "affordability.py", {"requested_amount_eur": 1000}, step_id="gather")
    gw.a2a_delegate("agentcard-fraud", "screening.fraud_sanctions", {"full_name": "A", "bureau": {}}, step_id="assess")
    assert gw.end_run("completed", "resolved") is True
    gw.ctx = None
    gw.poll_approval("apr-1")

    kinds = [c.kind for c in fake.calls]
    assert kinds == ["llm", "llm", "mcp", "mcp", "a2a", "run_end", "approval_poll"]
    assert [c.llm_api for c in fake.calls[:2]] == ["chat", "responses"]
    for call in fake.calls:
        _assert_release_headers(call, version)


def test_mcp_calls_carry_client_info(settings, fake):
    gw = _gateway_with_build(settings, fake)
    gw.begin_run("bds-APP-1-X")
    gw.mcp_call("mcp-bureau", "bureau_get_report", {"applicant_id": "CUST-1001"}, step_id="gather")
    gw.skill_run("affordability-check", "affordability.py", {"requested_amount_eur": 1000}, step_id="gather")
    for call in fake.calls_of("mcp"):
        params = call.body["params"]
        assert params["_meta"] == {"io.modelcontextprotocol/clientInfo": {"name": DISTRIBUTION, "version": gw.release.version}}
        # clientInfo is metadata, never a tool argument
        assert "_meta" not in params["arguments"]
    # the skill server's tools/call carries it too
    assert [c.tool for c in fake.calls_of("mcp")] == ["bureau_get_report", "skills__run_script"]


def test_a_whole_run_carries_the_release_on_every_call(settings, fake, tmp_path):
    gw = _gateway_with_build(settings, fake)
    result = process_application(gw, settings, PendingApprovals(tmp_path), "APP-20260923-001")
    assert result.state == "completed"
    assert len(fake.calls) >= 8
    for call in fake.calls:
        _assert_release_headers(call, gw.release.version)
        if call.kind == "mcp":
            assert call.body["params"]["_meta"][MCP_CLIENT_INFO_META_KEY]["name"] == DISTRIBUTION


def test_health_reports_the_release(settings, fake, tmp_path):
    from starlette.testclient import TestClient

    from screening_agent.scheduler import RetryTracker, Scheduler

    gw = _gateway_with_build(settings, fake)
    sched = Scheduler(settings, gw, PendingApprovals(tmp_path), RetryTracker(tmp_path))
    body = TestClient(sched.health_app()).get("/health").json()
    assert body["agent"] == DISTRIBUTION
    assert body["version"] == gw.release.version and body["build"] == BUILD
