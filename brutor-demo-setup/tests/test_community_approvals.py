"""The Community Edition path (the demo system is the Community sample and
acceptance test).

A Community gateway serves no /v1/portal/* and has no end-user sign-in, and
the Control Plane seeds no stock password. So by default:

  * setup.py creates no portal user: held decisions are decided by an
    administrator (Admin Console, Mission Control -> Approvals), and step 10
    only proves that administrator can read the approvals;
  * approve.py makes the same decision through the Control Plane, for a
    terminal and for an unattended run;
  * the administrator's password comes from the environment or from Brutor's
    own `.env`, never from a literal;
  * the User Portal approver is the Enterprise variant, behind
    --portal-approver, refused on a Community gateway before anything is
    created;
  * every control-plane route the scripts call exists in the Community API
    (checked against the Control Plane's recorded Community OpenAPI document
    when that repository is checked out next to this one).
"""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import approve
import setup
import verify

HERE = Path(setup.HERE)
GID = "rg-demo"
APPROVALS = "/v1/admin/approvals"


class FakeCP:
    """(method, path) -> body | (status, body) | callable(body, params).
    Unknown routes answer 404. A status of 400 or more that the caller did not
    tolerate raises StepError, as setup.Client does."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.step = ""
        self.token = None
        self.headers_sent = []

    def _answer(self, method, path, body=None, params=None, tolerate=()):
        self.calls.append((method, path, copy.deepcopy(body)))
        value = self.routes.get((method, path))
        if callable(value):
            value = value(body, params)
        status, data = value if isinstance(value, tuple) else ((200, value) if value is not None else (404, None))
        if status >= 400 and status not in tolerate:
            raise setup.StepError(self.step, f"{method} {path} -> HTTP {status}")
        return status, copy.deepcopy(data)

    def get(self, path, params=None, tolerate=()):
        return self._answer("GET", path, None, params, tolerate)

    def post(self, path, body=None, params=None, tolerate=(), headers=None):
        if headers is not None:
            self.headers_sent.append((path, dict(headers)))
        return self._answer("POST", path, body, params, tolerate)

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def provisioner(cp, portal_approver=False):
    prov = setup.Provisioner.__new__(setup.Provisioner)
    prov.cp = prov.raw = cp
    prov.ids = {"demo_group_id": GID}
    prov.env = {}
    prov.portal_approver = portal_approver
    return prov


# --------------------------------------------------------------------------- #
# The administrator's password: from the environment, never a literal
# --------------------------------------------------------------------------- #
SCRIPTS = ("setup.py", "verify.py", "approve.py", "demo.sh", ".env.example", "docker-compose.yml")


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_stock_password_in_any_script(name):
    text = (HERE / name).read_text()
    assert "Admin123" not in text and "Underwriter123" not in text
    assert "sysadmin" not in text and "system-admin" not in text  # the system administrator is Enterprise


def test_the_example_env_ships_no_password():
    values = dict(line.split("=", 1) for line in (HERE / ".env.example").read_text().splitlines()
                  if re.match(r"^[A-Z_]+=", line))
    assert values["ADMIN_PASSWORD"] == "" and values["ADMIN_USER"] == "" and values["BRUTOR_ENV_FILE"] == ""
    assert "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD" not in values


def test_no_password_configured_is_an_empty_one_not_a_default():
    assert setup.admin_credentials({}) == ("admin", "", "")


def test_the_package_name_is_read_from_the_environment():
    env = {"BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": "from-package", "BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME": "root"}
    assert setup.admin_credentials(env) == ("root", "from-package", "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD")


def test_an_explicit_password_wins_over_the_generated_one():
    env = {"ADMIN_USER": "ops", "ADMIN_PASSWORD": "changed", "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": "generated"}
    assert setup.admin_credentials(env) == ("ops", "changed", "ADMIN_PASSWORD")


def test_brutors_own_env_file_gives_the_administrator_and_nothing_else(tmp_path):
    package = tmp_path / ".env"
    package.write_text("# Brutor Agent Control\nJWT_SECRET=never-read\nENCRYPTION_KEY=never-read\n"
                       "BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME=admin2\nBRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD=\"minted\"\n")
    assert setup.read_env_names(str(package), ("BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME", "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD")) == {
        "BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME": "admin2", "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": "minted"}
    user, password, source = setup.admin_credentials({"BRUTOR_ENV_FILE": str(package)})
    assert (user, password) == ("admin2", "minted")
    assert source == f"BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD in {package}"


def test_a_missing_package_env_file_is_named():
    with pytest.raises(setup.StepError, match="BRUTOR_ENV_FILE points at /nowhere/.env"):
        setup.admin_credentials({"BRUTOR_ENV_FILE": "/nowhere/.env"})


def test_login_refuses_without_a_password_and_calls_nothing(monkeypatch):
    monkeypatch.setattr(setup, "ADMIN_PASSWORD", "")
    cp = FakeCP({})
    with pytest.raises(setup.StepError, match="BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD"):
        setup.admin_login(cp)
    assert cp.calls == []


def test_login_sends_the_configured_password(monkeypatch):
    monkeypatch.setattr(setup, "ADMIN_USER", "admin")
    monkeypatch.setattr(setup, "ADMIN_PASSWORD", "minted")
    cp = FakeCP({("POST", "/v1/admin/users/tenant/login"): {"access_token": "t"}})
    setup.admin_login(cp)
    assert cp.token == "t"
    assert cp.calls == [("POST", "/v1/admin/users/tenant/login",
                         {"username": "admin", "password": "minted", "tenant_id": setup.TENANT_ID})]


def test_a_refused_login_says_where_the_password_came_from(monkeypatch):
    monkeypatch.setattr(setup, "ADMIN_PASSWORD", "stale")
    monkeypatch.setattr(setup, "ADMIN_PASSWORD_SOURCE", "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD")
    cp = FakeCP({("POST", "/v1/admin/users/tenant/login"): (401, {"detail": "Invalid credentials"})})
    with pytest.raises(setup.StepError, match="came from BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD") as err:
        setup.admin_login(cp)
    assert "stale" not in str(err.value)


def test_a_sign_in_route_that_is_not_served_is_the_http_error_it_is(monkeypatch):
    """Whatever answers 404 at the sign-in path is not this release's Control
    Plane (a wrong CP_URL, another service on the port): the script reports
    the request and the status and guesses nothing about what answered."""
    monkeypatch.setattr(setup, "ADMIN_PASSWORD", "minted")
    with pytest.raises(setup.StepError) as err:
        setup.admin_login(FakeCP({}))
    message = str(err.value)
    assert message.endswith("POST /v1/admin/users/tenant/login -> HTTP 404")
    assert "minted" not in message


def test_the_scripts_start_without_a_password_only_to_say_so(tmp_path):
    """verify.py and approve.py on a machine with nothing configured: a named
    refusal, exit 1, no traceback."""
    # An exported empty value wins over a developer's local .env (load_dotenv
    # never overrides the environment), so this holds on any machine.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ADMIN_PASSWORD": "",
           "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": "", "BRUTOR_ENV_FILE": "", "CP_URL": "http://127.0.0.1:9"}
    for script in ("approve.py", "verify.py"):
        done = subprocess.run([sys.executable, str(HERE / script)], env=env, capture_output=True, text=True, timeout=60)
        assert done.returncode == 1, (script, done.stdout, done.stderr)
        assert "no administrator password" in done.stdout and "Traceback" not in done.stderr


# --------------------------------------------------------------------------- #
# Step 10 by default: an administrator decides; nothing is created
# --------------------------------------------------------------------------- #
def test_by_default_step_10_reads_the_pending_count_and_writes_nothing(capsys):
    cp = FakeCP({("GET", f"{APPROVALS}/pending-count"): {"pending_count": 2}})
    provisioner(cp).approvers()
    assert cp.calls == [("GET", f"{APPROVALS}/pending-count", None)]
    out = capsys.readouterr().out
    assert "Mission Control -> Approvals" in out and "./demo.sh approve" in out and "2 pending" in out
    assert "Portal" not in out


def test_a_control_plane_without_approval_routes_stops_the_setup():
    with pytest.raises(setup.StepError, match="no approval routes"):
        provisioner(FakeCP({})).approvers()


def test_an_administrator_who_cannot_read_approvals_stops_the_setup():
    cp = FakeCP({("GET", f"{APPROVALS}/pending-count"): (403, {"detail": "Permission denied"})})
    with pytest.raises(setup.StepError, match="tool_approval:read"):
        provisioner(cp).approvers()


def test_the_portal_user_is_provisioned_only_with_the_flag(monkeypatch):
    called = []
    monkeypatch.setattr(setup.Provisioner, "portal_underwriter", lambda self: called.append(1))
    routes = {("GET", f"{APPROVALS}/pending-count"): {"pending_count": 0}}
    provisioner(FakeCP(routes)).approvers()
    assert called == []
    provisioner(FakeCP(routes), portal_approver=True).approvers()
    assert called == [1]


def test_the_default_is_no_portal_approver():
    assert setup.Provisioner(residency=False, skip_lifecycle=False).portal_approver is False


def test_the_default_plan_creates_no_end_user(capsys):
    setup.dry_run(residency=False, skip_lifecycle=False)
    plan = capsys.readouterr().out
    assert "edition: Community (or Enterprise)" in plan
    assert "/v1/admin/end-users" not in plan and "/v1/portal" not in plan and "PORTAL_UNDERWRITER" not in plan
    assert "10. approvers" in plan and f"{APPROVALS}/pending-count" in plan


def test_the_enterprise_plan_names_the_probe_and_the_portal_user(capsys):
    setup.dry_run(residency=False, skip_lifecycle=False, portal_approver=True)
    plan = capsys.readouterr().out
    assert "needs the Enterprise Edition User Portal" in plan
    assert plan.index("1e. preflight.portal") < plan.index("2. models")
    assert "10e. portal approver (Enterprise Edition)" in plan and "/v1/admin/end-users" in plan
    assert "PORTAL_UNDERWRITER_PASSWORD" in plan


def test_the_old_flag_is_gone():
    done = subprocess.run([sys.executable, str(HERE / "setup.py"), "--no-portal-user", "--dry-run"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "unrecognized arguments" in done.stderr


# --------------------------------------------------------------------------- #
# --portal-approver: Enterprise Edition, refused on a Community gateway
# --------------------------------------------------------------------------- #
def test_a_community_gateway_refuses_the_portal_variant_before_anything_is_created():
    cp = FakeCP({})  # no /v1/portal/* at all: 404
    with pytest.raises(setup.StepError, match="Enterprise Edition feature") as err:
        provisioner(cp, portal_approver=True).require_portal()
    assert "Mission Control -> Approvals" in err.value.reason
    assert cp.calls == [("POST", "/v1/portal/auth/login", {})]
    # The gateway resolves the tenant before routing: without it an unknown
    # path is refused for the missing tenant, not answered 404.
    assert cp.headers_sent == [("/v1/portal/auth/login", {"X-Tenant-ID": setup.TENANT_ID})]


@pytest.mark.parametrize("status", [400, 401, 422])
def test_an_enterprise_gateway_passes_the_probe(status, capsys):
    cp = FakeCP({("POST", "/v1/portal/auth/login"): (status, {"detail": "x"})})
    provisioner(cp, portal_approver=True).require_portal()
    assert "serves the User Portal API" in capsys.readouterr().out


def test_a_gateway_error_on_the_probe_is_not_taken_for_an_edition():
    cp = FakeCP({("POST", "/v1/portal/auth/login"): (503, None)})
    with pytest.raises(setup.StepError, match="HTTP 503"):
        provisioner(cp, portal_approver=True).require_portal()


def test_the_portal_user_gets_a_generated_password_and_it_is_recorded(monkeypatch):
    monkeypatch.setattr(setup, "PORTAL_UNDERWRITER_PASSWORD", "")
    created = {}

    def create(body, params):
        created.update(body)
        return {"id": "7", "username": body["username"]}

    cp = FakeCP({
        ("GET", "/v1/admin/end-users"): {"users": []},
        ("POST", "/v1/admin/end-users"): create,
        ("GET", "/v1/admin/end-user-groups"): {"groups": [{"id": "eug-1", "name": setup.UNDERWRITER_GROUP_NAME}]},
        ("POST", "/v1/admin/end-user-groups/eug-1/members"): {"members": [{"id": 7}]},
        ("GET", f"/v1/admin/resource-groups/{GID}/end-user-groups"): {"end_user_groups": [{"id": "eug-1"}]},
    })
    prov = provisioner(cp, portal_approver=True)
    prov.portal_underwriter()
    assert len(created["password"]) >= 20 and created["password"] != setup.generate_password()
    assert prov.ids["underwriter_password"] == created["password"]


def _written(monkeypatch, prov):
    out = {}
    monkeypatch.setattr(setup, "write_demo_env", lambda values: out.update(values))
    prov.ids.update({k: "x" for k in (
        "screening_api_key", "fraud_api_key", "applications_mcp_server_id", "bureau_mcp_server_id",
        "skills_mcp_server_id", "fraud_card_id", "classifier_model_name", "drafter_model_name",
        "fraud_group_id", "org_group_id")})
    prov.write_env()
    return out


def test_demo_env_carries_no_portal_credentials_by_default_and_drops_old_ones(monkeypatch):
    prov = provisioner(FakeCP({}))
    prov.env = {"PORTAL_UNDERWRITER_USER": "underwriter", "PORTAL_UNDERWRITER_PASSWORD": "old", "KEEP": "1"}
    values = _written(monkeypatch, prov)
    assert not [k for k in values if k.startswith("PORTAL_")]
    assert values["KEEP"] == "1" and values["DEMO_SYSTEM_GROUP_ID"] == GID


def test_demo_env_carries_the_portal_credentials_in_the_enterprise_variant(monkeypatch):
    monkeypatch.setattr(setup, "PORTAL_UNDERWRITER_PASSWORD", "")
    prov = provisioner(FakeCP({}), portal_approver=True)
    prov.ids["underwriter_password"] = "generated-now"
    values = _written(monkeypatch, prov)
    assert values["PORTAL_UNDERWRITER_USER"] == setup.PORTAL_UNDERWRITER_USER
    assert values["PORTAL_UNDERWRITER_PASSWORD"] == "generated-now"


# --------------------------------------------------------------------------- #
# approve.py: the administrator's decision through the Control Plane
# --------------------------------------------------------------------------- #
def _row(i, **extra):
    return {"id": f"apr-{i}", "capability_name": "applications_set_recommendation", "requester_type": "api_key",
            "requester_display": "brutor-demo-screening-agent", "requester_group_id": GID, "status": "pending",
            "expires_at": "2026-11-12T10:00:00+00:00", **extra}


def _resolved(status):
    return lambda body, params: {"id": "x", "status": status, "resolved_by": "admin",
                                 "resolved_at": "2026-11-11T10:00:00+00:00", "approval_token": "tok",
                                 "message": "ok"}


def _approve_cp(rows, extra=None):
    seen = {}

    def listed(body, params):
        seen.update(params)
        page = params["page"]
        chunk = rows[(page - 1) * params["page_size"]: page * params["page_size"]]
        return {"items": chunk, "total": len(rows), "page": page, "page_size": params["page_size"]}

    routes = {("GET", APPROVALS): listed}
    for row in rows:
        routes[("GET", f"{APPROVALS}/{row['id']}")] = {"approval": {**row, "call_arguments": {
            "application_id": "APP-20261111-0007", "recommendation": "decline", "amount_eur": 31000}}}
        routes[("POST", f"{APPROVALS}/{row['id']}/approve")] = _resolved("approved")
        routes[("POST", f"{APPROVALS}/{row['id']}/reject")] = _resolved("rejected")
    routes.update(extra or {})
    cp = FakeCP(routes)
    cp.seen = seen
    return cp


def _run(argv, cp, env=None):
    lines = []
    code = approve.run(approve.parse(argv), cp, env if env is not None else {"DEMO_SYSTEM_GROUP_ID": GID},
                       out=lines.append)
    return code, "\n".join(lines)


def test_listing_asks_for_the_demo_systems_pending_holds_and_decides_nothing():
    cp = _approve_cp([_row(2), _row(1)])  # the endpoint lists newest first
    code, out = _run([], cp)
    assert code == 0 and cp.writes() == []
    assert cp.seen == {"status_filter": "pending", "group_id": GID, "page": 1, "page_size": 200}
    assert out.index("apr-1") < out.index("apr-2"), "oldest first"
    assert "application_id=APP-20261111-0007, recommendation=decline, amount_eur=31000" in out
    assert "Mission Control -> Approvals" in out


def test_all_approves_every_pending_hold_with_a_note():
    cp = _approve_cp([_row(2), _row(1)])
    code, out = _run(["--all"], cp)
    assert code == 0
    assert cp.writes() == [("POST", f"{APPROVALS}/apr-1/approve", {"note": approve.DEFAULT_NOTE}),
                           ("POST", f"{APPROVALS}/apr-2/approve", {"note": approve.DEFAULT_NOTE})]
    assert "2 decision(s) recorded" in out and "next tick" in out


def test_named_ids_are_approved_without_listing():
    cp = _approve_cp([_row(1), _row(2)])
    code, _ = _run(["apr-2", "--note", "checked by hand"], cp)
    assert code == 0
    assert cp.calls == [("POST", f"{APPROVALS}/apr-2/approve", {"note": "checked by hand"})]


def test_reject_posts_to_the_reject_route():
    cp = _approve_cp([_row(1)])
    code, out = _run(["--reject", "apr-1"], cp)
    assert code == 0 and cp.writes() == [("POST", f"{APPROVALS}/apr-1/reject", {"note": approve.DEFAULT_NOTE})]
    assert "rejected by admin" in out


@pytest.mark.parametrize("status,words", [
    (404, "no such approval"), (409, "already decided"), (410, "expired"), (503, "the Control Plane could not reach the gateway")])
def test_a_refused_decision_is_reported_and_fails_the_run(status, words):
    cp = _approve_cp([_row(1), _row(2)], {
        ("POST", f"{APPROVALS}/apr-1/approve"): (status, {"detail": "said the Control Plane"})})
    code, out = _run(["--all"], cp)
    assert code == 1
    assert f"✗ approve apr-1: HTTP {status}: {words}" in out and "said the Control Plane" in out
    assert "✓ approve apr-2" in out, "one refusal does not stop the others"
    assert "decision(s) recorded" not in out


def test_any_other_error_is_not_swallowed():
    cp = _approve_cp([_row(1)], {("POST", f"{APPROVALS}/apr-1/approve"): (500, {"detail": "boom"})})
    with pytest.raises(setup.StepError, match="HTTP 500"):
        _run(["--all"], cp)


def test_an_answer_that_is_not_the_decision_asked_for_is_a_failure():
    cp = _approve_cp([_row(1)], {("POST", f"{APPROVALS}/apr-1/approve"): _resolved("pending")})
    code, out = _run(["apr-1"], cp)
    assert code == 1 and "not 'approved'" in out


def test_nothing_pending_is_fine_without_wait_and_exit_3_with_it(monkeypatch):
    assert _run(["--all"], _approve_cp([]))[0] == 0
    assert _run([], _approve_cp([]))[0] == 0
    waited = []
    monkeypatch.setattr(approve, "wait_for_pending", lambda cp, gid, seconds: waited.append(seconds) or [])
    code, out = _run(["--all", "--wait", "30"], _approve_cp([]))
    assert code == 3 and waited == [30] and "no held decision is pending" in out


def test_wait_returns_as_soon_as_a_hold_appears():
    polls = []
    rows = [[], [], [_row(1)]]

    def listed(body, params):
        polls.append(1)
        chunk = rows[min(len(polls), len(rows)) - 1]
        return {"items": chunk, "total": len(chunk)}

    clock = iter(range(0, 1000, 10))
    got = approve.wait_for_pending(FakeCP({("GET", APPROVALS): listed}), GID, 600,
                                   sleep=lambda s: None, clock=lambda: next(clock))
    assert [r["id"] for r in got] == ["apr-1"] and len(polls) == 3


def test_wait_gives_up_at_the_deadline():
    clock = iter(range(0, 1000, 10))
    got = approve.wait_for_pending(FakeCP({("GET", APPROVALS): {"items": [], "total": 0}}), GID, 25,
                                   sleep=lambda s: None, clock=lambda: next(clock))
    assert got == []


def test_more_than_one_page_of_holds_is_read_to_the_end():
    rows = [_row(i) for i in range(450, 0, -1)]
    cp = _approve_cp(rows)
    assert [r["id"] for r in approve.pending(cp, GID)] == [f"apr-{i}" for i in range(1, 451)]
    assert len([c for c in cp.calls if c[1] == APPROVALS]) == 3


def test_the_group_is_found_by_name_when_demo_env_is_absent():
    cp = _approve_cp([], {("GET", "/v1/admin/resource-groups"): {"groups": [
        {"id": "rg-other", "name": "x"}, {"id": "rg-found", "name": setup.DEMO_SYSTEM_NAME}]}})
    _run([], cp, env={})
    assert cp.seen["group_id"] == "rg-found"


def test_an_unprovisioned_tenant_is_named():
    cp = _approve_cp([], {("GET", "/v1/admin/resource-groups"): {"groups": []}})
    with pytest.raises(setup.StepError, match="demo.sh provision"):
        _run([], cp, env={})


@pytest.mark.parametrize("argv", [["--all", "apr-1"], ["--reject", "apr-1", "--all"], ["--wait", "-1"]])
def test_contradictory_arguments_are_refused(argv):
    with pytest.raises(SystemExit):
        approve.parse(argv)


def test_verify_points_at_the_admin_console_for_pending_holds():
    assert "Mission Control -> Approvals" in (HERE / "verify.py").read_text()
    assert verify.admin_login is setup.admin_login


# --------------------------------------------------------------------------- #
# The Community API has every control-plane route the scripts call
# --------------------------------------------------------------------------- #
# The Control Plane's recorded Community OpenAPI document: the one the checkout
# beside this repository has COMMITTED (`git show HEAD:<path>`), so that an
# edit next door that is not committed can turn this repository neither red
# nor green. The file on disk is read when the sibling is not a git checkout
# (an unpacked source archive) or when BRUTOR_SIBLINGS=worktree asks for it (a
# change made in both repositories at once, before either is committed).
# BRUTOR_CONTROL_OPENAPI names a file to read instead (another commit of the
# Control Plane).
CONTROL_PLANE = HERE.parents[1] / "brutor-gateway-control"
SNAPSHOT_PATH = "tests/fixtures/openapi_snapshot_community.json"


def control_plane_document(root=CONTROL_PLANE, environ=None):
    """(text, where it was read from) of the recorded Community OpenAPI
    document; text is None when there is nothing to read."""
    environ = os.environ if environ is None else environ
    named = environ.get("BRUTOR_CONTROL_OPENAPI")
    if named:
        file = Path(named)
        return (file.read_text() if file.is_file() else None), str(file)
    root = Path(root)
    file = root / SNAPSHOT_PATH
    if not root.is_dir():
        return None, str(file)
    worktree = environ.get("BRUTOR_SIBLINGS", "").strip().lower() == "worktree"
    if (root / ".git").exists() and shutil.which("git") and not worktree:
        shown = subprocess.run(["git", "-C", str(root), "show", f"HEAD:{SNAPSHOT_PATH}"],
                               capture_output=True, text=True, timeout=30)
        return (shown.stdout if shown.returncode == 0 else None), f"{root.name} at HEAD, {SNAPSHOT_PATH}"
    return (file.read_text() if file.is_file() else None), str(file)


SNAPSHOT_TEXT, SNAPSHOT = control_plane_document()
ROUTE = re.compile(r"""["'](/v1/(?:[A-Za-z0-9_\-/.]|\{[^}]*\})*)""")


