# Risk Management File (Art 9)

**System:** Brutor Demo System (consumer loan pre-screening agent)
**Provider and deployer:** Borealis Consumer Finance AB (fictional)
**Classification:** High-risk, Annex III 5(b)
**Version:** 1.0, 2026-09-23
**Owner:** Anna Berg, Head of Credit Risk
**Quality management reference:** this file is the QMS entry point for the system (Art 17); procedures for change, incident and review are in sections 6 to 8

> Demonstration document. The lender, the data and the applicants are
> synthetic. It is structured as a real risk management file so the evidence
> and obligation mechanisms around it can be shown, and it does not claim
> conformity with the EU AI Act.

## 1. Scope

The system, its two models (classifier `gpt-5.2`, drafter `gpt-5.5`), the
affordability skill, the two MCP tools servers (origination, bureau) and the
delegated fraud screener, all reached through the Brutor gateway. The gateway
configuration bound to the system is captured in its contract; a change to
that configuration drifts the contract and is visible on the AI System page.

## 2. Risk identification

| # | Risk | Source | Consequence |
|---|---|---|---|
| R1 | Wrong recommendation (false decline or false approve) | Model error, bad bureau data, stale affordability policy | Applicant harmed or credit loss |
| R2 | Adverse decision recorded without human review | Software defect, misconfiguration of the approval band | Art 14 breach, applicant harm |
| R3 | Prompt injection in application free text | Malicious applicant | Model coerced into approving |
| R4 | Personal data leaves the EU or is over-collected | Provider location, over-broad tool calls | GDPR exposure |
| R5 | Runaway cost or looping behaviour | Agent bug, provider latency | Budget breach, delayed decisions |
| R6 | Silent failure: the agent stops processing | Crash, credential expiry, network | Applications age unprocessed |
| R7 | Letter contains prohibited marketing claims or omits the AI disclosure | Drafter output | Art 50 breach, consumer law |
| R8 | Fraud screener unavailable or wrong | A2A failure, mock logic | Screening step degraded |
| R9 | Configuration drift after approval | Operator change | Approved system no longer matches what runs |

## 3. Risk analysis and evaluation

Likelihood and severity on a three-point scale (L/M/H) before controls.

| # | Likelihood | Severity | Rating |
|---|---|---|---|
| R1 | M | H | High |
| R2 | L | H | High |
| R3 | M | H | High |
| R4 | M | M | Medium |
| R5 | L | M | Low |
| R6 | M | M | Medium |
| R7 | M | M | Medium |
| R8 | M | L | Low |
| R9 | M | H | High |

## 4. Controls

| # | Control | Mechanism | Evidence of operation |
|---|---|---|---|
| R1 | Deterministic rules override the models; models can only make an outcome stricter | `rules.py`; affordability skill | Run rationale names the rule; skill executions in the run |
| R1 | Human review of every decline and every approval before funds move | Argument policy band; credit team procedure | Tool Approvals; letters state review |
| R2 | The origination write is governed at the gateway, not in the agent | Argument policy on `applications_set_recommendation` (schema guard + `field_eq` + `field_gt`) | Policy denial and approval counts per run; continuous check on policy denials |
| R3 | Prompt injection guardrail on tool output and model input, block mode | Guardrail config, `mcp_output` and `chat_input` | Guardrail block count per run; continuous check "guardrail fired" (critical) |
| R4 | Sensitive reads sealed; residency profile documented; PII detection deliberately off for the letter with justification | `sensitive_data` flag; evidence ledger; `data-governance.md` | Evidence records with `bureau_get_report`; residency section in README |
| R5 | Operating envelope: cost, calls, tokens, duration and delegation depth per run; budgets and rate limits on the group | Envelope terms; LLM/MCP/skill global limits | Contract; envelope breaches in Signals |
| R6 | Liveness expectation: at least one run per 30 minutes with a 10 minute grace | Liveness config, continuous mode | Silence alerts in the inbox |
| R7 | Banned words on model output (block); disclosure sentence enforced by the agent and declared in the notice config | Guardrail `chat_output`; prompt; notice config | Transparency summary |
| R8 | Delegation through the gateway with a signed chain; depth capped at 2; timeout 30 s | A2A governance; grants | `via_system_ids` on the run; continuous check on depth |
| R9 | Contract minted from the effective configuration; drift is visible and the response policy downgrades autonomy on high drift | Contract; response policy | AI System page: Contract tab; Responses |

## 5. Residual risk

After controls: R1 Medium (model error still possible, bounded by human
review), R2 Low, R3 Low, R4 Medium (provider location, documented), R5 Low,
R6 Low, R7 Low, R8 Low, R9 Low. The residual risk is accepted by the owner
for the demonstration deployment.

## 6. Change management

Any change to the models, the rules, the affordability policy, the guardrails,
the argument policy or the envelope is made through the Admin Console, drifts
the contract, and requires a new contract to be approved and promoted before
the system may return to `active`. The skill is versioned; a policy change is
a new published version.

## 7. Monitoring and review (Art 72)

Post-market monitoring uses the AI System health signals (completion, error,
cost, drift, liveness, guardrail and policy events), the weekly assurance
digest and the monthly EU AI Act evidence report. The risk file is reviewed
quarterly and after any serious incident.

## 8. Incident handling (Art 73)

A serious incident (for example an adverse decision recorded without review)
is opened in Compliance, Incidents, linked to the run, and the deadline
clock (15 days; 10 days for widespread infringement; 2 days for death or
serious harm) starts from the moment Borealis became aware. The README shows
how `verify.py --open-incident` demonstrates the flow on synthetic data.
