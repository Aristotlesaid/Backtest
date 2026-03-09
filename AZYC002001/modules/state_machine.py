from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

STATE_FLAT = "FLAT"
STATE_SHORT_CONDOR = "SHORT_CONDOR"


@dataclass
class OptionLeg:
    role: str
    symbol: str
    option_type: str
    side: str
    qty_contracts: int
    strike: float
    expiry: date
    multiplier: int
    min_tick: float


@dataclass
class StrategyState:
    etf_symbol: str
    state: str = STATE_FLAT
    option_legs: List[OptionLeg] = field(default_factory=list)
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