def called_routes(script):
    """Every "/v1/..." literal of a script, with f-string fields as one segment."""
    found = set()
    for line in (HERE / script).read_text().splitlines():
        for match in ROUTE.finditer(line):
            found.add(re.sub(r"\{[^}]*\}", "{}", match.group(1)).rstrip("/"))
    return found


def routes_not_served(script, paths):
    """The Control Plane routes `script` calls that the OpenAPI `paths` lack."""
    patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", p).rstrip("/") + "$") for p in paths]
    # The one route that is not the Control Plane's: the gateway's User Portal
    # sign-in, probed by the Enterprise variant only.
    gateway_only = {setup.PORTAL_LOGIN_PATH}
    return sorted(r for r in called_routes(script) - gateway_only
                  if not any(p.match(r.replace("{}", "x")) for p in patterns))


def test_the_route_scan_sees_the_routes():
    routes = called_routes("setup.py") | called_routes("approve.py") | called_routes("verify.py")
    assert "/v1/admin/users/tenant/login" in routes and "/v1/admin/ai-systems/{}/contracts" in routes
    assert len(routes) > 60


def test_a_route_the_document_lacks_is_reported_by_name():
    """The check names exactly the routes a document does not have."""
    everything = called_routes("setup.py") | called_routes("approve.py") | called_routes("verify.py")
    served = [r.replace("{}", "{id}") for r in everything]
    lacking = {"/v1/admin/approvals", "/v1/admin/users/tenant/login"}
    without = [p for p in served if p not in lacking]
    assert routes_not_served("setup.py", without) == sorted(lacking)
    assert routes_not_served("verify.py", without) == ["/v1/admin/approvals"]
    # approve.py takes the approvals path from setup.py, so it has no literal of its own.
    assert setup.TOOL_APPROVALS_PATH == "/v1/admin/approvals" and routes_not_served("approve.py", without) == []
    assert routes_not_served("setup.py", served) == []


