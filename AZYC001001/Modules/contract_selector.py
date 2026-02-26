from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional

import pandas as pd


@dataclass
class ContractSelector:
    put_offset_abs: float
    call_offset_abs: float
    expiry_rule: str = "next_month_2nd"

    def select_put_contract(self, chain: pd.DataFrame, trade_date: date, spot_price: float) -> Optional[Dict[str, Any]]:
        expiry = self._select_expiry(chain, trade_date)
        if expiry is None:
            return None
        candidates = chain[(chain["EndDate"] == expiry) & (chain["OptType"].str.lower() == "put")]
        target = float(spot_price) - float(self.put_offset_abs)
        return self._select_by_strike(candidates, target, direction="lte")

    def select_call_contract(self, chain: pd.DataFrame, trade_date: date, spot_price: float) -> Optional[Dict[str, Any]]:
        expiry = self._select_expiry(chain, trade_date)
        if expiry is None:
            return None
        candidates = chain[(chain["EndDate"] == expiry) & (chain["OptType"].str.lower() == "call")]
        target = float(spot_price) + float(self.call_offset_abs)
        return self._select_by_strike(candidates, target, direction="gte")

    def _select_expiry(self, chain: pd.DataFrame, trade_date: date) -> Optional[date]:
        if chain.empty or "EndDate" not in chain:
            return None

        expiries = sorted(
            {
                self._to_date(x)
                for x in chain["EndDate"].dropna().tolist()
                if self._to_date(x) is not None and self._to_date(x) > trade_date
            }
        )

        if not expiries:
            expiries = sorted(
                {
                    self._to_date(x)
                    for x in chain["EndDate"].dropna().tolist()
                    if self._to_date(x) is not None and self._to_date(x) >= trade_date
                }
            )

        if not expiries:
            return None

        if self.expiry_rule == "next_month_2nd" and len(expiries) >= 2:
            return expiries[1]

        return expiries[0]

    def _select_by_strike(self, candidates: pd.DataFrame, target: float, direction: str) -> Optional[Dict[str, Any]]:
        if candidates.empty:
            return None

        table = candidates.copy()
        table["Strike"] = pd.to_numeric(table["Strike"], errors="coerce")
        table = table.dropna(subset=["Strike", "Symbol", "Multiplier"]).copy()
        if table.empty:
            return None

        if direction == "lte":
            directional = table[table["Strike"] <= target]
            if not directional.empty:
                chosen = directional.sort_values(["Strike", "Symbol"], ascending=[False, True]).iloc[0]
            else:
                table["_dist"] = (table["Strike"] - target).abs()
                chosen = table.sort_values(["_dist", "Strike", "Symbol"], ascending=[True, True, True]).iloc[0]
        elif direction == "gte":
            directional = table[table["Strike"] >= target]
            if not directional.empty:
                chosen = directional.sort_values(["Strike", "Symbol"], ascending=[True, True]).iloc[0]
            else:
                table["_dist"] = (table["Strike"] - target).abs()
                chosen = table.sort_values(["_dist", "Strike", "Symbol"], ascending=[True, True, True]).iloc[0]
        else:
            raise ValueError(f"Unsupported strike direction: {direction}")

        return {
            "Symbol": str(chosen["Symbol"]),
            "OptType": str(chosen["OptType"]).title(),
            "Strike": float(chosen["Strike"]),
            "EndDate": self._to_date(chosen["EndDate"]),
            "MinTick": float(chosen.get("MinTick", 0.0001) or 0.0001),
            "Multiplier": int(float(chosen.get("Multiplier", 10000))),
        }

    @staticmethod
    def _to_date(value: Any) -> Optional[date]:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
