"""The fraud screener's declared outcomes (RFC 0025 section 7a).

The catalogue ships as fraud_screener/outcomes.json; every completed task
reports `screening_completed.<verdict>` on the response header and in the
final Task's metadata, and every verdict the screening logic can produce is a
declared variant.
"""

import json

import pytest
import respx

from fraud_screener import outcomes
from fraud_screener.app import task_response
from fraud_screener.screening import verdict_for
from tests.conftest import LLM_URL, llm_response, payload, send_body

DECLARED = {"screening_completed.clear", "screening_completed.review", "screening_completed.hit"}


def test_the_catalogue_is_the_hand_listed_document():
    doc = json.loads(outcomes.CATALOGUE_FILE.read_text(encoding="utf-8"))
    assert set(doc) == {"outcomes"}
    [o] = doc["outcomes"]
    assert o["id"] == "screening_completed" and o["counts_toward_baseline"] is True
    assert [v["id"] for v in o["variants"]] == ["clear", "review", "hit"]
    assert all(1 <= len(v["label"]) <= 128 for v in o["variants"])
    assert outcomes.full_ids() == DECLARED


@pytest.mark.parametrize("verdict", ["clear", "review", "hit"])
def test_outcome_for_each_verdict(verdict):
    assert outcomes.outcome_for(verdict) == f"screening_completed.{verdict}"


@pytest.mark.parametrize("bad", ["maybe", "", "HIT", "clear.extra", "x" * 49])
def test_an_undeclared_verdict_is_never_reported(bad):
    with pytest.raises(ValueError):
        outcomes.outcome_for(bad)


def test_every_verdict_the_logic_can_produce_is_declared():
    """The three branches of verdict_for, hand-picked inputs."""
    produced = {
        verdict_for(payload(), None, None, None)["verdict"],  # clear
        verdict_for(payload(bureau={"inquiries_6m": 9}), None, None, None)["verdict"],  # review
        verdict_for(payload(full_name="Viktor Malenko"), None, None, None)["verdict"],  # hit
    }
    assert produced == {"clear", "review", "hit"}
    assert {outcomes.outcome_for(v) for v in produced} == DECLARED


@pytest.mark.parametrize(
    "overrides, verdict",
    [
        ({}, "clear"),
        ({"bureau": {"inquiries_6m": 6, "delinquencies_24m": 0, "open_credit_lines": 3}}, "review"),
        ({"full_name": "Sigrid Voss"}, "hit"),
    ],
)
@pytest.mark.parametrize("path", ["/message:send", "/message%3Asend"])
def test_the_response_carries_the_outcome_on_both_carriers(client, overrides, verdict, path):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(LLM_URL).mock(return_value=llm_response())
        r = client.post(path, json=send_body(payload(**overrides)))
    assert r.status_code == 200
    expected = f"screening_completed.{verdict}"
    assert r.headers["X-Brutor-Outcome-Id"] == expected
    task = r.json()["task"]
    assert json.loads(task["status"]["message"]["parts"][0]["text"])["verdict"] == verdict
    assert task["metadata"]["brutor.outcome.id"] == expected
    # the agent never claims its own conformance: that is the gateway's to judge
    assert "x-brutor-outcome-conformance" not in r.headers
    assert "brutor.outcome.conformance" not in task["metadata"]


def test_model_flag_reports_review(client):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(LLM_URL).mock(return_value=llm_response(indicators=["urgent cash, crypto"], suspicious=True))
        r = client.post("/message:send", json=send_body(payload()))
    assert r.headers["x-brutor-outcome-id"] == "screening_completed.review"


def test_task_response_requires_the_outcome():
    with pytest.raises(TypeError):
        task_response({"verdict": "clear"})  # type: ignore[call-arg]
    t = task_response({"verdict": "hit"}, outcome_id="screening_completed.hit")["task"]
    assert t["metadata"] == {"brutor.outcome.id": "screening_completed.hit"}
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
