# Brutor Demo System

A governed AI agent that pre-screens consumer loan applications for a
fictional EU lender, **Borealis Consumer Finance AB** (Stockholm), running
through the Core Gateway. It exists to show what AI system assurance looks
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
               │ all calls carry the agent's JWT-SVID (workload
               │ identity from SPIRE) + run/turn/step headers
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ Brutor Core Gateway  http://core:8100                              │
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

All demo containers join the external docker network of the running Brutor
Agent Control stack (`brutor-network`, or the name in `BRUTOR_NETWORK`) and
address the gateway as `http://core:8100`. The gateway addresses the demo
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
| [brutor-demo-setup/spire](brutor-demo-setup/spire/README.md) | SPIRE server, agent and OIDC Discovery Provider: the agents' workload identity | `brutor-demo-spire-server`, `-agent`, `-oidc` | 9208 (OIDC, loopback) |

## Workload identity

Neither agent holds an API key at run time. Each fetches a **JWT-SVID** from the
demo's SPIRE Agent, which issues it only to the attested container (docker workload
attestor: the container's `io.brutor.demo.workload` label), with audience `brutor`
and a five-minute lifetime, and presents it as its Bearer credential. `setup.py`
registers the demo's SPIRE as the tenant's identity provider and gives each agent
identity its SPIFFE ID as subject; the gateway verifies the token against SPIRE's
published keys and resolves the identity by that subject. Details, the join-token
bootstrap and the fallback without the Docker socket:
[brutor-demo-setup/spire/README.md](brutor-demo-setup/spire/README.md).

Each identity also has an API key from the development stage (in `.demo.env`;
`BRUTOR_CREDENTIAL=api_key` makes the agents use it). `./demo.sh copied-key` makes
the agents' calls with those keys from your machine, as someone who copied them
would: an AI System at stage `active` requires workload identity, so the gateway
must refuse each one (HTTP 403 `CREDENTIAL_ASSURANCE_INSUFFICIENT`). The script
reports what the gateway actually answered, and exits 1 when it accepted a key: a
gateway release that does not yet enforce an identity policy accepts them.

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
| 6 | `fraud_screen` | A2A outbound to the fraud screener | `hit` forces `decline`, `review` forces `refer`; the verdict is the screener's declared outcome as the gateway checked it |
| 7 | `decide` | LLM `gpt-5.5`, JSON | Recommendation, rationale, customer letter; hard rules override the model; letter carries the AI disclosure |
| 8 | `record` | MCP `applications_set_recommendation` | Last action carries `X-Brutor-Run-End: completed` and `X-Brutor-Outcome-Id: application_processed.loan_approved` / `loan_rejected` / `loan_referred` |

Human oversight: `applications_set_recommendation` is governed by an argument
policy. A `decline`, or an `amount_eur` above 25,000, returns HTTP 202
`approval_required`. The agent adds a note, stores the pending approval, ends
the run `application_processed.held_for_underwriter`, and polls on later ticks. When an underwriter approves,
the agent re-issues the identical call with the approval token in a short
`apply_approved_decision` run. Expired holds are re-raised; a rejected one is
handed off to manual review (`applications_hand_off`, status
`needs_manual_review`) and dropped.

Failures are classified (`screening_agent/failures.py`): a guardrail or policy
refusal of an application's content, or a tool error about it, is the
application's and hands it off to manual review after three attempts; an
outage, a provider out of credits (429), a resilience gate (503), an
`approval_required` hold after an autonomy demotion, `autonomy_denied` or a run
abort is the system's: never counted, the tick stops and the agent backs off
(10, 20, 40, 80 minutes, then every 70). Held and skipped applications never
starve new ones (the pending listing pages on). `./demo.sh requeue` puts
handed-off applications back in the queue.

Terminal states: `completed` with a declared outcome on every path that did its
job; `errored` if a node raises or a guardrail or policy blocks a call (the
gateway records the cause, e.g. `blocked_by_policy`; a refusal is not a
terminal state of its own). A failed run reports no outcome. Nothing ends as
`exhausted`: the graph has no loop.

## Declared outcomes

Each agent ships the catalogue of how its runs may end inside its package, and
that one file is what the agent validates its `X-Brutor-Outcome-Id` against and
what `setup.py` declares on the AI System (`PUT /v1/admin/ai-systems/{id}/outcomes`)
before minting the contract:

