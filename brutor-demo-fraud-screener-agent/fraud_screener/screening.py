"""The mock screening logic: sanctions list, heuristics, verdict.

Pure functions. The sanctions list is the shared constant the applications
generator draws about five percent of its applicant names from, so hits
actually occur in the demo. Every name is synthetic.
"""

from __future__ import annotations

import re
from typing import Any

# Shared with brutor-demo-applications-mcp. Twelve synthetic names.
SANCTIONS_LIST: tuple[str, ...] = (
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

INQUIRIES_REVIEW_THRESHOLD = 6
DELINQUENCIES_REVIEW_THRESHOLD = 2

_WS = re.compile(r"\s+")


def normalize_name(name: Any) -> str:
    """casefold + whitespace collapse + strip."""
    if not isinstance(name, str):
        return ""
    return _WS.sub(" ", name.casefold()).strip()


_SANCTIONED = frozenset(normalize_name(n) for n in SANCTIONS_LIST)


def sanctions_match(full_name: Any) -> bool:
    return normalize_name(full_name) in _SANCTIONED


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def heuristic_reasons(bureau: dict[str, Any] | None) -> list[str]:
    """Velocity heuristics on the bureau facts; each reason forces review."""
    bureau = bureau or {}
    reasons: list[str] = []
    inquiries = _int(bureau.get("inquiries_6m"))
    delinquencies = _int(bureau.get("delinquencies_24m"))
    if inquiries >= INQUIRIES_REVIEW_THRESHOLD:
        reasons.append(f"velocity: {inquiries} credit inquiries in the last 6 months (threshold {INQUIRIES_REVIEW_THRESHOLD})")
    if delinquencies >= DELINQUENCIES_REVIEW_THRESHOLD:
        reasons.append(f"history: {delinquencies} delinquencies in the last 24 months (threshold {DELINQUENCIES_REVIEW_THRESHOLD})")
    return reasons


def verdict_for(
    payload: dict[str, Any],
    llm: dict[str, Any] | None,
    llm_error: str | None,
    model_used: str | None,
) -> dict[str, Any]:
    """Combine the three signals into the response verdict.

    hit    if the normalized name is on the sanctions list
    review if a heuristic fired or the model flagged the purpose text
    clear  otherwise
    """
    matched = sanctions_match(payload.get("full_name"))
    heuristics = heuristic_reasons(payload.get("bureau"))

    model_reasons: list[str] = []
    if llm is not None:
        indicators = llm.get("fraud_indicators")
        if isinstance(indicators, list):
            model_reasons = [f"model: {str(i).strip()}" for i in indicators if str(i).strip()][:6]
        if llm.get("suspicious") and not model_reasons:
            model_reasons = ["model: purpose text judged suspicious"]

    reasons: list[str] = []
    if matched:
        reasons.append("sanctions: applicant name matches the screening list")
    reasons.extend(heuristics)
    reasons.extend(model_reasons)
    if llm is None and llm_error:
        reasons.append(f"llm_unavailable: heuristics only ({llm_error})")

    if matched:
        verdict = "hit"
    elif heuristics or model_reasons:
        verdict = "review"
    else:
        verdict = "clear"
        if not reasons:
            reasons.append("no sanctions match, no velocity flags, no fraud indicators in purpose text")
    return {
        "verdict": verdict,
        "sanctions_match": matched,
        "reasons": reasons,
        "model_used": model_used,
    }
