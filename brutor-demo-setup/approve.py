#!/usr/bin/env python3
"""approve.py: decide the Brutor Demo System's held decisions as an administrator.

The screening agent's adverse or large recommendations are held by the gateway
(DESIGN.md section 4, human oversight). A person decides each one. In every
edition of Brutor Agent Control that person can be an administrator with
`tool_approval:manage`, in the Admin Console (Mission Control -> Approvals).
This script is the same decision through the same control-plane routes, for a
terminal and for an unattended run:

    python approve.py                    list the pending holds of the demo system
    python approve.py --all              approve every pending hold
    python approve.py ID [ID ...]        approve these
    python approve.py --reject ID [...]  reject these (the agent hands the
                                         application off to manual review)
    python approve.py --all --wait 900   wait up to 900 s for a hold to appear,
                                         then approve what is pending
    --note TEXT                          recorded with the decision

Routes (Control Plane, tenant administrator's token):
    GET  /v1/admin/approvals?status_filter=pending&group_id=<demo system>
    GET  /v1/admin/approvals/{id}
    POST /v1/admin/approvals/{id}/approve   {"note": "..."}
    POST /v1/admin/approvals/{id}/reject    {"note": "..."}

The agent learns of the decision on its next tick, when it polls the gateway
(`GET /v1/approvals/{id}/poll` with its JWT-SVID) and receives the one-time
approval token. Nothing here talks to the agent.

Exit status: 0 = done (including "nothing pending" without --wait); 1 = a
decision was refused or a call failed; 3 = --wait ran out with nothing pending.
The administrator's credentials come from the environment exactly as for
setup.py (see `.env.example`); there is no stock password.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from setup import (  # noqa: E402  (shares the client and config with setup.py)
    ADMIN_USER, CP_URL, DEMO_SYSTEM_NAME, TOOL_APPROVALS_PATH, Client, StepError, _short,
    admin_login, find, listing, read_demo_env,
)

APPROVE = "approve"
REJECT = "reject"
PAGE_SIZE = 200  # the endpoint's maximum
DEFAULT_NOTE = "Decided through the Control Plane by approve.py (Brutor Demo System)"
#: What the Control Plane answers when a decision cannot be made, and what
#: that means for the operator. Anything else is reported verbatim.
REFUSALS = {
    404: "no such approval in this tenant",
    409: "already decided (or no longer pending)",
    410: "expired; the agent re-raises it on its next tick",
    503: "the Control Plane could not reach the gateway; nothing was changed, try again",
}


def demo_group_id(cp: Client, env: Dict[str, str]) -> str:
    """The Demo System's resource group: from .demo.env, else by name."""
    if env.get("DEMO_SYSTEM_GROUP_ID"):
        return env["DEMO_SYSTEM_GROUP_ID"]
    cp.step = "group"
    _, resp = cp.get("/v1/admin/resource-groups")
    row = find(listing(resp, "groups"), name=DEMO_SYSTEM_NAME)
    if not row:
        raise StepError("group", f"no AI System named {DEMO_SYSTEM_NAME}: run ./demo.sh provision first")
    return row["id"]


def pending(cp: Client, gid: str) -> List[dict]:
    """Every pending, unexpired hold raised under the Demo System, oldest first."""
    cp.step = "list"
    rows: List[dict] = []
    page = 1
    while True:
        _, resp = cp.get(TOOL_APPROVALS_PATH, params={
            "status_filter": "pending", "group_id": gid, "page": page, "page_size": PAGE_SIZE})
        items = listing(resp, "items")
        rows.extend(items)
        total = resp.get("total") if isinstance(resp, dict) else None
        if not items or not isinstance(total, int) or len(rows) >= total:
            break
        page += 1
    return list(reversed(rows))  # the endpoint lists newest first


def describe(cp: Client, row: dict) -> str:
    """One line for a hold: what would be recorded, for whom, until when."""
    cp.step = "detail"
    _, resp = cp.get(f"{TOOL_APPROVALS_PATH}/{row['id']}")
    detail = resp.get("approval") if isinstance(resp, dict) and isinstance(resp.get("approval"), dict) else {}
    args = detail.get("call_arguments") if isinstance(detail.get("call_arguments"), dict) else {}
    what = ", ".join(f"{k}={args[k]}" for k in ("application_id", "recommendation", "amount_eur") if k in args)
    return (f"{row['id']}  {row.get('capability_name')}"
            + (f"  [{what}]" if what else "")
            + f"  by {row.get('requester_display') or row.get('requester_type')}"
            + f"  expires {row.get('expires_at')}")


