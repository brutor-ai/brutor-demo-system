"""Tick loop and health server (DESIGN.md section 4, "Tick loop").

Each tick: resolve pending approvals, retry any hand-off that did not go
through, list pending applications (each listing call is its own short run
`bds-tick-<ulid>`, step `poll_pending`, closed on that same call so the ledger
never sees an abandoned one-call run), and process up to MAX_PER_TICK
sequentially, oldest first.

Failures are classified by failures.py:

- APPLICATION-class (a guardrail or policy refused this application's content,
  a tool error about it, a malformed record): counted per application in
  DATA_DIR. After max_attempts_per_application (3) the application is handed
  off: `applications_hand_off` moves it to the terminal status
  `needs_manual_review`, so it leaves the pending set for good. An operator
  re-queues it with `./demo.sh requeue`.
- SYSTEM-class (gateway, provider, delegate or governance condition: no
  answer, 5xx, 429, approval_required holds, autonomy denied, run aborted):
  never counted against the application. The tick stops at the first one and
  the scheduler backs off exponentially across ticks (BACKOFF_MAX_TICKS cap);
  any answered run clears the backoff.

Held (awaiting an underwriter) and skipped applications stay out of the batch
but can never starve it: the listing pages on with the cursor `after` until it
has MAX_PER_TICK screenable applications or the pending set is exhausted.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .approvals import PendingApprovals, hand_off_run, resolve_pending
from .config import Settings
from .failures import Failure, classify_failure, classify_reason
from .gateway import Gateway, GatewayError, new_ulid
from .graph import build_graph, process_application

log = logging.getLogger("screening_agent.scheduler")

RETRIES_FILE = "retries.json"
RETRIES_V1_BACKUP = "retries.v1.json"
TRACKER_VERSION = 2
RELEASED_KEEP = 500
SERVER_PAGE_MAX = 100  # applications_list_pending caps `limit` at 100


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty_state() -> dict[str, Any]:
    return {"version": TRACKER_VERSION, "attempts": {}, "skipped": {}, "released": []}


class RetryTracker:
    """Application-class attempt counts, the skip list and its history, in DATA_DIR.

    File `retries.json`, format v2:

        {"version": 2,
         "attempts": {app_id: n},          application-class failures only
         "skipped":  {app_id: {reason, failure_kind, attempts, at,
                               handed_off, handed_off_at?}},
         "released": [{application_id, why, released_at, ...}]}  newest last

    `handed_off` is true once applications_hand_off moved the application to
    needs_manual_review; until then the scheduler retries the hand-off each
    tick. `released` records why an entry left the skip list (requeued by an
    operator, back in pending after a hand-off, migrated), capped at 500.

    A v1 file (no "version": attempts + skipped, written before failures were
    classified) is migrated on read, and the original is kept as
    retries.v1.json: skip entries whose recorded reason is a SYSTEM-class
    failure are released (the application is screened again), the rest stay
    skipped and are handed off; v1 attempt counts mixed both classes and are
    not carried over.

    The file is re-read whenever it changes on disk, so `python -m
    screening_agent --requeue ...` run in the same container takes effect on
    the running scheduler's next tick.
    """

    def __init__(self, data_dir: Path | str, max_attempts: int = 3):
        self.path = Path(data_dir) / RETRIES_FILE
        self.max_attempts = max_attempts
        self._mtime_ns: int | None = None
        self._state = self._load()

    # -- persistence ------------------------------------------------------------
    def _stat(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _load(self) -> dict[str, Any]:
        self._mtime_ns = self._stat()
        if self._mtime_ns is None:
            return _empty_state()
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("retry file %s unreadable (%s); starting empty", self.path, exc)
            return _empty_state()
        if not isinstance(data, dict):
            log.error("retry file %s is not a JSON object; starting empty", self.path)
            return _empty_state()
        if data.get("version") != TRACKER_VERSION:
            data = self._migrate_v1(data, raw)
            self._write(data)
        for key, default in _empty_state().items():
            data.setdefault(key, default)
        return data

    def _migrate_v1(self, data: dict[str, Any], raw: str) -> dict[str, Any]:
        backup = self.path.with_name(RETRIES_V1_BACKUP)
        if not backup.exists():
            backup.write_text(raw, encoding="utf-8")
        state = _empty_state()
        now = _now()
        released = kept = 0
        for app_id, entry in (data.get("skipped") or {}).items():
            entry = dict(entry) if isinstance(entry, dict) else {"reason": str(entry)}
            failure = classify_reason(str(entry.get("reason", "")))
            if failure.is_system:
                state["released"].append(
                    {
                        **entry,
                        "application_id": app_id,
                        "failure_kind": failure.kind,
                        "released_at": now,
                        "why": f"migrated: {failure.label()} is the system's condition, not the application's; screened again",
                    }
                )
                released += 1
            else:
                state["skipped"][app_id] = {**entry, "failure_kind": failure.kind, "handed_off": False}
                kept += 1
        state["released"] = state["released"][-RELEASED_KEEP:]
        log.warning(
            "retry file %s migrated to v%d: %d skip(s) released (system-class reasons), %d kept for hand-off, "
            "%d v1 attempt count(s) not carried over; original kept as %s",
            self.path, TRACKER_VERSION, released, kept, len(data.get("attempts") or {}), backup.name,
        )
        return state

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self._mtime_ns = self._stat()

    def _save(self) -> None:
        self._write(self._state)

    def _refresh(self) -> None:
        if self._stat() != self._mtime_ns:
            self._state = self._load()

    # -- reads ------------------------------------------------------------------
    def is_skipped(self, application_id: str) -> bool:
        self._refresh()
        return application_id in self._state["skipped"]

    def skipped(self) -> dict[str, Any]:
        self._refresh()
        return {k: dict(v) for k, v in self._state["skipped"].items()}

    def released(self) -> list[dict[str, Any]]:
        self._refresh()
        return [dict(e) for e in self._state["released"]]

    def awaiting_hand_off(self) -> list[str]:
        self._refresh()
        return [k for k, v in self._state["skipped"].items() if not v.get("handed_off")]

    def attempts(self, application_id: str) -> int:
        self._refresh()
        return int(self._state["attempts"].get(application_id, 0))

    def exhausted(self, application_id: str) -> bool:
        return self.attempts(application_id) >= self.max_attempts

    # -- writes -----------------------------------------------------------------
    def record_failure(self, application_id: str, failure: Failure) -> int:
        """Count an APPLICATION-class failed attempt; returns the new count.
        System-class failures are refused: they are never the application's."""
        if failure.is_system:
            raise ValueError(f"system-class failure {failure.label()} must not count against {application_id}")
        n = self.attempts(application_id) + 1
        self._state["attempts"][application_id] = n
        self._save()
        return n

    def skip(self, application_id: str, reason: str, failure_kind: str) -> None:
        self._refresh()
        self._state["skipped"][application_id] = {
            "reason": reason[:500],
            "failure_kind": failure_kind,
            "attempts": int(self._state["attempts"].get(application_id, 0)),
            "at": _now(),
            "handed_off": False,
        }
        self._state["attempts"].pop(application_id, None)
        self._save()

    def mark_handed_off(self, application_id: str) -> None:
        self._refresh()
        entry = self._state["skipped"].get(application_id)
        if entry is not None:
            entry["handed_off"] = True
            entry["handed_off_at"] = _now()
            self._save()

    def clear(self, application_id: str) -> None:
        self._refresh()
        if application_id in self._state["attempts"]:
            self._state["attempts"].pop(application_id, None)
            self._save()

    def release(self, application_id: str, why: str) -> bool:
        """Take an application off the skip list (and reset its attempts),
        recording why. Returns False when it was neither skipped nor counted."""
        self._refresh()
        entry = self._state["skipped"].pop(application_id, None)
        attempts = self._state["attempts"].pop(application_id, None)
        if entry is None and attempts is None:
            return False
        record = dict(entry or {"attempts": attempts})
        record.update({"application_id": application_id, "released_at": _now(), "why": why})
        self._state["released"] = (self._state["released"] + [record])[-RELEASED_KEEP:]
        self._save()
        return True

    def requeue(self, application_ids: Iterable[str] | None = None, *, why: str = "requeued by an operator") -> list[str]:
        """Release the given applications (None = every skipped one) so the
        next tick screens them again. Returns the ids actually released."""
        self._refresh()
        ids = list(self._state["skipped"]) if application_ids is None else list(application_ids)
        return [app_id for app_id in ids if self.release(app_id, why)]


