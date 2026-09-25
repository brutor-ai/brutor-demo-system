# brutor-demo-applications-mcp

Mock **loan origination system** for the [Brutor Demo System](../README.md), exposed as
an MCP server. It is the system of record the screening agent reads applications from
and writes recommendations back to. Design: [../DESIGN.md](../DESIGN.md), section 5.1.

Everything it serves is synthetic. Names are assembled from two word lists of Nordic and
European given names and family names; no real person, lender or dataset is involved.

## How it fits the system

```
brutor-demo-screening-agent ──> Brutor Core Proxy ──> brutor-demo-applications-mcp:3014/mcp
   (LangGraph, every 10 min)     /v1/proxy/mcp/{id}         state: /data/applications.json
```

The agent never talks to this server directly. Every call goes through the Brutor Core
Proxy, which enforces grants, capability filters, the argument policy that holds
declines and large loans for an underwriter, guardrails and limits, and seals evidence.
The proxy addresses the container by name on the `brutor-network` docker network.

Stack: Python 3.12, the official MCP Python SDK (`mcp` 1.x, `FastMCP`) over streamable
HTTP with `stateless_http=True` and `json_response=True`. Both flags are mandatory: the
gateway posts raw JSON-RPC `tools/call` requests with no `initialize` handshake and
expects a JSON body back rather than an SSE stream.

## Tools

Bare names, exactly as the gateway exposes them.

| Tool | Read-only | Arguments | Returns |
|---|---|---|---|
| `applications_list_pending` | yes | `limit` (int, default 10, max 100) | `[{application_id, received_at, amount_eur, purpose_short}]`, status `received`, oldest first |
| `applications_get` | yes | `application_id` | The full record (below), or `{ok: false, error: "not_found"}` |
| `applications_set_recommendation` | **no** | `application_id`, `recommendation` (approve, refer, decline), `amount_eur`, `rationale`, `risk_band`, `affordability_class`, `fraud_verdict`, `customer_letter` | `{ok: true, application_id, status: "screened", recommendation, screened_at}`; `{ok: false, error: "already_screened"}` on a second call; `{ok: false, error: "invalid_recommendation"}` for anything but the three values |
| `applications_add_note` | no | `application_id`, `note` | `{ok: true, application_id, notes_count}` |
| `applications_stats` | yes | none | `{total, by_status, by_recommendation, generated_total, as_of}` |

Errors are returned as ordinary results with `ok: false` (never as JSON-RPC errors), so
the agent can branch on them and the gateway still records the call.

Every tool puts its JSON in `result.content[0].text`. `applications_list_pending`
returns the array as a single text item on purpose: FastMCP would otherwise split a
Python list into one content item per element (and an empty list into no content), and
the gateway contract in DESIGN.md section 8 is "the JSON string is in
`result.content[0].text`". The dict-returning tools also carry `structuredContent`.

### Why `amount_eur` is an argument although the record already has it

The gateway's argument policy ("Adverse or large decisions need an underwriter",
DESIGN.md 7.2) inspects the **call arguments**, not the stored record. It returns
`approval_required` when `recommendation == "decline"` or `amount_eur > 25000`. The agent
therefore passes `amount_eur` explicitly, and the server records it under
`screening.amount_eur` next to the original `requested_amount_eur`.

### Read-only annotations

`applications_list_pending`, `applications_get` and `applications_stats` carry
`readOnlyHint: true`; the two writers carry `readOnlyHint: false`. The gateway uses the
annotation to decide which calls are **sensitive reads** on a system flagged
`sensitive_data`, and seals those as evidence. Without the hint the Evidence Ledger would
not distinguish a read of an applicant's personal data from a write, so the annotations
are part of the contract.

### Application record

