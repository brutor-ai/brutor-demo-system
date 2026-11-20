"""Deterministic mock bureau logic.

Everything derives from `hashlib.sha256(applicant_id)`, so repeated calls for the same
applicant agree, across restarts and across container instances, with no state at all.
Each field family uses its own sub-seed (`<applicant_id>|identity`, `<applicant_id>|report`)
so that adding a field to one answer never shifts the other.

Targets (DESIGN.md section 5.2): about 4 percent of applicants fail identity
verification; credit scores span 300 to 900 and cluster in 600 to 800.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, date, datetime
from typing import Any

BUREAU_NAME = "Borealis Demo Bureau"
UNVERIFIED_RATE = 0.04
SCORE_MIN, SCORE_MAX = 300, 900
SCORE_MEAN, SCORE_SD = 700, 85


def _rng(applicant_id: str, family: str) -> random.Random:
    digest = hashlib.sha256(f"{applicant_id}|{family}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_dob(value: str) -> bool:
    try:
        parsed = date.fromisoformat(str(value).strip())
    except ValueError:
        return False
    return date(1900, 1, 1) <= parsed <= date.today()


def verify_identity(applicant_id: str, full_name: str, date_of_birth: str) -> dict[str, Any]:
    """KYC check. Deterministic per applicant; malformed input is always unverified."""
    applicant_id = str(applicant_id).strip()
    full_name = " ".join(str(full_name).split())
    rng = _rng(applicant_id, "identity")
    unverified_draw = rng.random()
    verified_score = round(rng.uniform(0.86, 0.99), 2)
    unverified_score = round(rng.uniform(0.15, 0.62), 2)

    if not applicant_id or not full_name or not _valid_dob(date_of_birth):
        return {
            "verified": False,
            "match_score": 0.0,
            "checked_at": _now_iso(),
            "applicant_id": applicant_id,
            "reason": "invalid_input",
            "bureau": BUREAU_NAME,
        }

    verified = unverified_draw >= UNVERIFIED_RATE
    return {
        "verified": verified,
        "match_score": verified_score if verified else unverified_score,
        "checked_at": _now_iso(),
        "applicant_id": applicant_id,
        "reason": "match" if verified else "no_match_on_register",
        "bureau": BUREAU_NAME,
    }


def get_report(applicant_id: str, today: date | None = None) -> dict[str, Any]:
    """Credit report. Deterministic per applicant except `report_date`, which is today."""
    applicant_id = str(applicant_id).strip()
    rng = _rng(applicant_id, "report")

    score = int(round(rng.gauss(SCORE_MEAN, SCORE_SD)))
    score = max(SCORE_MIN, min(SCORE_MAX, score))

    open_lines = rng.choices(range(0, 9), weights=[6, 14, 20, 20, 15, 10, 7, 5, 3])[0]
    total_debt = 0
    for _ in range(open_lines):
        total_debt += int(round(rng.uniform(500, 18_000) / 10) * 10)

    # Lower scores go with more trouble, but keep some noise so it is not a lookup table.
    trouble = max(0.0, (SCORE_MEAN - score) / 200.0)
    delinquencies = rng.choices(range(0, 4), weights=[70 - 30 * trouble, 18 + 10 * trouble,
                                                       8 + 12 * trouble, 4 + 8 * trouble])[0]
    inquiries = rng.choices(range(0, 9), weights=[22, 24, 18, 12, 8, 6, 4, 3, 3])[0]

    return {
        "score": score,
        "open_credit_lines": open_lines,
        "total_debt_eur": total_debt,
        "delinquencies_24m": delinquencies,
        "inquiries_6m": inquiries,
        "report_date": (today or datetime.now(UTC).date()).isoformat(),
        "applicant_id": applicant_id,
        "bureau": BUREAU_NAME,
    }