class Scheduler:
    def __init__(self, settings: Settings, gw: Gateway, store: PendingApprovals | None = None, tracker: RetryTracker | None = None):
        self.settings = settings
        self.gw = gw
        self.store = store or PendingApprovals(settings.data_dir)
        self.tracker = tracker or RetryTracker(settings.data_dir, settings.max_attempts_per_application)
        self.graph = build_graph(gw, settings, self.store)
        self.stop_event = threading.Event()
        self.started_at = time.time()
        # Backoff after a system-class failure: `streak` consecutive failed
        # probes, `ticks_left` ticks still to skip.
        self.backoff: dict[str, Any] = {"streak": 0, "ticks_left": 0, "last_failure": None}
        self._tripped = False  # a system failure stopped the current tick
        self._probe_last: str | None = None  # the application that tripped the backoff
        self.status: dict[str, Any] = {
            "ticks": 0,
            "tick_errors": 0,
            "ticks_backed_off": 0,
            "system_failures": 0,
            "last_tick_at": None,
            "last_tick_duration_ms": None,
            "applications_processed": 0,
            "runs_completed": 0,
            "runs_escalated": 0,
            "runs_errored": 0,
            "runs_blocked": 0,
            "approvals_applied": 0,
            "approvals_reraised": 0,
            "approvals_closed": 0,
            "handed_off": 0,
            "pending_approvals": self.store.count(),
            "held_applications": len(self.store.application_ids()),
            "skipped_applications": len(self.tracker.skipped()),
            "awaiting_hand_off": len(self.tracker.awaiting_hand_off()),
            "last_error": None,
        }

    # -- listing ----------------------------------------------------------------
    def list_pending(self, limit: int, after: str | None = None) -> list[dict[str, Any]]:
        """applications_list_pending in its own short run, closed on the same call.

        A proxied call without run headers makes the gateway mint a root that
        the idle sweeper later closes as `abandoned`, which would skew the
        completion and abandoned rates the envelope grades. So each listing
        call is one tiny run: bds-tick-<ulid>, step poll_pending, turn 1,
        X-Brutor-Run-End: completed, X-Brutor-Run-Outcome: resolved. `after`
        (the last application id of the previous page) is sent only when
        paging, so the first page's call is unchanged."""
        assert self.gw.ctx is None, "listing must not run inside an application run"
        args: dict[str, Any] = {"limit": limit}
        if after:
            args["after"] = after
        run_id = f"bds-tick-{new_ulid()}"
        with self.gw.run(run_id):
            out = self.gw.mcp_call(
                self.settings.applications_mcp_server_id,
                "applications_list_pending",
                args,
                step_id="poll_pending",
                step_name="Tick: poll pending applications",
                run_end="completed",
                outcome="resolved",
            )
        log.info("run=%s state=completed outcome=resolved tick_poll=1%s", run_id, f" after={after}" if after else "")
        if isinstance(out, dict):
            out = out.get("applications") or out.get("items") or []
        if not isinstance(out, list):
            raise GatewayError(f"applications_list_pending returned unexpected shape: {str(out)[:200]}")
        return [row for row in out if isinstance(row, dict) and row.get("application_id")]

    def _screenable(self, held: set[str], summary: dict[str, Any]) -> tuple[list[str], int]:
        """Screenable application ids, oldest first, and how many were listed.

        Pages past held and skipped applications (cursor `after`) until
        MAX_PER_TICK are found, the pending set is exhausted, or LIST_MAX_PAGES
        is reached. A skipped application that was handed off but is pending
        again was re-queued in the origination system: it is released here and
        screened afresh."""
        page_size = max(1, min(self.settings.list_limit, SERVER_PAGE_MAX))
        eligible: list[str] = []
        seen: set[str] = set()
        after: str | None = None
        pages = max(1, self.settings.list_max_pages)
        for page in range(1, pages + 1):
            rows = self.list_pending(page_size, after)
            fresh = [r for r in rows if str(r["application_id"]) not in seen]
            if rows and not fresh:
                log.warning("applications_list_pending ignored the cursor `after` (applications MCP older than 0.2.0?); paging stopped")
                break
            skipped = self.tracker.skipped()
            for row in fresh:
                app_id = str(row["application_id"])
                seen.add(app_id)
                if app_id in held:
                    summary["held"].append(app_id)
                elif app_id in skipped:
                    if skipped[app_id].get("handed_off"):
                        self.tracker.release(app_id, "pending again after hand-off (re-queued in the origination system)")
                        summary["released"].append(app_id)
                        eligible.append(app_id)
                    else:
                        summary["skipped"].append(app_id)
                else:
                    eligible.append(app_id)
            if len(eligible) >= self.settings.max_per_tick or len(rows) < page_size:
                break
            if page == pages:
                log.warning("stopped paging after %d page(s) of %d; %d screenable found", pages, page_size, len(eligible))
                break
            after = str(rows[-1]["application_id"])
        return eligible, len(seen)

    # -- failures and backoff ---------------------------------------------------
    def _trip(self, failure: Failure, application_id: str | None) -> None:
        """A system-class failure: stop this tick and back off across ticks."""
        self._tripped = True
        self.status["system_failures"] += 1
        streak = self.backoff["streak"] + 1
        skip = min(2 ** min(streak - 1, 16) - 1, max(0, self.settings.backoff_max_ticks))
        self.backoff.update(
            {
                "streak": streak,
                "ticks_left": skip,
                "last_failure": {"kind": failure.label(), "application_id": application_id, "detail": failure.detail[:300], "at": _now()},
            }
        )
        self._probe_last = application_id
        log.error(
            "SYSTEM failure (%s)%s: not counted against any application; tick stopped; backing off %d tick(s) "
            "(streak %d, next probe in ~%ds): %s",
            failure.label(),
            f" on application={application_id}" if application_id else "",
            skip,
            streak,
            (skip + 1) * self.settings.tick_seconds,
            failure.detail[:300],
        )

    def _answered(self) -> None:
        """The system answered (a run completed or failed on the application's
        own account): clear the backoff."""
        if self.backoff["streak"]:
            log.info("system answered again after %d failed probe(s); backoff cleared", self.backoff["streak"])
        self.backoff.update({"streak": 0, "ticks_left": 0})

    def _hand_off(self, application_id: str) -> Failure | None:
        """Move a skipped application to needs_manual_review. Returns None on
        success, else the classified failure (it stays awaiting hand-off)."""
        entry = self.tracker.skipped().get(application_id) or {}
        note = (
            f"Automated pre-screening gave up after {entry.get('attempts', '?')} attempts "
            f"({entry.get('failure_kind', 'application')}); handed off to manual underwriting. "
            f"Last error: {str(entry.get('reason', ''))[:300]}"
        )
        try:
            hand_off_run(
                self.gw,
                self.settings,
                application_id,
                note,
                suffix="giveup",
                step_id="give_up",
                step_name="Automated screening abandoned after retries",
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            failure = classify_failure(exc)
            log.error("application=%s hand-off failed (%s); it stays skipped and is retried next tick: %s", application_id, failure.label(), exc)
            if failure.is_system:
                self._trip(failure, None)
            return failure
        self.tracker.mark_handed_off(application_id)
        self.status["handed_off"] += 1
        return None

    def _retry_hand_offs(self, summary: dict[str, Any]) -> None:
        for app_id in self.tracker.awaiting_hand_off()[: max(1, self.settings.max_per_tick)]:
            if self._tripped or self.stop_event.is_set():
                return
            if self._hand_off(app_id) is None:
                summary["handed_off"].append(app_id)

    def _refresh_counts(self) -> None:
        self.status["pending_approvals"] = self.store.count()
        self.status["held_applications"] = len(self.store.application_ids())
        self.status["skipped_applications"] = len(self.tracker.skipped())
        self.status["awaiting_hand_off"] = len(self.tracker.awaiting_hand_off())

    # -- one application --------------------------------------------------------
    def process_one(self, application_id: str):
        result = process_application(self.gw, self.settings, self.store, application_id, graph=self.graph)
        self.status["applications_processed"] += 1
        if result.state == "completed":
            self.tracker.clear(application_id)
            self._answered()
            if result.outcome == "escalated":
                self.status["runs_escalated"] += 1
            else:
                self.status["runs_completed"] += 1
        else:
            if result.state == "blocked_policy":
                self.status["runs_blocked"] += 1
            else:
                self.status["runs_errored"] += 1
            self.status["last_error"] = f"{application_id}: {result.error}"
            failure = result.failure or classify_reason(f"{result.state}: {result.error}")
            if failure.is_system:
                self._trip(failure, application_id)
            else:
                self._answered()
                n = self.tracker.record_failure(application_id, failure)
                log.warning(
                    "application=%s failed on its own account (%s), attempt %d of %d",
                    application_id, failure.label(), n, self.tracker.max_attempts,
                )
                if self.tracker.exhausted(application_id):
                    reason = f"{result.state}: {result.error}"
                    log.error(
                        "application=%s failed %d times (%s); handing off to manual review (status needs_manual_review)",
                        application_id, n, failure.label(),
                    )
                    self.tracker.skip(application_id, reason, failure.kind)
                    self._hand_off(application_id)
        self._refresh_counts()
        return result

    # -- one tick ---------------------------------------------------------------
    def backoff_view(self) -> dict[str, Any]:
        return {
            "active": self.backoff["ticks_left"] > 0,
            "streak": self.backoff["streak"],
            "ticks_left": self.backoff["ticks_left"],
            "max_ticks": self.settings.backoff_max_ticks,
            "last_failure": self.backoff["last_failure"],
        }

    def tick(self) -> dict[str, Any]:
        started = time.monotonic()
        self._tripped = False
        summary: dict[str, Any] = {"approvals": {}, "processed": [], "skipped": [], "held": [], "handed_off": [], "released": []}
        try:
            if self.backoff["ticks_left"] > 0:
                self.backoff["ticks_left"] -= 1
                self.status["ticks_backed_off"] += 1
                last = self.backoff["last_failure"] or {}
                log.warning(
                    "tick %d: backing off after a system failure (%s); nothing called, %d more tick(s) to skip",
                    self.status["ticks"] + 1, last.get("kind", "?"), self.backoff["ticks_left"],
                )
                summary["backoff"] = self.backoff_view()
                return summary

            def on_event(kind: str, info: dict[str, Any]) -> None:
                if kind == "approved":
                    self.status["approvals_applied"] += 1
                elif kind == "reraised":
                    self.status["approvals_reraised"] += 1
                else:
                    self.status["approvals_closed"] += 1

            summary["approvals"] = resolve_pending(self.gw, self.settings, self.store, on_event=on_event)
            self.status["pending_approvals"] = self.store.count()

            self._retry_hand_offs(summary)
            if self._tripped:
                summary["stopped"] = "system failure during a hand-off; see backoff"
                summary["backoff"] = self.backoff_view()
                return summary

            held = self.store.application_ids()
            self.status["held_applications"] = len(held)
            eligible, listed = self._screenable(held, summary)
            if self._probe_last in eligible:
                # The application that tripped the backoff goes last in the
                # probe, so a failure misclassified as the system's cannot
                # keep the rest of the queue waiting behind it.
                eligible.remove(self._probe_last)
                eligible.append(self._probe_last)
            self._probe_last = None
            todo = eligible[: self.settings.max_per_tick]
            log.info(
                "tick %d: %d pending listed, %d to process, %d held for underwriter (%d in store), %d skipped, %d released",
                self.status["ticks"] + 1, listed, len(todo), len(summary["held"]), len(held), len(summary["skipped"]), len(summary["released"]),
            )
            for app_id in todo:
                if self.stop_event.is_set():
                    break
                result = self.process_one(app_id)
                summary["processed"].append(
                    {
                        "application_id": app_id,
                        "run_id": result.run_id,
                        "state": result.state,
                        "outcome": result.outcome,
                        "failure": result.failure.label() if result.failure else None,
                    }
                )
                if self._tripped:
                    summary["stopped"] = f"system failure on {app_id}; the rest of the batch waits for the next probe"
                    break
        except Exception as exc:  # the loop must survive a bad tick
            failure = classify_failure(exc)
            self.status["tick_errors"] += 1
            self.status["last_error"] = f"tick: {type(exc).__name__}: {str(exc)[:300]}"
            summary["error"] = self.status["last_error"]
            if failure.is_system:
                self._trip(failure, None)
            else:
                log.exception("tick failed: %s", exc)
        finally:
            if self._tripped:
                summary["backoff"] = self.backoff_view()
            self._refresh_counts()
            self.status["ticks"] += 1
            self.status["last_tick_at"] = _now()
            self.status["last_tick_duration_ms"] = int((time.monotonic() - started) * 1000)
        return summary

    def run_forever(self) -> None:
        log.info("screening agent started: tick every %ds, max %d per tick", self.settings.tick_seconds, self.settings.max_per_tick)
        while not self.stop_event.is_set():
            self.tick()
            if self.stop_event.wait(self.settings.tick_seconds):
                break
        log.info("screening agent stopped")

    def stop(self) -> None:
        self.stop_event.set()

    # -- health server ----------------------------------------------------------
    def health_app(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def health(_request):
            release = self.gw.release
            return JSONResponse(
                {
                    "status": "ok",
                    "agent": release.name,
                    "version": release.version,
                    "build": release.build,
                    "ticks": self.status["ticks"],
                }
            )

        async def status(_request):
            body = dict(self.status)
            body["uptime_seconds"] = int(time.time() - self.started_at)
            body["tick_seconds"] = self.settings.tick_seconds
            body["max_per_tick"] = self.settings.max_per_tick
            body["skipped"] = self.tracker.skipped()
            body["backoff"] = self.backoff_view()
            return JSONResponse(body)

        return Starlette(routes=[Route("/health", health), Route("/status", status)])

    def start_health_server(self, port: int | None = None) -> threading.Thread:
        import uvicorn

        port = port or self.settings.health_port
        config = uvicorn.Config(self.health_app(), host="0.0.0.0", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="health-server", daemon=True)
        thread.start()
        log.info("health server on :%d (/health, /status)", port)
        return thread