def decide(cp: Client, approval_id: str, decision: str, note: Optional[str]) -> Tuple[bool, str]:
    """POST the decision. (True, status) when the Control Plane recorded it;
    (False, reason) when it refused. A transport failure raises StepError."""
    cp.step = decision
    status, resp = cp.post(f"{TOOL_APPROVALS_PATH}/{approval_id}/{decision}",
                           {"note": note} if note else {}, tolerate=tuple(REFUSALS))
    if status in REFUSALS:
        detail = resp.get("detail") if isinstance(resp, dict) else None
        return False, f"HTTP {status}: {REFUSALS[status]}" + (f" ({_short(detail, 160)})" if detail else "")
    want = "approved" if decision == APPROVE else "rejected"
    got = resp.get("status") if isinstance(resp, dict) else None
    if got != want:
        return False, f"the Control Plane answered status {got!r}, not {want!r}: {_short(resp, 200)}"
    return True, f"{got} by {resp.get('resolved_by')} at {resp.get('resolved_at')}"


def wait_for_pending(cp: Client, gid: str, seconds: int, *, every: int = 10,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> List[dict]:
    """Poll until at least one hold is pending or the time is up."""
    deadline = clock() + seconds
    while True:
        rows = pending(cp, gid)
        if rows or clock() >= deadline:
            return rows
        sleep(min(every, max(0.0, deadline - clock())))


def run(args: argparse.Namespace, cp: Client, env: Dict[str, str], out: Callable[[str], None] = print) -> int:
    gid = demo_group_id(cp, env)
    decision = REJECT if args.reject else APPROVE
    explicit: List[str] = list(args.reject or []) + list(args.ids or [])

    if not args.all and not explicit:
        rows = wait_for_pending(cp, gid, args.wait) if args.wait else pending(cp, gid)
        if not rows:
            out(f"no held decision is pending for {DEMO_SYSTEM_NAME} ({gid})")
            return 3 if args.wait else 0
        out(f"{len(rows)} held decision(s) pending for {DEMO_SYSTEM_NAME} ({gid}):")
        for row in rows:
            out("  " + describe(cp, row))
        out("approve: python approve.py ID [ID ...] | --all     reject: python approve.py --reject ID [...]")
        out("or in the Admin Console: Mission Control -> Approvals")
        return 0

    if args.all:
        rows = wait_for_pending(cp, gid, args.wait) if args.wait else pending(cp, gid)
        if not rows:
            out(f"no held decision is pending for {DEMO_SYSTEM_NAME} ({gid})")
            return 3 if args.wait else 0
        targets = [row["id"] for row in rows]
    else:
        targets = explicit

    note = args.note or DEFAULT_NOTE
    failed = 0
    for approval_id in targets:
        done, text = decide(cp, approval_id, decision, note)
        out(f"{'✓' if done else '✗'} {decision} {approval_id}: {text}")
        failed += 0 if done else 1
    if failed:
        return 1
    out(f"{len(targets)} decision(s) recorded as {ADMIN_USER}. The agent applies each on its next tick "
        "(./demo.sh logs screening-agent).")
    return 0


def parse(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="List, approve or reject the Brutor Demo System's held decisions as the tenant administrator.")
    ap.add_argument("ids", nargs="*", metavar="ID", help="approval ids to approve")
    ap.add_argument("--all", action="store_true", help="approve every pending hold of the demo system")
    ap.add_argument("--reject", nargs="+", metavar="ID", help="reject these approval ids instead")
    ap.add_argument("--note", help="note recorded with each decision")
    ap.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                    help="wait up to this long for a pending hold before listing or approving (exit 3 if none)")
    args = ap.parse_args(argv)
    if args.reject and (args.all or args.ids):
        ap.error("--reject takes its own ids; it cannot be combined with --all or ids to approve")
    if args.all and args.ids:
        ap.error("--all approves everything pending; do not name ids as well")
    if args.wait < 0:
        ap.error("--wait must be 0 or more seconds")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse(argv)
    cp = Client(CP_URL)
    try:
        admin_login(cp)
        return run(args, cp, read_demo_env())
    except StepError as exc:
        print(f"✗ {exc.step}: {exc.reason}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
