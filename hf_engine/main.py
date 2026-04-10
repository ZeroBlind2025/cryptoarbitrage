"""
hf_engine.main
==============

Command-line entry point for the Hawkes-Glosten-Milgrom high-frequency
engine.

Usage::

    python -m hf_engine.main

All tuning happens through ``HFE_*`` environment variables read by
``hf_engine.config.HFEConfig.from_env``. Paper trading is the only mode
in v1 — no flags to flip into live trading exist yet, by design.
"""

from __future__ import annotations

import sys

from .config import HFEConfig
from .engine import HFEngine


def main() -> int:
    try:
        cfg = HFEConfig.from_env()
    except Exception as e:
        print(f"[HFE] config error: {e}", flush=True)
        return 2

    if not cfg.paper_trading:
        # Guard rail: v1 hard-codes paper trading. Flipping HFE_PAPER_TRADING
        # to false is not enough to unlock live trading — a live executor
        # does not exist yet. Bail out loudly instead of silently misbehaving.
        print(
            "[HFE] HFE_PAPER_TRADING=false is not supported in v1 — paper "
            "trading is the only implemented execution path. Aborting.",
            flush=True,
        )
        return 3

    engine = HFEngine(cfg=cfg)
    engine.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
