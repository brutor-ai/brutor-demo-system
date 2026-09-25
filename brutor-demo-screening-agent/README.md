# brutor-demo-screening-agent

The screening agent of the **Brutor Demo System**: a LangGraph agent that pre-screens
consumer loan applications for the fictional lender Borealis Consumer Finance AB. It is
the AI System `brutor-demo-system` (kind `agent`, EU AI Act high risk, Annex III 5(b)).
Every governed action it takes goes through the Brutor Core Proxy and carries the run,
turn and step headers, so one application lands in the ledger as exactly one run with
eight steps.

Design: [../DESIGN.md](../DESIGN.md) (sections 4, 5.5, 8, 9). System overview:
[../README.md](../README.md).

## What it does

Every `TICK_SECONDS` (default 600) the agent:

1. resolves pending underwriter approvals (`GET /v1/portal/approvals/{id}/poll`),
2. lists applications with status `received` (`applications_list_pending`, in its own short
   run `bds-tick-<ulid>` with the single step `poll_pending`, closed on that same call with
   `X-Brutor-Run-End: completed` / `X-Brutor-Run-Outcome: resolved`, so the ledger never
   sees a one-call run the idle sweeper has to close as `abandoned`),
3. processes up to `MAX_PER_TICK` (default 5) of them sequentially, one run each.

One run is the linear LangGraph graph below. The ledger vocabulary is run > step >
turn > action (ADR 0003): **steps** are phases of the task (`gather`, `assess`, `decide`),
**turns** are model passes (`t1` covers the four reads and the classifier call they feed,
`t2` covers the fraud screen, the drafter call and the record it triggers), and each node
makes one **action**. A completed run therefore reads 3 steps, 3 turns and 9 actions (10
when escalated, because of the note): the ninth action is the fraud screener's own model
call, which joins at depth 1 with a turn of its own and no step, since steps are the
orchestrating agent's phases. The node name is not a header; it appears as `node=` in the
log line.

| # | Node (step / turn) | Gateway call | Effect on the decision |
|---|---|---|---|
| 1 | `intake` (gather / t1) | MCP `applications_get` | loads the application |
| 2 | `verify_identity` (gather / t1) | MCP `bureau_verify_identity` | `verified=false` forces `refer` |
| 3 | `credit_report` (gather / t1) | MCP `bureau_get_report` | sensitive read, sealed as evidence |
| 4 | `affordability` (gather / t1) | Skills MCP `skills__run_script` (`affordability-check` / `affordability.py`) | `unaffordable` forces `decline` |
| 5 | `classify` (assess / t1) | LLM, classifier model (`gpt-5.2`), JSON output | `high` forces at least `refer` |
| 6 | `fraud_screen` (assess / t2) | A2A outbound to the Fraud and Sanctions Screener | `hit` forces `decline`, `review` forces at least `refer` |
| 7 | `decide` (decide / t2) | LLM, drafter model (`gpt-5.5`), JSON output | recommendation, rationale, customer letter |
| 8 | `record` (decide / t2) | MCP `applications_set_recommendation` (+ `applications_add_note` on a hold) | the last call closes the run |

The deterministic rules in `rules.py` are applied after the drafter and override it; the
rationale states which rule fired. The drafter is told the policy floor before it writes,
so the letter matches the recorded outcome. Every letter ends with the Art 50 disclosure
sentence; `prompts.ensure_disclosure` appends it if the model left it out. No LLM call
sends `temperature` or `top_p`.

**Human oversight.** `applications_set_recommendation` is governed by an argument policy
in the gateway: a `decline` or an `amount_eur` above 25,000 answers HTTP 202
`approval_required`. The agent then adds a note ("Held for underwriter, approval id ..."),
persists the pending approval to `DATA_DIR/pending_approvals.json`, and closes the run
`completed` / `escalated`. On a later tick, once a human approved it in the Admin Console
(Tool Approvals), the agent re-issues the identical call with `X-Approval-Token` in a new
short run `bds-<application_id>-approval-<ulid>` with the single step
`apply_approved_decision`, closed `completed` / `resolved`. Rejected or expired holds get
a note in their own short run (`completed` / `handed_off`) and are dropped.

The trial's approval timeout is 300 s (the `timeout_seconds` in the 202 body). When a hold
expires before an underwriter acts, the agent does **not** re-screen the application and
does not drop the hold: it re-raises it by re-issuing the identical
`applications_set_recommendation` call from the stored arguments in a short run
`bds-<application_id>-rehold-<ulid>` (single step `reraise_approval`, closed `completed` /
`escalated`). The gateway answers 202 with a new approval id, which replaces the old one in
`pending_approvals.json` (`reraise_count` increments). A held application is excluded from
every tick's batch, so it costs no model calls while it waits, and the Tool Approvals queue
always holds the current decision until an underwriter approves or rejects it. Only a
rejection closes the entry (note, `handed_off`). If the re-raise is recorded outright
(the argument policy changed), the decision is treated as applied. `RERAISE_MAX` (default
0, unlimited) caps the re-raises; when reached the application is noted and handed off.

