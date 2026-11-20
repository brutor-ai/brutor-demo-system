"""Runs scripts/affordability.py as a subprocess, the way the skill runner does."""

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "affordability.py")


def run(params, env_input=None):
    """Feed {"input_params": params} on stdin (or via SKILL_INPUT) and parse stdout."""
    env = dict(os.environ)
    env.pop("SKILL_INPUT", None)
    stdin = json.dumps({"input_params": params}) if env_input is None else ""
    if env_input is not None:
        env["SKILL_INPUT"] = json.dumps({"input_params": env_input})
    proc = subprocess.run(
        [sys.executable, SCRIPT],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line, got: {proc.stdout!r}"
    return json.loads(lines[0])


BASE = {
    "monthly_income_eur": 4200,
    "monthly_expenses_eur": 1900,
    "existing_debt_monthly_eur": 250,
    "requested_amount_eur": 12000,
    "term_months": 48,
}


def test_comfortable():
    out = run(BASE)
    assert "error" not in out
    assert out["affordability_class"] == "comfortable"
    assert out["policy_version"]
    # Annuity at 9.5 percent over 48 months on 12,000 EUR is about 301.50.
    assert 295 < out["monthly_installment_eur"] < 308
    assert out["dti_after"] <= 0.35
    assert out["disposable_after_eur"] >= 600
    assert out["flags"] == []


def test_tight():
    # DTI lands between 0.35 and 0.45 while disposable income stays above 300.
    params = dict(BASE, monthly_income_eur=3000, monthly_expenses_eur=1100,
                  existing_debt_monthly_eur=700, requested_amount_eur=12000, term_months=36)
    out = run(params)
    assert out["affordability_class"] == "tight"
    assert 0.35 < out["dti_after"] <= 0.45
    assert out["disposable_after_eur"] >= 300


def test_tight_by_disposable_floor():
    # Low DTI, but only a few hundred euros left: tight, not comfortable.
    params = dict(BASE, monthly_income_eur=2600, monthly_expenses_eur=1500,
                  existing_debt_monthly_eur=400, requested_amount_eur=9000, term_months=36)
    out = run(params)
    assert out["affordability_class"] == "tight"
    assert out["dti_after"] <= 0.35
    assert 300 <= out["disposable_after_eur"] < 600


def test_unaffordable_by_dti():
    params = dict(BASE, monthly_income_eur=2000, existing_debt_monthly_eur=1000,
                  requested_amount_eur=20000, term_months=36)
    out = run(params)
    assert out["affordability_class"] == "unaffordable"
    assert out["dti_after"] > 0.45
    assert "high_existing_debt" in out["flags"]


def test_unaffordable_by_disposable_floor():
    # Low DTI but almost nothing left each month: the disposable floor bites.
    params = dict(BASE, monthly_income_eur=3000, monthly_expenses_eur=2650,
                  existing_debt_monthly_eur=0, requested_amount_eur=4000, term_months=48)
    out = run(params)
    assert out["dti_after"] <= 0.35
    assert out["affordability_class"] == "unaffordable"


def test_default_rate_and_explicit_rate_differ():
    default = run(BASE)
    explicit = run(dict(BASE, annual_rate_pct=4.0))
    assert explicit["monthly_installment_eur"] < default["monthly_installment_eur"]
    assert explicit["inputs"]["annual_rate_pct"] == 4.0


def test_zero_rate_is_straight_line():
    out = run(dict(BASE, annual_rate_pct=0))
    assert out["monthly_installment_eur"] == pytest.approx(12000 / 48, abs=0.01)


def test_bad_input_is_reported_as_data():
    out = run({"monthly_income_eur": 4200})
    assert "error" in out
    assert "missing required input" in out["error"]
    assert "affordability_class" not in out


def test_non_numeric_input():
    out = run(dict(BASE, term_months="forty-eight"))
    assert "error" in out
    assert "term_months" in out["error"]


def test_out_of_range_amount():
    out = run(dict(BASE, requested_amount_eur=250000))
    assert "error" in out


def test_skill_input_env_fallback():
    out = run(None, env_input=BASE)
    assert out["affordability_class"] == "comfortable"


def test_empty_input_is_an_error_not_a_crash():
    out = run({})
    assert "error" in out
