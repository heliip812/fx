#!/usr/bin/env python3
"""FX Gamma Desk - entrypoint.

    python run.py                       # synthetic provider, empty book, port 8050
    python run.py --port 8060
    python run.py --provider live       # real endpoints (blocked in this sandbox)
    python run.py --provider chain      # manual -> live -> cache
    python run.py --no-demo             # start with a genuinely empty book
    python run.py --db /tmp/scratch.db  # a throwaway book

Zero configuration: no environment variables, no config file, no network.  On a fresh
install the SQLite file is created, a deterministic demo book is seeded (REQ-006, disable
with ``--no-demo``) and the synthetic provider serves a fully badged market.  Nothing is
ever labelled live that is not live.
"""
from __future__ import annotations

import argparse
import logging
import sys

DEFAULT_PORT = 8050


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py", description="FX gamma desk dashboard")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port (8050)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (127.0.0.1)")
    p.add_argument("--provider", default="synthetic",
                   choices=["synthetic", "manual", "live", "cache", "chain", "auto"],
                   help="market data provider (synthetic)")
    p.add_argument("--db", default=None,
                   help="SQLite path for the book (data/fxgamma.db)")
    p.add_argument("--no-demo", action="store_true",
                   help="do not seed the demo book on an empty database")
    p.add_argument("--debug", action="store_true", help="Dash debug mode + hot reload")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("fxgamma.run")

    from app.main import create_app
    from app.state import get_session

    app = create_app(provider=args.provider, db=args.db, seed_demo=not args.no_demo)
    s = get_session()
    snap = s.snapshot()
    stats = s.store.stats()
    log.info("provider=%s snapshot=%s asof=%s", args.provider, s.snapshot_id, snap.asof)
    log.info("book: %d option(s), %d spot line(s), %d mark override(s), %d manual quote(s)",
             stats["options"], stats["spots"], stats["marks"], stats["manual_quotes"])
    if s.errors:
        for e in s.errors:
            log.warning("degraded: %s", e)
    n_syn = sum(1 for p in snap.meta.values() if getattr(p, "kind", "") == "synthetic")
    if n_syn:
        log.warning("%d field(s) are SYNTHETIC and badged purple in the UI - "
                    "not market data, not tradable", n_syn)
    print(f"\n  FX Gamma Desk  ->  http://{args.host}:{args.port}/\n"
          f"  pages: /  /surface  /gamma-map  /book  /data\n"
          f"  provider: {args.provider}   book: {stats['options']} options, "
          f"{stats['spots']} spot\n", flush=True)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
