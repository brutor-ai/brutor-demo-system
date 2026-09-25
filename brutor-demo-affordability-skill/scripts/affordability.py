#!/usr/bin/env python3
"""affordability.py: the deterministic affordability policy for Borealis Consumer Finance AB.

Runs inside the Brutor skill runner (sandbox mode). The runner writes
``{"input_params": {...}}`` to stdin; for local runs the same JSON may be
passed in the ``SKILL_INPUT`` environment variable instead. The script prints
exactly one JSON object to stdout and always exits 0: a validation problem is
reported as ``{"error": "..."}`` so the calling agent can read it as data
rather than as a runner failure.

Python standard library only. The thresholds are documented in
``references/policy.md`` and must be changed there and here together.
"""

import json
import os
import sys

POLICY_VERSION = "2026.09"

# Policy thresholds (see references/policy.md).
DEFAULT_ANNUAL_RATE_PCT = 9.5
COMFORTABLE_MAX_DTI = 0.35
COMFORTABLE_MIN_DISPOSABLE_EUR = 600.0
TIGHT_MAX_DTI = 0.45
TIGHT_MIN_DISPOSABLE_EUR = 300.0

# Sanity bounds that mirror the origination system's own limits.
MIN_AMOUNT_EUR = 500.0
MAX_AMOUNT_EUR = 100000.0
MIN_TERM_MONTHS = 6
MAX_TERM_MONTHS = 120

REQUIRED = (
    "monthly_income_eur",
    "monthly_expenses_eur",
    "existing_debt_monthly_eur",
    "requested_amount_eur",
    "term_months",
)


def _read_input():
    """Return the input_params dict from stdin, falling back to SKILL_INPUT."""
    raw = ""
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
        except (OSError, ValueError):
            raw = ""
    if not raw.strip():
        raw = os.environ.get("SKILL_INPUT", "")
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    if isinstance(payload, dict) and "input_params" in payload:
        params = payload.get("input_params") or {}
    else:
        params = payload
    return params if isinstance(params, dict) else {}


def _number(params, key, required=True, default=None):
    value = params.get(key, default)
    if value is None:
        if required:
            raise ValueError(f"missing required input '{key}'")
        return default
    if isinstance(value, bool):
        raise ValueError(f"input '{key}' must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"input '{key}' must be a number, got {value!r}")


def annuity_installment(principal, annual_rate_pct, term_months):
    """Monthly payment for a fixed-rate annuity loan."""
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    monthly_rate = (annual_rate_pct / 100.0) / 12.0
    if monthly_rate == 0:
        return principal / term_months
    factor = (1.0 + monthly_rate) ** term_months
    return principal * monthly_rate * factor / (factor - 1.0)


def assess(params):
    """Apply the policy. Returns the result dict (or raises ValueError)."""
    missing = [k for k in REQUIRED if k not in params]
    if missing:
        raise ValueError("missing required input(s): " + ", ".join(missing))

    income = _number(params, "monthly_income_eur")
    expenses = _number(params, "monthly_expenses_eur")
    existing_debt = _number(params, "existing_debt_monthly_eur")
    amount = _number(params, "requested_amount_eur")
    term = _number(params, "term_months")
    rate = _number(params, "annual_rate_pct", required=False, default=DEFAULT_ANNUAL_RATE_PCT)

    if income <= 0:
        raise ValueError("monthly_income_eur must be greater than zero")
    for key, value in (("monthly_expenses_eur", expenses), ("existing_debt_monthly_eur", existing_debt)):
        if value < 0:
            raise ValueError(f"{key} must not be negative")
    if not (MIN_AMOUNT_EUR <= amount <= MAX_AMOUNT_EUR):
        raise ValueError(
            f"requested_amount_eur must be between {MIN_AMOUNT_EUR:.0f} and {MAX_AMOUNT_EUR:.0f}"
        )
    if term != int(term) or not (MIN_TERM_MONTHS <= term <= MAX_TERM_MONTHS):
        raise ValueError(f"term_months must be a whole number between {MIN_TERM_MONTHS} and {MAX_TERM_MONTHS}")
    if not (0 <= rate <= 60):
        raise ValueError("annual_rate_pct must be between 0 and 60")
    term = int(term)

    installment = annuity_installment(amount, rate, term)
    dti_before = existing_debt / income
    dti_after = (existing_debt + installment) / income
    disposable_after = income - expenses - existing_debt - installment

    flags = []
    if dti_before > TIGHT_MAX_DTI:
        flags.append("high_existing_debt")
    if disposable_after < 0:
        flags.append("negative_disposable_income")
    if amount > income * 12:
        flags.append("amount_exceeds_annual_income")
    if term > 84:
        flags.append("long_term")
    if income - expenses <= 0:
        flags.append("expenses_exceed_income")

    if dti_after <= COMFORTABLE_MAX_DTI and disposable_after >= COMFORTABLE_MIN_DISPOSABLE_EUR:
        affordability_class = "comfortable"
    elif dti_after <= TIGHT_MAX_DTI and disposable_after >= TIGHT_MIN_DISPOSABLE_EUR:
        affordability_class = "tight"
    else:
        affordability_class = "unaffordable"

    return {
        "monthly_installment_eur": round(installment, 2),
        "dti_before": round(dti_before, 4),
        "dti_after": round(dti_after, 4),
        "disposable_after_eur": round(disposable_after, 2),
        "affordability_class": affordability_class,
        "flags": flags,
        "policy_version": POLICY_VERSION,
        "inputs": {
            "monthly_income_eur": income,
            "monthly_expenses_eur": expenses,
            "existing_debt_monthly_eur": existing_debt,
            "requested_amount_eur": amount,
            "term_months": term,
            "annual_rate_pct": rate,
        },
    }


def main():
    try:
        params = _read_input()
        result = assess(params)
    except (ValueError, json.JSONDecodeError) as exc:
        result = {"error": str(exc), "policy_version": POLICY_VERSION}
    sys.stdout.write(json.dumps(result, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
