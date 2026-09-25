"""Tick loop and health server (DESIGN.md section 4, "Tick loop").

Each tick: resolve pending approvals, list pending applications (in its own
short run `bds-tick-<ulid>`, step `poll_pending`, closed on that same call so
the ledger never sees an abandoned one-call run), process up to MAX_PER_TICK
sequentially. An application
whose run errored or was blocked is left `received` so a later tick retries
it, but attempts are tracked per application in DATA_DIR and after
max_attempts_per_application (3) it is noted and skipped for good.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .approvals import PendingApprovals, note_run, resolve_pending
from .config import Settings
from .gateway import Gateway, GatewayError, new_ulid
from .graph import build_graph, process_application

log = logging.getLogger("screening_agent.scheduler")

RETRIES_FILE = "retries.json"


class RetryTracker:
    """Per-application attempt counter plus a skip list, both in DATA_DIR."""

    def __init__(self, data_dir: Path | str, max_attempts: int = 3):
        self.path = Path(data_dir) / RETRIES_FILE
        self.max_attempts = max_attempts
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"attempts": {}, "skipped": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("retry file %s unreadable (%s); starting empty", self.path, exc)
            return {"attempts": {}, "skipped": {}}
        data.setdefault("attempts", {})
        data.setdefault("skipped", {})
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def is_skipped(self, application_id: str) -> bool:
        return application_id in self._state["skipped"]

    def skipped(self) -> dict[str, Any]:
        return dict(self._state["skipped"])

    def attempts(self, application_id: str) -> int:
        return int(self._state["attempts"].get(application_id, 0))

    def record_failure(self, application_id: str, reason: str) -> int:
        """Count a failed attempt; returns the new count."""
        n = self.attempts(application_id) + 1
        self._state["attempts"][application_id] = n
        self._save()
        return n

    def exhausted(self, application_id: str) -> bool:
        return self.attempts(application_id) >= self.max_attempts

    def skip(self, application_id: str, reason: str) -> None:
        self._state["skipped"][application_id] = {
            "reason": reason[:500],
            "attempts": self.attempts(application_id),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._state["attempts"].pop(application_id, None)
        self._save()

    def clear(self, application_id: str) -> None:
        if application_id in self._state["attempts"]:
            self._state["attempts"].pop(application_id, None)
            self._save()


class Scheduler:
    def __init__(self, settings: Settings, gw: Gateway, store: PendingApprovals | None = None, tracker: RetryTracker | None = None):
        self.settings = settings
        self.gw = gw
        self.store = store or PendingApprovals(settings.data_dir)
        self.tracker = tracker or RetryTracker(settings.data_dir, settings.max_attempts_per_application)
        self.graph = build_graph(gw, settings, self.store)
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.status: dict[str, Any] = {
            "ticks": 0,
            "tick_errors": 0,
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
            "pending_approvals": self.store.count(),
            "held_applications": len(self.store.application_ids()),
            "skipped_applications": len(self.tracker.skipped()),
            "last_error": None,
        }

    # -- one tick -------------------------------------------------------------
    def list_pending(self, limit: int) -> list[dict[str, Any]]:
        """applications_list_pending in its own short run, closed on the same call.

        A proxied call without run headers makes the gateway mint a root that
        the idle sweeper later closes as `abandoned`, which would skew the
        completion and abandoned rates the envelope grades. So the tick's
        housekeeping is one tiny run: bds-tick-<ulid>, step poll_pending,
        turn 1, X-Brutor-Run-End: completed, X-Brutor-Run-Outcome: resolved."""
        assert self.gw.ctx is None, "listing must not run inside an application run"
        run_id = f"bds-tick-{new_ulid()}"
        with self.gw.run(run_id):
            out = self.gw.mcp_call(
                self.settings.applications_mcp_server_id,
                "applications_list_pending",
                {"limit": limit},
                step_id="poll_pending",
                step_name="Tick: poll pending applications",
                run_end="completed",
                outcome="resolved",
            )
        log.info("run=%s state=completed outcome=resolved tick_poll=1", run_id)
        if isinstance(out, dict):
            out = out.get("applications") or out.get("items") or []
        if not isinstance(out, list):
            raise GatewayError(f"applications_list_pending returned unexpected shape: {str(out)[:200]}")
        return [row for row in out if isinstance(row, dict) and row.get("application_id")]

    def process_one(self, application_id: str):
        result = process_application(self.gw, self.settings, self.store, application_id, graph=self.graph)
        self.status["applications_processed"] += 1
        if result.state == "completed":
            self.tracker.clear(application_id)
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
            n = self.tracker.record_failure(application_id, result.error or result.state)
            if self.tracker.exhausted(application_id):
                reason = f"{result.state}: {result.error}"
                log.error("application=%s failed %d times; skipping from now on (%s)", application_id, n, reason[:200])
                self.tracker.skip(application_id, reason)
                try:
                    note_run(
                        self.gw,
                        self.settings,
                        application_id,
                        f"Automated pre-screening gave up after {n} attempts ({result.state}); handed off to manual underwriting. Last error: {(result.error or '')[:300]}",
                        suffix="giveup",
                        step_id="give_up",
                        step_name="Automated screening abandoned after retries",
                        outcome="handed_off",
                    )
                except Exception as exc:  # best effort
                    log.error("application=%s could not add give-up note: %s", application_id, exc)
        self.status["pending_approvals"] = self.store.count()
        self.status["held_applications"] = len(self.store.application_ids())
        self.status["skipped_applications"] = len(self.tracker.skipped())
        return result

    def tick(self) -> dict[str, Any]:
        started = time.monotonic()
        summary: dict[str, Any] = {"approvals": {}, "processed": [], "skipped": [], "held": []}
        try:
            def on_event(kind: str, info: dict[str, Any]) -> None:
                if kind == "approved":
                    self.status["approvals_applied"] += 1
                elif kind == "reraised":
                    self.status["approvals_reraised"] += 1
                else:
                    self.status["approvals_closed"] += 1

            summary["approvals"] = resolve_pending(self.gw, self.settings, self.store, on_event=on_event)
            self.status["pending_approvals"] = self.store.count()

            # Held (awaiting an underwriter) and skipped applications are excluded
            # BEFORE the batch is chosen; the batch is oldest-first among the rest.
            # The listing limit is large so held ones never crowd out the batch.
            held = self.store.application_ids()
            self.status["held_applications"] = len(held)
            limit = max(self.settings.list_limit, self.settings.max_per_tick + len(self.tracker.skipped()) + len(held), 1)
            candidates = self.list_pending(limit)
            eligible: list[str] = []
            for row in candidates:
                app_id = str(row["application_id"])
                if app_id in held:
                    summary["held"].append(app_id)
                elif self.tracker.is_skipped(app_id):
                    summary["skipped"].append(app_id)
                elif app_id not in eligible:
                    eligible.append(app_id)
            todo = eligible[: self.settings.max_per_tick]
            log.info(
                "tick %d: %d pending, %d to process, %d held for underwriter (%d in store), %d skipped",
                self.status["ticks"] + 1, len(candidates), len(todo), len(summary["held"]), len(held), len(summary["skipped"]),
            )
            for app_id in todo:
                if self.stop_event.is_set():
                    break
                result = self.process_one(app_id)
                summary["processed"].append({"application_id": app_id, "run_id": result.run_id, "state": result.state, "outcome": result.outcome})
        except Exception as exc:  # the loop must survive a bad tick
            self.status["tick_errors"] += 1
            self.status["last_error"] = f"tick: {type(exc).__name__}: {str(exc)[:300]}"
            log.exception("tick failed: %s", exc)
            summary["error"] = self.status["last_error"]
        finally:
            self.status["pending_approvals"] = self.store.count()
            self.status["held_applications"] = len(self.store.application_ids())
            self.status["ticks"] += 1
            self.status["last_tick_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
            return JSONResponse({"status": "ok", "agent": "brutor-demo-screening-agent", "ticks": self.status["ticks"]})

        async def status(_request):
            body = dict(self.status)
            body["uptime_seconds"] = int(time.time() - self.started_at)
            body["tick_seconds"] = self.settings.tick_seconds
            body["max_per_tick"] = self.settings.max_per_tick
            body["skipped"] = self.tracker.skipped()
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