## Gateway calls and headers

All calls: `Authorization: Bearer sk_brutor_api_...` and `X-Tenant-ID`. Attribution to
the AI System comes from the key's resource group; `X-Brutor-AI-System` is never sent.

| Header | Value | When |
|---|---|---|
| `x-brutor-run-id` | `bds-<application_id>-<ulid>` (max 128 chars), identical on every call of the run | every call inside a run |
| `X-Brutor-Turn-Id` | the model pass: `t1` (nodes 1 to 5) or `t2` (nodes 6 to 8) | every call inside a run |
| `X-Brutor-Turn-Seq` | `1` or `2` | every call inside a run |
| `X-Brutor-Step-Id` | the phase: `gather` (nodes 1 to 4), `assess` (5 to 6), `decide` (7 to 8); never the node name | every call inside a run |
| `X-Brutor-Step-Name` | `Gather the application facts`, `Assess risk and fraud`, `Decide and record` | every call inside a run |
| `traceparent` | `00-<32 hex trace id per run>-<16 hex span id per call>-01` | every call inside a run |
| `X-Brutor-Run-End` | a literal terminal state: `completed`, `errored`, `blocked_policy`, ... (never `true`) | the last call of the run only |
| `X-Brutor-Run-Outcome` | `resolved`, `escalated` or `handed_off` | alongside `X-Brutor-Run-End` only |
| `X-Approval-Token` | the one-time token from the approval poll | the `apply_approved_decision` call only |

Housekeeping outside application runs: the tick's `applications_list_pending` call is its
own closed run `bds-tick-<ulid>` (above); the approval polls are `GET /v1/portal/...`
calls, not proxied actions, and carry no run headers.

Routes used:

| Call | Route | Notes |
|---|---|---|
| LLM (chat) | `POST {gw}/v1/proxy/llm/chat/completions` | OpenAI SDK with `base_url={gw}/v1/proxy/llm`, per-call `extra_headers`, `messages`, `response_format: {"type": "json_object"}`. The `x-brutor-run-id` **response** header is the derived root, kept for the fallback close. |
| LLM (responses) | `POST {gw}/v1/proxy/llm/responses` | Same SDK and headers; `input` = the same messages as `[{role, content}]` items, `text: {"format": {"type": "json_object"}}`. The proxy accepts `model`, `input` (string or items), `instructions`, `max_output_tokens`, `stream` and passes other fields through; it returns the same `x-brutor-run-id` header. The text is read from `output_text`, else from `output[].content[]` items of type `output_text`. |
| MCP | `POST {gw}/v1/proxy/mcp/{server_id}` | raw JSON-RPC `tools/call`, `Accept: application/json, text/event-stream`. 202 `approval_required` = hold, 403 = policy block, `result.isError` = tool error. |
| Skill | same, server `system-agent-skill-server-<tenant>`, tool `skills__run_script` | `{"skill_name": "affordability-check", "script": "affordability.py", "args": {...}}` |
| A2A | `POST {gw}/v1/proxy/a2a/outbound` | `{"target_card_id", "capability": "screening.fraud_sanctions", "message": {...}}`; the verdict JSON is read from `task.status.message.parts[0].text` |
| Run close (fallback) | `POST {gw}/v1/runs/{derived_root}/end` | `{"state": "errored" or "blocked_policy"}` when a node raised; only possible once an LLM call has answered |
| Approval poll | `GET {gw}/v1/portal/approvals/{id}/poll` | `{"status": "pending or approved or rejected or expired", "approval_token"?}` |

**Two model APIs.** Each model is called on either the chat route or the responses route,
chosen by `CLASSIFIER_API` and `DRAFTER_API` (`chat` or `responses`). The drafter defaults
to `responses` because `gpt-5.5` is flagged `requires_responses_api` in the trial catalog:
the chat route answers it with HTTP 400 "Model 'gpt-5.5' only supports the Responses
endpoint". If an env value is wrong the client corrects itself once per process: a chat
call that gets that 400 is retried on the responses route with the identical headers, a
warning is logged, and the model is remembered as responses-only. Run, turn, step and
close headers are the same on both routes; neither sends `temperature` or `top_p`.

Terminal states: `completed` + `resolved` on the normal path; `completed` + `escalated`
on an approval hold; `errored` when a node raises; `blocked_policy` on a 403 guardrail or
policy block. When a run fails before any LLM call has answered there is no derived root
to close with, the agent logs a warning, and the gateway sweeps the run as `abandoned`
after the system's idle window (300 s in the demo).

