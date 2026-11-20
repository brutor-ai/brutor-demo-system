# brutor-demo-setup

Provisioning, orchestration and the compliance documents for the
[Brutor Demo System](../README.md). Design:
[DESIGN.md sections 5.6, 6 and 7](../DESIGN.md#56-setup-and-orchestration-brutor-demo-setup).

## What is here

```
setup.py            idempotent REST provisioning (DESIGN section 6); writes .demo.env
verify.py           reads back health, runs, contract, gate, obligations, evidence, Annex IV
approve.py          lists, approves or rejects held decisions as the tenant administrator
copied_key.py       the agents' calls made with their copied development-stage API keys
docker-compose.yml  the four demo containers and SPIRE on Brutor Agent Control's docker network (brutor-network)
spire/              SPIRE server, agent and OIDC Discovery Provider configuration; how workload identity works (README.md)
demo.sh             up | spire | provision | start | status | logs | down | run-one | approve | resolve | generate | requeue | copied-key
.env.example        OPENAI_API_KEY, CP_URL, GW_URL, where the administrator's password comes from, model names
requirements.txt    requests (the three scripts are otherwise standard library)
tests/              provisioning, read-back and approval tests against a fake Control Plane
docs/               fria.md, risk-assessment.md, data-governance.md, evaluation-report.md,
                    instructions-for-use.md (the evidence rows point at these, sha256 stamped)
```

## How it fits the system

Nothing in this repo runs at request time. `setup.py` tells the Control
Plane what the system is (two AI Systems under an organisation unit), what it
may use (models, MCP servers, the skill, the fraud screener's card), who it
runs as (the demo's SPIRE as workload identity provider, agent identities whose
subjects are the agents' SPIFFE IDs, with grants; one development-stage API key
per system), and how it is
governed (guardrails, the approval band, limits, envelope, liveness, response
policy, continuous checks, the EU AI Act profile, evidence, notice) and
finally mints the contract and moves the lifecycle to `active`. The
containers then run against that configuration.

Order matters. `demo.sh up` starts SPIRE first and registers the agents'
workload identities (spire/README.md), then the two MCP servers and the fraud agent so the gateway can discover their tools and fetch the live agent card,
then provisions, then starts the screening agent.

## Running it

Prerequisites: **Brutor Agent Control Community Edition (or Enterprise)**
running (Control Plane on :5050, Core Gateway on :8100, docker network
`brutor-network`), Docker, Python 3.10+. It must have workload identity: until the
first release that carries it, build it from source as the top-level
[README](../README.md#quick-start) shows (`deploy/compose` of
https://github.com/brutor-ai/brutor-agent-control, with `docker-compose.build.yml`).

The administrator's password is not in this repository and has no default:
Brutor Agent Control generates one per installation and keeps it in its own
`.env` as `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD`. The scripts take, in this order:

1. `ADMIN_USER` / `ADMIN_PASSWORD` (set these if you changed the password in
   the Admin Console);
2. `BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME` / `BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD` from the
   environment or this directory's `.env`;
3. the same two names from the file `BRUTOR_ENV_FILE` points at (Brutor's own
   `.env`; nothing else in that file is read).

With none of them, `setup.py`, `verify.py` and `approve.py` stop at sign-in
with a message that names these three.

```bash
cd brutor-demo-setup
cp .env.example .env            # set OPENAI_API_KEY and BRUTOR_ENV_FILE=/path/to/brutor/.env
python3 -m venv .venv            # virtual environment for setup.py / verify.py (git-ignored)
source .venv/bin/activate        # again in every new terminal
python -m pip install -r requirements.txt
./demo.sh up                     # build, start, provision, start the agent
./demo.sh status                 # four /health endpoints + verify.py --brief
./demo.sh logs screening-agent   # watch the ticks
./demo.sh approve                # list held decisions; --all approves every pending one
./demo.sh resolve                # one agent tick now, so decisions are applied
./demo.sh down                   # stop; add --volumes to forget applications and approvals
```

`setup.py` on its own:

```bash
python3 setup.py --dry-run          # print the plan; no network
python3 setup.py                    # provision (re-runnable)
python3 setup.py --skip-lifecycle   # everything except contract + lifecycle
python3 setup.py --residency        # also set the tenant EU residency profile (see below)
python3 setup.py --portal-approver  # ENTERPRISE EDITION ONLY: also create the underwriter as a User Portal user (step 10e)
```

Every step prints `✓ step: id` or `⚠ step: reason`; an unrecoverable problem
prints `✗ step: reason` and stops. Re-runs find everything by name and only
fill gaps. The API keys' plaintext is returned once, so `.demo.env` is read
first and a key it already holds is never overwritten; if a key exists on the
server but `.demo.env` has lost it, a replacement with a dated name is minted
and the script says so.

`verify.py`:

```bash
python3 verify.py                   # full read-back; exit 1 if an expectation fails
python3 verify.py --brief           # one line per surface
python3 verify.py --json            # raw payloads
python3 verify.py --open-incident   # open a demo Art 73 incident linked to the latest run
```

The expectations (DESIGN section 9): at least one run in the window with
three steps (gather, assess, decide), three turns and nine actions,
`chain_integrity` intact or client_asserted, one skill call, one A2A call,
three model calls; health readable; lifecycle `active`; an unexpired
`impact_assessment` on file.

## What setup.py provisions

| Step | Objects |
|---|---|
| 1 | Preflight: Control Plane, gateway, evidence signing key (warns if absent), the three containers |
| 2 | Models `gpt-5.2` and `gpt-5.5` found by `model_name`; provider key set; catalog import if missing |
| 3 | Organisation unit `borealis-consumer-finance`: gpt-5.2 bound here; company-wide LLM limits (50 USD/day, 1,000 USD/month, 120 rpm, concurrency 4) and MCP limits (2,000 calls/hour); approval window 24 h (every hold, inherited by both AI Systems) |
| 4 | AI Systems `brutor-demo-system` (agent, high risk, sensitive, autonomous; idle timeout 300 s, delegation depth 2) and `brutor-demo-fraud-screener` (agent, minimal), both with **inherit resources from parent** on |
| 5 | gpt-5.5 bound directly to the demo system (`portal_visible` false); gpt-5.2 is inherited from the org on both systems, and `effective-llm-models` is read back to confirm |
| 6 | MCP servers `brutor-demo-applications` and `brutor-demo-credit-bureau` with default server configs, tools discovered and enabled; the skills server config bound |
| 7 | Skill `affordability-check`: SKILL.md, `affordability.py` (sandbox), `policy.md`; validated, published, bound |
| 8 | Agent card `brutor-demo-fraud-screener` from the live card, signed, bound group-side |
| 9 | Workload identity provider `Borealis SPIRE (demo)` (OIDC, issuer `http://brutor-demo-spire-oidc:8008`, audience `brutor`); identities `brutor-demo-screening-worker` and `brutor-demo-fraud-screener` with their SPIFFE IDs as subjects and the grants in DESIGN 7.1; one development-stage API key each (`.demo.env`), which `copied_key.py` uses |
| 9b | Composition: the demo system depends on the fraud screener (shared service, critical), with `expected_outcomes` = the screener's three verdicts, read from the screening agent's `outcomes.json` |
| 9c | Declared outcomes: `PUT /v1/admin/ai-systems/{id}/outcomes` with each agent's `outcomes.json` (`screening_agent/outcomes.json`, `fraud_screener/outcomes.json`), only when the stored catalogue differs; a catalogue that cannot be read or written stops the script. The contracts minted in step 14 capture them |
| 10 | Human oversight: reads the approvals pending count (the administrator decides held calls; nothing is created). 10e, `--portal-approver`, Enterprise Edition only: end user `underwriter` ("Anna Berg (Underwriter)"), end-user group `borealis-underwriters`, membership, group bound to the demo system (see below) |
| 11 | Guardrail configs (org baseline: prompt injection + secrets; system: banned words only), argument policy, the systems' own LLM/MCP/skill limits, operating envelope (DESIGN 7.3) |
| 12 | Liveness, response policy, three continuous checks |
| 13 | EU AI Act and GDPR enabled; profiles; four evidence rows with sha256 of the docs; Art 50 notice; asset sync; monthly report |
| 14 | Contract minted, approved, promoted; lifecycle proposed to approved to active |
| 15 | `.demo.env` (keys, ids, model names; with `--portal-approver` also the underwriter's portal credentials) |

## Who decides a held decision

Held decisions (every decline, every loan over 25,000 EUR) wait for a person.

**Default, every edition: the administrator.** In the Admin Console,
**Mission Control, Approvals**, or from a terminal with `approve.py`, which
signs in as the tenant administrator and calls the same control-plane routes:

```bash
python approve.py                      # list the Demo System's pending holds
python approve.py <id> [<id> ...]      # approve these
python approve.py --reject <id> [...]  # reject these (the agent hands the application to manual review)
python approve.py --all                # approve every pending hold
python approve.py --all --wait 900     # unattended: wait up to 900 s for a hold, then approve
python approve.py --all --note "..."   # the note is recorded with each decision
```

Exit status 0 = recorded (or nothing pending); 1 = a decision was refused
(404 unknown, 409 already decided, 410 expired, 503 the Control Plane could
not reach the gateway and nothing changed) or a call failed; 3 = `--wait` ran
out. The agent applies a decision on its next tick; `./demo.sh resolve` runs
one tick now. Step 10 of `setup.py` creates nothing for this path: it reads
`GET /v1/admin/approvals/pending-count` once and stops if the route is
missing or the administrator lacks `tool_approval:read`. Compliance, Human
Oversight in the Admin Console shows the resulting tiles (requested, decided
by a human, lapsed).

**Enterprise Edition variant: `--portal-approver`.** The Enterprise Edition
has a User Portal, where a member of the system's resource group can decide
(**Inbox, Approvals**, http://localhost:3001). The Community Edition gateway
serves no `/v1/portal/*` and has no end-user sign-in, so this is off by
default and `setup.py --portal-approver` first asks the gateway
(`POST /v1/portal/auth/login` with an empty body): a 404 stops the setup
before anything is created. On an Enterprise gateway step 10e then creates
end user `underwriter` (display name "Anna Berg (Underwriter)",
`underwriter@borealis.example`) through `POST /v1/admin/end-users`, the manual
end-user group `borealis-underwriters`, the membership through
`POST /v1/admin/end-user-groups/{id}/members` (a `member_ids` key on the group
body is silently dropped), and binds the group to the demo system with
`POST /v1/admin/resource-groups/{gid}/end-user-groups`. The portal lists the
requests whose requester group is one of the caller's resource groups, and a
user's groups are direct memberships plus the end-user groups bound to a
resource group. Portal sign-in checks only the password and `is_active`, and
the Inbox tab is always shown, so no model is made portal-visible (the model
bindings' `portal_visible` flag is part of the contract closure; end-user
groups and their bindings are not, which is why this step cannot drift a
minted contract). The password is `PORTAL_UNDERWRITER_PASSWORD` if you set
it, otherwise generated; the credentials are written to `.demo.env` as
`PORTAL_UNDERWRITER_USER` / `PORTAL_UNDERWRITER_PASSWORD`. If the user
already exists her password is left as it is.

## Series helpers

For a recording that needs exactly one run, or more applications to screen:

```bash
./demo.sh run-one                # process the first pending application on the running agent
./demo.sh run-one APP-20260923-0004   # a specific one
./demo.sh generate 5             # add five synthetic applications to the origination mock
./demo.sh requeue APP-20260927-0042   # put a handed-off application back in the queue
./demo.sh requeue --all-skipped       # every application the agent gave up on
```

`run-one` execs `python -m screening_agent --application <id>` inside
`brutor-demo-screening-agent` (the agent container must be up; the default id
is the first row of `applications_list_pending`, fetched with a raw JSON-RPC
call to `http://127.0.0.1:3014/mcp`). `generate N` execs
`python -m applications_mcp.generate --count N` inside
`brutor-demo-applications-mcp`; the next tick, or `run-one`, picks them up.
`requeue` undoes a hand-off: the agent moves an application to the terminal status
`needs_manual_review` after three failures attributable to the application (or when an
underwriter rejects its recommendation). It execs `python -m screening_agent --requeue
...` (or `--requeue-all-skipped`) in the agent container to clear its skip list, then
`python -m applications_mcp.requeue ...` in the applications container to move the
applications back to `received`; the next tick screens them with fresh attempts. Outages
and governance holds never put an application on the skip list (DESIGN.md section 4,
"Failure classes"), so after an incident there is normally nothing to re-queue.

## Inheritance

The estate is one tree: the organisation group carries what every system
under Borealis shares (gpt-5.2, the company-wide budget and rate ceilings,
the baseline guardrails), and both AI Systems have *inherit resources from
parent* switched on. Resources compose additively: a system can use what it
is bound to directly (gpt-5.5 on the demo system) plus what it inherits
(gpt-5.2), and each inheritance is gated by the gateway. Limits and policies
compose restrictively: the demo system's own 15 USD/day cap wins over the
org's 50, never the other way round. Guardrail configs match nearest ancestor
first, so the baseline lives once, on the org, and the system config carries
only what is specific to it.

Note for the gateway: the core caches group bindings for 300 s, so after a
change to a model binding, either wait or restart the gateway (`docker compose restart core` in the
folder of the Brutor package) before the next run.

## Residency

`--residency` sets the tenant profile to `allowed_regions: [eu-west-1,
eu-central-1], block_global: true`. It is off by default because the OpenAI
and Anthropic providers are `global` as shipped; with `block_global` the
gateway refuses every model call. To use it, first point the two models at an
EU-hosted endpoint (an Azure OpenAI deployment in an EU region, or an EU
provider entry) in the Admin Console, then run `setup.py --residency`. The
bureau MCP server is meant to carry region `eu-west-1`; the current admin API
does not accept `region` on server create or update, so `setup.py` tries,
reads it back and warns if it is unset. Set it in the Admin Console if you
turn residency on.

## Why PII detection is off

The customer letter must carry the applicant's name; a PII guardrail on
`chat_output` would either block every letter or redact the one field that
makes it a letter. The guardrail config leaves PII detection off and says so
in its description; bureau reads are instead sealed as evidence so the
sensitive data path is provable rather than filtered.

## Gateway contracts this repo relies on

- Admin API on the Control Plane: login `POST /v1/admin/users/tenant/login`,
  then the `/v1/admin/*` routes named in the step table above. Every one is
  in the Community Edition API (`tests/test_community_approvals.py` checks the
  three scripts against the Control Plane's recorded Community OpenAPI
  document when that repository is checked out next to this one; the document
  read is the one committed there, `BRUTOR_SIBLINGS=worktree` reads its
  working tree and `BRUTOR_CONTROL_OPENAPI` names another file).
  Approvals: `GET /v1/admin/approvals/pending-count`,
  `GET /v1/admin/approvals?status_filter=pending&group_id=...`,
  `GET /v1/admin/approvals/{id}` and
  `POST /v1/admin/approvals/{id}/approve|reject` (body `{"note"}`;
  permissions `tool_approval:read` / `tool_approval:manage`).
  With `--portal-approver` (Enterprise Edition) also
  `POST /v1/admin/end-users`, `POST /v1/admin/end-user-groups`,
  `POST /v1/admin/end-user-groups/{id}/members` and
  `POST /v1/admin/resource-groups/{gid}/end-user-groups` for the underwriter.
  Declared outcomes: `GET` / `PUT /v1/admin/ai-systems/{id}/outcomes` (body
  `{"outcomes": [...]}`, answer `{"ai_system_id", "outcomes"}`) and `expected_outcomes`
  on `POST` / `PATCH /v1/admin/ai-systems/{id}/dependencies`.
- Gateway: `GET /health` and `GET /.well-known/brutor-evidence-keys.json`.
  The agent (not this repo) polls `GET /v1/approvals/{id}/poll` with its API
  key.
- Enterprise Edition only, with `--portal-approver`: `POST
  /v1/portal/auth/login` on the gateway is probed once with an empty body (a
  404 means Community Edition and stops the setup); the underwriter then uses
  the Portal API (`GET /v1/approvals`, `POST /v1/approvals/{id}/approve|reject`).
- The demo containers' `GET /health` and the fraud agent's
  `GET /.well-known/agent-card.json`.
