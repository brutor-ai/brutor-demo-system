import asyncio
import json

from starlette.testclient import TestClient

from credit_bureau_mcp import server


def test_tools_direct():
    ident = server.bureau_verify_identity("CUST-424242", "Nils Dahl", "1975-02-03")
    assert isinstance(ident["verified"], bool)
    report = server.bureau_get_report("CUST-424242")
    assert 300 <= report["score"] <= 900
    assert report["bureau"] == "Borealis Demo Bureau"


def test_both_tools_are_read_only():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {"bureau_verify_identity", "bureau_get_report"}
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True, tool.name
    assert set(tools["bureau_verify_identity"].inputSchema["required"]) == {
        "applicant_id", "full_name", "date_of_birth",
    }


def test_raw_jsonrpc_tools_call_without_initialize():
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    with TestClient(server.mcp.streamable_http_app()) as client:
        health = client.get("/health").json()
        assert health == {"status": "ok", "service": "brutor-demo-credit-bureau-mcp", "bureau": "Borealis Demo Bureau"}

        listed = client.post("/mcp", headers=headers,
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert listed.status_code == 200, listed.text
        assert listed.headers["content-type"].startswith("application/json")
        assert {t["name"] for t in listed.json()["result"]["tools"]} == {
            "bureau_verify_identity", "bureau_get_report",
        }

        called = client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "bureau_get_report", "arguments": {"applicant_id": "CUST-1"}}},
        )
        assert called.status_code == 200, called.text
        result = called.json()["result"]
        assert result.get("isError") is not True
        payload = json.loads(result["content"][0]["text"])
        assert 300 <= payload["score"] <= 900
        assert result["structuredContent"]["score"] == payload["score"]

        verify = client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "bureau_verify_identity",
                             "arguments": {"applicant_id": "CUST-1", "full_name": "Ida Moen",
                                           "date_of_birth": "1990-05-05"}}},
        )
        payload = json.loads(verify.json()["result"]["content"][0]["text"])
        assert "verified" in payload and "match_score" in payload and "checked_at" in payload
