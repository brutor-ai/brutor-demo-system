"""Workload identity: the demo's SPIRE as the gateway's identity provider, each
agent identity's subject its SPIFFE ID, SPIRE's configuration and registration
entries saying the same, and copied_key.py judging the gateway's answer to a
copied API key from what the gateway actually returned."""

import copy
import re
from pathlib import Path

import pytest

import copied_key
import setup
import verify
from test_release_identity import FakeCP, provisioner

HERE = Path(setup.HERE)
SPIRE = HERE / "spire"
COMPOSE = (HERE / "docker-compose.yml").read_text(encoding="utf-8")
DEMO_SH = (HERE / "demo.sh").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The identity provider (setup.py step 9)
# --------------------------------------------------------------------------- #
def _idp_cp(stored=None):
    """A Control Plane holding at most one IdP, which keeps what is written."""
    state = {"idp": copy.deepcopy(stored)}

    def create(body):
        state["idp"] = dict(body, id="idp-1")
        return {"idp": dict(state["idp"])}

    def patch(body):
        state["idp"].update(body)
        return {"idp": dict(state["idp"])}

    return FakeCP({
        ("GET", "/v1/admin/idps"): lambda _b: {"idps": {"idp-1": dict(state["idp"])} if state["idp"] else {}},
        ("GET", "/v1/admin/idps/idp-1"): lambda _b: {"idp": dict(state["idp"])},
        ("POST", "/v1/admin/idps"): create,
        ("PATCH", "/v1/admin/idps/idp-1"): patch,
    })


def test_the_idp_is_the_spire_issuer_with_the_audience_brutor():
    cp = _idp_cp()
    assert provisioner(cp).workload_idp() == "idp-1"
    [(_, path, body)] = cp.writes()
    assert path == "/v1/admin/idps"
    assert body["issuer"] == setup.SPIRE_OIDC_ISSUER == "http://brutor-demo-spire-oidc:8008"
    assert body["protocol"] == "oidc" and body["type"] == "spiffe" and body["enabled"] is True
    assert body["workload_audiences"] == ["brutor"]
    assert body["jwks_uri"] == f"{setup.SPIRE_OIDC_ISSUER}/keys"


def test_the_idp_is_found_by_issuer_and_only_what_differs_is_patched():
    cp = _idp_cp()
    provisioner(cp).workload_idp()
    provisioner(cp).workload_idp()  # unchanged re-run
    assert len(cp.writes()) == 1
    cp.routes[("GET", "/v1/admin/idps/idp-1")] = lambda _b: {"idp": {
        "id": "idp-1", "issuer": setup.SPIRE_OIDC_ISSUER, "type": "spiffe", "enabled": True,
        "jwks_uri": setup.SPIRE_JWKS_URI, "well_known_url": f"{setup.SPIRE_OIDC_ISSUER}/.well-known/openid-configuration",
        "workload_audiences": ["something-else"]}}
    provisioner(cp).workload_idp()
    assert cp.writes()[-1][1:] == ("/v1/admin/idps/idp-1", {"workload_audiences": ["brutor"]})


# --------------------------------------------------------------------------- #
# The agent identities' subjects
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spiffe_id", [setup.SCREENING_SPIFFE_ID, setup.FRAUD_SPIFFE_ID])
def test_subjects_are_spiffe_ids_in_the_demo_trust_domain(spiffe_id):
    assert re.fullmatch(r"spiffe://borealis\.example/brutor-demo/[a-z-]+", spiffe_id)
    assert spiffe_id.startswith(f"spiffe://{setup.SPIRE_TRUST_DOMAIN}/")


def _identity_cp(row, keep=True):
    state = dict(row)

    def patch(body):
        if keep:
            state.update(body)
        return dict(state)

    return FakeCP({("GET", "/v1/admin/agent-identities/agent-1"): lambda _b: dict(state),
                   ("PATCH", "/v1/admin/agent-identities/agent-1"): patch})


def test_an_identity_without_its_subject_converges_once():
    cp = _identity_cp({"id": "agent-1", "idp_server_id": None, "idp_subject": None})
    prov = provisioner(cp)
    prov.ids["workload_idp_id"] = "idp-1"
    prov._converge_workload_subject("screening", "agent-1", setup.SCREENING_SPIFFE_ID)
    assert [c[2] for c in cp.writes()] == [{"idp_server_id": "idp-1", "idp_subject": setup.SCREENING_SPIFFE_ID}]
    prov._converge_workload_subject("screening", "agent-1", setup.SCREENING_SPIFFE_ID)
    assert len(cp.writes()) == 1


def test_a_subject_the_control_plane_does_not_keep_is_an_error():
    cp = _identity_cp({"id": "agent-1"}, keep=False)
    prov = provisioner(cp)
    prov.ids["workload_idp_id"] = "idp-1"
    with pytest.raises(setup.StepError, match="did not keep"):
        prov._converge_workload_subject("fraud", "agent-1", setup.FRAUD_SPIFFE_ID)


