"""Loan Applications MCP server (Brutor Demo System, DESIGN.md section 5.1).

FastMCP over streamable HTTP at `/mcp`, `stateless_http=True` and `json_response=True`.
Both flags are mandatory: the Brutor Core Proxy posts raw JSON-RPC `tools/call`
requests without an `initialize` handshake and expects a plain JSON body back, not an
SSE stream.

Read-only tools carry `readOnlyHint=True`. The gateway reads that annotation to decide
which calls count as sensitive reads for evidence sealing, so the annotation is part of
the contract, not decoration.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from applications_mcp.generator import Generator, generator_enabled, purpose_short
from applications_mcp.store import Store, resolve_data_dir

SERVICE_NAME = "brutor-demo-applications-mcp"
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "3014"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

VALID_RECOMMENDATIONS = ("approve", "refer", "decline")
STATUSES = ("received", "screened")

log = logging.getLogger("applications_mcp")

mcp = FastMCP(
    name=SERVICE_NAME,
    instructions=(
        "Mock loan origination system of Borealis Consumer Finance AB (synthetic data). "
        "List pending applications, read one, record a screening recommendation, add notes."
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    log_level=LOG_LEVEL if LOG_LEVEL in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO",
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

_store: Store | None = None
_generator: Generator | None = None


def configure(data_dir: str | Path | None = None) -> Store:
    """Create (or replace) the process-wide store. Tests call this with a temp dir."""
    global _store
    _store = Store(resolve_data_dir(str(data_dir) if data_dir is not None else None))
    log.info("using data file %s", _store.path)
    return _store


def get_store() -> Store:
    if _store is None:
        return configure()
    return _store


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _not_found(application_id: str) -> dict[str, Any]:
    return {"ok": False, "error": "not_found", "application_id": application_id}


# ---- tools ---------------------------------------------------------------------------


def list_pending(limit: int = 10) -> list[dict[str, Any]]:
    """Pending applications (status `received`), oldest first, as Python objects."""
    limit = max(1, min(int(limit), 100))
    pending = [a for a in get_store().all_applications() if a["status"] == "received"]
    pending.sort(key=lambda a: (a["received_at"], a["application_id"]))
    return [
        {
            "application_id": a["application_id"],
            "received_at": a["received_at"],
            "amount_eur": a["requested_amount_eur"],
            "purpose_short": purpose_short(a["purpose"]),
        }
        for a in pending[:limit]
    ]


@mcp.tool(annotations=READ_ONLY)
def applications_list_pending(limit: int = 10) -> str:
    """List applications that have not been screened yet (status `received`), oldest first.

    Returns a JSON array `[{application_id, received_at, amount_eur, purpose_short}]` as
    one text item. (It is returned as a string on purpose: FastMCP would otherwise split
    a Python list into one content item per element, and an empty list into no content
    at all, while the gateway contract is "the JSON is in result.content[0].text".)

    Args:
        limit: maximum number of applications to return (1 to 100, default 10).
    """
    return json.dumps(list_pending(limit))


@mcp.tool(annotations=READ_ONLY)
def applications_get(application_id: str) -> dict[str, Any]:
    """Return the full application record, including the applicant's personal data.

    Args:
        application_id: id of the form `APP-<yyyymmdd>-<seq>`.
    """
    record = get_store().get(application_id)
    if record is None:
        return _not_found(application_id)
    return record


@mcp.tool(annotations=WRITE)
def applications_set_recommendation(
    application_id: str,
    recommendation: str,
    amount_eur: float,
    rationale: str,
    risk_band: str,
    affordability_class: str,
    fraud_verdict: str,
    customer_letter: str,
) -> dict[str, Any]:
    """Record the screening recommendation and mark the application as screened.

    Fails with `already_screened` if a recommendation was recorded before, and with
    `invalid_recommendation` unless `recommendation` is approve, refer or decline.

    Args:
        application_id: the application to update.
        recommendation: one of approve, refer, decline.
        amount_eur: the loan amount the recommendation applies to. The record already
            holds `requested_amount_eur`; this argument exists because the gateway's
            argument policy reads `amount_eur` from the call arguments to decide whether
            an underwriter has to approve the call (amounts above 25,000 EUR are held).
        rationale: why this recommendation, including which deterministic rule fired.
        risk_band: low, medium or high from the classifier.
        affordability_class: comfortable, tight or unaffordable from the skill.
        fraud_verdict: clear, review or hit from the fraud screener.
        customer_letter: the letter drafted for the applicant, with the AI disclosure.
    """
    recommendation = str(recommendation).strip().lower()
    if recommendation not in VALID_RECOMMENDATIONS:
        return {
            "ok": False,
            "error": "invalid_recommendation",
            "application_id": application_id,
            "allowed": list(VALID_RECOMMENDATIONS),
        }

    def _apply(state: dict[str, Any]) -> dict[str, Any]:
        record = state["applications"].get(application_id)
        if record is None:
            return _not_found(application_id)
        if record["status"] == "screened":
            return {
                "ok": False,
                "error": "already_screened",
                "application_id": application_id,
                "status": "screened",
                "screened_at": record.get("screened_at"),
            }
        screened_at = _now_iso()
        record["status"] = "screened"
        record["recommendation"] = recommendation
        record["screened_at"] = screened_at
        record["screening"] = {
            "recommendation": recommendation,
            "amount_eur": float(amount_eur),
            "rationale": rationale,
            "risk_band": risk_band,
            "affordability_class": affordability_class,
            "fraud_verdict": fraud_verdict,
            "customer_letter": customer_letter,
            "recorded_at": screened_at,
        }
        return {
            "ok": True,
            "application_id": application_id,
            "status": "screened",
            "recommendation": recommendation,
            "screened_at": screened_at,
        }

    return get_store().mutate(_apply)


@mcp.tool(annotations=WRITE)
def applications_add_note(application_id: str, note: str) -> dict[str, Any]:
    """Append a free-text note to an application (for example an approval hold).

    Args:
        application_id: the application to annotate.
        note: the note text.
    """
    note = str(note).strip()
    if not note:
        return {"ok": False, "error": "empty_note", "application_id": application_id}

    def _apply(state: dict[str, Any]) -> dict[str, Any]:
        record = state["applications"].get(application_id)
        if record is None:
            return _not_found(application_id)
        record.setdefault("notes", []).append({"at": _now_iso(), "note": note})
        return {"ok": True, "application_id": application_id, "notes_count": len(record["notes"])}

    return get_store().mutate(_apply)


@mcp.tool(annotations=READ_ONLY)
def applications_stats() -> dict[str, Any]:
    """Counts of applications by status and by recommendation."""
    store = get_store()
    by_status = {s: 0 for s in STATUSES}
    by_recommendation = {r: 0 for r in VALID_RECOMMENDATIONS}
    total = 0
    for record in store.all_applications():
        total += 1
        by_status[record["status"]] = by_status.get(record["status"], 0) + 1
        rec = record.get("recommendation")
        if rec:
            by_recommendation[rec] = by_recommendation.get(rec, 0) + 1
    gen = store.generator_state()
    return {
        "total": total,
        "by_status": by_status,
        "by_recommendation": by_recommendation,
        "generated_total": int(gen.get("seq", 0)),
        "as_of": _now_iso(),
    }


# ---- health ----------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    store = get_store()
    return JSONResponse(
        {
            "status": "ok",
            "service": SERVICE_NAME,
            "applications": store.count(),
            "generator": "on" if _generator is not None else "off",
        }
    )


# ---- entry point -----------------------------------------------------------------------


def main() -> None:
    global _generator
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    configure()
    if generator_enabled():
        _generator = Generator(get_store())
        _generator.start()
        log.info(
            "generator on: interval=%ss batch=%s..%s seed=%s",
            _generator.interval_seconds, _generator.min_per_interval,
            _generator.max_per_interval, _generator.seed,
        )
    else:
        log.info("generator off (GENERATE_ENABLED=false)")
    log.info("serving %s on http://%s:%s/mcp", SERVICE_NAME, HOST, PORT)
    try:
        mcp.run(transport="streamable-http")
    finally:
        if _generator is not None:
            _generator.stop()


if __name__ == "__main__":
    main()
