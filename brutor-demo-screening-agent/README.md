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
2. retries any hand-off to manual review that did not go through yet,
3. lists applications with status `received` (`applications_list_pending`, in the tick's
   own run `bds-tick-<ulid>`, step `poll_pending`), paging on with the cursor `after` past
   held and skipped applications until it has `MAX_PER_TICK` to screen, and closes the tick
   run on one more listing call (`limit` 1) with `X-Brutor-Run-End: completed` and
   `X-Brutor-Outcome-Id: no_applications_available` (nothing to screen) or
   `applications_dispatched` (work found) — both declared as no work, so a poll never
   counts as a success of the work, and the ledger never sees a run the idle sweeper has
   to close as `abandoned` (a listing that fails leaves the tick run with a failed last
   action, which the gateway sweeps as `errored`),
4. processes up to `MAX_PER_TICK` (default 5) of them sequentially, one run each, and
   stops early at the first **system** failure (see "Failures" below).

One run is the linear LangGraph graph below. The ledger vocabulary is run > step >
turn > action (ADR 0003): **steps** are phases of the task (`gather`, `assess`, `decide`),
**turns** are model passes (`t1` covers the four reads and the classifier call they feed,
`t2` covers the fraud screen, the drafter call and the record it triggers), and each node
makes one **action**. A completed run therefore reads 3 steps, 3 turns and 9 actions (10
when held for an underwriter, because of the note): the ninth action is the fraud screener's own model
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
| 8 | `record` (decide / t2) | MCP `applications_set_recommendation` (+ `applications_add_note` on a hold) | the last call closes the run with its declared outcome |

The deterministic rules in `rules.py` are applied after the drafter and override it; the
rationale states which rule fired. The drafter is told the policy floor before it writes,
so the letter matches the recorded outcome. Every letter ends with the Art 50 disclosure
sentence; `prompts.ensure_disclosure` appends it if the model left it out. No LLM call
sends `temperature` or `top_p`.

**Human oversight.** `applications_set_recommendation` is governed by an argument policy
in the gateway: a `decline` or an `amount_eur` above 25,000 answers HTTP 202
`approval_required`. The agent then adds a note ("Held for underwriter, approval id ..."),
persists the pending approval to `DATA_DIR/pending_approvals.json`, and closes the run
`completed` / `application_processed.held_for_underwriter`. On a later tick, once a human approved it in the Admin Console
(Tool Approvals), the agent re-issues the identical call with `X-Approval-Token` in a new
short run `bds-<application_id>-approval-<ulid>` with the single step
`apply_approved_decision`, closed `completed` /
`application_processed.underwriter_decision_applied`. A rejected hold (and, with
`RERAISE_MAX` set, a hold that expired too often) is handed off: `applications_hand_off`
in its own short run (`completed` / `application_processed.sent_to_manual_review`) moves
the application to
`needs_manual_review`, so it is not screened into a fresh hold on the next tick. An
approved decision that cannot be applied is kept and retried; only application-class
failures count toward its three attempts, so an outage never drops a human's approval.

Setup gives every hold a 24-hour window (the organisation's approval window; the
`timeout_seconds` in the 202 body; 300 s on platforms older than a193). When a hold
expires before an underwriter acts, the agent does **not** re-screen the application and
does not drop the hold: it re-raises it by re-issuing the identical
`applications_set_recommendation` call from the stored arguments in a short run
`bds-<application_id>-rehold-<ulid>` (single step `reraise_approval`, closed `completed` /
`application_processed.underwriter_hold_renewed`). The gateway answers 202 with a new approval id, which replaces the old one in
`pending_approvals.json` (`reraise_count` increments). A held application is excluded from
every tick's batch, so it costs no model calls while it waits, and the Tool Approvals queue
always holds the current decision until an underwriter approves or rejects it. Only a
rejection closes the entry (hand-off, `sent_to_manual_review`; if the hand-off itself is
refused, a note, `underwriter_hold_closed`). If the re-raise is recorded outright (the
argument policy changed), the decision is treated as applied; the run already declared
`underwriter_hold_renewed` on its closing header and an MCP-only run cannot be re-closed. `RERAISE_MAX` (default
0, unlimited) caps the re-raises; when reached the application is noted and handed off.

## Gateway calls and headers

All calls: `Authorization: Bearer sk_brutor_api_...` and `X-Tenant-ID`. Attribution to
the AI System comes from the key's resource group; `X-Brutor-AI-System` is never sent.

