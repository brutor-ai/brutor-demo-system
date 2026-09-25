# brutor-demo-credit-bureau-mcp

Mock **KYC and credit reference agency** ("Borealis Demo Bureau") for the
[Brutor Demo System](../README.md), exposed as an MCP server. The screening agent uses
it to verify each applicant and to pull a credit report before the affordability check.
Design: [../DESIGN.md](../DESIGN.md), section 5.2.

Everything it returns is synthetic. Answers are derived from a SHA-256 hash of the
`applicant_id`; no register, real bureau or real person is involved.

## How it fits the system

```
brutor-demo-screening-agent ──> Brutor Core Proxy ──> brutor-demo-credit-bureau-mcp:3015/mcp
   (LangGraph, every 10 min)     /v1/proxy/mcp/{id}        stateless, deterministic per applicant
```

The agent never talks to this server directly. Every call goes through the Brutor Core
Proxy, which enforces grants and capability filters, applies guardrails and limits, and
seals both tool calls as **sensitive-read evidence** because the calling AI System is
flagged `sensitive_data`. The proxy addresses the container by name on the
`brutor-network` docker network.

Stack: Python 3.12, the official MCP Python SDK (`mcp` 1.x, `FastMCP`) over streamable
HTTP with `stateless_http=True` and `json_response=True`. Both flags are mandatory: the
gateway posts raw JSON-RPC `tools/call` requests with no `initialize` handshake and
expects a JSON body back rather than an SSE stream. There is no state and no
background work, so any number of replicas give the same answers.

## Tools

Bare names, exactly as the gateway exposes them. Both are read-only.

| Tool | Read-only | Arguments | Returns |
|---|---|---|---|
| `bureau_verify_identity` | yes | `applicant_id`, `full_name`, `date_of_birth` (`YYYY-MM-DD`) | `{verified: bool, match_score: 0..1, checked_at, reason, applicant_id, bureau}`; about 4 percent unverified |
| `bureau_get_report` | yes | `applicant_id` | `{score: 300..900, open_credit_lines, total_debt_eur, delinquencies_24m, inquiries_6m, report_date, applicant_id, bureau: "Borealis Demo Bureau"}` |

The JSON is in `result.content[0].text`; the same object is also present as
`structuredContent`.

### Determinism

Every field is drawn from `random.Random` seeded with
`sha256(f"{applicant_id}|identity")` or `sha256(f"{applicant_id}|report")`, so repeated
calls for the same applicant agree across restarts and across containers, and a change
to one tool never shifts the other's answers. Only `checked_at` and `report_date`
change: `report_date` is always today's date.

* `bureau_verify_identity`: about 4 percent of applicants are `verified: false` with a
  `match_score` below 0.62 and `reason: "no_match_on_register"`. A malformed request
  (empty name or a date that is not `YYYY-MM-DD`) is `verified: false` with
  `reason: "invalid_input"`. The screening agent turns `verified: false` into `refer`.
* `bureau_get_report`: scores are drawn from a normal distribution around 700 (standard
  deviation 85) and clamped to 300 to 900, so most fall between 600 and 800. Lower scores
  carry more delinquencies. `inquiries_6m` is 6 or more for roughly one applicant in
  ten, which is what triggers the fraud screener's velocity `review`.

### Read-only annotations

Both tools carry `readOnlyHint: true`. On an AI System flagged `sensitive_data` the
gateway seals read-only calls that return personal data as sensitive-read evidence
(DESIGN.md section 4, step 3, and the Art 10 data governance row in section 7.5). The
annotation is therefore part of the contract: drop it and the Evidence Ledger stops
recording that a credit report was read.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `3015` | Port |
| `LOG_LEVEL` | `INFO` | Python log level |

## Run locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
python -m credit_bureau_mcp                    # http://0.0.0.0:3015/mcp, health at /health
python -m pytest                               # in-process tests, no network
./smoke.sh                                     # raw JSON-RPC against the running server
```

The tests call the bureau functions directly (distribution and determinism checks), list
tools through FastMCP to check the `readOnlyHint` annotations, and post a raw
`tools/call` to the ASGI app without an `initialize` request, which is what the gateway
does.

## Run in Docker

```bash
docker build -t brutor-demo-credit-bureau-mcp .
docker run -d --name brutor-demo-credit-bureau-mcp --network brutor-network \
  -p 127.0.0.1:3015:3015 brutor-demo-credit-bureau-mcp
curl -s http://127.0.0.1:3015/health
```

The image runs as a non-root user, exposes 3015 and has a `HEALTHCHECK` on `/health`
implemented with `urllib` (there is no curl in `python:3.12-slim`). In the full demo the
container is started by `brutor-demo-setup/docker-compose.yml`, which joins the trial's
external `brutor-network`.

## How the gateway registers it

`brutor-demo-setup/setup.py` creates the MCP server through the admin API
(`POST /v1/admin/mcp-servers`) with:

| Field | Value |
|---|---|
| `base_url` | `http://brutor-demo-credit-bureau-mcp:3015` |
| `mcp_endpoint_path` | `/mcp` |
| `auth_type` | `none` |
| `region` | `eu-west-1` |

Note that the proxy appends `/mcp` itself: the agent calls
`POST {gateway}/v1/proxy/mcp/{server_id}` with a JSON-RPC body, and the proxy forwards it
to `http://brutor-demo-credit-bureau-mcp:3015/mcp`. There is no authentication on this
server; network isolation inside `brutor-network` is the boundary, and host publishing
is loopback only.

## Smoke test

`./smoke.sh [base_url]` (default `http://127.0.0.1:3015`) hits `/health`, posts a raw
`tools/list` and checks that both tools are listed with `readOnlyHint: true`, then posts
`tools/call` for `bureau_verify_identity` and `bureau_get_report` and parses
`result.content[0].text` as JSON. Headers used are the same ones the gateway sends:
`Content-Type: application/json` and `Accept: application/json, text/event-stream`.

## Layout

```
pyproject.toml
Dockerfile                    python:3.12-slim, non-root, HEALTHCHECK on /health
smoke.sh
src/credit_bureau_mcp/
  server.py                   FastMCP app, the two tools, /health, main()
  bureau.py                   deterministic verify_identity() and get_report()
tests/                        pytest, in-process, no network
```
