"""CLI: `python -m screening_agent` runs forever; `--once` runs one tick;
`--application APP-...` processes a single application; `--requeue APP-... |
--requeue-all-skipped` takes applications off the agent's skip list (the
running scheduler picks the change up on its next tick; `./demo.sh requeue`
also moves them back to `received` in the origination system)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .approvals import PendingApprovals
from .config import Settings
from .gateway import Gateway
from .graph import process_application
from .scheduler import RetryTracker, Scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screening_agent", description="Brutor Demo System screening agent")
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    parser.add_argument("--application", metavar="APP_ID", help="process one application and exit")
    parser.add_argument("--force", action="store_true", help="with --application: screen it even if it is held for an underwriter")
    parser.add_argument("--health-port", type=int, default=None, help="override HEALTH_PORT")
    parser.add_argument("--no-health", action="store_true", help="do not start the health server")
    requeue = parser.add_mutually_exclusive_group()
    requeue.add_argument("--requeue", nargs="+", metavar="APP_ID", help="take these applications off the skip list and reset their attempts")
    requeue.add_argument("--requeue-all-skipped", action="store_true", help="take every skipped application off the skip list")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("screening_agent")
    if args.requeue or args.requeue_all_skipped:
        # Local state only (DATA_DIR); needs no gateway settings.
        tracker = RetryTracker(settings.data_dir, settings.max_attempts_per_application)
        released = tracker.requeue(None if args.requeue_all_skipped else args.requeue)
        for app_id in released:
            print(f"{app_id}: released from the skip list")
        if args.requeue:
            for app_id in sorted(set(args.requeue) - set(released)):
                print(f"{app_id}: was not skipped (nothing to release)")
        print(f"{len(released)} application(s) released; {len(tracker.skipped())} still skipped")
        return 0
    missing = settings.missing()
    if missing:
        log.error("missing required settings: %s", ", ".join(missing))
        return 2
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    gw = Gateway(settings)
    log.info("release %s (sent as X-Brutor-Agent-* headers and MCP clientInfo, RFC 0023)", gw.release.label())
    store = PendingApprovals(settings.data_dir)

    if args.application:
        if args.application in store.application_ids() and not args.force:
            held = [e for e in store.all() if e.get("application_id") == args.application]
            print(f"{args.application} is held for an underwriter (approval id {held[0].get('approval_id')}); "
                  f"screening it again would raise a duplicate hold. Use --force to override.")
            return 3
        result = process_application(gw, settings, store, args.application)
        print(f"run={result.run_id} state={result.state} outcome={result.outcome or '-'}" + (f" error={result.error}" if result.error else ""))
        return 0 if result.state == "completed" else 1

    scheduler = Scheduler(settings, gw, store)
    if args.once:
        summary = scheduler.tick()
        for item in summary.get("processed", []):
            print(f"{item['application_id']}: run={item['run_id']} state={item['state']} outcome={item['outcome'] or '-'}")
        print(f"approvals: {summary.get('approvals')}; skipped: {summary.get('skipped')}; handed off: {summary.get('handed_off')}")
        if summary.get("backoff"):
            print(f"backoff: {summary['backoff']}")
        return 1 if summary.get("error") else 0

    if not args.no_health:
        scheduler.start_health_server(args.health_port)

    def _stop(signum, _frame):
        log.info("signal %s received; stopping after the current application", signum)
        scheduler.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    scheduler.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
