from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional

import pandas as pd
from ResearchIV.modules.iv_solver import bs_greeks


@dataclass
class ContractSelector:
    short_put_offset_abs: float
    long_put_offset_abs: float
    short_call_offset_abs: float
    long_call_offset_abs: float
    expiry_rule: str = "near_month"
    strike_selection_mode: str = "abs"
    short_put_target_delta: float = 0.20
    long_put_target_delta: float = 0.10
    short_call_target_delta: float = 0.20
    long_call_target_delta: float = 0.10

    def select_iron_condor(
        self,
        chain: pd.DataFrame,
        trade_date: date,
        spot_price: float,
        vol_proxy: Optional[float] = None,
        risk_free_rate: float = 0.0,
        dividend_yield: float = 0.0,
        ttm_years_override: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
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

        mode = str(self.strike_selection_mode).strip().lower()
        if mode == "delta" and vol_proxy is not None and float(vol_proxy) > 0:
            ttm_years = float(ttm_years_override) if ttm_years_override is not None else self._ttm_years(expiry, trade_date)
            if ttm_years <= 0:
                ttm_years = 1.0 / 365.0

            short_put = self._pick_by_delta(
                candidates=puts,
                target_delta=-abs(float(self.short_put_target_delta)),
                option_type="put",
                spot_price=float(spot_price),
                ttm_years=ttm_years,
                risk_free_rate=float(risk_free_rate),
                dividend_yield=float(dividend_yield),
                vol_proxy=float(vol_proxy),
            )
            if short_put is None:
                return None

            long_put_pool = puts[puts["Strike"] < float(short_put["Strike"])].copy()
            long_put = self._pick_by_delta(
                candidates=long_put_pool,
                target_delta=-abs(float(self.long_put_target_delta)),
                option_type="put",
                spot_price=float(spot_price),
                ttm_years=ttm_years,
                risk_free_rate=float(risk_free_rate),
                dividend_yield=float(dividend_yield),
                vol_proxy=float(vol_proxy),
            )
            if long_put is None:
                return None

            short_call = self._pick_by_delta(
                candidates=calls,
                target_delta=abs(float(self.short_call_target_delta)),
                option_type="call",
                spot_price=float(spot_price),
                ttm_years=ttm_years,
                risk_free_rate=float(risk_free_rate),
                dividend_yield=float(dividend_yield),
                vol_proxy=float(vol_proxy),
            )
            if short_call is None:
                return None

            long_call_pool = calls[calls["Strike"] > float(short_call["Strike"])].copy()
            long_call = self._pick_by_delta(
                candidates=long_call_pool,
                target_delta=abs(float(self.long_call_target_delta)),
                option_type="call",
                spot_price=float(spot_price),
                ttm_years=ttm_years,
                risk_free_rate=float(risk_free_rate),
                dividend_yield=float(dividend_yield),
                vol_proxy=float(vol_proxy),
            )
            if long_call is None:
                return None
        else:
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
            "selection_mode": mode,
            "vol_proxy": float(vol_proxy) if vol_proxy is not None else None,
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
    def _pick_by_delta(
        candidates: pd.DataFrame,
        target_delta: float,
        option_type: str,
        spot_price: float,
        ttm_years: float,
        risk_free_rate: float,
        dividend_yield: float,
        vol_proxy: float,
    ) -> Optional[Dict[str, Any]]:
        if candidates.empty:
            return None

        scored = candidates.copy()
        deltas: list[float] = []
        dists: list[float] = []
        for row in scored.itertuples(index=False):
            strike = float(getattr(row, "Strike"))
            try:
                delta = float(
                    bs_greeks(
                        spot=float(spot_price),
                        strike=strike,
                        ttm_years=max(float(ttm_years), 1.0 / 365.0),
                        rate=float(risk_free_rate),
                        dividend_yield=float(dividend_yield),
                        vol=max(float(vol_proxy), 1e-6),
                        option_type=str(option_type),
                    )["Delta"]
                )
            except Exception:
                delta = float("nan")
            deltas.append(delta)
            if pd.isna(delta):
                dists.append(float("inf"))
            else:
                dists.append(abs(delta - float(target_delta)))

        scored["_delta"] = deltas
        scored["_delta_dist"] = dists
        scored = scored.replace([float("inf")], pd.NA).dropna(subset=["_delta_dist"]).copy()
        if scored.empty:
            return None

        chosen = scored.sort_values(["_delta_dist", "Strike", "Symbol"], ascending=[True, True, True]).iloc[0]
        result = ContractSelector._row_to_contract(chosen)
        result["DeltaApprox"] = float(chosen["_delta"])
        return result

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

    @staticmethod
    def _ttm_years(expiry: date, trade_date: date) -> float:
        ttm_days = (pd.Timestamp(expiry).normalize() - pd.Timestamp(trade_date).normalize()).days
        return max(float(ttm_days), 1.0) / 365.0
