"""Book aggregation, scenarios, gamma zones, attribution and hedging.

``risk``         pricing the book, ladders, scenarios, decay, reporting-ccy conversion
``zones``        gamma zones with sensitivity, cost-aware hedge bands, pin risk
``attribution``  second-order daily P&L explain with an honest residual
``hedging``      the one-line hedge instruction

See ``docs/01_architecture.md`` s5 and AMENDMENT v1.1.
"""
from __future__ import annotations

from . import attribution, hedging, risk, zones
from .attribution import daily_pnl, residual_ratio, top_offenders
from .hedging import hedge_suggestion
from .risk import (book_greeks, fx_rate, price_book, scenario_grid, shift_market,
                   spot_ladder, time_decay)
from .zones import (GammaZoneDetail, gamma_zones, hedge_bands, pin_risk,
                    touch_probability, zone_frame)

__all__ = [
    "risk", "zones", "attribution", "hedging",
    "price_book", "book_greeks", "spot_ladder", "scenario_grid", "time_decay",
    "shift_market", "fx_rate", "gamma_zones", "hedge_bands", "pin_risk",
    "daily_pnl", "residual_ratio", "top_offenders", "hedge_suggestion",
    "GammaZoneDetail", "zone_frame", "touch_probability",
]
