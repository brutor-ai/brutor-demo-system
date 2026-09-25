"""CLI: `python -m screening_agent` runs forever; `--once` runs one tick;
`--application APP-...` processes a single application."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .approvals import PendingApprovals
from .config import Settings
from .gateway import Gateway
from .graph import process_application
from .scheduler import Scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screening_agent", description="Brutor Demo System screening agent")
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    parser.add_argument("--application", metavar="APP_ID", help="process one application and exit")
    parser.add_argument("--force", action="store_true", help="with --application: screen it even if it is held for an underwriter")
    parser.add_argument("--health-port", type=int, default=None, help="override HEALTH_PORT")
    parser.add_argument("--no-health", action="store_true", help="do not start the health server")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("screening_agent")
    missing = settings.missing()
    if missing:
        log.error("missing required settings: %s", ", ".join(missing))
        return 2
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    gw = Gateway(settings)
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
        print(f"approvals: {summary.get('approvals')}; skipped: {summary.get('skipped')}")
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
