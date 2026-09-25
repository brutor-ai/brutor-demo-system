---
name: affordability-check
description: Deterministic affordability assessment for a consumer loan application. Computes the annuity installment, debt-to-income ratios and disposable income, and classifies the application as comfortable, tight or unaffordable under the Borealis affordability policy.
version: 1.0.0
tags: [credit, affordability, policy, deterministic, borealis-demo]
---

# Affordability Check

Apply the Borealis Consumer Finance AB affordability policy to one loan
application. The policy is a fixed set of arithmetic rules, not a model, so the
same inputs always produce the same answer. Use this skill before any
risk classification: an `unaffordable` result is a hard decline under the
Borealis screening rules, whatever a model says afterwards.

## When to use

- A loan application has been loaded and the applicant's monthly income,
  expenses and existing debt service are known.
- The requested amount and term are known.

## Inputs

Pass these as the `args` of `skills__run_script`. The runner delivers them to
the script as `{"input_params": {...}}` on stdin.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `monthly_income_eur` | number | yes | Net monthly income |
| `monthly_expenses_eur` | number | yes | Regular monthly living costs |
| `existing_debt_monthly_eur` | number | yes | Monthly service on existing debt |
| `requested_amount_eur` | number | yes | Loan principal requested |
| `term_months` | integer | yes | Repayment term in months |
| `annual_rate_pct` | number | no | Nominal annual rate; default 9.5 |

## Output

One JSON object:

| Field | Meaning |
|---|---|
| `monthly_installment_eur` | Annuity installment for the requested loan |
| `dti_before` | Existing debt service divided by income |
| `dti_after` | (Existing debt service + new installment) divided by income |
| `disposable_after_eur` | Income minus expenses, existing debt service and the new installment |
| `affordability_class` | `comfortable`, `tight` or `unaffordable` |
| `flags` | Informational flags such as `high_existing_debt` or `long_term` |
| `policy_version` | Version of the policy applied |

If the inputs are invalid the script still exits 0 and returns
`{"error": "<reason>", "policy_version": "..."}`. Treat an `error` as
"could not assess", never as "affordable".

## Workflow

1. Run [affordability.py](scripts/affordability.py) with the inputs above.
2. Read `affordability_class` from its output and carry it into the decision.
3. Consult [policy.md](references/policy.md) if you need to explain a result
   to an underwriter; it states the thresholds the script implements.

## Rules the script applies

- Installment: fixed-rate annuity formula over `term_months`.
- `comfortable`: `dti_after` at most 0.35 and disposable income at least 600 EUR.
- `tight`: `dti_after` at most 0.45 and disposable income at least 300 EUR.
- Otherwise `unaffordable`.

Do not override the class. If the result looks wrong, say so in the
rationale and recommend `refer` so a human underwriter looks at it.
