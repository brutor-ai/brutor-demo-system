#!/usr/bin/env bash
# Smoke test: raw JSON-RPC against a running Credit Bureau MCP server.
# Usage: ./smoke.sh [base_url]   (default http://127.0.0.1:3015)
set -euo pipefail

BASE="${1:-http://127.0.0.1:${MCP_PORT:-3015}}"
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
expected={"bureau_verify_identity","bureau_get_report"}
missing=expected-set(tools)
assert not missing, f"missing tools: {missing}"
for n,t in sorted(tools.items()):
    ro=(t.get("annotations") or {}).get("readOnlyHint")
    assert ro is True, f"{n} must be read-only"
    print(f"  {n:<28} readOnlyHint={ro}")
'

echo "== tools/call bureau_verify_identity"
CALL=$(curl -sS --fail -X POST "$BASE/mcp" -H "$HDR_CT" -H "$HDR_ACCEPT" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"bureau_verify_identity","arguments":{"applicant_id":"CUST-483920","full_name":"Elin Bergstrom","date_of_birth":"1987-04-12"}}}')
echo "$CALL" | python3 -c '
import json,sys
res=json.load(sys.stdin)["result"]
assert not res.get("isError"), res
payload=json.loads(res["content"][0]["text"])
assert "verified" in payload, payload
print("  result.content[0].text =", json.dumps(payload))
'

echo "== tools/call bureau_get_report"
CALL=$(curl -sS --fail -X POST "$BASE/mcp" -H "$HDR_CT" -H "$HDR_ACCEPT" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"bureau_get_report","arguments":{"applicant_id":"CUST-483920"}}}')
echo "$CALL" | python3 -c '
import json,sys
res=json.load(sys.stdin)["result"]
assert not res.get("isError"), res
payload=json.loads(res["content"][0]["text"])
assert 300 <= payload["score"] <= 900, payload
print("  result.content[0].text =", json.dumps(payload))
'
echo "smoke OK"
