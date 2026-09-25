import asyncio
import json

from starlette.testclient import TestClient

from applications_mcp import server
from applications_mcp.generator import Generator

LETTER = "Dear applicant. This assessment was prepared with the help of an AI system."


def _seed(store, n=3):
    return Generator(store, seed=5).generate(n)


def test_list_pending_and_get(store):
    created = _seed(store, 4)
    pending = json.loads(server.applications_list_pending(limit=2))
    assert len(pending) == 2
    assert pending == server.list_pending(limit=2)
    assert set(pending[0]) == {"application_id", "received_at", "amount_eur", "purpose_short"}
    full = server.applications_get(created[0]["application_id"])
    assert full["applicant"]["full_name"]
    assert full["status"] == "received"
    assert server.applications_get("APP-19700101-0001") == {
        "ok": False, "error": "not_found", "application_id": "APP-19700101-0001",
    }


def test_set_recommendation_once(store):
    created = _seed(store, 1)
    app_id = created[0]["application_id"]
    args = dict(
        application_id=app_id, recommendation="approve", amount_eur=created[0]["requested_amount_eur"],
        rationale="all checks passed", risk_band="low", affordability_class="comfortable",
        fraud_verdict="clear", customer_letter=LETTER,
    )
    first = server.applications_set_recommendation(**args)
    assert first["ok"] is True and first["status"] == "screened" and first["screened_at"]
    record = server.applications_get(app_id)
    assert record["status"] == "screened"
    assert record["recommendation"] == "approve"
    assert record["screened_at"] == first["screened_at"]
    assert record["screening"]["customer_letter"] == LETTER

    again = server.applications_set_recommendation(**{**args, "recommendation": "decline"})
    assert again == {
        "ok": False, "error": "already_screened", "application_id": app_id,
        "status": "screened", "screened_at": first["screened_at"],
    }
    assert server.applications_get(app_id)["recommendation"] == "approve"
    assert server.applications_list_pending() == "[]"


def test_set_recommendation_validates(store):
    created = _seed(store, 1)
    bad = server.applications_set_recommendation(
        application_id=created[0]["application_id"], recommendation="maybe", amount_eur=1,
        rationale="", risk_band="", affordability_class="", fraud_verdict="", customer_letter="",
    )
    assert bad["ok"] is False and bad["error"] == "invalid_recommendation"
    missing = server.applications_set_recommendation(
        application_id="APP-00000000-0000", recommendation="refer", amount_eur=1,
        rationale="", risk_band="", affordability_class="", fraud_verdict="", customer_letter="",
    )
    assert missing["error"] == "not_found"


def test_add_note_and_stats(store):
    created = _seed(store, 2)
    app_id = created[0]["application_id"]
    assert server.applications_add_note(app_id, "held for underwriter, approval id x") == {
        "ok": True, "application_id": app_id, "notes_count": 1,
    }
    assert server.applications_add_note(app_id, "second")["notes_count"] == 2
    assert server.applications_add_note(app_id, "   ")["error"] == "empty_note"
    assert server.applications_get(app_id)["notes"][0]["note"].startswith("held for")

    server.applications_set_recommendation(
        application_id=app_id, recommendation="decline", amount_eur=5, rationale="r",
        risk_band="high", affordability_class="unaffordable", fraud_verdict="clear", customer_letter=LETTER,
    )
    stats = server.applications_stats()
    assert stats["total"] == 2
    assert stats["by_status"] == {"received": 1, "screened": 1}
    assert stats["by_recommendation"] == {"approve": 0, "refer": 0, "decline": 1}
    assert stats["generated_total"] == 2


def test_tool_annotations_mark_reads_as_read_only(store):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == {
        "applications_list_pending", "applications_get", "applications_set_recommendation",
        "applications_add_note", "applications_stats",
    }
    for name in ("applications_list_pending", "applications_get", "applications_stats"):
        assert tools[name].annotations.readOnlyHint is True, name
    for name in ("applications_set_recommendation", "applications_add_note"):
        assert tools[name].annotations.readOnlyHint is False, name
    schema = tools["applications_set_recommendation"].inputSchema
    assert "amount_eur" in schema["required"]
    assert "recommendation" in schema["required"]


def test_raw_jsonrpc_tools_call_without_initialize(store):
    """The gateway posts tools/call directly; stateless + json_response must serve it."""
    _seed(store, 2)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    with TestClient(server.mcp.streamable_http_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["service"] == "brutor-demo-applications-mcp"

        listed = client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200, listed.text
        assert listed.headers["content-type"].startswith("application/json")
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert "applications_list_pending" in names

        called = client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "applications_list_pending", "arguments": {"limit": 1}}},
        )
        assert called.status_code == 200, called.text
        body = called.json()
        assert body["result"].get("isError") is not True
        assert len(body["result"]["content"]) == 1
        payload = json.loads(body["result"]["content"][0]["text"])
        assert isinstance(payload, list)
        assert len(payload) == 1 and payload[0]["application_id"].startswith("APP-")

        got = client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "applications_get",
                             "arguments": {"application_id": payload[0]["application_id"]}}},
        )
        record = json.loads(got.json()["result"]["content"][0]["text"])
        assert record["applicant"]["full_name"]
        assert got.json()["result"]["structuredContent"]["application_id"] == payload[0]["application_id"]

        stats = client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "applications_stats", "arguments": {}}},
        )
        assert json.loads(stats.json()["result"]["content"][0]["text"])["total"] == 2