| Header | Value | When |
|---|---|---|
| `x-brutor-run-id` | `bds-<application_id>-<ulid>`, or `bds-tick-<ulid>` for the tick run (max 128 chars), identical on every call of the run | every call inside a run |
| `X-Brutor-Turn-Id` | the model pass: `t1` (nodes 1 to 5) or `t2` (nodes 6 to 8) | every call inside a run |
| `X-Brutor-Turn-Seq` | `1` or `2` | every call inside a run |
| `X-Brutor-Step-Id` | the phase: `gather` (nodes 1 to 4), `assess` (5 to 6), `decide` (7 to 8); never the node name | every call inside a run |
| `X-Brutor-Step-Name` | `Gather the application facts`, `Assess risk and fraud`, `Decide and record` | every call inside a run |
| `traceparent` | `00-<32 hex trace id per run>-<16 hex span id per call>-01` | every call inside a run |
| `X-Brutor-Run-End` | a literal terminal state: `completed`, `errored`, ... (never `true`; `blocked_policy` no longer exists) | the last call of the run only |
| `X-Brutor-Outcome-Id` | a declared outcome from `screening_agent/outcomes.json`, e.g. `application_processed.loan_approved` (see "Declared outcomes") | alongside `X-Brutor-Run-End: completed` only |
| `X-Approval-Token` | the one-time token from the approval poll | the `apply_approved_decision` call only |

