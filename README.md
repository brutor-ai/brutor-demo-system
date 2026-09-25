# Brutor Demo System

A governed AI agent that pre-screens consumer loan applications for a
fictional EU lender, **Borealis Consumer Finance AB** (Stockholm), running
through the Brutor gateway. It exists to show what AI system assurance looks
like on a real, running, high-risk agent: one skill, two MCP servers, two
models, a delegated second agent, a ten-minute schedule, and the EU AI Act
obligations that come with creditworthiness evaluation of natural persons
(Annex III point 5(b)).

Everything is synthetic. No real person, lender or bureau is involved. See
[what the demo does not claim](#what-the-demo-does-not-claim).

The design is in [DESIGN.md](DESIGN.md); every repo here implements a part of
it.

## Architecture

```
                         every 10 min tick
┌──────────────────────────────┐
│ brutor-demo-screening-agent  │  LangGraph graph, one run per application
│ (AI System: Brutor Demo      │
│  System, kind=agent)         │
└──────────────┬───────────────┘
               │ all calls carry the Brutor API key bound to the
               │ agent identity + run/turn/step headers
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ Brutor Core Proxy  http://core:8100                                │
│  /v1/proxy/llm/chat/completions      -> OpenAI gpt-5.2 / gpt-5.5   │
│  /v1/proxy/mcp/{applications id}     -> brutor-demo-applications-mcp│
│  /v1/proxy/mcp/{bureau id}           -> brutor-demo-credit-bureau-mcp│
│  /v1/proxy/mcp/system-agent-skill-server-default -> skill runner   │
│  /v1/proxy/a2a/outbound              -> brutor-demo-fraud-screener  │
│  governance: grants, capability filters, argument bands (approval),│
│  guardrails, limits, run caps, residency, evidence sealing         │
└────────────────────────────────────────────────────────────────────┘
               ▲
               │ echoes the signed x-brutor-delegation-* chain on its own LLM call
┌──────────────┴───────────────┐
│ brutor-demo-fraud-screener   │  A2A remote agent (AI System: Brutor Demo Fraud
│ -agent                       │  Screener, kind=agent, minimal risk)
└──────────────────────────────┘
```

All demo containers join the trial's external docker network `brutor-network`
and address the gateway as `http://core:8100`. The gateway addresses the demo
containers by container name.

## Components

| Repo | What it is | Container | Port |
|---|---|---|---|
| [brutor-demo-applications-mcp](brutor-demo-applications-mcp/README.md) | Mock loan origination system (FastMCP): pending applications, records, notes, recommendations; generates synthetic applications | `brutor-demo-applications-mcp` | 3014 |
| [brutor-demo-credit-bureau-mcp](brutor-demo-credit-bureau-mcp/README.md) | Mock KYC and credit reference agency (FastMCP): identity verification, credit report | `brutor-demo-credit-bureau-mcp` | 3015 |
| [brutor-demo-affordability-skill](brutor-demo-affordability-skill/README.md) | The `affordability-check` Agent Skill: deterministic policy script, published to the gateway | none (lives in the gateway) | n/a |
| [brutor-demo-fraud-screener-agent](brutor-demo-fraud-screener-agent/README.md) | Fraud and sanctions screener: A2A remote agent, its own AI System | `brutor-demo-fraud-screener-agent` | 9200 |
| [brutor-demo-screening-agent](brutor-demo-screening-agent/README.md) | The screening agent: LangGraph, eight steps, tick loop, approval handling | `brutor-demo-screening-agent` | 9201 (health) |
| [brutor-demo-setup](brutor-demo-setup/README.md) | Provisioning (`setup.py`), read-back (`verify.py`), docker compose, `demo.sh`, the compliance documents | none | n/a |

## The task, step by step

One run is one application. The graph is linear; each node is a step, and
every node that calls the gateway gets a turn id.

| # | Step | Through the gateway | Notes |
|---|---|---|---|
| 1 | `intake` | MCP `applications_get` | Loads the application |
| 2 | `verify_identity` | MCP `bureau_verify_identity` | `verified=false` forces `refer` |
| 3 | `credit_report` | MCP `bureau_get_report` | Sensitive read, sealed as evidence |
| 4 | `affordability` | Skills MCP `skills__run_script` | Deterministic policy; `unaffordable` forces `decline` |
| 5 | `classify` | LLM `gpt-5.2`, JSON | `risk_band` low, medium, high |
| 6 | `fraud_screen` | A2A outbound to the fraud screener | `hit` forces `decline`, `review` forces `refer` |
| 7 | `decide` | LLM `gpt-5.5`, JSON | Recommendation, rationale, customer letter; hard rules override the model; letter carries the AI disclosure |
| 8 | `record` | MCP `applications_set_recommendation` | Last action carries `X-Brutor-Run-End` and `X-Brutor-Run-Outcome` |

Human oversight: `applications_set_recommendation` is governed by an argument
policy. A `decline`, or an `amount_eur` above 25,000, returns HTTP 202
`approval_required`. The agent adds a note, stores the pending approval, ends
the run `escalated`, and polls on later ticks. When an underwriter approves,
the agent re-issues the identical call with the approval token in a short
`apply_approved_decision` run. Rejected or expired approvals get a note and
are dropped.

Terminal states: `completed` + `resolved` on the normal path, `completed` +
`escalated` on a hold, `errored` if a node raises, `blocked_policy` on a
guardrail or policy block. Nothing ends as `exhausted`: the graph has no loop.

## Governance configuration

| Control | Setting |
|---|---|
| Agent grants (`brutor-demo-screening-worker`) | `llm_call *` allow, rate 300/hour; `mcp_tool` allow for the four applications tools, the two bureau tools and `skills__list`, `skills__load`, `skills__run_script`; `a2a_call` allow on the fraud card with `max_delegation_depth` 2 |
| Agent grants (`brutor-demo-fraud-screener`) | `llm_call *` allow, rate 300/hour; nothing else |
| Argument policy | `applications_set_recommendation`: schema guard (`recommendation`, `amount_eur` required) then `decline` and `amount_eur > 25000` need approval |
| Resource-group tree | Org `Borealis Consumer Finance AB` binds gpt-5.2 and carries the company-wide limits and baseline guardrails; both AI Systems inherit resources from the parent (gpt-5.2 inherited, gpt-5.5 direct on the demo system). Resources compose additively and gated; limits and policies compose restrictively |
| Guardrails | Org baseline "Borealis Baseline Guardrails" (inherited): prompt injection **block** on `chat_input`, `mcp_output`, `a2a_inbound`; secrets **block** on `chat_input`, `mcp_input`. System config "Brutor Demo Screening Guardrails": banned words on `chat_output` ("guaranteed approval", "pre-approved", "no credit check"); PII detection off because the letter carries the applicant's name |
| Limits | Org: 50 USD/day, 1,000 USD/month, 120 requests/minute, concurrency 4, 2,000 MCP calls/hour. Demo system (tighter, so it wins): 15 USD/day, 300 USD/month, warning at 80 percent; 60 requests/minute; concurrency 2; 600 MCP calls/hour; 500 skill executions/day. Fraud screener: 2/40 USD |
| Operating envelope | 0.50 USD, 6 LLM calls, 60,000 tokens, depth 2, 300 s per run; 0.15 USD mean per completed task; completion rate at least 0.9; error rate at most 0.1; approval escalation rate at most 0.5 |
| Response policy | Drift at severity high or above sets autonomy to `approval_required`; a human restores it; 3600 s cooldown; no suspend |
| Continuous checks | `run.guardrail_block_count > 0` (critical), `run.max_delegation_depth > 2` (critical), `run.policy_denial_count > 0` (warning) |
| Liveness | Demo system continuous, window 1800 s, grace 600 s, at least one run; fraud screener on demand |

## EU AI Act mapping

| Article | What the demo does | Where to look |
|---|---|---|
| Art 6 + Annex III 5(b) | Declared high-risk creditworthiness system; the fraud screener declared minimal under the fraud carve-out | AI System, Compliance tab; profile |
| Art 9 risk management | `docs/risk-assessment.md` on file as `risk_assessment` evidence; response policy downgrades autonomy on drift | Evidence rows; Response policies |
| Art 10 data governance | `docs/data-governance.md`; synthetic data only; bureau reads sealed as sensitive reads | Evidence rows; Evidence Ledger |
| Art 11 + Annex IV | Technical documentation export | `GET /v1/admin/ai-systems/{gid}/documentation?framework=eu-ai-act` |
| Art 12 logging | Every action is a proxy_logs row in a signed chain; sealed action records per verdict | Runs, Audit chains, Evidence Ledger |
| Art 13 transparency to deployers | `docs/instructions-for-use.md` | Evidence + profile reference |
| Art 14 human oversight | Argument band holds every decline and every loan over 25,000 for an underwriter; operator run abort | User Portal, Inbox, Approvals (as `underwriter`); Compliance, Human Oversight tiles; `GET /compliance/oversight` |
| Art 15 accuracy/robustness | Envelope caps, guardrails, `eval_report` evidence, continuous checks | Contract; checks |
| Art 26 deployer obligations | Owner + oversight assignment declared; FRIA on file (Art 27) | Lifecycle gate requirements |
| Art 27 FRIA | `docs/fria.md` as `impact_assessment`; required by the gate for high risk | Lifecycle |
| Art 49 registration | Asset register sync | Asset Register |
| Art 50 transparency to persons | Disclosure sentence in every letter; notice config | `GET /compliance/transparency` |
| Art 72 post-market monitoring | Liveness, drift, health, weekly digest, monthly report | Mission Control; Reports |
| Art 73 serious incidents | Incident endpoint with the 15/10/2-day deadline rules; `verify.py --open-incident` shows the flow | Compliance, Incidents |

## Quick start

Prerequisites: the Brutor trial bundle running (control plane :5050, core
proxy :8100, network `brutor-network`), Docker, Python 3.10+, an OpenAI key.

```bash
cd brutor-demo-system/brutor-demo-setup
cp .env.example .env          # set OPENAI_API_KEY
python3 -m pip install -r requirements.txt
./demo.sh up
```

`demo.sh up` builds and starts the two MCP servers and the fraud agent, waits
for their `/health`, runs `setup.py` (which prints one line per provisioning
step and writes `.demo.env`), and starts the screening agent. The first tick
runs at start; the origination mock has already created three applications.
`setup.py` also creates the portal user `underwriter` / `Underwriter123!`
("Anna Berg (Underwriter)") in an end-user group bound to the demo system; she
is the one who approves held decisions in the User Portal (see below).

Then:

```bash
./demo.sh status              # container health + verify.py --brief
./demo.sh logs screening-agent
./demo.sh run-one [APP-id]    # process exactly one pending application now
./demo.sh generate 5          # add five synthetic applications
python3 brutor-demo-setup/verify.py   # full read-back with the section 9 expectations
```

## What to look at afterwards

In the Admin Console (http://localhost:3002, `admin` / `Admin123!` on tenant
`default` in the trial):

- **AI Systems, Brutor Demo System.** *Lifecycle*: stage `active`, the gate
  requirements (FRIA on file, approver named) satisfied, the history.
  *Contract*: the minted, approved and promoted configuration; `drifted` turns
  true the moment you change anything bound to the system. *Signals*:
  completion, error and cost against the envelope, drift, liveness. *Runs*:
  one row per application; open one to see the eight steps, the model calls,
  the skill call, the fraud screener at depth 1 with a verified chain, and
  the guardrail and policy counters.
- **Mission Control, Analytics, Agents.** The two agent identities, what they
  called, what was allowed and what was denied.
- **User Portal (http://localhost:3001), Inbox, Approvals, as `underwriter`.**
  Every held decision (a decline or a loan over 25,000 EUR) waits here with
  its full arguments. There is no Admin Console page for pending approvals;
  they are decided by a member of the system's resource group.
- **Compliance, EU AI Act.** The obligations board with dates, the profile,
  transparency and oversight summaries, incidents, the monthly report.
- **Compliance, Human Oversight.** The tiles: requested, decided by a human,
  lapsed.
- **Evidence Ledger.** Sealed records: every bureau read (the system is
  flagged `sensitive_data`) and every governed verdict, verifiable against
  the tenant's public key at `/.well-known/brutor-evidence-keys.json`.

## Approving a held decision

The trial's approval window is 300 seconds. If nobody decides in time, the agent
records a note, the application stays in `received`, and the next tick screens it
again and raises a fresh approval, so the queue always shows the current decision
until an underwriter acts. Rejecting an approval also leaves a note on the
application.


1. Wait for a run to end `escalated` (the log line says "held for
   underwriter, approval id ..."; `verify.py` lists pending approvals).
2. User Portal (http://localhost:3001), log in as `underwriter` /
   `Underwriter123!` (created by `setup.py`; the credentials are in
   `.demo.env` as `PORTAL_UNDERWRITER_USER` / `PORTAL_UNDERWRITER_PASSWORD`).
   Inbox, Approvals. Open the request. You see the tool
   (`applications_set_recommendation`), the full arguments (recommendation,
   amount, rationale, affordability class, fraud verdict, the letter) and
   which policy rule held it. The underwriter sees it because her end-user
   group `borealis-underwriters` is bound to the demo system's resource
   group; approvals are scoped by group membership, not by admin role. The
   Admin Console has no approvals queue; Compliance, Human Oversight shows the
   tiles (requested, decided by a human, lapsed).
3. Approve (optionally with a note) or reject.
4. On its next tick the agent polls `GET /v1/portal/approvals/{id}/poll`,
   receives the one-time approval token, and re-issues the identical write
   with `X-Approval-Token` in a new short run (`apply_approved_decision`,
   outcome `resolved`). A rejection produces a note on the application and
   the recommendation is dropped. Approvals expire; an expired hold is
   treated as rejected.

Nothing changes between what the underwriter saw and what is recorded: the
gateway matches the retried call against the approved arguments.

## EU residency

Off by default. The OpenAI and Anthropic providers in the trial are `global`,
so enabling `block_global` refuses every model call. To turn it on, point the
two models at an EU-hosted endpoint in the Admin Console first, then run
`setup.py --residency` (or set `EU_STRICT_RESIDENCY=1` in `.env`). Details in
[brutor-demo-setup/README.md](brutor-demo-setup/README.md#residency).

## Get the code

The demo system is published as one public repository:
https://github.com/brutor-ai/brutor-demo-system (the six component folders in this tree).

```
git clone https://github.com/brutor-ai/brutor-demo-system.git
cd brutor-demo-system/brutor-demo-setup
cp .env.example .env        # set OPENAI_API_KEY
./demo.sh up                # against a running trial bundle
```

## Release status

The demo is a source-built add-on. It is not part of the platform release scripts
or the trial bundle: `./demo.sh up` builds its four images locally and provisions
against whatever trial stack is running. Last verified against platform 0.10.93
on 2026-09-23.

## What the demo does not claim

- It does not claim the models run in the EU. OpenAI and Anthropic are
  `global` providers in the trial; EU residency enforcement is opt-in and
  documented.
- It does not claim the fraud screener detects real fraud; it is a mock with
  a synthetic sanctions list.
- It does not claim conformity with the EU AI Act. It shows the evidence,
  oversight and documentation mechanisms a real deployment would rely on, on
  synthetic data.
- Evidence sealing only happens when the trial was started with
  `BRUTOR_EVIDENCE_SIGNING_KEY` set (docker-start.sh mints it). `setup.py`
  warns when it is absent.
- The screening agent has no loop, so a run never ends `exhausted`; the demo
  says so rather than faking it.

## License

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). You may copy, change
and build on the demo, including commercially. The Brutor name and logo are trademarks
of Brutor AI Ltd. and are not covered by the licence. The Brutor platform the demo
runs against is licensed separately.
