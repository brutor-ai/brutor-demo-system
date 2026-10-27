"""The fraud screener's declared outcomes (RFC 0025 section 7a; DESIGN.md section 8).

`outcomes.json` beside this module is the ONE statement of how a screening
may end: `screening_completed` with the verdict as its variant (`clear`,
`review`, `hit`). brutor-demo-setup PUTs it as the Fraud Screener AI System's
catalogue. Every completed task reports its outcome twice, because the
gateway accepts either carrier:

    X-Brutor-Outcome-Id: screening_completed.<verdict>      (HTTP response header, sync)
    task.metadata["brutor.outcome.id"]                       (the final Task, streamed / async)

The gateway checks it against this system's active contract and hands the
checked pair (id + X-Brutor-Outcome-Conformance) to the caller.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOGUE_FILE = Path(__file__).with_name("outcomes.json")

OUTCOME_ID_HEADER = "X-Brutor-Outcome-Id"
OUTCOME_ID_META = "brutor.outcome.id"
OUTCOME_ID_RE = re.compile(r"^[a-z0-9_-]{1,48}(\.[a-z0-9_-]{1,48})?$")
SCREENING_COMPLETED = "screening_completed"


@lru_cache(maxsize=1)
def catalogue() -> list[dict[str, Any]]:
    return json.loads(CATALOGUE_FILE.read_text(encoding="utf-8"))["outcomes"]


def full_ids() -> frozenset[str]:
    out: set[str] = set()
    for outcome in catalogue():
        variants = outcome.get("variants") or []
        out.update(f"{outcome['id']}.{v['id']}" for v in variants) if variants else out.add(outcome["id"])
    return frozenset(out)


def outcome_for(verdict: str) -> str:
    """`screening_completed.<verdict>`, if this agent declares it; else ValueError
    (a verdict the catalogue does not name is a bug, never sent)."""
    outcome_id = f"{SCREENING_COMPLETED}.{verdict}"
    if not OUTCOME_ID_RE.fullmatch(outcome_id) or outcome_id not in full_ids():
        raise ValueError(f"verdict {verdict!r} has no declared outcome in {CATALOGUE_FILE.name}")
    return outcome_id
