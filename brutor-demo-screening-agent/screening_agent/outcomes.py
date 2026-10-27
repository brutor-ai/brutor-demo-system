"""The agent's declared outcomes (RFC 0025; DESIGN.md section 8, "Declared outcomes").

`outcomes.json` beside this module is the ONE statement of how a run of this
agent may end. The agent validates every `X-Brutor-Outcome-Id` it sends
against it (an id it did not declare is a bug, raised before the call), and
brutor-demo-setup PUTs its `outcomes` list verbatim as the AI System's
catalogue and its `dependencies[].expected_outcomes` on the dependency edge to
the fraud screener. Change the file, re-run setup, approve the new contract.

Wire grammar: `<outcome>[.<variant>]`, each part `[a-z0-9_-]{1,48}`. An
outcome that declares variants is reported with one of them; an outcome
without variants is reported bare.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOGUE_FILE = Path(__file__).with_name("outcomes.json")

OUTCOME_ID_RE = re.compile(r"^[a-z0-9_-]{1,48}(\.[a-z0-9_-]{1,48})?$")

# -- the ids the agent reports (each must be declared in outcomes.json) -------
APPLICATION_PROCESSED = "application_processed"
LOAN_APPROVED = f"{APPLICATION_PROCESSED}.loan_approved"
LOAN_REJECTED = f"{APPLICATION_PROCESSED}.loan_rejected"
LOAN_REFERRED = f"{APPLICATION_PROCESSED}.loan_referred"
HELD_FOR_UNDERWRITER = f"{APPLICATION_PROCESSED}.held_for_underwriter"
UNDERWRITER_DECISION_APPLIED = f"{APPLICATION_PROCESSED}.underwriter_decision_applied"
UNDERWRITER_HOLD_RENEWED = f"{APPLICATION_PROCESSED}.underwriter_hold_renewed"
UNDERWRITER_HOLD_CLOSED = f"{APPLICATION_PROCESSED}.underwriter_hold_closed"
SENT_TO_MANUAL_REVIEW = f"{APPLICATION_PROCESSED}.sent_to_manual_review"
NO_APPLICATIONS_AVAILABLE = "no_applications_available"
APPLICATIONS_DISPATCHED = "applications_dispatched"

# The final recommendation (rules.py) -> the variant the record step reports.
RECOMMENDATION_OUTCOMES: dict[str, str] = {
    "approve": LOAN_APPROVED,
    "decline": LOAN_REJECTED,
    "refer": LOAN_REFERRED,
}

# The fraud screener's catalogue (its own outcomes.json) names the verdict as
# the variant of this outcome.
FRAUD_OUTCOME = "screening_completed"
FRAUD_DELEGATE = "brutor-demo-fraud-screener-agent"

REPORTED: tuple[str, ...] = (
    LOAN_APPROVED,
    LOAN_REJECTED,
    LOAN_REFERRED,
    HELD_FOR_UNDERWRITER,
    UNDERWRITER_DECISION_APPLIED,
    UNDERWRITER_HOLD_RENEWED,
    UNDERWRITER_HOLD_CLOSED,
    SENT_TO_MANUAL_REVIEW,
    NO_APPLICATIONS_AVAILABLE,
    APPLICATIONS_DISPATCHED,
)


class UndeclaredOutcome(ValueError):
    """The agent tried to report an outcome its own catalogue does not declare."""


@lru_cache(maxsize=1)
def catalogue_document() -> dict[str, Any]:
    """The parsed outcomes.json: {"outcomes": [...], "dependencies": [...]}."""
    return json.loads(CATALOGUE_FILE.read_text(encoding="utf-8"))


def catalogue() -> list[dict[str, Any]]:
    """The outcome catalogue as the control plane stores it (spec section 5)."""
    return catalogue_document()["outcomes"]


def full_ids(outcomes: list[dict[str, Any]] | None = None) -> frozenset[str]:
    """Every id a run may report: `outcome.variant` for an outcome with
    variants, the bare `outcome` otherwise."""
    out: set[str] = set()
    for outcome in catalogue() if outcomes is None else outcomes:
        variants = outcome.get("variants") or []
        if variants:
            out.update(f"{outcome['id']}.{v['id']}" for v in variants)
        else:
            out.add(outcome["id"])
    return frozenset(out)


def counts_toward_baseline(outcome_id: str) -> bool:
    head = outcome_id.split(".", 1)[0]
    for outcome in catalogue():
        if outcome["id"] == head:
            return bool(outcome.get("counts_toward_baseline"))
    raise UndeclaredOutcome(outcome_id)


def expected_delegate_outcomes(delegate: str = FRAUD_DELEGATE) -> frozenset[str]:
    """The delegate outcomes this agent is built to handle (the dependency
    edge's `expected_outcomes`)."""
    for dep in catalogue_document().get("dependencies") or []:
        if dep.get("delegate") == delegate:
            return frozenset(dep.get("expected_outcomes") or [])
    raise KeyError(f"no dependency on {delegate} in {CATALOGUE_FILE.name}")


def check_grammar(value: Any) -> str:
    """Raise ValueError unless `value` is `<outcome>[.<variant>]`."""
    if not isinstance(value, str) or not OUTCOME_ID_RE.fullmatch(value):
        raise ValueError(f"malformed outcome id {value!r}; expected <outcome>[.<variant>] with parts [a-z0-9_-]{{1,48}}")
    return value


def validate(value: Any) -> str:
    """The id, if it is well formed and declared by this agent's catalogue;
    otherwise raise (UndeclaredOutcome is a ValueError)."""
    check_grammar(value)
    if value not in full_ids():
        raise UndeclaredOutcome(f"outcome {value!r} is not declared in {CATALOGUE_FILE.name}; declared: {sorted(full_ids())}")
    return value