@pytest.mark.skipif(SNAPSHOT_TEXT is None, reason=f"no Control Plane OpenAPI document ({SNAPSHOT}): "
                    "check out brutor-gateway-control beside this repository or set BRUTOR_CONTROL_OPENAPI")
@pytest.mark.parametrize("script", ["setup.py", "verify.py", "approve.py"])
def test_every_control_plane_route_is_in_the_community_api(script):
    missing = routes_not_served(script, json.loads(SNAPSHOT_TEXT)["paths"])
    assert missing == [], (
        f"{script} calls routes this Control Plane does not serve: {missing}. The document read is {SNAPSHOT}.")


def _sibling(tmp_path, checkout):
    """A Control Plane beside this repository whose working tree holds one
    document; when it is a checkout, `git show` answers with another."""
    root = tmp_path / "brutor-gateway-control"
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / SNAPSHOT_PATH).write_text('{"paths": {"/v1/admin/edited-and-not-committed": {}}}')
    if checkout:
        (root / ".git").mkdir()
    return root


def _git_shows(monkeypatch, text, returncode=0):
    asked = []

    def run(command, **kwargs):
        asked.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout=text, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return asked


def test_the_document_read_is_the_one_the_control_plane_committed(tmp_path, monkeypatch):
    root = _sibling(tmp_path, checkout=True)
    asked = _git_shows(monkeypatch, '{"paths": {"/v1/admin/committed": {}}}')
    text, where = control_plane_document(root, environ={})
    assert json.loads(text)["paths"] == {"/v1/admin/committed": {}}
    assert asked == [["git", "-C", str(root), "show", f"HEAD:{SNAPSHOT_PATH}"]]
    assert "at HEAD" in where