```json
{
  "application_id": "APP-20260923-0001",
  "received_at": "2026-09-23T08:00:00Z",
  "applicant": {
    "applicant_id": "CUST-483920",
    "full_name": "Elin Bergstrom",
    "date_of_birth": "1987-04-12",
    "country": "SE",
    "email": "elin.bergstrom17@example.com",
    "employment_status": "employed"
  },
  "requested_amount_eur": 14300,
  "term_months": 48,
  "purpose": "Consolidating two existing consumer loans into a single monthly payment.",
  "monthly_income_eur": 4250,
  "monthly_expenses_eur": 2130,
  "existing_debt_monthly_eur": 310,
  "status": "received",
  "recommendation": null,
  "notes": [],
  "screened_at": null
}
```

After `applications_set_recommendation` the record gains `status: "screened"`,
`recommendation`, `screened_at` and a `screening` object with the rationale, bands,
verdict, `amount_eur` and the customer letter. Notes are `{at, note}` entries.

## The generator

A background thread creates applications so the demo has a steady feed:

* **3** applications at first start when the store is empty;
* **`GENERATE_MIN_PER_INTERVAL` to `GENERATE_MAX_PER_INTERVAL`** applications (default
  2 to 3, so about 360 a day) every `GENERATE_INTERVAL_SECONDS` (default 600) after that.

The volume and the mix are tuned for two platform signals (retuned 2026-09-25). The
baselines learn "normal" from the run ledger, so screening runs must outnumber the
agent's one-action tick and hold follow-up runs: 2 to 3 applications per tick does
that. And every decline (unaffordable or sanctions hit) and every loan above 25,000 EUR
is held for an underwriter; the platform reports oversight as the share of holds a
person actually resolved, so the hold share is kept near 1.7 percent, about 6 holds a
day, which one underwriter answers in the User Portal each day. The previous mix (16
percent holds at 0 to 2 per interval) left most holds unanswered.

Generation is deterministic. Application number `seq` is built from
`random.Random(f"{GENERATOR_SEED}:{seq}")` and `seq` is persisted in the store, so ids
(`APP-<yyyymmdd>-<seq>`, one global sequence) keep counting after a restart and the
content of application 17 does not depend on how many times the container restarted.

The financial fields are sampled jointly so the affordability skill sees a sensible
mix. Each application first draws its target affordability class (comfortable 65,
tight 25, unaffordable 10, in percent), its amount and its term, and then an
income plus expense and existing-debt ratios that place the annuity installment inside
that class's band under the real policy in
`brutor-demo-affordability-skill/scripts/affordability.py` (9.5 percent annuity; DTI
after at most 0.35 and disposable at least 600 EUR is comfortable; DTI at most 0.45 and
disposable at least 300 EUR is tight; everything else is unaffordable). Unaffordable
applicants are squeezed by expenses and existing debt rather than by implausibly low
pay. `generator.py` mirrors the policy in `classify()` to verify each record after
rounding; `tests/test_generator.py` runs the real skill script over 4,000 generated
applications and asserts the shares within 5 points and zero mismatches between the
mirror and the script, so a policy change in the skill fails this suite.

Special cases and their target rates:

| Case | Rate | What it exercises |
|---|---|---|
| Affordability class `unaffordable` | about 0.7 percent | The skill's verdict forces `decline` and therefore an approval hold |
| Amount above 25,000 EUR | about 0.5 percent | The argument policy holds the recommendation for an underwriter (Art 14 oversight) |
| Applicant name from the shared mock sanctions list | about 0.5 percent | The fraud screener returns `hit`, which forces `decline` and therefore an approval hold |
| Purpose text with a prompt-injection attempt | about 2 percent | The gateway's `mcp_output` prompt-injection guardrail should block the read |

Expected underwriter holds per day = applications per day times the share of
applications that are unaffordable, sanctioned or large, minus their overlap (measured at
1.7 percent over 4,000 generated applications). At the defaults (2 to 3 per interval,
about 360 a day) that is about 6 holds a day. Raise the rates in `generator.py` to make
the underwriter queue busier; every hold nobody answers counts against oversight.

