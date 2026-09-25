"""Pending approval store and resolution (DESIGN.md section 4, human oversight).

When applications_set_recommendation comes back HTTP 202 approval_required,
the record node stores {application_id, approval_id, tool_args, run_id} here.
On every tick resolve_pending() polls each entry:

    approved  -> new run bds-<application_id>-approval-<ulid>, one step
                 apply_approved_decision, the identical tool call with
                 X-Approval-Token, Run-End completed / outcome resolved
    rejected  -> applications_add_note in its own short run
                 (Run-End completed / outcome handed_off), entry dropped
    expired   -> the hold is RE-RAISED: the identical tool call is re-issued
                 (no token) in run bds-<application_id>-rehold-<ulid>, step
                 reraise_approval, closed Run-End completed / outcome
                 escalated; the gateway answers 202 with a new approval id,
                 which replaces the old one. The application is never
                 re-screened while it is held, so the Tool Approvals queue
                 always holds the current decision until a human acts.
    pending   -> kept for the next tick
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .gateway import ApprovalRequired, Gateway, GatewayError, PolicyBlocked, new_ulid

log = logging.getLogger("screening_agent.approvals")

PENDING_FILE = "pending_approvals.json"
MAX_APPLY_ATTEMPTS = 3


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class PendingApprovals:
    """JSON-file backed list of approvals waiting on a human."""

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / PENDING_FILE

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("pending approvals file %s unreadable (%s); starting empty", self.path, exc)
            return []
        return data if isinstance(data, list) else []

    def save(self, entries: list[dict[str, Any]]) -> None:
        _atomic_write(self.path, entries)

    def add(self, application_id: str, approval_id: str, tool_args: dict[str, Any], run_id: str) -> dict[str, Any]:
        """One hold per application: a new hold replaces any earlier entry for
        the same application id (the earlier approval expires on its own)."""
        entries = [e for e in self.load() if e.get("approval_id") != approval_id and e.get("application_id") != application_id]
        entry = {
            "application_id": application_id,
            "approval_id": approval_id,
            "tool_args": tool_args,
            "run_id": run_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "apply_attempts": 0,
            "reraise_count": 0,
        }
        entries.append(entry)
        self.save(entries)
        return entry

    def remove(self, approval_id: str) -> None:
        entries = [e for e in self.load() if e.get("approval_id") != approval_id]
        self.save(entries)

    def update(self, entry: dict[str, Any]) -> None:
        entries = [e for e in self.load() if e.get("approval_id") != entry.get("approval_id")]
        entries.append(entry)
        self.save(entries)

    def all(self) -> list[dict[str, Any]]:
        return self.load()

    def count(self) -> int:
        return len(self.load())

    def application_ids(self) -> set[str]:
        return {str(e.get("application_id")) for e in self.load()}


def note_run(
    gw: Gateway,
    settings: Settings,
    application_id: str,
    note: str,
    *,
    suffix: str,
    step_id: str,
    step_name: str,
    outcome: str = "handed_off",
) -> str:
    """Add a note to an application inside its own short run that is closed
    on the same call. Returns the run id."""
    run_id = f"bds-{application_id}-{suffix}-{new_ulid()}"
    with gw.run(run_id):
        gw.mcp_call(
            settings.applications_mcp_server_id,
            "applications_add_note",
            {"application_id": application_id, "note": note},
            step_id=step_id,
            step_name=step_name,
            run_end="completed",
            outcome=outcome,
        )
    log.info("run=%s state=completed outcome=%s application=%s note=%s", run_id, outcome, application_id, note[:80])
    return run_id


def apply_approved(gw: Gateway, settings: Settings, entry: dict[str, Any], token: str) -> str:
    """Re-issue the identical applications_set_recommendation with the token
    in a new short run. Returns the run id. Raises on failure."""
    application_id = str(entry["application_id"])
    run_id = f"bds-{application_id}-approval-{new_ulid()}"
    with gw.run(run_id):
        try:
            gw.mcp_call(
                settings.applications_mcp_server_id,
                "applications_set_recommendation",
                dict(entry["tool_args"]),
                step_id="apply_approved_decision",
                step_name="Apply underwriter-approved decision",
                run_end="completed",
                outcome="resolved",
                approval_token=token,
            )
        except PolicyBlocked:
            gw.end_run("blocked_policy", None)
            raise
        except Exception:
            gw.end_run("errored", None)
            raise
    log.info("run=%s state=completed outcome=resolved application=%s approval=%s", run_id, application_id, entry["approval_id"])
    return run_id


def reraise_hold(gw: Gateway, settings: Settings, entry: dict[str, Any]) -> str | None:
    """Re-issue the identical applications_set_recommendation call (no token)
    so the gateway opens a fresh approval. Returns the new approval id, or
    None when the call went through without a hold (the policy changed):
    the caller then treats the decision as applied. Raises on other errors."""
    application_id = str(entry["application_id"])
    run_id = f"bds-{application_id}-rehold-{new_ulid()}"
    with gw.run(run_id):
        try:
            gw.mcp_call(
                settings.applications_mcp_server_id,
                "applications_set_recommendation",
                dict(entry["tool_args"]),
                step_id="reraise_approval",
                step_name="Re-raise underwriter approval",
                run_end="completed",
                outcome="escalated",
            )
        except ApprovalRequired as held:
            log.info("run=%s state=completed outcome=escalated application=%s approval=%s (re-raised from %s)", run_id, application_id, held.approval_id, entry.get("approval_id"))
            return held.approval_id
        except PolicyBlocked:
            gw.end_run("blocked_policy", None)
            raise
        except Exception:
            gw.end_run("errored", None)
            raise
    # No 202: the call was recorded outright. The run closed "escalated" on the
    # header because we expected a hold; correct the ledger.
    log.warning("run=%s re-raise for application=%s was recorded without a hold; treating as applied", run_id, application_id)
    return None


def resolve_pending(
    gw: Gateway,
    settings: Settings,
    store: PendingApprovals,
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Poll every pending approval and act on the answer. Never raises for a
    single entry; counts what happened."""
    counts = {"approved": 0, "rejected": 0, "reraised": 0, "applied_without_hold": 0, "handed_off": 0, "pending": 0, "errored": 0}
    for entry in store.all():
        approval_id = str(entry.get("approval_id"))
        application_id = str(entry.get("application_id"))
        try:
            poll = gw.poll_approval(approval_id)
        except GatewayError as exc:
            log.warning("approval=%s poll failed: %s", approval_id, exc)
            counts["errored"] += 1
            continue
        status = str(poll.get("status", "")).lower()
        if status == "pending":
            counts["pending"] += 1
            continue
        if status == "approved":
            token = poll.get("approval_token")
            if not token:
                log.warning("approval=%s approved but no token returned yet; retrying next tick", approval_id)
                counts["pending"] += 1
                continue
            try:
                run_id = apply_approved(gw, settings, entry, str(token))
            except ApprovalRequired as exc:
                # The token was not honoured; a fresh hold was opened. Track it instead.
                log.warning("approval=%s re-held as %s", approval_id, exc.approval_id)
                _replace_approval(store, entry, exc.approval_id)
                counts["pending"] += 1
                continue
            except Exception as exc:
                entry["apply_attempts"] = int(entry.get("apply_attempts", 0)) + 1
                if entry["apply_attempts"] >= MAX_APPLY_ATTEMPTS:
                    log.error("approval=%s apply failed %d times, dropping: %s", approval_id, entry["apply_attempts"], exc)
                    store.remove(approval_id)
                    _safe_note(gw, settings, application_id, f"Approved decision could not be recorded after {entry['apply_attempts']} attempts (approval id {approval_id}): {exc}", suffix="approval")
                else:
                    log.error("approval=%s apply failed (attempt %d): %s", approval_id, entry["apply_attempts"], exc)
                    store.update(entry)
                counts["errored"] += 1
                continue
            store.remove(approval_id)
            counts["approved"] += 1
            if on_event:
                on_event("approved", {"application_id": application_id, "approval_id": approval_id, "run_id": run_id})
            continue
        if status == "rejected":
            try:
                _safe_note(
                    gw,
                    settings,
                    application_id,
                    f"Underwriter rejected the automated recommendation "
                    f"'{entry.get('tool_args', {}).get('recommendation')}' (approval id {approval_id}); "
                    f"handed off to manual underwriting.",
                    suffix="approval",
                )
            finally:
                store.remove(approval_id)
            counts["rejected"] += 1
            if on_event:
                on_event("rejected", {"application_id": application_id, "approval_id": approval_id})
            continue
        # expired, not_found or anything else: the human never decided. Keep the
        # application held by re-raising the identical call, never re-screen it.
        reraise_count = int(entry.get("reraise_count", 0))
        if settings.reraise_max and reraise_count >= settings.reraise_max:
            log.warning("approval=%s expired %d times (RERAISE_MAX=%d); handing off application=%s", approval_id, reraise_count, settings.reraise_max, application_id)
            try:
                _safe_note(
                    gw,
                    settings,
                    application_id,
                    f"Underwriter hold expired {reraise_count + 1} times without a decision (last approval id {approval_id}); "
                    f"recommendation '{entry.get('tool_args', {}).get('recommendation')}' was not recorded; handed off to manual underwriting.",
                    suffix="approval",
                )
            finally:
                store.remove(approval_id)
            counts["handed_off"] += 1
            if on_event:
                on_event("expired", {"application_id": application_id, "approval_id": approval_id})
            continue
        try:
            new_id = reraise_hold(gw, settings, entry)
        except Exception as exc:
            log.error("approval=%s expired (%s) and re-raise failed; will retry next tick: %s", approval_id, status, exc)
            counts["errored"] += 1
            continue
        if new_id is None:
            store.remove(approval_id)
            counts["applied_without_hold"] += 1
            if on_event:
                on_event("approved", {"application_id": application_id, "approval_id": approval_id, "run_id": None})
            continue
        _replace_approval(store, entry, new_id, reraised=True)
        counts["reraised"] += 1
        if on_event:
            on_event("reraised", {"application_id": application_id, "approval_id": new_id, "previous_approval_id": approval_id})
    return counts


def _replace_approval(store: PendingApprovals, entry: dict[str, Any], new_approval_id: str, *, reraised: bool = False) -> None:
    """Swap the approval id on a stored entry, keeping the identical tool args."""
    old_id = str(entry.get("approval_id"))
    updated = dict(entry)
    updated["approval_id"] = new_approval_id
    updated["previous_approval_id"] = old_id
    updated["reraised_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if reraised:
        updated["reraise_count"] = int(entry.get("reraise_count", 0)) + 1
    store.remove(old_id)
    store.update(updated)


def _safe_note(gw: Gateway, settings: Settings, application_id: str, note: str, *, suffix: str) -> None:
    try:
        note_run(
            gw,
            settings,
            application_id,
            note,
            suffix=suffix,
            step_id="approval_closed",
            step_name="Approval hold closed",
            outcome="handed_off",
        )
    except Exception as exc:  # the note is best effort; the entry is dropped either way
        log.error("application=%s could not add note: %s", application_id, exc)
