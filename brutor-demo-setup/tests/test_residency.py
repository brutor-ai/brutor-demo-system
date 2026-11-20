"""Tenant settings are written at `/v1/admin/tenants/current`.

The Control Plane's by-id tenant route is Enterprise tenant management and
answers a tenant administrator 403. Setup runs as the tenant's administrator,
so its residency step writes "this installation's" settings. And a step that
was asked for and refused must say so, not tolerate the refusal and then
print that residency was set."""

import pytest

import setup
from test_release_identity import FakeCP, provisioner

PATH = "/v1/admin/tenants/current"


def _provisioner(cp):
    prov = provisioner(cp)
    prov.step = lambda name: None
    return prov


def test_residency_is_written_to_this_installations_settings():
    cp = FakeCP({("PATCH", PATH): lambda body: {"data_residency": body["data_residency"]}})
    _provisioner(cp).residency_profile()
    assert cp.writes() == [("PATCH", PATH, {"data_residency": {
        "allowed_regions": ["eu-west-1", "eu-central-1"], "block_global": True}})]


def test_no_tenant_is_addressed_by_id():
    source = open(setup.__file__, encoding="utf-8").read()
    assert "/v1/admin/tenants/{TENANT_ID}" not in source


def test_a_refused_residency_step_is_an_error_not_a_success(capsys):
    cp = FakeCP({})  # the Control Plane answers 404
    with pytest.raises(setup.StepError):
        _provisioner(cp).residency_profile()
    assert "tenant residency:" not in capsys.readouterr().out
