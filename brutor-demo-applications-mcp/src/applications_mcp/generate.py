"""CLI: add applications on demand.

    python -m applications_mcp.generate --count 5 [--data-dir ./data]

Uses the same deterministic generator as the server, so ids and content continue the
persisted sequence. It is safe to run while the server is up as long as both point at
the same file: the store reloads the file whenever its modification time changes
(inside the container: `docker exec brutor-demo-applications-mcp python -m
applications_mcp.generate --count 3`).
"""

from __future__ import annotations

import argparse
import json
import sys

from applications_mcp.generator import Generator
from applications_mcp.store import Store, resolve_data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic loan applications.")
    parser.add_argument("--count", type=int, default=1, help="number of applications to add")
    parser.add_argument("--data-dir", default=None, help="override DATA_DIR")
    parser.add_argument("--seed", type=int, default=None, help="override GENERATOR_SEED")
    parser.add_argument("--json", action="store_true", help="print full records as JSON")
    args = parser.parse_args(argv)

    store = Store(resolve_data_dir(args.data_dir))
    created = Generator(store, seed=args.seed).generate(args.count)
    if args.json:
        json.dump(created, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for record in created:
            print(
                f"{record['application_id']}  {record['applicant']['full_name']:<24} "
                f"{record['requested_amount_eur']:>7} EUR  {record['term_months']:>2} months"
            )
        print(f"{len(created)} application(s) written to {store.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