Failed applications stay `received` and are retried on the next tick. Attempts are
tracked per application in `DATA_DIR/retries.json`; after 3 failures the agent adds a
note (own short run, `completed` / `handed_off`), puts the application on a local skip
list and never touches it again. That is what stops the prompt-injection sample from
looping forever once the `mcp_output` guardrail blocks it.

Logging: one line per gateway call (`gateway kind=... target=... status=... node=...
step=... turn=... run=... ms=...`) and one line per run close (`run=... state=... outcome=...`).

The housekeeping runs (tick poll `poll_pending`, re-raise `reraise_approval`, approval
apply `apply_approved_decision`, notes `approval_closed` / `give_up`) are single-step,
single-turn (`t1`), single-action runs.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `BRUTOR_GATEWAY_URL` | `http://core:8100` | the Core Proxy |
| `BRUTOR_API_KEY` | required | `sk_brutor_api_...` bound to the `brutor-demo-screening-worker` identity |
| `BRUTOR_TENANT_ID` | `default` | tenant |
| `APPLICATIONS_MCP_SERVER_ID` | required | DB id of the Loan Applications MCP server |
| `BUREAU_MCP_SERVER_ID` | required | DB id of the Credit Bureau MCP server |
| `SKILLS_MCP_SERVER_ID` | `system-agent-skill-server-default` | the skills system server |
| `FRAUD_CARD_ID` | required | `agentcard-...` id of the fraud screener card |
| `FRAUD_CAPABILITY` | `screening.fraud_sanctions` | capability requested on the card |
| `CLASSIFIER_MODEL` | `gpt-5.2` | model name for `classify` |
| `DRAFTER_MODEL` | `gpt-5.5` | model name for `decide` |
| `CLASSIFIER_API` | `chat` | `chat` or `responses`: which route the classifier is called on |
| `DRAFTER_API` | `responses` | `chat` or `responses`: which route the drafter is called on (gpt-5.5 is Responses-only) |
| `TICK_SECONDS` | `600` | tick interval |
| `MAX_PER_TICK` | `5` | applications per tick, chosen oldest-first among applications that are neither held nor skipped |
| `LIST_LIMIT` | `50` | how many pending applications a tick lists before excluding held and skipped ones |
| `RERAISE_MAX` | `0` | how many times an expired hold is re-raised; 0 = unlimited |
| `DATA_DIR` | `/data` | pending approvals, retry tracker |
| `HEALTH_PORT` | `9201` | health server port |
| `LOG_LEVEL` | `INFO` | logging level |

`brutor-demo-setup/setup.py` writes all of these into `.demo.env`.

## Run locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
set -a; . ../brutor-demo-setup/.demo.env; set +a
export BRUTOR_GATEWAY_URL=http://localhost:8100 DATA_DIR=./data

python -m screening_agent --once                       # one tick, then exit
python -m screening_agent --application APP-20260923-001   # one application, then exit (refuses a held one; --force overrides)
python -m screening_agent                              # run forever, health on :9201
```

`GET :9201/health` answers `{"status": "ok"}`; `GET :9201/status` shows the last tick
time, applications processed, runs completed / escalated / errored / blocked, pending
approvals, held applications, re-raised holds and the skip list.

## Run in Docker

```bash
docker build -t brutor-demo-screening-agent .
docker run -d --name brutor-demo-screening-agent \
  --network brutor-network \
  --env-file ../brutor-demo-setup/.demo.env \
  -v brutor-demo-screening-data:/data \
  -p 127.0.0.1:9201:9201 \
  brutor-demo-screening-agent
```

The container joins the trial's external `brutor-network` and addresses the gateway as
`http://core:8100`. `docker compose` in `brutor-demo-setup` does the same.

## Tests

```bash
python -m pytest
```

No network. The tests cover `rules.py`, the header builder (run, turn, step and close
headers; a `true` run end is rejected; length limits; the same run id across calls), the
pending approval store and its resolution, and the graph end to end against a fake
gateway (`tests/fake_gateway.py`, an `httpx.MockTransport`) that records every call's
headers and asserts that all calls of a run share the run id, that the last call carries
`X-Brutor-Run-End`, and that the approval path escalates, persists and later applies the
approved decision with the token.

## What it does not do

- It has no loop. The graph is linear with a fixed node order, so a run can never end
  `exhausted`. The envelope caps (`max_llm_calls_per_run` 6) are far above the two
  model calls a run makes.
- It never sends `X-Brutor-Run-End: true`; the state is always a literal.
- It does not use session headers; the agent has no conversation.
- It does not retry a failed application forever; three failures and it is handed off.
- It does not detect real fraud or evaluate real people. Everything is synthetic; see
  DESIGN.md section 11.