- **Screening agent** (`screening_agent/outcomes.json`): `application_processed`
  (counts toward the operating baseline) with the variants `loan_approved`,
  `loan_rejected`, `loan_referred` (the recorded recommendation),
  `held_for_underwriter`, `underwriter_decision_applied`,
  `underwriter_hold_renewed`, `underwriter_hold_closed` and
  `sent_to_manual_review`; and two **no-work** outcomes that do not count:
  `no_applications_available` (a tick with nothing to screen) and
  `applications_dispatched` (a tick that found work and handed it to
  per-application runs). An empty poll is therefore never read as a success of
  the work, and does not dilute cost per processed outcome or the baselines.
- **Fraud screener** (`fraud_screener/outcomes.json`): `screening_completed`
  with `clear`, `review`, `hit`, returned on every A2A response as
  `X-Brutor-Outcome-Id` and in the final Task's `metadata["brutor.outcome.id"]`.
  The gateway checks it against the screener's contract and hands the caller
  `X-Brutor-Outcome-Conformance`; the screening agent fails the run (like an
  unreachable delegate) on anything but a `declared` outcome among the three it
  expects, and branches on that checked value, not on the response body. Its
  dependency edge declares the same three as `expected_outcomes`.

The scheduler's tick is its own run (`bds-tick-<ulid>`), closed with one of the
two no-work outcomes before any application is screened; each application is a
run of its own.

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
| Art 14 human oversight | Argument band holds every decline and every loan over 25,000 for an underwriter; operator run abort | Admin Console, Mission Control, Approvals (or `./demo.sh approve`); Compliance, Human Oversight tiles; `GET /compliance/oversight` |
| Art 15 accuracy/robustness | Envelope caps, guardrails, `eval_report` evidence, continuous checks | Contract; checks |
| Art 26 deployer obligations | Owner + oversight assignment declared; FRIA on file (Art 27) | Lifecycle gate requirements |
| Art 27 FRIA | `docs/fria.md` as `impact_assessment`; required by the gate for high risk | Lifecycle |
| Art 49 registration | Asset register sync | Asset Register |
| Art 50 transparency to persons | Disclosure sentence in every letter; notice config | `GET /compliance/transparency` |
| Art 72 post-market monitoring | Liveness, drift, health, weekly digest, monthly report | Mission Control; Reports |
| Art 73 serious incidents | Incident endpoint with the 15/10/2-day deadline rules; `verify.py --open-incident` shows the flow | Compliance, Incidents |

## Quick start

Prerequisites: **Brutor Agent Control Community Edition (or Enterprise)**
running (Control Plane :5050, Core Gateway :8100, docker network
`brutor-network`), Docker, Python 3.10+, an OpenAI key. Get Brutor Agent
Control from https://trial.brutor.ai/ ("Download Brutor"; the Community Edition
is one folder started with `docker compose up -d`) and see
https://docs.brutor.ai/installation/quickstart/ for the first start.
[docs/getting-started-guide.md](docs/getting-started-guide.md) walks through
the whole path from a fresh download, with the output of a real run. Everything
the demo does by default works on the Community Edition; the one Enterprise
Edition variant is named where it appears.

```bash
cd brutor-demo-system/brutor-demo-setup
cp .env.example .env          # set OPENAI_API_KEY and BRUTOR_ENV_FILE (below)
python3 -m venv .venv         # a virtual environment for the setup scripts
source .venv/bin/activate
python -m pip install -r requirements.txt
./demo.sh up
```

**The gateway must be allowed to reach the demo.** Brutor Agent Control refuses
outbound calls to private addresses (the docker network included) unless they are
listed. In the `.env` of the Brutor folder, set
`BRUTOR_OUTBOUND_ALLOW_HOSTS=witness,brutor-demo-applications-mcp,brutor-demo-credit-bureau-mcp,brutor-demo-fraud-screener-agent,brutor-demo-spire-oidc`
and restart it (`docker compose up -d`) before `./demo.sh up`.

**The administrator's password.** Brutor Agent Control has no stock password:
each installation has its own, in the `.env` of the Brutor folder, as
`BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD`. Set `BRUTOR_ENV_FILE=/path/to/that/.env` in
the demo's `.env` (only the administrator's user name and password are read
from it), or export `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` in your shell. If you
changed the password in the Admin Console, set `ADMIN_PASSWORD` instead.
Without a password the scripts stop at sign-in and say so.

The virtual environment keeps the setup scripts' dependencies (`setup.py`,
`verify.py`) out of your system Python; `.venv/` is git-ignored. `demo.sh` runs
whichever `python3` is first on your `PATH`, which is the environment's while it
is active, so activate it again in every new terminal before running `demo.sh`
or the scripts: `source .venv/bin/activate` from `brutor-demo-setup`. The
containers bring their own Python and need nothing from it.