def test_the_working_tree_is_read_only_on_request_or_without_a_checkout(tmp_path, monkeypatch):
    asked = _git_shows(monkeypatch, '{"paths": {"/v1/admin/committed": {}}}')
    edited = {"/v1/admin/edited-and-not-committed": {}}
    text, where = control_plane_document(_sibling(tmp_path / "a", checkout=True), environ={"BRUTOR_SIBLINGS": "worktree"})
    assert json.loads(text)["paths"] == edited and where.endswith(SNAPSHOT_PATH)
    text, _ = control_plane_document(_sibling(tmp_path / "b", checkout=False), environ={})
    assert json.loads(text)["paths"] == edited
    assert asked == []


def test_a_named_document_wins_and_an_absent_one_is_skipped(tmp_path, monkeypatch):
    asked = _git_shows(monkeypatch, "{}", returncode=128)
    named = tmp_path / "another-commit.json"
    named.write_text('{"paths": {}}')
    root = _sibling(tmp_path, checkout=True)
    assert control_plane_document(root, environ={"BRUTOR_CONTROL_OPENAPI": str(named)}) == ('{"paths": {}}', str(named))
    assert asked == []
    # Not there: no sibling at all, a named file that does not exist, a checkout
    # whose HEAD has no such file.
    assert control_plane_document(tmp_path / "nowhere", environ={})[0] is None
    assert control_plane_document(root, environ={"BRUTOR_CONTROL_OPENAPI": str(tmp_path / "missing.json")})[0] is None
    assert control_plane_document(root, environ={})[0] is None


@pytest.mark.skipif(not (CONTROL_PLANE / ".git").exists() or bool(os.environ.get("BRUTOR_CONTROL_OPENAPI"))
                    or bool(os.environ.get("BRUTOR_SIBLINGS")),
                    reason="no Control Plane checkout beside this repository, or another document was asked for")
def test_the_checkout_beside_this_repository_is_read_at_head():
    committed = subprocess.run(["git", "-C", str(CONTROL_PLANE), "show", f"HEAD:{SNAPSHOT_PATH}"],
                               capture_output=True, text=True, timeout=30)
    assert committed.returncode == 0 and SNAPSHOT_TEXT == committed.stdout
