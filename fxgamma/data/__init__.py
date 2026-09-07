"""Market data layer: adapters, cache, synthetic generator, provider factory.

Quick start::

    from fxgamma.data import get_provider
    p = get_provider("synthetic")            # offline, deterministic, badged synthetic
    snap = p.snapshot(["EURUSD", "USDJPY"])

Marking the book to the desk's own curve (amendment v1.2, T-1)::

    from fxgamma.data import ManualQuoteProvider, get_provider
    m = ManualQuoteProvider()
    print(m.paste("1M 7.05 -0.15 0.20\n3M 7.30 -0.20 0.25", pair="EURUSD").summary())
    p = get_provider("chain", manual=m)      # manual -> live -> cache

See ``docs/04_data_sources.md`` for every endpoint, its terms of use, and an honest
assessment of how good a substitute each free source is for the real OTC quote.
"""
from .base import (EVENT_COLUMNS, OI_COLUMNS, SPOT_COLUMNS, MarketDataProvider, SmileQuotes,
                   SourceStatus, meta_lookup, surface_backend)
from .cache import Cache, get_cache
from .manual import (ManualMark, ManualQuoteProvider, ManualQuoteStore, ParsedGrid,
                     parse_grid)
from .provider import ChainProvider, CacheProvider, LiveProvider, PROVIDERS, get_provider
from .synthetic import SyntheticProvider

__all__ = [
    "MarketDataProvider", "SmileQuotes", "SourceStatus", "meta_lookup", "surface_backend",
    "SPOT_COLUMNS", "OI_COLUMNS", "EVENT_COLUMNS",
    "Cache", "get_cache", "get_provider", "PROVIDERS",
    "LiveProvider", "CacheProvider", "ChainProvider", "SyntheticProvider",
    "ManualQuoteProvider", "ManualQuoteStore", "ManualMark", "ParsedGrid", "parse_grid",
]
