#!/usr/bin/env bash
# Smoke test: raw JSON-RPC against a running Loan Applications MCP server.
# Usage: ./smoke.sh [base_url]   (default http://127.0.0.1:3014)
set -euo pipefail

BASE="${1:-http://127.0.0.1:${MCP_PORT:-3014}}"
HDR_CT="Content-Type: application/json"
HDR_ACCEPT="Accept: application/json, text/event-stream"

echo "== GET $BASE/health"
curl -sS --fail "$BASE/health"; echo

echo "== tools/list"
LIST=$(curl -sS --fail -X POST "$BASE/mcp" -H "$HDR_CT" -H "$HDR_ACCEPT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
echo "$LIST" | python3 -c '
import json,sys
body=json.load(sys.stdin)
tools={t["name"]:t for t in body["result"]["tools"]}
expected={"applications_list_pending","applications_get","applications_set_recommendation","applications_add_note","applications_stats"}
missing=expected-set(tools)
assert not missing, f"missing tools: {missing}"
for n,t in sorted(tools.items()):
    ro=(t.get("annotations") or {}).get("readOnlyHint")
    print(f"  {n:<36} readOnlyHint={ro}")
'

echo "== tools/call applications_stats"
CALL=$(curl -sS --fail -X POST "$BASE/mcp" -H "$HDR_CT" -H "$HDR_ACCEPT" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"applications_stats","arguments":{}}}')
echo "$CALL" | python3 -c '
import json,sys
body=json.load(sys.stdin)
res=body["result"]
assert not res.get("isError"), res
text=res["content"][0]["text"]
payload=json.loads(text)
assert "by_status" in payload, payload
print("  result.content[0].text =", json.dumps(payload))
'

echo "== tools/call applications_list_pending"
CALL=$(curl -sS --fail -X POST "$BASE/mcp" -H "$HDR_CT" -H "$HDR_ACCEPT" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"applications_list_pending","arguments":{"limit":3}}}')
echo "$CALL" | python3 -c '
import json,sys
body=json.load(sys.stdin)
res=body["result"]
assert not res.get("isError"), res
payload=json.loads(res["content"][0]["text"])
assert isinstance(payload, list), payload
print(f"  {len(payload)} pending application(s)")
for a in payload: print("   ", a["application_id"], a["amount_eur"], "EUR -", a["purpose_short"])
'
echo "smoke OK"