def test_a_new_identity_is_created_with_its_issuer_and_subject():
    created = {}

    def create(body):
        created.update(body)
        return {"agent": dict(body, id="agent-1")}

    cp = FakeCP({
        ("GET", "/v1/admin/agent-identities"): {"agents": []},
        ("POST", "/v1/admin/agent-identities"): create,
        ("GET", "/v1/admin/agent-identities/agent-1"): lambda _b: dict(created, id="agent-1"),
        ("GET", "/v1/admin/agent-identities/agent-1/grants"): {"grants": []},
    })
    prov = provisioner(cp)
    prov.ids["workload_idp_id"] = "idp-1"
    prov._ensure_identity("screening", setup.SCREENING_IDENTITY, "d", "o", "rg-demo", [], setup.SCREENING_DIST,
                          setup.SCREENING_SPIFFE_ID)
    assert created["source"] == "idp"
    assert (created["idp_server_id"], created["idp_subject"]) == ("idp-1", setup.SCREENING_SPIFFE_ID)


def test_demo_env_names_the_credential_and_keeps_the_keys_for_development(monkeypatch):
    written = {}
    monkeypatch.setattr(setup, "write_demo_env", written.update)
    prov = provisioner(FakeCP({}))
    prov.env, prov.portal_approver = {}, False
    prov.ids.update({
        "screening_api_key": "k1", "fraud_api_key": "k2", "applications_mcp_server_id": "a",
        "bureau_mcp_server_id": "b", "skills_mcp_server_id": "s", "fraud_card_id": "c",
        "classifier_model_name": "gpt-5.2", "drafter_model_name": "gpt-5.5", "fraud_group_id": "rg-f",
        "org_group_id": "rg-o"})
    prov.write_env()
    assert written["BRUTOR_CREDENTIAL"] == "workload"
    assert {"BRUTOR_API_KEY", "FRAUD_BRUTOR_API_KEY"} <= set(written)


# --------------------------------------------------------------------------- #
# SPIRE: configuration, compose and registration entries agree with setup.py
# --------------------------------------------------------------------------- #
def _conf(name, key):
    m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', (SPIRE / name).read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_spire_issues_what_the_idp_verifies():
    assert _conf("server.conf", "trust_domain") == setup.SPIRE_TRUST_DOMAIN
    assert _conf("server.conf", "jwt_issuer") == setup.SPIRE_OIDC_ISSUER
    assert _conf("agent.conf", "trust_domain") == setup.SPIRE_TRUST_DOMAIN
    assert _conf("agent-unix.conf", "trust_domain") == setup.SPIRE_TRUST_DOMAIN
    assert "brutor-demo-spire-oidc" in (SPIRE / "oidc-discovery-provider.conf").read_text(encoding="utf-8")
    # minutes, not hours: the agents refresh it
    assert _conf("server.conf", "default_jwt_svid_ttl") == "5m"


def test_spire_images_are_pinned_by_version_and_digest():
    images = re.findall(r"image:\s*(ghcr\.io/spiffe/\S+)", COMPOSE)
    assert len(images) == 3
    for image in images:
        assert re.fullmatch(r"ghcr\.io/spiffe/[a-z-]+:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", image), image


def test_each_agent_reaches_the_workload_api_and_carries_its_label():
    for label in ("screening-agent", "fraud-screener-agent"):
        assert f"io.brutor.demo.workload: {label}" in COMPOSE
    assert COMPOSE.count("SPIFFE_ENDPOINT_SOCKET: unix:///run/spire/sockets/api.sock") == 2
    assert COMPOSE.count("brutor-demo-spire-agent-socket:/run/spire/sockets:ro") == 2


def test_registration_entries_name_the_subjects_setup_registers():
    entries = re.findall(r'^\s*"([a-z-]+) (\d+) spiffe://\$SPIRE_TRUST_DOMAIN(/\S+)"$', DEMO_SH, re.M)
    assert re.search(rf'^SPIRE_TRUST_DOMAIN="{re.escape(setup.SPIRE_TRUST_DOMAIN)}"$', DEMO_SH, re.M)
    by_label = {label: (uid, f"spiffe://{setup.SPIRE_TRUST_DOMAIN}{path}") for label, uid, path in entries}
    assert by_label == {
        "screening-agent": ("10101", setup.SCREENING_SPIFFE_ID),
        "fraud-screener-agent": ("10102", setup.FRAUD_SPIFFE_ID),
    }
    # the unix fallback tells the agents apart by the user id their images run as
    for dist, (uid, _) in (("brutor-demo-screening-agent", by_label["screening-agent"]),
                           ("brutor-demo-fraud-screener-agent", by_label["fraud-screener-agent"])):
        dockerfile = (Path(setup.SYSTEM_ROOT) / dist / "Dockerfile").read_text(encoding="utf-8")
        assert f"--uid {uid}" in dockerfile


def test_the_join_token_is_never_echoed():
    body = DEMO_SH[DEMO_SH.index("cmd_spire() {"):DEMO_SH.index("cmd_up() {")]
    assert not re.search(r'(echo|printf|ok|log|warn)\b[^\n]*\$\{?token', body)


