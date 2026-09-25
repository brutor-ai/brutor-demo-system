# Instructions for Use (Art 13) and Staff Training (Art 4)

**System:** Brutor Demo System (consumer loan pre-screening agent)
**Provider:** Borealis Consumer Finance AB (fictional)
**For:** the deployer's credit team, underwriters on duty, and the platform operators
**Version:** 1.0, 2026-09-23

> Demonstration document. It is written for a credit team that does not
> exist, about a system that only ever sees synthetic applications. Its
> purpose is to show what the Art 13 instructions and the Art 4 literacy
> material look like when they are referenced from the AI System's compliance
> profile and evidence rows.

## 1. What the system does

Every ten minutes the system picks up new consumer loan applications and, for
each one, verifies the applicant, pulls a credit report, runs the Borealis
affordability policy, classifies risk, asks the fraud and sanctions screener
for a verdict, drafts a recommendation with a rationale and a customer letter,
and records the recommendation in the origination system.

The system **recommends**. It does not decide. A person reviews every decision
before it is final.

## 2. What it must not be used for

- Deciding an application on its own. A recommendation that has not been
  reviewed by a member of the credit team is not a decision.
- Applications outside 2,000 to 60,000 EUR or outside 12 to 84 months.
- Business loans, secured loans or applicants under 18.
- Anything other than the six supported countries (SE, FI, NO, DK, DE, NL).

## 3. How to read a recommendation

| Field | Meaning |
|---|---|
| `recommendation` | `approve`, `refer` or `decline` |
| `rationale` | Why. If a hard rule fired it is named here (identity unverified, unaffordable, sanctions hit, fraud review, high risk band) |
| `risk_band` | The classifier's view: low, medium, high |
| `affordability_class` | comfortable, tight, unaffordable (deterministic) |
| `fraud_verdict` | clear, review, hit (from the fraud screener) |
| `customer_letter` | Draft letter; always contains the AI disclosure sentence |

A hard rule always wins over the models. If the rationale says a rule fired,
the models did not decide the outcome.

## 4. Held decisions: what an underwriter does

Every `decline` and every loan above 25,000 EUR is held. The agent adds a note
to the application ("held for underwriter, approval id ...") and the request
appears in the Admin Console under Governance, Tool Approvals.

1. Open the approval. Read the full arguments: recommendation, amount,
   rationale, affordability class, fraud verdict and the letter.
2. Open the run (AI Systems, Brutor Demo System, Runs) if you need the bureau
   report the agent saw; the sealed record in the Evidence Ledger proves which
   report that was.
3. **Approve** if you agree with the recommendation. The agent re-issues the
   identical write with the approval token on its next tick; nothing changes
   between what you saw and what is recorded.
4. **Reject** if you disagree. The agent records a note and drops the
   recommendation; process the application manually in the origination system.
5. Never approve on the summary line alone. The four-eyes rule applies to any
   decline above 40,000 EUR: a second underwriter reads it before approval.

Approvals expire. An expired hold is treated as rejected and gets a note.

## 5. Human oversight controls available to you

| Control | Where | Effect |
|---|---|---|
| Approve or reject a held decision | Tool Approvals | The write happens or does not |
| Abort a running run | AI Systems, Runs, Abort | The run ends `cancelled`; nothing is recorded |
| Set the system to `approval_required` | AI Systems, Lifecycle / autonomy | Every governed action needs approval |
| Suspend the system | AI Systems, Lifecycle | No runs until restored |
| Ask for a human review | Any applicant, through the letter's contact channel | Handled outside the AI system |

## 6. Known limitations

- The models are probabilistic. Two identical applications may receive
  different `risk_band` values within the same band boundary. Hard rules and
  the affordability policy are not probabilistic.
- The fraud screener uses a synthetic sanctions list; a name match is not
  evidence of anything about a person.
- The system has no memory between runs and no conversation; it cannot be
  asked questions.
- Model inference in the trial runs on `global` providers. Do not treat the
  demo as EU-resident processing.
- The system stops (liveness alert after 30 minutes of silence) if its API
  key is revoked, the gateway is down, or its budget is exhausted. It does
  not retry silently; the alert is the signal.

## 7. Accuracy and performance

See `evaluation-report.md`. On synthetic data every hard rule fired when its
condition held, every held decision was held, every letter carried the
disclosure, and no prompt injection reached a model. None of this is a claim
about real applicants.

## 8. Logs and records (Art 12)

Every call the system makes is a row in the gateway's signed audit chain,
attributed to the system, its agent identity, the run, the turn and the step.
Bureau reads and every governed verdict are sealed as evidence records. The
records are retained for at least 183 days once the EU AI Act framework is
enabled on the tenant. Underwriter approvals and rejections are recorded with
who, when and the note.

## 9. Incidents

If you believe an adverse decision was recorded without review, a letter went
out with a false statement, or the system behaved outside its envelope, open
an incident in Compliance, Incidents, link the run, and tell the Head of
Credit Risk the same day. The deadline clock starts when Borealis becomes
aware, not when the incident is opened.

## Staff training

This section is the AI literacy material referenced from the compliance
profile (`ai_literacy_reference`). Every person who reviews recommendations
completes it before their first approval and again annually.

### Who must complete it

Underwriters on duty, credit team members who release funds, the platform
operators who change the system's configuration, and the compliance staff who
read its evidence.

### What it covers

1. **What an AI recommendation is.** A model's output is a prediction with
   uncertainty, not a finding of fact. The hard rules and the affordability
   policy are not predictions.
2. **Automation bias.** People tend to accept a confident-looking
   recommendation. The four-eyes rule and the requirement to read the full
   arguments exist because of this. Exercise: five held decisions, two of
   which contain a rationale that names the wrong rule; trainees must find
   them.
3. **What the system cannot see.** It does not see the applicant's
   explanation, their history with Borealis, or anything after the
   application date. A `refer` is an invitation to look, not a verdict.
4. **Reading a run.** How to open a run, follow the eight steps, find the
   bureau report that was sealed, see the fraud screener's verdict at depth 1,
   and check the guardrail and policy counters.
5. **When to stop the system.** Cost or error rate climbing, letters with
   unexpected content, a liveness alert, a drift finding, or a colleague's
   doubt. Stopping is cheap; an unreviewed decline is not.
6. **Your rights and the applicant's.** The disclosure sentence, the right to
   request a human review, and how to log a complaint.
7. **Incidents.** What counts as serious, who to tell, and why the clock
   matters.

### Assessment

A short case-based test: ten held decisions with full arguments; the trainee
approves, rejects or escalates each and writes one line of reasoning. Pass
mark is nine of ten with no unreviewed approval. Results are kept by Credit
Risk for the annual audit.
