from fraud_screener.screening import SANCTIONS_LIST, heuristic_reasons, normalize_name, sanctions_match, verdict_for


def test_sanctions_list_is_the_shared_twelve():
    assert SANCTIONS_LIST == (
        "Viktor Malenko",
        "Ingrid Solvaag",
        "Dmitri Orlovsky",
        "Helena Kastrup",
        "Rasmus Lindqvist-Berg",
        "Oksana Verhoeven",
        "Bjorn Haldane",
        "Marta Szczepan",
        "Leon Aubrecht",
        "Sigrid Voss",
        "Tomasz Wielgus",
        "Anneli Kuusk",
    )
    for name in SANCTIONS_LIST:
        assert sanctions_match(name)
        assert sanctions_match(name.upper())
        assert sanctions_match("  " + name.replace(" ", "   ") + " ")
    assert not sanctions_match("Maja Lindholm")
    assert not sanctions_match(None)
    assert normalize_name("  Viktor \t Malenko ") == "viktor malenko"


def test_heuristics_thresholds():
    assert heuristic_reasons({"inquiries_6m": 5, "delinquencies_24m": 1}) == []
    assert len(heuristic_reasons({"inquiries_6m": 6, "delinquencies_24m": 0})) == 1
    assert len(heuristic_reasons({"inquiries_6m": 9, "delinquencies_24m": 2})) == 2
    assert heuristic_reasons(None) == []
    assert heuristic_reasons({"inquiries_6m": "not a number"}) == []


def test_verdict_precedence():
    clean = {"full_name": "Maja Lindholm", "bureau": {"inquiries_6m": 0, "delinquencies_24m": 0}}
    v = verdict_for(clean, {"fraud_indicators": [], "suspicious": False}, None, "gpt-5.2")
    assert v["verdict"] == "clear" and v["model_used"] == "gpt-5.2" and v["reasons"]
    v = verdict_for(dict(clean, full_name="Anneli Kuusk"), {"fraud_indicators": ["x"], "suspicious": True}, None, "gpt-5.2")
    assert v["verdict"] == "hit" and v["sanctions_match"] is True
    v = verdict_for(clean, {"fraud_indicators": [], "suspicious": True}, None, "gpt-5.2")
    assert v["verdict"] == "review" and "model: purpose text judged suspicious" in v["reasons"]
    v = verdict_for(clean, None, "ConnectError: refused", None)
    assert v["verdict"] == "clear" and v["model_used"] is None
    assert v["reasons"] == ["llm_unavailable: heuristics only (ConnectError: refused)"]
