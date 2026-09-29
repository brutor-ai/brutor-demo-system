"""CLI: put applications that were handed off to manual review back in the queue.

    python -m applications_mcp.requeue APP-20260927-0042 [APP-... ...]
    python -m applications_mcp.requeue --all

Moves each application from `needs_manual_review` back to `received` (so
`applications_list_pending` lists it again and the screening agent screens it on
its next tick) and appends a note saying so. Applications in any other status are
reported and left alone: a screened application keeps its recorded recommendation.

This is an operator action, not an agent tool: the screening agent can hand an
application off (`applications_hand_off`) but can never pull one back. Safe to run
while the server is up (the store reloads the file when it changes); inside the
container: `docker exec brutor-demo-applications-mcp python -m applications_mcp.requeue --all`,
or `./demo.sh requeue` which also clears the agent's local skip list.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from applications_mcp.store import Store, resolve_data_dir

MANUAL_REVIEW = "needs_manual_review"
NOTE = "Re-queued for automated pre-screening by an operator (was in manual review)."


def requeue(store: Store, application_ids: Iterable[str] | None = None, *, note: str = NOTE) -> dict[str, Any]:
    """Move applications from needs_manual_review back to received.

    `application_ids=None` re-queues every application in manual review.
    Returns {"requeued": [...], "not_found": [...], "not_in_manual_review": {id: status}}."""
    wanted = None if application_ids is None else list(dict.fromkeys(application_ids))
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _apply(state: dict[str, Any]) -> dict[str, Any]:
        apps = state["applications"]
        ids = [k for k, v in apps.items() if v.get("status") == MANUAL_REVIEW] if wanted is None else wanted
        out: dict[str, Any] = {"requeued": [], "not_found": [], "not_in_manual_review": {}}
        for app_id in ids:
            record = apps.get(app_id)
            if record is None:
                out["not_found"].append(app_id)
                continue
            if record.get("status") != MANUAL_REVIEW:
                out["not_in_manual_review"][app_id] = record.get("status")
                continue
            record["status"] = "received"
            record["requeued_at"] = now
            record.setdefault("notes", []).append({"at": now, "note": note})
            out["requeued"].append(app_id)
        return out

    return store.mutate(_apply)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-queue applications handed off to manual review.")
    parser.add_argument("application_ids", nargs="*", metavar="APP_ID", help="applications to re-queue")
    parser.add_argument("--all", action="store_true", help="re-queue every application in needs_manual_review")
    parser.add_argument("--data-dir", default=None, help="override DATA_DIR")
    args = parser.parse_args(argv)
    if args.all == bool(args.application_ids):
        parser.error("name one or more APP_IDs, or pass --all (not both)")

    store = Store(resolve_data_dir(args.data_dir))
    out = requeue(store, None if args.all else args.application_ids)
    for app_id in out["requeued"]:
        print(f"{app_id}: needs_manual_review -> received")
    for app_id in out["not_found"]:
        print(f"{app_id}: not found")
    for app_id, status in out["not_in_manual_review"].items():
        print(f"{app_id}: status is {status}, left alone")
    print(f"{len(out['requeued'])} application(s) re-queued in {store.path}")
    return 0 if not out["not_found"] else 1


if __name__ == "__main__":
    sys.exit(main())
