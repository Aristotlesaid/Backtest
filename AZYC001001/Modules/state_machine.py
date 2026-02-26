from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

STATE_FLAT = "FLAT"
STATE_SHORT_PUT = "SHORT_PUT"
STATE_LONG_ETF_SHORT_CALL = "LONG_ETF_SHORT_CALL"


@dataclass
class OptionLeg:
    symbol: str
    option_type: str
    qty_contracts: int
    strike: float
    expiry: date
    multiplier: int
    min_tick: float


@dataclass
class StrategyState:
    etf_symbol: str
    state: str = STATE_FLAT
    option_leg: Optional[OptionLeg] = None
    etf_qty: int = 0
    etf_avg_cost: float = 0.0
    cash_ledger: float = 0.0
    current_cycle_id: Optional[int] = None
    next_cycle_id: int = 1

    def start_cycle(self) -> int:
        if self.current_cycle_id is not None:
            return self.current_cycle_id
        cycle_id = self.next_cycle_id
        self.next_cycle_id += 1
        self.current_cycle_id = cycle_id
        return cycle_id

    def close_cycle(self) -> None:
        self.current_cycle_id = None