`demo.sh up` builds and starts the two MCP servers and the fraud agent, waits
for their `/health`, runs `setup.py` (which prints one line per provisioning
step and writes `.demo.env`), and starts the screening agent. The first tick
runs at start; the origination mock has already created three applications.
Held decisions wait for a person: the administrator decides them in the Admin
Console or with `./demo.sh approve` (see [below](#approving-a-held-decision)).

Then:

```bash
./demo.sh status              # container health + verify.py --brief
./demo.sh logs screening-agent
./demo.sh run-one [APP-id]    # process exactly one pending application now
./demo.sh approve             # list the held decisions; --all approves them
./demo.sh resolve             # one agent tick now, so decisions are applied
./demo.sh generate 5          # add five synthetic applications
./demo.sh requeue --all-skipped   # re-queue what the agent handed off to manual review
./demo.sh copied-key          # the agents' calls with their copied API keys: refused in production
python verify.py              # full read-back with the section 9 expectations
```

## What to look at afterwards

In the Admin Console (http://localhost:3002; sign in as the administrator of
your installation, `BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME` /
`BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` in Brutor's `.env`):

- **AI Systems, Brutor Demo System.** *Lifecycle*: stage `active`, the gate
  requirements (FRIA on file, approver named) satisfied, the history.
  *Contract*: the minted, approved and promoted configuration; `drifted` turns
  true the moment you change anything bound to the system. *Signals*:
  completion, error and cost against the envelope, drift, liveness. *Runs*:
  one row per application; open one to see its three steps (gather, assess,
  decide) over the eight nodes' calls, the model calls,
  the skill call, the fraud screener at depth 1 with a verified chain, and
  the guardrail and policy counters.
- **Mission Control, Analytics, Agents.** The two agent identities, what they
  called, what was allowed and what was denied.
- **Mission Control, Approvals.** Every held decision (a decline or a loan
  over 25,000 EUR) waits here with its full arguments, for an administrator
  with `tool_approval:manage` to approve or reject.
- **Compliance, EU AI Act.** The obligations board with dates, the profile,
  transparency and oversight summaries, incidents, the monthly report.
- **Compliance, Human Oversight.** The tiles: requested, decided by a human,
  lapsed.
- **Evidence Ledger.** Sealed records: every bureau read (the system is
  flagged `sensitive_data`) and every governed verdict, verifiable against
  the tenant's public key at `/.well-known/brutor-evidence-keys.json`.

## Approving a held decision

Setup gives every approval hold a 24-hour window: the organisation's approval
window (Governance → Approvals on `borealis-consumer-finance`, inherited by both AI
Systems) covers every hold — the recommendation band, notes, skill runs and holds raised
while a drift response has demoted the system. If nobody decides in time, the agent
records a note, the application stays in `received`, and the next tick screens it
again and raises a fresh approval, so the queue always shows the current decision
until an underwriter acts. Rejecting an approval hands the application off to
manual review (status `needs_manual_review`, with a note), so it is not screened
into a fresh hold again.


1. Wait for a run to end `application_processed.held_for_underwriter` (the log line says "held for
   underwriter, approval id ..."; `verify.py` lists pending approvals, and so
   does `./demo.sh approve`).
2. Admin Console (http://localhost:3002), Mission Control, Approvals. Open
   the request. You see the tool (`applications_set_recommendation`), the
   full arguments (recommendation, amount, rationale, affordability class,
   fraud verdict, the letter) and which policy rule held it. Deciding needs
   `tool_approval:manage`, which the tenant administrator has. Compliance,
   Human Oversight shows the tiles (requested, decided by a human, lapsed).
3. Approve (optionally with a note) or reject.
4. On its next tick the agent polls `GET /v1/approvals/{id}/poll` with its
   workload identity, receives the one-time approval token, and re-issues the identical
   write with `X-Approval-Token` in a new short run (`apply_approved_decision`,
   outcome `application_processed.underwriter_decision_applied`). A rejection
   hands the application off to manual review (outcome
   `application_processed.sent_to_manual_review`) and the recommendation is dropped. Approvals expire; an expired hold is
   re-raised by the agent, so the queue always holds the current decision.

**From a terminal, or unattended.** `approve.py` makes the same decision
through the same control-plane routes, signed in as the administrator:

```bash
./demo.sh approve                    # list the pending holds (id, application, recommendation, amount)
./demo.sh approve <approval-id>      # approve one
./demo.sh approve --reject <id>      # reject one
./demo.sh approve --all --wait 900   # wait up to 15 minutes for a hold, then approve every pending one
./demo.sh resolve                    # run one agent tick now instead of waiting for the next
```

`approve` exits 0 when every decision was recorded, 1 when the Control Plane
refused one (already decided, expired, gateway unreachable), 3 when `--wait`
ran out with nothing pending.

**Enterprise Edition variant: an underwriter in the User Portal.** The
Enterprise Edition has a User Portal with end-user sign-in, where a member of
the system's resource group can decide instead of an administrator. Run
`./demo.sh up --portal-approver` (or `python setup.py --portal-approver`):
it creates the portal user `underwriter` ("Anna Berg (Underwriter)") in the
end-user group `borealis-underwriters`, binds the group to the demo system,
and writes her credentials to `.demo.env` (`PORTAL_UNDERWRITER_USER` /
`PORTAL_UNDERWRITER_PASSWORD`; the password is generated unless you set
`PORTAL_UNDERWRITER_PASSWORD`). She then decides in the User Portal
(http://localhost:3001), Inbox, Approvals. On a Community Edition gateway,
which serves no User Portal, the flag stops the setup before it creates
anything and says why.

Nothing changes between what the underwriter saw and what is recorded: the
gateway matches the retried call against the approved arguments.

## EU residency

Off by default. The OpenAI and Anthropic providers are `global` as shipped,
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
cp .env.example .env        # set OPENAI_API_KEY and BRUTOR_ENV_FILE
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
./demo.sh up                # against a running Brutor Agent Control (Community Edition or Enterprise)
```

## Tests

Nothing here needs a running Brutor Agent Control or an OpenAI key. CI
(`.github/workflows/ci.yml`) runs the same commands on Python 3.12 and 3.13.

```bash
# set-up scripts: provisioning, read-back and approval against a fake Control Plane
cd brutor-demo-setup
python -m pip install -r requirements.txt pytest
python -m pytest -q tests
python setup.py --dry-run            # the provisioning plan; no network

# each agent and MCP server, from its own folder
cd ../brutor-demo-screening-agent    # or -fraud-screener-agent, -applications-mcp, -credit-bureau-mcp
python -m pip install ".[test]"
python -m pytest -q

# the skill (needs only pytest)
cd ../brutor-demo-affordability-skill
python -m pytest -q
```

`brutor-demo-setup/tests/test_names_and_credentials.py` holds the whole
repository to the product's names and refuses a written-out password or a
secret with a value in a template. It also refuses a pointer at a record a
reader cannot open (an internal design-document, finding or build number);
published standards are cited by number from the test's `PUBLIC_STANDARDS` list.

## Release status

The demo is a source-built add-on and the Community Edition's sample and
acceptance test. It is not part of the platform release scripts or of the
Community or Enterprise packages: `./demo.sh up` builds its four images locally
and provisions against the Brutor Agent Control stack that is running.

**What it needs.** The Community Edition package of the current release of
Brutor Agent Control. The Enterprise Edition trial package of the same release
runs it too and adds the User Portal variant (`--portal-approver`).

**How it is verified.** Against a running stack, `python verify.py` (in
`brutor-demo-setup`) reads back what setup declared and what the agents did,
and exits 0 only when every expectation is met: a full screening run with its
three steps, three turns, nine actions, three model calls, one skill call and
one A2A call on an intact chain; lifecycle `active`; the contract not drifted;
an unexpired impact assessment on file; the agents' declared releases and
outcomes as setup wrote them. Without a stack, `brutor-demo-setup/tests`
checks every route the scripts call against the Control Plane's Community API
description when the Control Plane source is checked out beside this
repository.

## What the demo does not claim

- It does not claim the models run in the EU. OpenAI and Anthropic are
  `global` providers as shipped; EU residency enforcement is opt-in and
  documented.
- It does not claim the fraud screener detects real fraud; it is a mock with
  a synthetic sanctions list.
- It does not claim conformity with the EU AI Act. It shows the evidence,
  oversight and documentation mechanisms a real deployment would rely on, on
  synthetic data.
- Evidence sealing only happens when Brutor Agent Control was started with
  `BRUTOR_EVIDENCE_SIGNING_KEY` set (the downloaded package has it;
  `./start.sh` generates it for a clone). `setup.py` warns when it is absent.
- The screening agent has no loop, so a run never ends `exhausted`; the demo
  says so rather than faking it.

## License

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE). You may copy, change
and build on the demo, including commercially. The Brutor name and logo are trademarks
of Brutor AI Ltd. and are not covered by the licence. Brutor Agent Control, which the
demo runs against, is licensed separately (the Community Edition under Apache-2.0).
