# Getting started with the Brutor Demo System

From a freshly downloaded Brutor Agent Control Community Edition package to a
governed agent run, a decision held for a person, and that decision approved.
The outputs below are examples of what the scripts print; identifiers, counts
and timings are your installation's.

The Demo System is a loan pre-screening agent for a fictional lender. It works
through two models, two MCP servers, a skill and a second agent over A2A, and
every one of those calls goes through the Core Gateway. All data is synthetic.
[README.md](../README.md) describes what it shows; [DESIGN.md](../DESIGN.md) is
the design.

## What you need

- Docker with Compose v2 (`docker compose version`).
- Python 3.10 or later, for the set-up scripts.
- An OpenAI API key. The demo uses `gpt-5.2` and `gpt-5.5` by default; one
  screening run makes three model calls.
- Free ports: 3014, 3015, 9200, 9201 and 9208 for the demo, and 3002, 5050 and
  8100 for Brutor Agent Control.
- Brutor Agent Control Community Edition with workload identity. Until the first
  release that carries it, build it from source (below).

## 1. Start Brutor Agent Control

From source:

```bash
git clone https://github.com/brutor-ai/brutor-agent-control.git
cd brutor-agent-control/deploy/compose
COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml ./start.sh
docker compose ps
```

`start.sh` fills the empty secrets in this folder's `.env`, the administrator's
password included, then builds every image, starts the stack and waits until it
is healthy. There is nothing else to edit and no license key. (From a release
package that carries workload identity, the same is `./start.sh` in its folder.) All seven services (`admin-console`, `control-plane`, `core`, `postgres`,
`redis`, `skill-runner`, `witness`) must be `healthy`. The folder's own
`docs/getting-started-guide.md` covers the first start, sign-in and a first API
key.

Do not export `COMPOSE_PROJECT_NAME` in the shell you start the package from:
it overrides the package's project name, and `docker compose down` then does
nothing, without saying so.

## 2. Configure the demo

```bash
git clone https://github.com/brutor-ai/brutor-demo-system.git
cd brutor-demo-system/brutor-demo-setup
cp .env.example .env
```

Set two values in `.env`:

```bash
OPENAI_API_KEY=...                           # your key
BRUTOR_ENV_FILE=/path/to/the/package/.env    # the Community package's .env
```

`BRUTOR_ENV_FILE` is how the demo signs in. Brutor Agent Control has no stock
password: the administrator's password is `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` in
the package's `.env`, and the demo reads the administrator's user name and
password from that file and nothing else. Leave `ADMIN_USER` and
`ADMIN_PASSWORD` empty unless you changed the password in the Admin Console:
they are read first, so a value left there from another installation wins over
the package's.

## 3. Start the demo

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
./demo.sh up
```

Before that, allow the Core Gateway to reach the demo's containers: in the
package's `.env`, set
`BRUTOR_OUTBOUND_ALLOW_HOSTS=witness,brutor-demo-applications-mcp,brutor-demo-credit-bureau-mcp,brutor-demo-fraud-screener-agent,brutor-demo-spire-oidc`
and run `docker compose up -d` in the package folder again. The gateway refuses
outbound calls to private addresses (the Docker network included) that are not
listed.

`demo.sh up` starts SPIRE (the agents' workload identity: server, agent and OIDC
Discovery Provider, see `spire/README.md`), builds four images, starts the two MCP
servers and the fraud screener, provisions the AI System through the Control Plane, and starts the
screening agent. The four containers join the package's Docker network
`brutor-network` and call the Core Gateway as `http://core:8100`.

Provisioning prints one line per step: `✓` done, `⚠` a warning, `✗` an error
that stops it. Some of the lines:

```text
✓ login: admin @ default (http://localhost:5050)
✓ models.classifier: gpt-5.2 (llm-...) provider key set
✓ models.drafter: gpt-5.5 (llm-...) provider key set
✓ ai_system.demo: brutor-demo-system (group-...) created
✓ mcp.applications.capabilities: 6 tool(s) enabled: applications_list_pending, applications_get, ...
✓ skill.publish: published
✓ workload_idp: Borealis SPIRE (demo) (idp-...) created: issuer http://brutor-demo-spire-oidc:8008, audience brutor
✓ identity.screening.workload: subject spiffe://borealis.example/brutor-demo/screening-agent
✓ api_key.screening: brutor-demo-screening-agent minted (bound to agent-...)
✓ approvers.admin: admin decides held calls in the Admin Console, Mission Control -> Approvals, or with ./demo.sh approve (0 pending in the tenant now)
✓ frameworks: eu-ai-act enabled
✓ contract.demo: v1 ctr-... (minted), approved, promoted
✓ lifecycle.demo: -> active (approved_by Anna Berg, Head of Credit Risk)
✓ write_env: .../brutor-demo-setup/.demo.env
  ✓ screening-agent healthy (http://127.0.0.1:9201/health)
```

`setup.py` can be run again at any time: it finds what exists by name and only
fills gaps. `.demo.env` holds the development-stage API keys it minted; it is
git-ignored. The agents do not use them: each presents a JWT-SVID from SPIRE.
`./demo.sh copied-key` shows what the gateway does with a copied key.

## 4. The first governed run

The agent screens one application on its first tick: four tool calls, one
skill run, one A2A call to the fraud screener and three model calls, one of
them made by the fraud screener.

