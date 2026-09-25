# Fundamental Rights Impact Assessment (Art 27)

**System:** Brutor Demo System (consumer loan pre-screening agent)
**Deployer:** Borealis Consumer Finance AB, Stockholm (fictional)
**Classification:** High-risk, Annex III point 5(b), creditworthiness evaluation of natural persons
**Assessment date:** 2026-09-23
**Prepared by:** Legal & Compliance, with Credit Risk and Data Governance
**Approved by:** Anna Berg, Head of Credit Risk
**Review:** annually, or on any change to the models, the decision rules or the affected population

> This document is part of a demonstration. Borealis Consumer Finance AB does
> not exist, no natural person is assessed, and every application the system
> processes is synthetic. The document is written the way a real FRIA would be
> so that the mechanisms around it (evidence rows, the lifecycle gate, the
> obligations board) can be shown operating on a credible artefact. It is not
> legal advice and it does not claim conformity with the EU AI Act.

## 1. Purpose of the deployment

The system pre-screens unsecured consumer loan applications between 2,000 and
60,000 EUR. It produces a recommendation (approve, refer, decline), a rationale
and a draft customer letter. It does not make the final decision. Every
decline, and every application above 25,000 EUR, is held for a human
underwriter before anything is recorded in the origination system, and every
approval is reviewed by a member of the credit team before funds are released.

## 2. Period and frequency of use

Continuous: the agent polls the origination system every ten minutes and
processes up to five new applications per tick. Expected volume in production
would be 50 to 200 applications per day.

## 3. Natural persons affected

Adults resident in Sweden, Finland, Norway, Denmark, Germany and the
Netherlands who apply for a consumer loan. Groups who may be affected
differently: recent immigrants with thin credit files, self-employed
applicants with irregular income, applicants with a mother tongue other than
the letter language, and applicants who share a name with a person on a
sanctions list.

## 4. Rights at risk and how they could be affected

| Right | Risk | How it could arise |
|---|---|---|
| Non-discrimination (Charter Art 21) | Indirect discrimination through proxies | The risk classifier reads the free-text purpose and the bureau record; language style or nationality could correlate with outcome |
| Protection of personal data (Art 8) | Excessive processing; data leaving the EU | The bureau report and the applicant's name are sent to a model provider; the provider may be outside the EU |
| Good administration and effective remedy (Arts 41, 47) | Decisions the applicant cannot understand or contest | A letter that hides the real reason, or a rationale that names a rule that did not fire |
| Human dignity (Art 1) | Automated treatment of a sensitive life event | A decline sent without a person having looked at it |

## 5. Measures in place

| Measure | Where it lives |
|---|---|
| Deterministic hard rules override the models: unverified identity forces refer, unaffordable forces decline, sanctions hit forces decline | Screening agent `rules.py`; affordability skill |
| Every decline and every loan over 25,000 EUR is held for an underwriter (argument policy, HTTP 202 `approval_required`) | Gateway argument policy "Adverse or large decisions need an underwriter" |
| The letter always states that an AI system was used, that a person reviews every decision, and that a human review can be requested | Drafter prompt; Art 50 notice config |
| The rationale names which rule fired, so the underwriter and the applicant see the same reason | Screening agent `decide` step |
| Bureau reads are sealed as evidence and every action is in the signed audit chain | Gateway evidence ledger; `sensitive_data` flag on the system |
| Prompt injection in application text is blocked before it reaches the model | Gateway guardrail on `mcp_output` |
| Cost, call and delegation ceilings per run; the system cannot loop or escalate beyond its envelope | Operating envelope; contract |
| Drift of high severity downgrades the system to `approval_required` until a human restores it | Response policy |
| Quarterly bias review on outcome distributions by country and by affordability class (synthetic data in the demo) | `data-governance.md` section 5 |

## 6. Residual risk

- Model provider location: the trial's providers are `global`. In a real
  deployment the models would be pinned to an EU endpoint and residency
  enforcement (`block_global`) turned on. The demo documents this rather than
  claiming it.
- Proxy discrimination in the classifier cannot be excluded by design alone;
  it is monitored by the bias review and mitigated by the fact that the
  classifier can only make a recommendation stricter, never more lenient
  (a `high` band forces at least `refer`).
- An underwriter who approves held decisions without reading them defeats the
  oversight measure. Training and the four-eyes rule in
  `instructions-for-use.md` address this; the gateway records who approved
  what and when.

## 7. Complaint and redress

Applicants may request a human review through the letter's contact channel at
any time. Reviews are handled outside the AI system by the credit team and are
recorded against the application in the origination system. Complaints about
the use of AI go to the Data Protection Officer.

## 8. Notification

Under Art 27(3) the deployer notifies the market surveillance authority of the
results of this assessment. In the demo this step is represented by the
evidence row that points at this document; no authority is notified.

## 9. Conclusion

The deployment can proceed to `active` provided the measures in section 5
remain in force, the residual risks in section 6 stay documented and reviewed,
and the lifecycle gate continues to require this assessment to be on file and
unexpired.