The sanctions list lives in `src/applications_mcp/sanctions.py` and is duplicated
character for character in `brutor-demo-fraud-screener-agent`. If one side changes, hits
stop matching; change both.

Add applications on demand (works while the server is running; the store reloads the
file when it changes):

```bash
python -m applications_mcp.generate --count 5            # host, uses DATA_DIR or ./data
docker exec brutor-demo-applications-mcp python -m applications_mcp.generate --count 5
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `3014` | Port |
| `DATA_DIR` | `/data` | Where `applications.json` lives. If it is not writable the server falls back to `./data` |
| `GENERATE_ENABLED` | `true` | `false` disables the background generator (tests, manual demos) |
| `GENERATE_INTERVAL_SECONDS` | `600` | Seconds between timed batches |
| `GENERATE_MIN_PER_INTERVAL` | `2` | Smallest timed batch |
| `GENERATE_MAX_PER_INTERVAL` | `3` | Largest timed batch (must be at least the minimum) |
| `GENERATOR_SEED` | `20260923` | Seed for deterministic generation |
| `LOG_LEVEL` | `INFO` | Python log level |

The JSON file is written atomically (temp file plus rename) under a lock, so a crash
mid-write never leaves a truncated store.

## Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
python -m applications_mcp                     # http://0.0.0.0:3014/mcp, health at /health
python -m pytest                               # in-process tests, no network
./smoke.sh                                     # raw JSON-RPC against the running server
```

The tests call the tool functions directly, list tools through FastMCP to check the
`readOnlyHint` annotations, and post a raw `tools/call` to the ASGI app without an
`initialize` request, which is exactly what the gateway does.

## Run in Docker

```bash
docker build -t brutor-demo-applications-mcp .
docker run -d --name brutor-demo-applications-mcp --network brutor-network \
  -p 127.0.0.1:3014:3014 -v brutor-demo-applications-data:/data \
  brutor-demo-applications-mcp
curl -s http://127.0.0.1:3014/health
```

The image runs as a non-root user, exposes 3014 and has a `HEALTHCHECK` on `/health`
implemented with `urllib` (there is no curl in `python:3.12-slim`). In the full demo the
container is started by `brutor-demo-setup/docker-compose.yml`, which joins the trial's
external `brutor-network` and mounts a named volume on `/data`.

## How the gateway registers it

`brutor-demo-setup/setup.py` creates the MCP server through the admin API
(`POST /v1/admin/mcp-servers`) with:

| Field | Value |
|---|---|
| `base_url` | `http://brutor-demo-applications-mcp:3014` |
| `mcp_endpoint_path` | `/mcp` |
| `auth_type` | `none` |
| `region` | `eu-west-1` |

Note that the proxy appends `/mcp` itself: the agent calls
`POST {gateway}/v1/proxy/mcp/{server_id}` with a JSON-RPC body, and the proxy forwards it
to `http://brutor-demo-applications-mcp:3014/mcp`. There is no authentication on this
server; network isolation inside `brutor-network` is the boundary, and host publishing
is loopback only.

## Smoke test

`./smoke.sh [base_url]` (default `http://127.0.0.1:3014`) hits `/health`, posts a raw
`tools/list` and checks all five tools and their `readOnlyHint` values, then posts
`tools/call` for `applications_stats` and `applications_list_pending` and parses
`result.content[0].text` as JSON. Headers used are the same ones the gateway sends:
`Content-Type: application/json` and `Accept: application/json, text/event-stream`.

## Layout

```
pyproject.toml
Dockerfile                    python:3.12-slim, non-root, HEALTHCHECK on /health
smoke.sh
src/applications_mcp/
  server.py                   FastMCP app, tools, /health, main()
  store.py                    JSON store, atomic writes, lock, reload on external change
  generator.py                deterministic synthetic applications + background thread
  sanctions.py                shared mock sanctions list (kept identical in the fraud screener)
  generate.py                 CLI: python -m applications_mcp.generate --count N
tests/                        pytest, in-process, no network
```
