"""Deterministic decision rules (DESIGN.md section 5.5), applied after the drafter.

Pure functions, no I/O. The rules, in precedence order:

    affordability unaffordable  -> decline
    fraud verdict hit           -> decline
    identity unverified         -> refer
    fraud verdict review        -> at least refer
    classifier risk band high   -> at least refer
    otherwise                   -> the drafter's recommendation stands

A hard rule (decline) beats a refer rule; an unverified identity beats the
model's own decline because no decision on an applicant we could not verify
should be recorded without a human. "At least refer" means the model may
only make the recommendation stricter (refer -> decline), never looser.
"""

from __future__ import annotations

RECOMMENDATIONS = ("approve", "refer", "decline")
_SEVERITY = {"approve": 0, "refer": 1, "decline": 2}

RULE_UNAFFORDABLE = "affordability_unaffordable"
RULE_FRAUD_HIT = "fraud_hit"
RULE_IDENTITY_UNVERIFIED = "identity_unverified"
RULE_FRAUD_REVIEW = "fraud_review"
RULE_RISK_HIGH = "risk_band_high"
RULE_MODEL_INVALID = "model_output_invalid"


def normalize_recommendation(value: object) -> str | None:
    """Map a model answer onto the allowed vocabulary, or None when it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if text in RECOMMENDATIONS else None


def final_recommendation(
    model_reco: object,
    identity_verified: bool,
    affordability_class: str | None,
    fraud_verdict: str | None,
    risk_band: str | None,
) -> tuple[str, str | None]:
    """Return (recommendation, rule_fired). rule_fired is None when the
    drafter's recommendation stands."""
    aff = (affordability_class or "").strip().lower()
    fraud = (fraud_verdict or "").strip().lower()
    risk = (risk_band or "").strip().lower()

    if aff == "unaffordable":
        return "decline", RULE_UNAFFORDABLE
    if fraud == "hit":
        return "decline", RULE_FRAUD_HIT
    if not identity_verified:
        return "refer", RULE_IDENTITY_UNVERIFIED

    model = normalize_recommendation(model_reco)
    if model is None:
        # An unreadable model answer is never approved on trust.
        return "refer", RULE_MODEL_INVALID

    if fraud == "review" and _SEVERITY[model] < _SEVERITY["refer"]:
        return "refer", RULE_FRAUD_REVIEW
    if risk == "high" and _SEVERITY[model] < _SEVERITY["refer"]:
        return "refer", RULE_RISK_HIGH
    return model, None


def policy_floor(
    identity_verified: bool,
    affordability_class: str | None,
    fraud_verdict: str | None,
    risk_band: str | None,
) -> tuple[str, str | None]:
    """The strictest outcome the rules already force before the drafter runs,
    so the drafter can be told what it may choose. Same logic as
    final_recommendation with the most lenient model answer."""
    return final_recommendation("approve", identity_verified, affordability_class, fraud_verdict, risk_band)


def allowed_recommendations(floor: str) -> list[str]:
    """Recommendations at least as strict as the floor."""
    return [r for r in RECOMMENDATIONS if _SEVERITY[r] >= _SEVERITY[floor]]
