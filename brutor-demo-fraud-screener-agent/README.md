# brutor-demo-fraud-screener-agent

The **Fraud and Sanctions Screener** of the Brutor Demo System: an A2A 1.0 remote agent
that the screening agent delegates to, through the Brutor gateway, in step 6
(`fraud_screen`) of every screening run. It is its own AI System
(`brutor-demo-fraud-screener`, kind `agent`, EU AI Act tier minimal: fraud detection is
carved out of Annex III 5(b)).

It is a **mock**. It matches a synthetic sanctions list, applies two velocity
heuristics, and makes one model call for fraud indicators in the stated loan purpose.
It does not detect real fraud.

Design: [../DESIGN.md](../DESIGN.md) (sections 5.4 and 8). System overview:
[../README.md](../README.md).

## Routes

| Route | Purpose |
|---|---|
| `GET /health` | liveness, `{"status": "ok"}` |
| `GET /.well-known/agent-card.json` | the A2A 1.0 agent card (below) |
| `POST /message:send` | the A2A `message:send` call |
| `POST /message%3Asend` | the same handler; the gateway percent-encodes the colon and Starlette treats the two as different routes |

No authentication: the gateway sends none, and the docker network is the boundary.

The card carries `protocolVersion "1.0"`, `url` and `supportedInterfaces[0].url` equal
to `A2A_PUBLIC_URL` (`protocolBinding "JSONRPC"`), `capabilities {"streaming": false}`,
`defaultInputModes` / `defaultOutputModes` `["text"]`, `data_classification ["PII"]`,
and one skill with `id` = `name` = `screening.fraud_sanctions` and tags
`["screening", "fraud", "sanctions"]`. `brutor-demo-setup` fetches this live card and
registers it with the gateway so the two never drift.

## Request and response

The handler reads `body["message"]`, falling back to `body["params"]["message"]`, and
takes the first text part (`parts[i].text`, or `parts[i].kind == "text"`). The text is
JSON:

```json
{"applicant_id": "CUST-1001", "full_name": "Maja Lindholm", "date_of_birth": "1987-04-12",
 "country": "SE", "purpose": "Kitchen renovation", "amount_eur": 12000,
 "bureau": {"inquiries_6m": 1, "delinquencies_24m": 0, "open_credit_lines": 2}}
```

Logic, in order:

1. **Sanctions list.** `full_name` is normalized (casefold, whitespace collapsed,
   stripped) and compared with the twelve synthetic names in
   `fraud_screener/screening.py`. The list is shared with the applications generator, so
   about five percent of demo applicants hit it.
2. **Heuristics.** `inquiries_6m >= 6` or `delinquencies_24m >= 2` forces `review`.
3. **Model.** One chat completion through the gateway with `CLASSIFIER_MODEL`, JSON
   output `{"fraud_indicators": [...], "suspicious": bool}`, asking for fraud indicators
   in the purpose text (which is handed over as data, never as instructions).

Verdict: `hit` if the sanctions list matched, `review` if a heuristic fired or the model
flagged something, else `clear`. If the model call fails for any reason (gateway down,
403, timeout, unreadable answer, no API key) the agent degrades to sanctions plus
heuristics and says so with a reason `llm_unavailable: heuristics only (...)`; the
verdict is still returned, with `model_used: null`.

Response, exactly:

```json
{"task": {"id": "<uuid>", "contextId": "<uuid>",
  "status": {"state": "TASK_STATE_COMPLETED",
    "message": {"messageId": "<uuid>", "role": "ROLE_AGENT",
      "parts": [{"kind": "text", "text": "{\"verdict\": \"clear\", \"sanctions_match\": false, \"reasons\": [\"...\"], \"model_used\": \"gpt-5.2\"}"}]}}}}
```

## The delegation echo rule

The gateway signs a delegation chain when it forwards the screening agent's A2A call:
`x-brutor-delegation-root`, `-parent`, `-depth`, `-sig`, and, because the caller is an
agent identity, `-actor` and `-subject`. On its own model call this agent:

- copies **every** inbound header whose name starts with `x-brutor-delegation-`,
  unchanged (values are never rewritten, the HMAC would not verify);
