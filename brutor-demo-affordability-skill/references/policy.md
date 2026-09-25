# Borealis Consumer Finance AB: Affordability Policy

Policy version: 2026.09
Owner: Head of Credit Risk (Anna Berg)
Applies to: unsecured consumer loans, 2,000 to 60,000 EUR, 12 to 84 months
Implemented by: `scripts/affordability.py` in the `affordability-check` skill

This document is part of a demonstration. Borealis Consumer Finance AB is a
fictional lender and every figure below is illustrative. The thresholds were
chosen to be plausible for a Nordic consumer lender, not copied from any real
institution's credit policy.

## 1. Purpose

The affordability check answers one question before any risk model is
consulted: can the applicant carry the new installment on top of what they
already pay, and still have enough left to live on? It is deliberately
arithmetic. A model may be wrong in ways nobody can explain; a ratio cannot.

## 2. Inputs

All amounts are monthly and net, in EUR, as declared by the applicant and, in
production, verified against payslips or open-banking data.

- Net monthly income
- Regular monthly living expenses (housing, utilities, food, transport,
  insurance, dependants)
- Monthly service on existing debt (all other loans and credit lines)
- Requested principal and term
- Nominal annual interest rate (default 9.5 percent if not priced yet)

## 3. Calculation

1. **Installment.** The monthly payment is the fixed-rate annuity:

   `installment = P * r * (1 + r)^n / ((1 + r)^n - 1)`

   where `P` is the principal, `r` the monthly rate (annual rate / 12) and
   `n` the term in months. At zero interest the installment is `P / n`.

2. **Debt-to-income before.** `existing_debt_monthly / income`.

3. **Debt-to-income after.** `(existing_debt_monthly + installment) / income`.

4. **Disposable income after.**
   `income - expenses - existing_debt_monthly - installment`.

## 4. Classification

| Class | Condition |
|---|---|
| `comfortable` | DTI after at most 0.35 **and** disposable income after at least 600 EUR |
| `tight` | DTI after at most 0.45 **and** disposable income after at least 300 EUR |
| `unaffordable` | Anything else |

The two conditions of a class must both hold. An applicant with a low DTI but
only 200 EUR left each month is `unaffordable`, because the disposable floor
exists to absorb ordinary shocks (a car repair, a dental bill) without a missed
payment.

## 5. Informational flags

Flags do not change the class. They are recorded so an underwriter sees why a
`tight` result is tight.

| Flag | Raised when |
|---|---|
| `high_existing_debt` | DTI before the new loan already exceeds 0.45 |
| `negative_disposable_income` | Disposable income after the loan is below zero |
| `amount_exceeds_annual_income` | Principal is more than twelve months of net income |
| `long_term` | Term is longer than 84 months |
| `expenses_exceed_income` | Declared expenses are at or above income before any debt |

## 6. How the screening agent uses the result

- `unaffordable` is a hard decline. The drafter model may not recommend
  approval, and the deterministic rules in the agent enforce that.
- `tight` does not force an outcome but must be mentioned in the rationale.
- `comfortable` is a precondition for approval, not a reason for it; the
  credit report, identity verification and fraud screening still apply.

## 7. Change control

Thresholds are changed by publishing a new version of the skill through the
Brutor Admin Console. The script and this document carry the same
`policy_version`; a change to one without the other fails review. The
gateway records which skill version each run executed, so a decision can
always be traced to the policy that produced it.
