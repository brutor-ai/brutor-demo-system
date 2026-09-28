"""RFC 0023 in provisioning and read-back: the agent identities declare their
implementation and approved release, the fraud screener's card version is its
release version (one source: pyproject.toml), and verify.py reads it all back."""

import copy
import tomllib
from pathlib import Path

import pytest

import setup
import verify

SYSTEM_ROOT = Path(setup.SYSTEM_ROOT)


def pyproject_version(dist):
    with (SYSTEM_ROOT / dist / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


class FakeCP:
    """Routes (method, path) to canned JSON; records every write."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.step = ""

    def _answer(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        value = self.routes.get((method, path))
        if callable(value):
            value = value(body)
        return (200 if value is not None else 404), copy.deepcopy(value)

    def get(self, path, params=None, tolerate=()):
        return self._answer("GET", path)

    def post(self, path, body=None, params=None, tolerate=()):
        return self._answer("POST", path, body)

    def put(self, path, body=None, params=None, tolerate=()):
        return self._answer("PUT", path, body)

    def patch(self, path, body=None, params=None, tolerate=()):
        return self._answer("PATCH", path, body)

    def writes(self, method=None):
        return [c for c in self.calls if c[0] != "GET" and (method is None or c[0] == method)]


def provisioner(cp):
    prov = setup.Provisioner.__new__(setup.Provisioner)
    prov.cp = prov.raw = cp
    prov.ids = {"demo_group_id": "rg-demo"}
    return prov


# --------------------------------------------------------------------------- #
# Declarations (setup.py step 9)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dist", [setup.SCREENING_DIST, setup.FRAUD_DIST])
def test_declaration_is_the_distribution_and_its_pyproject_version(dist):
    assert setup.implementation_declaration(dist) == {
        "implementation_name": dist,
        "approved_versions": [pyproject_version(dist)],
        "require_release": True,
    }


def _identity_cp(row, keep=True):
    state = dict(row)

    def patch(body):
        if keep:
            state.update(body)
        return dict(state)

    cp = FakeCP({("GET", "/v1/admin/agent-identities/agent-1"): lambda _b: dict(state),
                 ("PATCH", "/v1/admin/agent-identities/agent-1"): patch})
    return cp


def test_implementation_converges_once_and_only_what_differs():
    want = setup.implementation_declaration(setup.SCREENING_DIST)
    cp = _identity_cp({"id": "agent-1", "implementation_name": None, "approved_versions": [], "require_release": False})
    prov = provisioner(cp)
    prov._converge_implementation("screening", "agent-1", want)
    assert [c[2] for c in cp.writes()] == [want]
    prov._converge_implementation("screening", "agent-1", want)  # re-run: no drift
    assert len(cp.writes()) == 1
    prov._converge_implementation("screening", "agent-1", dict(want, approved_versions=["9.9.9"]))
    assert cp.writes()[-1][2] == {"approved_versions": ["9.9.9"]}


def test_implementation_not_kept_by_the_control_plane_is_an_error():
    cp = _identity_cp({"id": "agent-1"}, keep=False)
    with pytest.raises(setup.StepError, match="did not keep"):
        provisioner(cp)._converge_implementation("fraud", "agent-1", setup.implementation_declaration(setup.FRAUD_DIST))


# --------------------------------------------------------------------------- #
# Fraud screener card (setup.py step 8)
# --------------------------------------------------------------------------- #
def test_fallback_card_version_is_the_pyproject_version():
    assert setup.FALLBACK_FRAUD_CARD["version"] == pyproject_version(setup.FRAUD_DIST)


class _Live:
    def __init__(self, card):
        self.status_code, self._card = 200, card

    def json(self):
        return copy.deepcopy(self._card)


def _card_cp(stored_card, stored_version):
    return FakeCP({
        ("GET", "/v1/admin/agent-cards"): {"cards": [{"id": "card-1", "name": setup.FRAUD_CARD_NAME}]},
        ("GET", "/v1/admin/agent-cards/card-1"): {"id": "card-1", "card_json": stored_card, "version": stored_version},
        ("PATCH", "/v1/admin/agent-cards/card-1"): {"id": "card-1"},
        ("POST", "/v1/admin/agent-cards/card-1/sign"): {"id": "card-1", "trust_tier": "tenant_signed"},
        ("PUT", "/v1/admin/resource-groups/rg-demo/agent-cards"): {"ok": True},
    })


def test_new_release_refreshes_the_card_body_and_version_then_re_signs(monkeypatch):
    old = dict(setup.FALLBACK_FRAUD_CARD, version="0.1.0")
    live = dict(setup.FALLBACK_FRAUD_CARD, version="0.2.0")
    monkeypatch.setattr(setup.requests, "get", lambda *a, **k: _Live(live))
    cp = _card_cp(old, "0.1.0")
    provisioner(cp).agent_card()
    writes = cp.writes()
    assert writes[0] == ("PATCH", "/v1/admin/agent-cards/card-1", {"card_json": live, "version": "0.2.0"})
    # the PATCH drops the old signature; the card is signed again after it
    assert writes[1][:2] == ("POST", "/v1/admin/agent-cards/card-1/sign")
    assert writes[2][:2] == ("PUT", "/v1/admin/resource-groups/rg-demo/agent-cards")


def test_stale_version_column_alone_is_converged(monkeypatch):
    live = dict(setup.FALLBACK_FRAUD_CARD)
    monkeypatch.setattr(setup.requests, "get", lambda *a, **k: _Live(live))
    cp = _card_cp(live, "1.0.0")
    provisioner(cp).agent_card()
    assert cp.writes("PATCH") == [("PATCH", "/v1/admin/agent-cards/card-1", {"version": live["version"]})]


def test_unchanged_card_is_not_patched(monkeypatch):
    live = dict(setup.FALLBACK_FRAUD_CARD)
    monkeypatch.setattr(setup.requests, "get", lambda *a, **k: _Live(live))
    cp = _card_cp(live, live["version"])
    provisioner(cp).agent_card()
    assert cp.writes("PATCH") == []


# --------------------------------------------------------------------------- #
# verify.py read-back
# --------------------------------------------------------------------------- #
def _verify_cp(*, labels=(), declared=True, releases=None):
    routes = {
        ("GET", "/v1/admin/resource-groups/rg-demo"): {"id": "rg-demo", "intended_client_labels": list(labels)},
        ("GET", "/v1/admin/agent-identities"): {"agents": [
            {"id": "agent-s", "name": setup.SCREENING_IDENTITY}, {"id": "agent-f", "name": setup.FRAUD_IDENTITY}]},
    }
    for aid, dist in (("agent-s", setup.SCREENING_DIST), ("agent-f", setup.FRAUD_DIST)):
        want = setup.implementation_declaration(dist)
        row = dict(want) if declared else {"implementation_name": None, "approved_versions": [], "require_release": False}
        routes[("GET", f"/v1/admin/agent-identities/{aid}")] = dict(row, id=aid)
        routes[("GET", f"/v1/admin/agent-identities/{aid}/releases")] = {
            "agent_id": aid, "releases": (releases or {}).get(aid, []), "total": 0}
    return FakeCP(routes)


def _release(dist, *, approved=True, name_matches=True):
    version = pyproject_version(dist)
    return {"id": "rel-1", "name": dist, "version": version, "build": None, "label": f"{dist}@{version}",
            "trust": "declared", "run_count": 42, "approved": approved, "name_matches": name_matches,
            "current": True, "last_seen_at": "2026-09-27T10:00:00+00:00"}


def _run_verify(cp, capsys):
    rep = verify.Report(brief=True)
    verify.check_agent_releases(cp, rep, "rg-demo")
    return rep, capsys.readouterr().out


def test_verify_all_declared_and_no_release_yet_is_a_warning_not_a_failure(capsys):
    rep, out = _run_verify(_verify_cp(), capsys)
    assert rep.failures == []
    assert "✓ client_labels:" in out
    assert "✓ identity.screening:" in out and "✓ identity.fraud:" in out
    assert "⚠ release.screening: none observed yet" in out and "⚠ release.fraud: none observed yet" in out


def test_verify_approved_current_release_passes(capsys):
    releases = {"agent-s": [_release(setup.SCREENING_DIST)], "agent-f": [_release(setup.FRAUD_DIST)]}
    rep, out = _run_verify(_verify_cp(releases=releases), capsys)
    assert rep.failures == []
    assert f"✓ release.screening: {setup.SCREENING_DIST}@" in out and "trust=declared" in out


@pytest.mark.parametrize("kw", [{"labels": ["brutor-demo-screening-agent"]}, {"declared": False},
                                {"releases": {"agent-s": [_release(setup.SCREENING_DIST, approved=False)]}},
                                {"releases": {"agent-f": [_release(setup.FRAUD_DIST, name_matches=False)]}}])
def test_verify_flags_what_differs_from_the_declaration(capsys, kw):
    rep, _ = _run_verify(_verify_cp(**kw), capsys)
    assert rep.failures, kw