- sends **no** `x-brutor-run-id` (a signed chain places the action in the caller's run at
  depth 1 with `trace_continuity=verified`; asserting a run id would be ignored at best);
- adds `X-Brutor-Turn-Id` (`t01-fraud_llm-<suffix>`, one pass of its own loop) and
  `X-Brutor-Turn-Seq: 1`, but **no** `X-Brutor-Step-Id` / `X-Brutor-Step-Name`: steps are
  the phases of the task as the orchestrating agent declares them, a delegate cannot know
  which phase it serves, and the ledger counts distinct step and turn ids across all
  depths, so a delegate step would inflate the caller's step count. A completed screening
  run therefore reads 3 steps, 3 turns (two of the screening agent, one of this agent)
  and 9 actions;
- authenticates with its **own** key (`Authorization: Bearer sk_brutor_api_...`, bound to
  the `brutor-demo-fraud-screener` identity) plus `X-Tenant-ID`, so the action is
  attributed to the fraud screener system and that system appears in the caller run's
  `via_system_ids`.

The inbound `Authorization` header (if any) and the inbound `x-brutor-run-id` are never
forwarded. The agent answers promptly: chains older than about 11 minutes are rejected
by the gateway, and the gateway's outbound timeout is 30 s.

Each request logs `x-correlation-id`, the delegation depth, whether a chain was present,
the applicant id and the verdict. The model call logs one line with status and latency.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `BRUTOR_GATEWAY_URL` | `http://core:8100` | the Core Proxy |
| `FRAUD_BRUTOR_API_KEY` (falls back to `BRUTOR_API_KEY`) | empty (model step skipped) | `sk_brutor_api_...` bound to `brutor-demo-fraud-screener` |
| `BRUTOR_TENANT_ID` | `default` | tenant |
| `CLASSIFIER_MODEL` | `gpt-5.2` | model for the fraud-indicator call |
| `A2A_PUBLIC_URL` | `http://brutor-demo-fraud-screener-agent:9200` | the URL in the card, as the gateway reaches it |
| `A2A_BIND_HOST` | `0.0.0.0` | bind address |
| `A2A_PORT` | `9200` | port |
| `LLM_TIMEOUT_SECONDS` | `20` | model call timeout |
| `LOG_LEVEL` | `INFO` | logging level |

## Run locally

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
export BRUTOR_GATEWAY_URL=http://localhost:8100 BRUTOR_API_KEY=sk_brutor_api_... A2A_PUBLIC_URL=http://localhost:9200
python -m fraud_screener

curl -s localhost:9200/.well-known/agent-card.json
curl -s -X POST localhost:9200/message:send -H 'content-type: application/json' -d '{"message": {"parts": [{"kind": "text", "text": "{\"full_name\": \"Sigrid Voss\", \"purpose\": \"car\", \"amount_eur\": 9000, \"bureau\": {\"inquiries_6m\": 1, \"delinquencies_24m\": 0}}"}]}}'
```

## Run in Docker

```bash
docker build -t brutor-demo-fraud-screener-agent .
docker run -d --name brutor-demo-fraud-screener-agent \
  --network brutor-network \
  --env-file ../brutor-demo-setup/.demo.env \
  -p 127.0.0.1:9200:9200 \
  brutor-demo-fraud-screener-agent
```

The container name is the host in the card URL, so the gateway can reach it over
`brutor-network`. Start it before `setup.py` runs: the gateway fetches the card at
registration time.

## Tests

```bash
python -m pytest
```

No network. The tests cover the card, both `message:send` route spellings, the
`params.message` fallback, the exact response shape, the delegation headers echoed onto
the model call (with `respx` intercepting the gateway route) and the absence of
`x-brutor-run-id` there, a sanctions hit, the heuristics, the model flag, and the
degradation when the model call fails.

## What it does not do

- It is a mock: a synthetic list of twelve names and two thresholds. It does not detect
  real fraud or screen against any real sanctions list.
- It does not stream (`capabilities.streaming` is false) and has no `message:stream`.
- It does not delegate onward, so the chain never goes deeper than depth 1 from it.
- It never asserts a run of its own; its only governed action belongs to the caller's run.