# --------------------------------------------------------------------------- #
# verify.py reads it back
# --------------------------------------------------------------------------- #
class _Rep:
    def __init__(self):
        self.lines = []

    def line(self, label, text, good=None):
        self.lines.append((label, good))

    def detail(self, text):
        pass


def test_verify_reads_back_the_idp_and_the_subjects():
    idp = {"id": "idp-1", "name": "x", "issuer": setup.SPIRE_OIDC_ISSUER, "enabled": True,
           "workload_audiences": ["brutor"], "jwks_uri": setup.SPIRE_JWKS_URI}
    agents = {"agents": [{"id": "a1", "name": setup.SCREENING_IDENTITY}, {"id": "a2", "name": setup.FRAUD_IDENTITY}]}
    cp = FakeCP({
        ("GET", "/v1/admin/idps"): {"idps": {"idp-1": idp}},
        ("GET", "/v1/admin/agent-identities"): agents,
        ("GET", "/v1/admin/agent-identities/a1"): {"idp_server_id": "idp-1", "idp_subject": setup.SCREENING_SPIFFE_ID},
        ("GET", "/v1/admin/agent-identities/a2"): {"idp_server_id": "idp-1", "idp_subject": "spiffe://elsewhere/x"},
    })
    rep = _Rep()
    verify.check_workload_identity(cp, rep)
    assert rep.lines == [("workload_idp", True), ("workload.screening", True), ("workload.fraud", False)]


# --------------------------------------------------------------------------- #
# copied_key.py
# --------------------------------------------------------------------------- #
REFUSAL = ('{"error": {"code": "CREDENTIAL_ASSURANCE_INSUFFICIENT", "message": "AI System brutor-demo-system '
           'requires workload identity"}}')


@pytest.mark.parametrize("status, text, headers, mcp, verdict", [
    (403, REFUSAL, {"Brutor-Credential-Required": "workload"}, False, copied_key.REFUSED),
    (403, REFUSAL, {}, True, copied_key.REFUSED),
    (200, '{"choices": []}', {"x-brutor-run-id": "r"}, False, copied_key.ACCEPTED),
    (200, '{"jsonrpc": "2.0", "id": 1, "result": {"content": []}}', {}, True, copied_key.ACCEPTED),
    # authenticated and routed; what came after (the provider, a cooldown) is not the gateway's verdict on the key
    (401, '{"error": {"message": "no provider key"}}', {"x-brutor-run-id": "r"}, False, copied_key.ACCEPTED),
    (503, '{"error": {"message": "cooldown"}}', {"X-Brutor-Run-Id": "r"}, False, copied_key.ACCEPTED),
    # the gateway's own refusal of an unknown key, a policy deny, an MCP error: none is the identity policy
    (401, '{"error": {"message": "Authentication required"}}', {}, False, copied_key.UNEXPECTED),
    (403, '{"error": "agent_not_authorized"}', {}, False, copied_key.UNEXPECTED),
    (200, '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}', {}, True, copied_key.UNEXPECTED),
])
def test_copied_key_judges_what_the_gateway_answered(status, text, headers, mcp, verdict):
    assert copied_key.judge(status, text, headers, mcp)[0] == verdict


class _Resp:
    def __init__(self, status, text, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


KEYS = {"BRUTOR_API_KEY": "sk_brutor_api_screeningSECRET", "FRAUD_BRUTOR_API_KEY": "sk_brutor_api_fraudSECRET",
        "APPLICATIONS_MCP_SERVER_ID": "mcp-apps", "CLASSIFIER_MODEL": "gpt-5.2"}


@pytest.mark.parametrize("answer, code", [
    (lambda: _Resp(403, REFUSAL, {"Brutor-Credential-Required": "workload"}), 0),
    (lambda: _Resp(200, '{"result": {}}'), 1),
    (lambda: _Resp(500, "boom"), 2),
])
def test_copied_key_exit_status_and_no_key_printed(monkeypatch, capsys, answer, code):
    sent = []

    def request(method, url, json=None, headers=None, timeout=None):
        sent.append((url, headers["Authorization"]))
        return answer()

    monkeypatch.setattr(copied_key, "read_demo_env", lambda: dict(KEYS))
    monkeypatch.setattr(copied_key, "stages", lambda env: {setup.DEMO_SYSTEM_NAME: "active",
                                                           setup.FRAUD_SYSTEM_NAME: "active"})
    monkeypatch.setattr(copied_key.requests, "request", request)
    assert copied_key.main([]) == code
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert [a for _, a in sent] == [f"Bearer {KEYS['BRUTOR_API_KEY']}"] * 2 + [f"Bearer {KEYS['FRAUD_BRUTOR_API_KEY']}"]
    assert sent[1][0].endswith("/v1/proxy/mcp/mcp-apps")


def test_copied_key_needs_the_provisioned_keys(monkeypatch, capsys):
    monkeypatch.setattr(copied_key, "read_demo_env", lambda: {})
    assert copied_key.main([]) == 2
    assert "run ./demo.sh provision first" in capsys.readouterr().out
