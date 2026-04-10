"""
hf_engine
=========

Hawkes-Glosten-Milgrom high-frequency binary engine for short-duration
Polymarket prediction markets (5m/15m/30m/1h).

This package is intentionally standalone — it does not import from
``momentum_engine.py``, ``hft_server.py``, ``copy_trader.py``, or any
other module in the repository root. It exposes its own configuration
(``HFE_*`` env vars), its own WebSocket feed, its own market state, and
its own paper-trading log directory. Nothing in this package can
trigger or influence the momentum engine.

Entry points
------------

* ``python -m hf_engine.main`` — start the engine
* ``hf_engine.engine.HFEngine`` — programmatic access
* ``hf_engine.config.HFEConfig`` — configuration container
"""

from .config import HFEConfig
from .hawkes import HawkesTracker, hawkes_log_likelihood
from .glosten_milgrom import GlostenMilgromPosterior, information_weight

# ``HFEngine`` depends on the websocket-client package via ``hf_engine.feed``.
# We import it lazily so that downstream code which only needs the math
# primitives does not have to pull in the full WebSocket stack.
def __getattr__(name: str):
    if name == "HFEngine":
        from .engine import HFEngine as _HFEngine

        return _HFEngine
    raise AttributeError(f"module 'hf_engine' has no attribute {name!r}")


__all__ = [
    "HFEConfig",
    "HFEngine",
    "HawkesTracker",
    "hawkes_log_likelihood",
    "GlostenMilgromPosterior",
    "information_weight",
]
