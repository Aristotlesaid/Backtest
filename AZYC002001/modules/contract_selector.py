from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional

import pandas as pd


@dataclass
class ContractSelector:
    short_put_offset_abs: float
    long_put_offset_abs: float
    short_call_offset_abs: float
    long_call_offset_abs: float
    expiry_rule: str = "near_month"

    def select_iron_condor(self, chain: pd.DataFrame, trade_date: date, spot_price: float) -> Optional[Dict[str, Any]]:
        expiry = self._select_expiry(chain, trade_date)
        if expiry is None:
            return None

        table = chain[chain["EndDate"] == expiry].copy()
        if table.empty:
            return None

        table["OptType"] = table["OptType"].astype(str).str.title()
        puts = table[table["OptType"] == "Put"].copy()
        calls = table[table["OptType"] == "Call"].copy()
        if puts.empty or calls.empty:
            return None

        puts = self._normalize_side_table(puts)
        calls = self._normalize_side_table(calls)
        if puts.empty or calls.empty:
            return None

        short_put_target = float(spot_price) - float(self.short_put_offset_abs)
        long_put_target = float(spot_price) - float(self.long_put_offset_abs)
        short_call_target = float(spot_price) + float(self.short_call_offset_abs)
        long_call_target = float(spot_price) + float(self.long_call_offset_abs)

        short_put = self._pick_directional(puts, short_put_target, direction="lte")
        if short_put is None:
            return None

        long_put_pool = puts[puts["Strike"] < float(short_put["Strike"])].copy()
        long_put = self._pick_nearest(long_put_pool, long_put_target)
        if long_put is None:
            return None

        short_call = self._pick_directional(calls, short_call_target, direction="gte")
        if short_call is None:
            return None

        long_call_pool = calls[calls["Strike"] > float(short_call["Strike"])].copy()
        long_call = self._pick_nearest(long_call_pool, long_call_target)
        if long_call is None:
            return None

        multiplier = int(short_put["Multiplier"])
        if any(int(x["Multiplier"]) != multiplier for x in [long_put, short_call, long_call]):
            return None

        width_put = float(short_put["Strike"]) - float(long_put["Strike"])
        width_call = float(long_call["Strike"]) - float(short_call["Strike"])
        if width_put <= 0 or width_call <= 0:
            return None

        max_loss_per_set = max(width_put, width_call) * multiplier

        return {
            "expiry": expiry,
            "short_put": short_put,
            "long_put": long_put,
            "short_call": short_call,
            "long_call": long_call,
            "width_put": float(width_put),
            "width_call": float(width_call),
            "max_loss_per_set": float(max_loss_per_set),
        }

    def _select_expiry(self, chain: pd.DataFrame, trade_date: date) -> Optional[date]:
        if chain.empty or "EndDate" not in chain.columns:
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

        rule = str(self.expiry_rule).strip().lower()
        if rule in {"next_month_2nd", "second_near_month"} and len(expiries) >= 2:
            return expiries[1]
        return expiries[0]

    @staticmethod
    def _normalize_side_table(table: pd.DataFrame) -> pd.DataFrame:
        side = table.copy()
        side["Strike"] = pd.to_numeric(side["Strike"], errors="coerce")

        if "MinTick" in side.columns:
            side["MinTick"] = pd.to_numeric(side["MinTick"], errors="coerce")
        else:
            side["MinTick"] = pd.NA
        side["MinTick"] = side["MinTick"].fillna(0.0001)

        if "Multiplier" in side.columns:
            side["Multiplier"] = pd.to_numeric(side["Multiplier"], errors="coerce")
        else:
            side["Multiplier"] = pd.NA

        side = side.dropna(subset=["Symbol", "Strike", "Multiplier"]).copy()
        side["Multiplier"] = side["Multiplier"].astype(int)
        return side

    @staticmethod
    def _pick_directional(candidates: pd.DataFrame, target: float, direction: str) -> Optional[Dict[str, Any]]:
        if candidates.empty:
            return None

        table = candidates.copy()
        if direction == "lte":
            directional = table[table["Strike"] <= target]
            if not directional.empty:
                chosen = directional.sort_values(["Strike", "Symbol"], ascending=[False, True]).iloc[0]
            else:
                chosen = ContractSelector._pick_nearest_row(table, target)
        elif direction == "gte":
            directional = table[table["Strike"] >= target]
            if not directional.empty:
                chosen = directional.sort_values(["Strike", "Symbol"], ascending=[True, True]).iloc[0]
            else:
                chosen = ContractSelector._pick_nearest_row(table, target)
        else:
            raise ValueError(f"Unsupported direction: {direction}")

        return ContractSelector._row_to_contract(chosen)

    @staticmethod
    def _pick_nearest(candidates: pd.DataFrame, target: float) -> Optional[Dict[str, Any]]:
        if candidates.empty:
            return None
        chosen = ContractSelector._pick_nearest_row(candidates, target)
        return ContractSelector._row_to_contract(chosen)

    @staticmethod
    def _pick_nearest_row(table: pd.DataFrame, target: float) -> pd.Series:
        scoped = table.copy()
        scoped["_dist"] = (scoped["Strike"] - float(target)).abs()
        return scoped.sort_values(["_dist", "Strike", "Symbol"], ascending=[True, True, True]).iloc[0]

    @staticmethod
    def _row_to_contract(row: pd.Series) -> Dict[str, Any]:
        return {
            "Symbol": str(row["Symbol"]),
            "OptType": str(row["OptType"]).title(),
            "Strike": float(row["Strike"]),
            "EndDate": ContractSelector._to_date(row["EndDate"]),
            "MinTick": float(row.get("MinTick", 0.0001) or 0.0001),
            "Multiplier": int(float(row.get("Multiplier", 10000))),
        }

    @staticmethod
    def _to_date(value: Any) -> Optional[date]:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
