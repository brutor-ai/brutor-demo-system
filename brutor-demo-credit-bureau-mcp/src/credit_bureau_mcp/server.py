"""Credit Bureau MCP server (Brutor Demo System, DESIGN.md section 5.2).

FastMCP over streamable HTTP at `/mcp`, `stateless_http=True` and `json_response=True`.
Both flags are mandatory: the Brutor Core Proxy posts raw JSON-RPC `tools/call`
requests without an `initialize` handshake and expects a plain JSON body back.

Both tools are read-only and carry `readOnlyHint=True`. On a system flagged
`sensitive_data` the gateway seals read-only calls that return personal data as
sensitive-read evidence, so the annotation is part of the contract.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from credit_bureau_mcp import bureau

SERVICE_NAME = "brutor-demo-credit-bureau-mcp"
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "3015"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

log = logging.getLogger("credit_bureau_mcp")

mcp = FastMCP(
    name=SERVICE_NAME,
    instructions=(
        "Mock KYC and credit reference agency (Borealis Demo Bureau, synthetic data). "
        "Verify an applicant's identity and fetch a credit report."
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    log_level=LOG_LEVEL if LOG_LEVEL in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO",
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True)


@mcp.tool(annotations=READ_ONLY)
def bureau_verify_identity(applicant_id: str, full_name: str, date_of_birth: str) -> dict[str, Any]:
    """Verify an applicant against the KYC register.

    Returns `{verified, match_score (0..1), checked_at, reason, bureau}`. The outcome is
    deterministic per `applicant_id`; about 4 percent of applicants are unverified.

    Args:
        applicant_id: the lender's applicant reference (for example `CUST-483920`).
        full_name: the applicant's full name as written on the application.
        date_of_birth: ISO date, `YYYY-MM-DD`.
    """
    return bureau.verify_identity(applicant_id, full_name, date_of_birth)


@mcp.tool(annotations=READ_ONLY)
def bureau_get_report(applicant_id: str) -> dict[str, Any]:
    """Fetch the applicant's credit report.

    Returns `{score (300..900), open_credit_lines, total_debt_eur, delinquencies_24m,
    inquiries_6m, report_date, bureau}`. Deterministic per `applicant_id`;
    `report_date` is today's date.

    Args:
        applicant_id: the lender's applicant reference.
    """
    return bureau.get_report(applicant_id)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": SERVICE_NAME, "bureau": bureau.BUREAU_NAME})


def main() -> None:
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("serving %s on http://%s:%s/mcp", SERVICE_NAME, HOST, PORT)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
