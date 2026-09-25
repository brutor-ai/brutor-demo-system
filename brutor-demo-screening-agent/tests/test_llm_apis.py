"""The two LLM routes: chat and responses, same headers, and the 400 fallback."""

import pytest

from screening_agent.approvals import PendingApprovals
from screening_agent.config import Settings
from screening_agent.gateway import GatewayError
from screening_agent.graph import process_application

MSGS = [
    {"role": "system", "content": "You are the risk classification step. Respond with JSON."},
    {"role": "user", "content": "classify"},
]


def _header_subset(call):
    keys = ("x-brutor-run-id", "x-brutor-turn-id", "x-brutor-turn-seq", "x-brutor-step-id", "x-brutor-step-name", "x-brutor-run-end", "x-brutor-run-outcome", "x-tenant-id")
    return {k: call.headers.get(k) for k in keys}


def test_default_apis_from_env(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_API", raising=False)
    monkeypatch.delenv("DRAFTER_API", raising=False)
    s = Settings.from_env()
    assert s.classifier_api == "chat" and s.drafter_api == "responses"
    monkeypatch.setenv("DRAFTER_API", "Chat")
    assert Settings.from_env().drafter_api == "chat"
    monkeypatch.setenv("DRAFTER_API", "completions")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_responses_path_headers_and_json(settings, fake):
    gw = fake.gateway(settings)
    with gw.run("bds-APP-1-X") as ctx:
        chat = gw.llm("gpt-5.2", MSGS, api="chat", step_id="assess", step_name="Assess", turn=("t1", 1), node="classify")
        resp = gw.llm("gpt-5.2", MSGS, api="responses", step_id="assess", step_name="Assess", turn=("t1", 1), node="classify")
        assert chat == resp == {"risk_band": "low", "key_factors": ["stable income", "moderate DTI"]}
        assert ctx.derived_root == "root-derived-0001"
        closing = gw.llm("gpt-5.2", MSGS, api="responses", step_id="decide", step_name="Decide", turn=("t2", 2), node="decide", run_end="completed", outcome="resolved")
        assert closing["risk_band"] == "low"
        assert ctx.closed
    calls = fake.calls_of("llm")
    assert [c.llm_api for c in calls] == ["chat", "responses", "responses"]
    # same node -> identical run/turn/step headers on both routes
    assert _header_subset(calls[0]) == _header_subset(calls[1])
    assert calls[1].headers["x-brutor-run-id"] == "bds-APP-1-X"
    assert calls[1].headers["x-brutor-step-id"] == "assess" and calls[1].headers["x-brutor-turn-id"] == "t1"
    assert calls[1].headers["traceparent"] != calls[0].headers["traceparent"]
    # close headers on the responses route
    assert calls[2].headers["x-brutor-run-end"] == "completed"
    assert calls[2].headers["x-brutor-run-outcome"] == "resolved"
    assert calls[2].headers["x-brutor-turn-seq"] == "2"
    # body shape of the responses route
    body = calls[1].body
    assert body["input"] == MSGS and body["text"] == {"format": {"type": "json_object"}}
    assert "messages" not in body and "response_format" not in body
    assert "temperature" not in body and "top_p" not in body
    assert fake.run_ends == []


def test_responses_text_extraction():
    from screening_agent.gateway import Gateway

    assert Gateway._responses_text({"output_text": '{"a": 1}'}) == '{"a": 1}'
    data = {"output": [
        {"type": "reasoning", "id": "rs", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": '{"a":'}, {"type": "output_text", "text": " 1}"}]},
    ]}
    assert Gateway._responses_text(data) == '{"a": 1}'
    assert Gateway._responses_text({"output": []}) == ""
    assert Gateway._responses_text("nope") == ""


def test_graph_uses_configured_api_per_model(settings, fake, tmp_path):
    gw = fake.gateway(settings)
    result = process_application(gw, settings, PendingApprovals(tmp_path), "APP-20260923-001")
    assert result.state == "completed"
    llm = fake.calls_of("llm")
    assert [(c.body["model"], c.llm_api) for c in llm] == [("gpt-5.2", "chat"), ("gpt-5.5", "responses")]
    assert fake.recorded[0]["args"]["recommendation"] == "approve"


def test_chat_400_falls_back_to_responses_once_per_process(settings, fake, tmp_path):
    settings.drafter_api = "chat"  # mis-set env
    fake.responses_only_models = {"gpt-5.5"}
    gw = fake.gateway(settings)
    store = PendingApprovals(tmp_path)
    first = process_application(gw, settings, store, "APP-20260923-001")
    assert first.state == "completed"
    llm = [(c.body["model"], c.llm_api, c.headers["x-brutor-run-id"]) for c in fake.calls_of("llm")]
    assert [(m, a) for m, a, _ in llm] == [("gpt-5.2", "chat"), ("gpt-5.5", "chat"), ("gpt-5.5", "responses")]
    assert len({r for _, _, r in llm}) == 1
    # the retry carried the identical step/turn headers
    failed, retried = fake.calls_of("llm")[1], fake.calls_of("llm")[2]
    assert _header_subset(failed) == _header_subset(retried)
    assert gw.responses_only == {"gpt-5.5"}
    assert fake.recorded[0]["args"]["recommendation"] == "approve"

    # second run in the same process goes straight to /responses
    fake.calls.clear()
    fake.recorded.clear()
    fake.application["status"] = "received"
    second = process_application(gw, settings, store, "APP-20260923-001")
    assert second.state == "completed"
    assert [(c.body["model"], c.llm_api) for c in fake.calls_of("llm")] == [("gpt-5.2", "chat"), ("gpt-5.5", "responses")]


def test_other_400_is_not_retried(settings, fake):
    import httpx

    from screening_agent.gateway import Gateway

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(400, json={"message": "invalid request: bad messages"})
        return fake.handler(request)

    gw = Gateway(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with gw.run("bds-APP-1-X"):
        with pytest.raises(GatewayError) as err:
            gw.llm("gpt-5.2", MSGS, api="chat", step_id="assess", step_name="Assess", turn=("t1", 1))
    assert err.value.status == 400
    assert gw.responses_only == set()
