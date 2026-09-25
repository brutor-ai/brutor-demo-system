# brutor-demo-setup

Provisioning, orchestration and the compliance documents for the
[Brutor Demo System](../README.md). Design:
[DESIGN.md sections 5.6, 6 and 7](../DESIGN.md#56-setup-and-orchestration-brutor-demo-setup).

## What is here

```
setup.py            idempotent REST provisioning (DESIGN section 6); writes .demo.env
verify.py           reads back health, runs, contract, gate, obligations, evidence, Annex IV
docker-compose.yml  the four demo containers on the trial's external brutor-network
demo.sh             up | provision | start | status | logs | down | run-one | generate
.env.example        OPENAI_API_KEY, CP_URL, GW_URL, admin credentials, model names
requirements.txt    requests (setup.py and verify.py are otherwise standard library)
docs/               fria.md, risk-assessment.md, data-governance.md, evaluation-report.md,
                    instructions-for-use.md (the evidence rows point at these, sha256 stamped)
```

## How it fits the system

Nothing in this repo runs at request time. `setup.py` tells the Brutor control
plane what the system is (two AI Systems under an organisation unit), what it
may use (models, MCP servers, the skill, the fraud screener's card), who it
runs as (agent identities with grants, one API key per system), and how it is
governed (guardrails, the approval band, limits, envelope, liveness, response
policy, continuous checks, the EU AI Act profile, evidence, notice) and
finally mints the contract and moves the lifecycle to `active`. The
containers then run against that configuration.

Order matters. `demo.sh up` starts the two MCP servers and the fraud agent
first so the gateway can discover their tools and fetch the live agent card,
then provisions, then starts the screening agent.

## Running it

Prerequisites: the Brutor trial bundle running (control plane on :5050, core
proxy on :8100, docker network `brutor-network`), Docker, Python 3.10+.

```bash
cd brutor-demo-setup
cp .env.example .env            # set OPENAI_API_KEY
python3 -m pip install -r requirements.txt
./demo.sh up                     # build, start, provision, start the agent
./demo.sh status                 # four /health endpoints + verify.py --brief
./demo.sh logs screening-agent   # watch the ticks
./demo.sh down                   # stop; add --volumes to forget applications and approvals
```

`setup.py` on its own:

```bash
python3 setup.py --dry-run          # print the plan; no network
python3 setup.py                    # provision (re-runnable)
python3 setup.py --skip-lifecycle   # everything except contract + lifecycle
python3 setup.py --residency        # also set the tenant EU residency profile (see below)
python3 setup.py --no-portal-user   # skip the underwriter portal user (step 10)
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
eight steps, `chain_integrity` intact or client_asserted, one skill call, one
A2A call, at least two model calls; health readable; lifecycle `active`;
an unexpired `impact_assessment` on file.

## What setup.py provisions

| Step | Objects |
|---|---|
| 1 | Preflight: control plane, gateway, evidence signing key (warns if absent), the three containers |
| 2 | Models `gpt-5.2` and `gpt-5.5` found by `model_name`; provider key set; catalog import if missing |
| 3 | Organisation unit `borealis-consumer-finance`: gpt-5.2 bound here; company-wide LLM limits (50 USD/day, 1,000 USD/month, 120 rpm, concurrency 4) and MCP limits (2,000 calls/hour) |
| 4 | AI Systems `brutor-demo-system` (agent, high risk, sensitive, autonomous; idle timeout 300 s, delegation depth 2) and `brutor-demo-fraud-screener` (agent, minimal), both with **inherit resources from parent** on |
| 5 | gpt-5.5 bound directly to the demo system (`portal_visible` false); gpt-5.2 is inherited from the org on both systems, any direct gpt-5.2 binding from an earlier run is removed, and `effective-llm-models` is read back to confirm |
| 6 | MCP servers `brutor-demo-applications` and `brutor-demo-credit-bureau` with default server configs, tools discovered and enabled; the skills server config bound |
| 7 | Skill `affordability-check`: SKILL.md, `affordability.py` (sandbox), `policy.md`; validated, published, bound |
| 8 | Agent card `brutor-demo-fraud-screener` from the live card, signed, bound group-side |
| 9 | Identities `brutor-demo-screening-worker` and `brutor-demo-fraud-screener` with the grants in DESIGN 7.1; one API key each |
| 10 | Portal underwriter: end user `underwriter` ("Anna Berg (Underwriter)"), end-user group `borealis-underwriters`, membership, group bound to the demo system (see below) |
| 11 | Guardrail configs (org baseline: prompt injection + secrets; system: banned words only), argument policy, the systems' own LLM/MCP/skill limits, operating envelope (DESIGN 7.3) |
| 12 | Liveness, response policy, three continuous checks |
| 13 | EU AI Act and GDPR enabled; profiles; four evidence rows with sha256 of the docs; Art 50 notice; asset sync; monthly report |
| 14 | Contract minted, approved, promoted; lifecycle proposed to approved to active |
| 15 | `.demo.env` (keys, ids, model names, the underwriter's portal credentials) |

## The underwriter portal user

Held decisions (every decline, every loan over 25,000 EUR) are approved in the
**User Portal** (http://localhost:3001), **Inbox, Approvals**, not in the Admin
Console: the portal lists the requests whose requester group is one of the
caller's resource groups, and a user's groups are direct memberships plus the
end-user groups bound to a resource group (`portal_common.rs
get_user_group_ids`). Step 10 therefore creates end user `underwriter`
(password `Underwriter123!`, display name "Anna Berg (Underwriter)",
`underwriter@borealis.example`) through `POST /v1/admin/end-users`, the manual
end-user group `borealis-underwriters`, the membership through
`POST /v1/admin/end-user-groups/{id}/members` (a `member_ids` key on the group
body is silently dropped), and binds the group to the demo system with
`POST /v1/admin/resource-groups/{gid}/end-user-groups`. Portal login checks
only the password and `is_active`, and the Inbox tab is always shown, so no
model is made portal-visible (the model bindings' `portal_visible` flag is part
of the contract closure; end-user groups and their bindings are not, which is
why this step cannot drift a minted contract). The credentials are written to
`.demo.env` as `PORTAL_UNDERWRITER_USER` / `PORTAL_UNDERWRITER_PASSWORD`;
`--no-portal-user` skips the step. Compliance, Human Oversight in the Admin
Console shows the resulting tiles (requested, decided by a human, lapsed).

## Series helpers

For a recording that needs exactly one run, or more applications to screen:

```bash
./demo.sh run-one                # process the first pending application on the running agent
./demo.sh run-one APP-20260923-0004   # a specific one
./demo.sh generate 5             # add five synthetic applications to the origination mock
```

`run-one` execs `python -m screening_agent --application <id>` inside
`brutor-demo-screening-agent` (the agent container must be up; the default id
is the first row of `applications_list_pending`, fetched with a raw JSON-RPC
call to `http://127.0.0.1:3014/mcp`). `generate N` execs
`python -m applications_mcp.generate --count N` inside
`brutor-demo-applications-mcp`; the next tick, or `run-one`, picks them up.

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
provisioning change that moves a model from a direct binding to an inherited
one, either wait or `docker restart brutor-gateway-core` before the next run.

## Residency

`--residency` sets the tenant profile to `allowed_regions: [eu-west-1,
eu-central-1], block_global: true`. It is off by default because the OpenAI
and Anthropic providers in the trial are `global`; with `block_global` the
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

- Admin API on the control plane: login `POST /v1/admin-users/tenant/login`,
  then the `/v1/admin/*` routes named in the step table above, including
  `POST /v1/admin/end-users`, `POST /v1/admin/end-user-groups`,
  `POST /v1/admin/end-user-groups/{id}/members` and
  `POST /v1/admin/resource-groups/{gid}/end-user-groups` for the underwriter.
- Portal API on the core proxy (used by the underwriter, not by this repo):
  `POST /v1/portal/auth/login`, `GET /v1/portal/approvals`,
  `POST /v1/portal/approvals/{id}/approve|reject`.
- Gateway: `GET /health` and `GET /.well-known/brutor-evidence-keys.json`.
- The demo containers' `GET /health` and the fraud agent's
  `GET /.well-known/agent-card.json`.