Housekeeping: the tick's `applications_list_pending` calls are the tick's own run
`bds-tick-<ulid>` (above); the approval polls are `GET /v1/portal/...` calls, not proxied
actions, and carry no run headers. `X-Brutor-Run-Outcome` (RFC 0010's four fixed values)
was removed by RFC 0025 and is never sent.

### Declared outcomes (RFC 0025)

`screening_agent/outcomes.json` is the catalogue of how a run may end; `outcomes.py`
loads it, and `brutor-demo-setup` PUTs the same file's `outcomes` list as the AI
System's catalogue. The client (`gateway.py`) refuses, before the call, an id that is
malformed (`^[a-z0-9_-]{1,48}(\.[a-z0-9_-]{1,48})?$`), not in the catalogue (an outcome
with variants is never sent bare), or sent without `X-Brutor-Run-End: completed`.

| Exit | Outcome id |
|---|---|
| `record`, recommendation `approve` / `decline` / `refer` | `application_processed.loan_approved` / `loan_rejected` / `loan_referred` |
| `record`, the recommendation was held (202), the note closes the run | `application_processed.held_for_underwriter` |
| approvals: approved decision applied | `application_processed.underwriter_decision_applied` |
| approvals: lapsed hold re-raised | `application_processed.underwriter_hold_renewed` |
| approvals: hand-off or apply impossible, a note instead | `application_processed.underwriter_hold_closed` |
| approvals (rejected, `RERAISE_MAX` reached) and the scheduler's give-up: hand-off | `application_processed.sent_to_manual_review` |
| tick, nothing to screen | `no_applications_available` (no work) |
| tick, work found and handed to per-application runs | `applications_dispatched` (no work) |
| any failed run | none |

**The fraud screener's outcome.** The gateway checks the screener's
`screening_completed.<verdict>` against the screener's contract and returns
`X-Brutor-Outcome-Id` + `X-Brutor-Outcome-Conformance` (and the pair in the final Task's
`metadata`, `brutor.outcome.id` / `brutor.outcome.conformance`). `a2a_delegate` reads
either carrier (both present must agree) and raises `DelegateDeviated` unless the
conformance is `declared` and the id is one of the dependency's expected outcomes
(`clear`, `review`, `hit`). The graph branches on that checked variant, never on the
body's `verdict` field. A deviation fails the run like an unreachable delegate: system
class `delegate_deviated`, the tick backs off.

Routes used:

| Call | Route | Notes |
|---|---|---|
| LLM (chat) | `POST {gw}/v1/proxy/llm/chat/completions` | OpenAI SDK with `base_url={gw}/v1/proxy/llm`, per-call `extra_headers`, `messages`, `response_format: {"type": "json_object"}`. The `x-brutor-run-id` **response** header is the derived root, kept for the fallback close. |
| LLM (responses) | `POST {gw}/v1/proxy/llm/responses` | Same SDK and headers; `input` = the same messages as `[{role, content}]` items, `text: {"format": {"type": "json_object"}}`. The proxy accepts `model`, `input` (string or items), `instructions`, `max_output_tokens`, `stream` and passes other fields through; it returns the same `x-brutor-run-id` header. The text is read from `output_text`, else from `output[].content[]` items of type `output_text`. |
| MCP | `POST {gw}/v1/proxy/mcp/{server_id}` | raw JSON-RPC `tools/call`, `Accept: application/json, text/event-stream`. 202 `approval_required` = hold, 403 = policy block, `result.isError` = tool error. |
| Skill | same, server `system-agent-skill-server-<tenant>`, tool `skills__run_script` | `{"skill_name": "affordability-check", "script": "affordability.py", "args": {...}}` |
| A2A | `POST {gw}/v1/proxy/a2a/outbound` | `{"target_card_id", "capability": "screening.fraud_sanctions", "message": {...}}`; the verdict JSON is read from `task.status.message.parts[0].text`, the verdict itself from the checked outcome (above) |
| Run close (fallback) | `POST {gw}/v1/runs/{derived_root}/end` | `{"state": "errored"}` when a node raised (`{"state": "completed", "outcome_id": ...}` if a header close did not take); only possible once an LLM call has answered |
| Approval poll | `GET {gw}/v1/portal/approvals/{id}/poll` | `{"status": "pending or approved or rejected or expired", "approval_token"?}` |

**Two model APIs.** Each model is called on either the chat route or the responses route,
chosen by `CLASSIFIER_API` and `DRAFTER_API` (`chat` or `responses`). The drafter defaults
to `responses` because `gpt-5.5` is flagged `requires_responses_api` in the trial catalog:
the chat route answers it with HTTP 400 "Model 'gpt-5.5' only supports the Responses
endpoint". If an env value is wrong the client corrects itself once per process: a chat
call that gets that 400 is retried on the responses route with the identical headers, a
warning is logged, and the model is remembered as responses-only. Run, turn, step and
close headers are the same on both routes; neither sends `temperature` or `top_p`.

Terminal states: `completed` with a declared outcome; `errored` when a node raises,
including a 403 guardrail or policy block (the gateway records the cause
`blocked_by_policy`). When a run fails before any LLM call has answered there is no
derived root to close with, the agent logs a warning, and the gateway sweeps the run
after the system's idle window (300 s in the demo) as `errored`, since its last action
failed.

## Failures

Every failed run is classified in one place, `screening_agent/failures.py` (the table of
error shapes is its module docstring; `tests/test_failures.py` drives each shape through
the real gateway client):

| Class | Kinds | Examples the agent sees | Effect |
|---|---|---|---|
| **application** | `content_blocked`, `rejected_request`, `tool_error`, `malformed_output`, `unexpected` | 403 `guardrail_blocked` / `semantic_policy_blocked` / argument-policy deny (JSON-RPC -32000); 400/413/422; a tool error about this application; unusable model output; a malformed record | counted per application; after 3 the application is handed off to `needs_manual_review` |
| **system** | `unreachable`, `server_error`, `rate_limited`, `approval_hold`, `governance_refusal`, `unavailable`, `delegate_failed`, `delegate_deviated` | DNS or connection errors; 5xx incl. 503 `gateway_resilience_gated`; 429 incl. `credit_balance_exhausted` and `run_cap_reached`; `skill_error_202: approval_required`; `autonomy_denied`, run aborted, 401, a 403 without a content verdict; 404/409; A2A task FAILED; a delegate outcome that is not declared or not expected | never counted; the tick stops; exponential backoff |

**Application failures.** The application stays `received` and is retried on the next
tick. Application-class attempts are counted in `DATA_DIR/retries.json`; after 3 the agent
calls `applications_hand_off` (own short run `bds-<id>-giveup-<ulid>`, step `give_up`,
`completed` / `application_processed.sent_to_manual_review`), which moves the application to the terminal status
`needs_manual_review` with the reason as a note, so it leaves the pending set for good.
That is what stops the prompt-injection sample from looping once the `mcp_output`
guardrail blocks it. If the hand-off call itself fails, the application stays on the
local skip list and the hand-off is retried at the start of each tick.

**System failures.** Nothing is counted against any application. The tick stops at the
first one and the next `2^(streak-1) - 1` ticks are skipped entirely (no gateway calls),
capped at `BACKOFF_MAX_TICKS` (6): with 10-minute ticks the probes run at +10, +20, +40,
+80 minutes, then every 70 minutes until the system answers. Any run that completes, or
fails on the application's own account, clears the backoff. The application that
tripped it is probed last next time, so a failure misclassified as the system's cannot
hold the rest of the queue behind it. Logs say `SYSTEM failure (system/<kind>)` and
`backing off`; `GET :9201/status` shows `backoff` (`streak`, `ticks_left`,
`last_failure`) and `system_failures`.

**No starvation.** Held (awaiting an underwriter) and skipped applications stay out of
the batch but cannot fill it: the listing pages on with `after` (`LIST_LIMIT` rows per
page, at most `LIST_MAX_PAGES` pages) until `MAX_PER_TICK` screenable applications are
found. Handed-off applications are not pending at all.

**Re-queue.** `./demo.sh requeue APP-... [APP-...]` or `./demo.sh requeue --all-skipped`
moves applications back from `needs_manual_review` to `received` in the origination system
(`python -m applications_mcp.requeue`) and clears them from the agent's skip list
(`python -m screening_agent --requeue ... | --requeue-all-skipped`); the running scheduler
re-reads the file and screens them on its next tick with fresh attempts. An application
that was handed off and shows up as pending again is released automatically.

**Tracker file.** `retries.json` v2 holds `attempts` (application-class only),
`skipped` (reason, `failure_kind`, attempts, `handed_off`) and a capped `released`
history. A v1 file (written before this classification) is migrated on read: skips whose
recorded reason is system-class are released and screened again, the rest are handed
off; the original is kept as `retries.v1.json`.

Logging: one line per gateway call (`gateway kind=... target=... status=... node=...
step=... turn=... run=... ms=...`) and one line per run close (`run=... state=... outcome=...`).

The housekeeping runs (tick poll `poll_pending`, re-raise `reraise_approval`, approval
apply `apply_approved_decision`, hand-offs and notes `approval_closed` / `give_up`) are single-step,
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
| `LIST_LIMIT` | `50` | page size of `applications_list_pending` (the server caps it at 100) |
| `LIST_MAX_PAGES` | `20` | at most this many listing pages per tick while looking past held and skipped applications |
| `BACKOFF_MAX_TICKS` | `6` | after a system failure, at most this many ticks are skipped between probes |
| `RERAISE_MAX` | `0` | how many times an expired hold is re-raised; 0 = unlimited |
| `DATA_DIR` | `/data` | pending approvals, retry tracker |
| `HEALTH_PORT` | `9201` | health server port |
| `LOG_LEVEL` | `INFO` | logging level |
| `BRUTOR_AGENT_BUILD` | empty | build id of this release (e.g. git SHA), sent as `X-Brutor-Agent-Build`; baked into the image by the Dockerfile build arg of the same name |

`brutor-demo-setup/setup.py` writes all of these into `.demo.env`, except
`BRUTOR_AGENT_BUILD`, which comes from the image build.

Every gateway call names this agent's implementation and release (RFC 0023):
`X-Brutor-Agent-Name: brutor-demo-screening-agent`, `X-Brutor-Agent-Version` = the
installed distribution version (`project.version` in `pyproject.toml`, the only place
it is written) and `X-Brutor-Agent-Build` when set; MCP calls also carry
`params._meta["io.modelcontextprotocol/clientInfo"] = {name, version}`. The agent must
be installed (`pip install .` or `-e .`) so its version can be read; see
`screening_agent/identity.py` and ../DESIGN.md section 8.

## Run locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
set -a; . ../brutor-demo-setup/.demo.env; set +a
export BRUTOR_GATEWAY_URL=http://localhost:8100 DATA_DIR=./data

python -m screening_agent --once                       # one tick, then exit
python -m screening_agent --application APP-20260923-001   # one application, then exit (refuses a held one; --force overrides)
python -m screening_agent                              # run forever, health on :9201
python -m screening_agent --requeue APP-20260923-001   # take it off the skip list (also: --requeue-all-skipped)
```

`GET :9201/health` answers `{"status": "ok"}`; `GET :9201/status` shows the last tick
time, applications processed, runs completed / escalated / errored / blocked, pending
approvals, held applications, re-raised holds, hand-offs, the skip list and the backoff
state (`backoff`, `system_failures`, `ticks_backed_off`).

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
approved decision with the token. `tests/test_failures.py` pins the failure classification
for every kind (including the exact errors seen live on 2026-09-27/28);
`tests/test_scheduler.py` proves that system failures never skip and back off, that
application failures hand off after three attempts, that a full page of held and skipped
applications does not starve new ones, that re-queueing works and that a v1 tracker file
migrates without loss.

## What it does not do

- It has no loop. The graph is linear with a fixed node order, so a run can never end
  `exhausted`. The envelope caps (`max_llm_calls_per_run` 6) are far above the two
  model calls a run makes.
- It never sends `X-Brutor-Run-End: true`; the state is always a literal.
- It does not use session headers; the agent has no conversation.
- It does not retry a failed application forever; three failures and it is handed off.
- It does not detect real fraud or evaluate real people. Everything is synthetic; see
  DESIGN.md section 11.