```bash
./demo.sh logs screening-agent
```

```text
gateway kind=mcp target=applications_get status=200 node=intake step=gather ... ms=19
gateway kind=mcp target=bureau_verify_identity status=200 node=verify_identity ... ms=19
gateway kind=mcp target=bureau_get_report status=200 node=credit_report ... ms=14
gateway kind=mcp target=skills__run_script status=200 node=affordability ... ms=53
gateway kind=llm target=gpt-5.2 status=200 node=classify step=assess ... ms=3800
gateway kind=a2a target=screening.fraud_sanctions status=200 node=fraud_screen ... ms=1595
gateway kind=llm_responses target=gpt-5.5 status=200 node=decide step=decide ... ms=8121
gateway kind=mcp target=applications_set_recommendation status=200 node=record ... ms=17
run=bds-APP-20261112-0001-... state=completed outcome=application_processed.loan_approved application=APP-20261112-0001 ms=13850
```

```bash
./demo.sh status
```

```text
✓ lifecycle: stage=active
✓ contract: active v1, 1 version(s), drifted=False
✓ approvals: 0 pending for an underwriter
✓ obligations: 37 (applies=31, not_applicable=6)
✓ evidence: 19 sealed record(s)
```

In the Admin Console (http://localhost:3002, user name `admin`, password the
value of `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` in the package's `.env`), the run is
under AI Systems, Brutor Demo System, Runs.

## 5. The first approval

Every decline and every loan over 25,000 EUR is held for a person. Most
applications are neither, so a hold does not appear on demand: about one
application in sixty is held.

```bash
./demo.sh generate 5         # add five synthetic applications
./demo.sh run-one            # screen one now; repeat until a run ends held
```

```text
run=bds-APP-20261112-0007-... state=completed outcome=application_processed.held_for_underwriter
```

Decide it in the Admin Console under Mission Control, Approvals, or from the
terminal:

```bash
./demo.sh approve                    # list the held decisions
./demo.sh approve <approval-id>      # approve one
./demo.sh approve --reject <id>      # reject one
./demo.sh resolve                    # one agent tick now, so the decision is applied
```

`approve` signs in as the administrator and calls the Control Plane's
`/v1/admin/approvals` routes, the same ones the Admin Console uses. Approving
the same hold twice is refused with 409. On its next tick the agent polls the
Core Gateway, receives a one-time token and repeats the identical call, which
is then forwarded. A rejected decision goes to manual review:

```text
gateway kind=approval_poll target=01m76h5r24k1wvnd760vx25922 status=200 ...
gateway kind=mcp target=applications_hand_off status=200 step=approval_closed ...
run=... state=completed outcome=application_processed.sent_to_manual_review application=APP-20261112-0015 status=needs_manual_review
```

`resolve` runs a whole tick, so it also screens up to `MAX_PER_TICK` pending
applications, with their model calls.

## 6. Read it back

```bash
python verify.py
```

`verify.py` reads health, lifecycle, contract, runs, evidence, oversight and
documentation back through the Control Plane and exits 1 if an expectation
fails. This is the check that the demo works on your stack: a full screening
run (three steps, three turns, nine actions, three model calls, one skill
call, one A2A call, an intact chain), lifecycle `active`, the contract not
drifted, an unexpired impact assessment on file, and the agents' releases and
declared outcomes as set-up wrote them.

## 7. Stop

Stop the demo before the package: `docker compose down` removes
`brutor-network`, which the demo's containers are attached to.

```bash
./demo.sh down                 # add --volumes to forget applications and approvals
docker compose down            # in the package folder; data is kept
docker compose down -v         # ... and delete all data
```

## On the Enterprise Edition trial

The demo runs the same way against the Enterprise Edition trial package. What
differs:

- The trial package is started with `./docker-start.sh` and needs its license
  key. It publishes the same addresses (Admin Console 3002, Control Plane 5050,
  Core Gateway 8100), uses the same Docker network `brutor-network`, and keeps
  the tenant administrator's password under the same name,
  `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD`, in its own `.env`. Point `BRUTOR_ENV_FILE`
  at that file.
- The Enterprise Edition has a User Portal (http://localhost:3001), which the
  Community Edition does not serve. With `./demo.sh up --portal-approver` (or
  `python setup.py --portal-approver`) set-up also creates the portal user
  `underwriter` in the end-user group `borealis-underwriters` and writes her
  generated credentials to `.demo.env`; she decides held decisions in the User
  Portal under Inbox, Approvals. Approval by the administrator, in the Admin
  Console or with `./demo.sh approve`, works on both editions.
- On the Community Edition `--portal-approver` stops before it creates
  anything and says why (exit status 1).

## Which Brutor Agent Control

The demo needs a Community Edition (or Enterprise Edition) with workload
identity: today a build from source of
https://github.com/brutor-ai/brutor-agent-control (section 1), and from the first
release that carries it, that release's package. `python verify.py` exiting 0 is
how a stack is shown to run it.

## More

- [README.md](../README.md): what the demo shows, component by component, and
  the EU AI Act mapping.
- [brutor-demo-setup/README.md](../brutor-demo-setup/README.md): every
  `demo.sh`, `setup.py`, `verify.py` and `approve.py` option.
- Brutor Agent Control documentation: https://docs.brutor.ai
