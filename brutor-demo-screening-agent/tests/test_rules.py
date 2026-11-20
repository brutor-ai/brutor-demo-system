from screening_agent.rules import (
    RULE_FRAUD_HIT,
    RULE_FRAUD_REVIEW,
    RULE_IDENTITY_UNVERIFIED,
    RULE_MODEL_INVALID,
    RULE_RISK_HIGH,
    RULE_UNAFFORDABLE,
    allowed_recommendations,
    final_recommendation,
    policy_floor,
)


def test_model_stands_when_no_rule_fires():
    assert final_recommendation("approve", True, "comfortable", "clear", "low") == ("approve", None)
    assert final_recommendation("refer", True, "tight", "clear", "medium") == ("refer", None)
    assert final_recommendation("decline", True, "tight", "clear", "low") == ("decline", None)


def test_identity_unverified_forces_refer():
    assert final_recommendation("approve", False, "comfortable", "clear", "low") == ("refer", RULE_IDENTITY_UNVERIFIED)
    # Even a model decline is downgraded: nobody decides on an unverified person.
    assert final_recommendation("decline", False, "comfortable", "clear", "low") == ("refer", RULE_IDENTITY_UNVERIFIED)


def test_unaffordable_forces_decline():
    assert final_recommendation("approve", True, "unaffordable", "clear", "low") == ("decline", RULE_UNAFFORDABLE)
    # Decline rules beat identity.
    assert final_recommendation("approve", False, "unaffordable", "clear", "low") == ("decline", RULE_UNAFFORDABLE)


def test_fraud_hit_forces_decline():
    assert final_recommendation("approve", True, "comfortable", "hit", "low") == ("decline", RULE_FRAUD_HIT)
    assert final_recommendation("approve", True, "comfortable", "HIT", "low")[0] == "decline"


def test_fraud_review_is_at_least_refer():
    assert final_recommendation("approve", True, "comfortable", "review", "low") == ("refer", RULE_FRAUD_REVIEW)
    assert final_recommendation("refer", True, "comfortable", "review", "low") == ("refer", None)
    assert final_recommendation("decline", True, "comfortable", "review", "low") == ("decline", None)


def test_high_risk_is_at_least_refer():
    assert final_recommendation("approve", True, "comfortable", "clear", "high") == ("refer", RULE_RISK_HIGH)
    assert final_recommendation("decline", True, "comfortable", "clear", "high") == ("decline", None)


def test_invalid_model_output_refers():
    assert final_recommendation("maybe", True, "comfortable", "clear", "low") == ("refer", RULE_MODEL_INVALID)
    assert final_recommendation(None, True, "comfortable", "clear", "low") == ("refer", RULE_MODEL_INVALID)
    assert final_recommendation(" Approve ", True, "comfortable", "clear", "low") == ("approve", None)


def test_policy_floor_and_allowed():
    assert policy_floor(True, "comfortable", "clear", "low") == ("approve", None)
    assert policy_floor(True, "comfortable", "review", "low") == ("refer", RULE_FRAUD_REVIEW)
    assert policy_floor(True, "unaffordable", "clear", "low") == ("decline", RULE_UNAFFORDABLE)
    assert allowed_recommendations("approve") == ["approve", "refer", "decline"]
    assert allowed_recommendations("refer") == ["refer", "decline"]
    assert allowed_recommendations("decline") == ["decline"]
