from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

from ResearchIV.modules.iv_solver import implied_vol_newton

try:
    from .analyze import analyze
    from .contract_selector import ContractSelector
    from .data_loader import CausisDataLoader
    from .execution import apply_slippage_to_open, estimate_option_fee, option_cash_flow
    from .io_store import save_results
    from .state_machine import STATE_FLAT, STATE_SHORT_CONDOR, OptionLeg, StrategyState
except ImportError:  # pragma: no cover
    from analyze import analyze
    from contract_selector import ContractSelector
    from data_loader import CausisDataLoader
    from execution import apply_slippage_to_open, estimate_option_fee, option_cash_flow
    from io_store import save_results
    from state_machine import STATE_FLAT, STATE_SHORT_CONDOR, OptionLeg, StrategyState


DEFAULT_CONFIG: Dict[str, Any] = {
    "strategy_id": "AZYC002001",
    "etf_symbol": "S.CN.SZSE.159915",
    "option_code": "",
    "start_date": "2025-01-01",
    "end_date": "2025-12-31",
    "frequency": "minute1",
    "fixed_contracts": 1,
    "short_put_offset_abs": 0.10,
    "long_put_offset_abs": 0.20,
    "short_call_offset_abs": 0.10,
    "long_call_offset_abs": 0.20,
    "strike_selection_mode": "delta",
    "short_put_target_delta": 0.20,
    "long_put_target_delta": 0.10,
    "short_call_target_delta": 0.20,
    "long_call_target_delta": 0.10,
    "expiry_rule": "near_month",
    "min_volume_signal": 20,
    "min_volume_fill": 20,
    "cost_option_fee_per_contract": 0.0,
    "cost_option_slippage_ticks": 0.0,
    "cost_etf_fee_rate": 0.0,
    "cost_etf_slippage_bps": 0.0,
    "dividend_yield": 0.0,
    "initial_capital": 1_000_000.0,
    "contract_sizing": "capital",
    "risk_free_rate": 0.0,
    "use_iv_rank_filter": True,
    "iv_rank_entry_min": 0.50,
    "iv_rank_lookback_days": 252,
    "iv_rank_min_history": 60,
    "iv_rank_min_iv": 0.01,
    "use_disk_cache": True,
    "cache_dir": "../../DataCache",
    "entry_start_time": "09:30:00",
    "trade_start_offset_days": 0,
    "output_dir": "outputs/AZYC002001",
}


ORDER_COLUMNS = [
    "order_id",
    "strategy_id",
    "cycle_id",
    "cycle_closed",
    "ts_signal",
    "ts_fill",
    "symbol",
    "asset_type",
    "option_type",
    "leg_role",
    "side",
    "effect",
    "qty",
    "price",
    "notional",
    "fee",
    "slippage",
    "cash_flow",
    "status",
    "reason",
]

POSITION_COLUMNS = [
    "ts",
    "snapshot_type",
    "state",
    "cycle_id",
    "expiry",
    "short_put_symbol",
    "long_put_symbol",
    "short_call_symbol",
    "long_call_symbol",
    "short_put_strike",
    "long_put_strike",
    "short_call_strike",
    "long_call_strike",
    "contracts_per_set",
    "multiplier",
    "put_wing_width",
    "call_wing_width",
    "max_loss_per_set",
    "option_mv",
    "etf_symbol",
    "etf_qty",
    "etf_avg_cost",
    "etf_mark",
    "etf_mv",
    "cash_ledger",
    "equity_ledger",
]

DAILY_COLUMNS = [
    "date",
    "cash_eod",
    "option_mv_eod",
    "etf_mv_eod",
    "equity_eod",
    "realized_pnl_cum",
    "unrealized_pnl_eod",
    "daily_pnl",
    "total_pnl_cum",
    "drawdown",
]


class IronCondorBacktestEngine:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.loader = CausisDataLoader(
            start_date=config["start_date"],
            end_date=config["end_date"],
            frequency=config["frequency"],
            option_code=config["option_code"],
            use_disk_cache=bool(config.get("use_disk_cache", True)),
            cache_dir=config.get("cache_dir"),
        )
        self.selector = ContractSelector(
            short_put_offset_abs=float(config["short_put_offset_abs"]),
            long_put_offset_abs=float(config["long_put_offset_abs"]),
            short_call_offset_abs=float(config["short_call_offset_abs"]),
            long_call_offset_abs=float(config["long_call_offset_abs"]),
            expiry_rule=str(config["expiry_rule"]),
            strike_selection_mode=str(config.get("strike_selection_mode", "abs")),
            short_put_target_delta=float(config.get("short_put_target_delta", 0.20)),
            long_put_target_delta=float(config.get("long_put_target_delta", 0.10)),
            short_call_target_delta=float(config.get("short_call_target_delta", 0.20)),
            long_call_target_delta=float(config.get("long_call_target_delta", 0.10)),
        )

        lookback_days = int(config.get("iv_rank_lookback_days", 252))
        daily_start = (pd.Timestamp(config["start_date"]) - pd.Timedelta(days=max(lookback_days + 30, 90))).strftime(
            "%Y-%m-%d"
        )
        self.daily_loader = CausisDataLoader(
            start_date=daily_start,
            end_date=config["end_date"],
            frequency="day",
            option_code=config["option_code"],
            use_disk_cache=bool(config.get("use_disk_cache", True)),
            cache_dir=config.get("cache_dir"),
        )
        self._daily_spot_series: Optional[pd.Series] = None
        self._atm_iv_daily_cache: Dict[Any, Optional[float]] = {}
        self._iv_rank_cache: Dict[Any, Optional[float]] = {}
        self._trade_dates: List[Any] = []

        self.state = StrategyState(etf_symbol=str(config["etf_symbol"]))

        self.orders: List[Dict[str, Any]] = []
        self.positions: List[Dict[str, Any]] = []
        self.daily_rows: List[Dict[str, Any]] = []
        self._order_seq = 1

        self._prev_equity = 0.0
        self._equity_peak = 0.0

        self.trade_start_date = (
            pd.Timestamp(self.config["start_date"]).date()
            + pd.Timedelta(days=int(self.config.get("trade_start_offset_days", 0)))
        )

    def run(self) -> Dict[str, pd.DataFrame]:
        etf_bars = self.loader.load_etf_bars(self.config["etf_symbol"])
        if etf_bars.empty:
            raise ValueError("ETF minute bars are empty for configured backtest range.")

        etf_bars = etf_bars.copy()
        etf_bars["trade_date"] = etf_bars["CLOCK"].dt.date
        self._trade_dates = sorted(etf_bars["trade_date"].dropna().unique().tolist())

        for trade_date, day_bars in etf_bars.groupby("trade_date", sort=True):
            day_bars = day_bars.reset_index(drop=True).sort_values("CLOCK").reset_index(drop=True)
            eod_row = self.loader.pick_eod_bar(day_bars)

            can_open_today = trade_date >= self.trade_start_date
            if self.state.state == STATE_FLAT and can_open_today:
                self._scan_open_condor(day_bars, trade_date)

            expiry = self._active_expiry()
            if self.state.option_legs and expiry == trade_date:
                self._settle_expiry(eod_row)

            self._record_daily_snapshot(trade_date, eod_row)

        orders_df = pd.DataFrame(self.orders)
        positions_df = pd.DataFrame(self.positions)
        daily_df = pd.DataFrame(self.daily_rows)

        if orders_df.empty:
            orders_df = pd.DataFrame(columns=ORDER_COLUMNS)
        else:
            orders_df = orders_df[ORDER_COLUMNS].sort_values(["ts_fill", "order_id"]).reset_index(drop=True)

        if positions_df.empty:
            positions_df = pd.DataFrame(columns=POSITION_COLUMNS)
        else:
            positions_df = positions_df[POSITION_COLUMNS].sort_values(["ts", "snapshot_type"]).reset_index(drop=True)

        if daily_df.empty:
            daily_df = pd.DataFrame(columns=DAILY_COLUMNS)
        else:
            daily_df = daily_df[DAILY_COLUMNS].sort_values("date").reset_index(drop=True)

        return {
            "orders": orders_df,
            "positions": positions_df,
            "daily_pnl": daily_df,
        }

    def _active_expiry(self):
        if not self.state.option_legs:
            return None
        return self.state.option_legs[0].expiry

    def _scan_open_condor(self, day_bars: pd.DataFrame, trade_date) -> bool:
        day_bars = self._filter_entry_window(day_bars)
        if day_bars.empty:
            return False

        if bool(self.config.get("use_iv_rank_filter", False)):
            iv_rank = self._get_iv_rank(trade_date)
            if iv_rank is None or iv_rank < float(self.config.get("iv_rank_entry_min", 0.50)):
                return False

        option_chain = self.loader.load_option_chain(trade_date)
        if option_chain.empty:
            return False

        for row in day_bars.itertuples(index=False):
            signal_ts = pd.Timestamp(row.CLOCK)
            spot_price = float(row.CLOSE)

            vol_proxy = None
            if str(self.config.get("strike_selection_mode", "abs")).strip().lower() == "delta":
                vol_proxy = self._estimate_atm_iv_intraday(option_chain, trade_date, signal_ts, spot_price)
                if vol_proxy is None or vol_proxy <= 0:
                    continue

            condor = self.selector.select_iron_condor(
                option_chain,
                trade_date,
                spot_price,
                vol_proxy=vol_proxy,
                risk_free_rate=float(self.config.get("risk_free_rate", 0.0)),
                dividend_yield=float(self.config.get("dividend_yield", 0.0)),
            )
            if not condor:
                continue

            qty_contracts = self._resolve_condor_qty(condor)
            if qty_contracts <= 0:
                continue

            opened = self._try_open_condor(condor=condor, signal_ts=signal_ts, qty_contracts=qty_contracts)
            if opened:
                return True

        return False

    def _ttm_years_to_expiry(self, condor_expiry, trade_date, signal_ts: Optional[pd.Timestamp]) -> float:
        if condor_expiry is None:
            return 1.0 / 365.0
        expiry_ts = pd.Timestamp(condor_expiry).normalize() + pd.Timedelta(hours=15)
        now_ts = pd.Timestamp(signal_ts) if signal_ts is not None else pd.Timestamp(trade_date).normalize()
        seconds = max(1.0, (expiry_ts - now_ts).total_seconds())
        return float(seconds) / (365.0 * 24.0 * 3600.0)

    def _estimate_atm_iv_intraday(
        self,
        chain: pd.DataFrame,
        trade_date,
        signal_ts: pd.Timestamp,
        spot_price: float,
    ) -> Optional[float]:
        atm = self._resolve_atm_pair(chain, trade_date, float(spot_price))
        if atm is None:
            return None

        expiry = atm["expiry"]
        strike = float(atm["strike"])
        call_symbol = str(atm["call_symbol"])
        put_symbol = str(atm["put_symbol"])

        call_px = self.loader.get_close_price_at_or_before(call_symbol, signal_ts)
        put_px = self.loader.get_close_price_at_or_before(put_symbol, signal_ts)
        ttm_years = self._ttm_years_to_expiry(expiry, trade_date, signal_ts)

        values: List[float] = []
        for px, option_type in [(call_px, "call"), (put_px, "put")]:
            if px is None or float(px) < float(self.config.get("iv_rank_min_iv", 0.01)):
                continue
            try:
                iv = implied_vol_newton(
                    market_price=float(px),
                    spot=float(spot_price),
                    strike=float(strike),
                    ttm_years=float(ttm_years),
                    rate=float(self.config.get("risk_free_rate", 0.0)),
                    dividend_yield=float(self.config.get("dividend_yield", 0.0)),
                    option_type=str(option_type),
                    initial_vol=0.3,
                    tol=1e-6,
                    max_iter=80,
                )
            except Exception:
                continue
            if iv > 0:
                values.append(float(iv))

        if not values:
            return None
        return float(sum(values) / len(values))

    def _get_daily_spot_series(self) -> pd.Series:
        if self._daily_spot_series is not None:
            return self._daily_spot_series

        bars = self.daily_loader.load_etf_bars(self.config["etf_symbol"])
        if bars.empty:
            self._daily_spot_series = pd.Series(dtype=float)
            return self._daily_spot_series

        frame = bars.copy()
        frame = frame.reset_index(drop=True)
        frame["date"] = pd.to_datetime(frame["CLOCK"], errors="coerce").dt.date
        frame["close"] = pd.to_numeric(frame["CLOSE"], errors="coerce")
        frame = frame.dropna(subset=["date", "close"]).sort_values("CLOCK")
        frame = frame.drop_duplicates(subset=["date"], keep="last")
        self._daily_spot_series = frame.set_index("date")["close"]
        return self._daily_spot_series

    def _compute_daily_atm_iv(self, trade_date) -> Optional[float]:
        if trade_date in self._atm_iv_daily_cache:
            return self._atm_iv_daily_cache[trade_date]

        spot_series = self._get_daily_spot_series()
        if trade_date not in spot_series.index:
            self._atm_iv_daily_cache[trade_date] = None
            return None
        spot = float(spot_series.loc[trade_date])

        chain = self.daily_loader.load_option_chain(trade_date)
        if chain.empty:
            self._atm_iv_daily_cache[trade_date] = None
            return None

        atm = self._resolve_atm_pair(chain, trade_date, spot)
        if atm is None:
            self._atm_iv_daily_cache[trade_date] = None
            return None

        expiry = atm["expiry"]
        strike = float(atm["strike"])
        ts = pd.Timestamp(trade_date).normalize() + pd.Timedelta(hours=15)
        call_px = self.daily_loader.get_close_price_at_or_before(str(atm["call_symbol"]), ts)
        put_px = self.daily_loader.get_close_price_at_or_before(str(atm["put_symbol"]), ts)
        ttm_years = self._ttm_years_to_expiry(expiry, trade_date, ts)

        values: List[float] = []
        for px, option_type in [(call_px, "call"), (put_px, "put")]:
            if px is None or float(px) < float(self.config.get("iv_rank_min_iv", 0.01)):
                continue
            try:
                iv = implied_vol_newton(
                    market_price=float(px),
                    spot=float(spot),
                    strike=float(strike),
                    ttm_years=float(ttm_years),
                    rate=float(self.config.get("risk_free_rate", 0.0)),
                    dividend_yield=float(self.config.get("dividend_yield", 0.0)),
                    option_type=str(option_type),
                    initial_vol=0.3,
                    tol=1e-6,
                    max_iter=80,
                )
            except Exception:
                continue
            if iv > 0:
                values.append(float(iv))

        value = float(sum(values) / len(values)) if values else None
        self._atm_iv_daily_cache[trade_date] = value
        return value

    def _get_iv_rank(self, trade_date) -> Optional[float]:
        if trade_date in self._iv_rank_cache:
            return self._iv_rank_cache[trade_date]

        current_iv = self._compute_daily_atm_iv(trade_date)
        if current_iv is None:
            self._iv_rank_cache[trade_date] = None
            return None

        lookback = int(self.config.get("iv_rank_lookback_days", 252))
        min_history = int(self.config.get("iv_rank_min_history", 60))
        hist_days = [d for d in self._trade_dates if d < trade_date][-lookback:]
        hist_values = [self._compute_daily_atm_iv(d) for d in hist_days]
        hist_values = [float(v) for v in hist_values if v is not None]

        if len(hist_values) < min_history:
            self._iv_rank_cache[trade_date] = None
            return None

        lo = min(hist_values)
        hi = max(hist_values)
        if hi - lo <= 1e-12:
            rank = 0.5
        else:
            rank = (float(current_iv) - float(lo)) / float(hi - lo)

        rank = float(max(0.0, min(1.0, rank)))
        self._iv_rank_cache[trade_date] = rank
        return rank

    def _resolve_atm_pair(self, chain: pd.DataFrame, trade_date, spot_price: float) -> Optional[Dict[str, Any]]:
        expiry = self.selector._select_expiry(chain, trade_date)
        if expiry is None:
            return None

        sub = chain.copy()
        sub = sub[sub["EndDate"] == expiry].copy()
        if sub.empty:
            return None

        sub["OptType"] = sub["OptType"].astype(str).str.title()
        sub["Strike"] = pd.to_numeric(sub["Strike"], errors="coerce")
        sub = sub.dropna(subset=["Strike", "Symbol"]).copy()
        if sub.empty:
            return None

        calls = sub[sub["OptType"] == "Call"].copy()
        puts = sub[sub["OptType"] == "Put"].copy()
        if calls.empty or puts.empty:
            return None

        call_by_strike = calls.groupby("Strike", as_index=False).first()
        put_by_strike = puts.groupby("Strike", as_index=False).first()
        merged = call_by_strike.merge(put_by_strike, on="Strike", suffixes=("_call", "_put"))
        if merged.empty:
            return None

        merged["_dist"] = (merged["Strike"] - float(spot_price)).abs()
        row = merged.sort_values(["_dist", "Strike"], ascending=[True, True]).iloc[0]
        return {
            "expiry": expiry,
            "strike": float(row["Strike"]),
            "call_symbol": str(row["Symbol_call"]),
            "put_symbol": str(row["Symbol_put"]),
        }

    def _filter_entry_window(self, day_bars: pd.DataFrame) -> pd.DataFrame:
        start_time = pd.to_datetime(str(self.config.get("entry_start_time", "09:30:00"))).time()
        return day_bars[day_bars["CLOCK"].dt.time >= start_time].copy()

    def _resolve_condor_qty(self, condor: Dict[str, Any]) -> int:
        mode = str(self.config.get("contract_sizing", "capital")).strip().lower()
        if mode in {"fixed", "fixed_contracts"}:
            return max(1, int(self.config["fixed_contracts"]))

        principal = float(self.config.get("initial_capital", 0.0))
        max_loss_per_set = float(condor.get("max_loss_per_set", 0.0))
        if principal <= 0 or max_loss_per_set <= 0:
            return 0

        qty = int(principal // max_loss_per_set)
        return max(0, qty)

    def _try_open_condor(
        self,
        condor: Dict[str, Any],
        signal_ts: pd.Timestamp,
        qty_contracts: int,
    ) -> bool:
        legs = [
            ("SHORT_PUT", condor["short_put"], "SELL", "OPEN_SHORT_PUT"),
            ("LONG_PUT", condor["long_put"], "BUY", "OPEN_LONG_PUT"),
            ("SHORT_CALL", condor["short_call"], "SELL", "OPEN_SHORT_CALL"),
            ("LONG_CALL", condor["long_call"], "BUY", "OPEN_LONG_CALL"),
        ]

        pending: List[Dict[str, Any]] = []
        for leg_role, contract, side, effect in legs:
            signal_row, fill_row = self.loader.get_signal_and_fill_rows(contract["Symbol"], signal_ts)
            if signal_row is None or fill_row is None:
                return False

            fill_ts = pd.Timestamp(fill_row["CLOCK"])
            if fill_ts.date() != pd.Timestamp(signal_ts).date():
                return False

            signal_vol = float(signal_row.get("VOLUME", 0.0) or 0.0)
            fill_vol = float(fill_row.get("VOLUME", 0.0) or 0.0)

            if signal_vol <= float(self.config["min_volume_signal"]):
                return False
            if fill_vol <= float(self.config["min_volume_fill"]):
                return False

            open_price = float(fill_row["OPEN"])
            min_tick = float(contract["MinTick"])
            fill_price = apply_slippage_to_open(
                open_price=open_price,
                side=side,
                slippage_ticks=float(self.config["cost_option_slippage_ticks"]),
                min_tick=min_tick,
            )

            multiplier = int(contract["Multiplier"])
            qty = int(qty_contracts)
            notional = fill_price * qty * multiplier
            fee = estimate_option_fee(qty, float(self.config["cost_option_fee_per_contract"]))

            if side.upper() == "SELL":
                slippage_cash = max(0.0, (open_price - fill_price) * qty * multiplier)
            else:
                slippage_cash = max(0.0, (fill_price - open_price) * qty * multiplier)

            cash_flow = option_cash_flow(fill_price, qty, multiplier, side) - fee
            signed_qty = -qty if side.upper() == "SELL" else qty

            pending.append(
                {
                    "leg_role": leg_role,
                    "ts_fill": fill_ts,
                    "symbol": str(contract["Symbol"]),
                    "option_type": str(contract["OptType"]).title(),
                    "side": side.upper(),
                    "effect": effect,
                    "qty": qty,
                    "signed_qty": signed_qty,
                    "price": float(fill_price),
                    "notional": float(notional),
                    "fee": float(fee),
                    "slippage": float(slippage_cash),
                    "cash_flow": float(cash_flow),
                    "strike": float(contract["Strike"]),
                    "expiry": contract["EndDate"],
                    "multiplier": multiplier,
                    "min_tick": min_tick,
                }
            )

        if self.state.current_cycle_id is None:
            self.state.start_cycle()

        self.state.cash_ledger += float(sum(x["cash_flow"] for x in pending))
        self.state.option_legs = [
            OptionLeg(
                role=str(x["leg_role"]),
                symbol=str(x["symbol"]),
                option_type=str(x["option_type"]),
                side="SHORT" if int(x["signed_qty"]) < 0 else "LONG",
                qty_contracts=int(x["signed_qty"]),
                strike=float(x["strike"]),
                expiry=x["expiry"],
                multiplier=int(x["multiplier"]),
                min_tick=float(x["min_tick"]),
            )
            for x in pending
        ]
        self.state.state = STATE_SHORT_CONDOR

        for x in pending:
            self._append_order(
                ts_signal=signal_ts,
                ts_fill=x["ts_fill"],
                symbol=x["symbol"],
                asset_type="OPTION",
                option_type=x["option_type"],
                leg_role=x["leg_role"],
                side=x["side"],
                effect=x["effect"],
                qty=int(x["qty"]),
                price=float(x["price"]),
                notional=float(x["notional"]),
                fee=float(x["fee"]),
                slippage=float(x["slippage"]),
                cash_flow=float(x["cash_flow"]),
                status="filled",
                reason=x["effect"],
                cycle_id=self.state.current_cycle_id,
                cycle_closed=False,
            )

        event_ts = max(pd.Timestamp(x["ts_fill"]) for x in pending)
        self._record_position(event_ts, snapshot_type="event")
        return True

    def _settle_expiry(self, eod_row: pd.Series) -> None:
        if not self.state.option_legs:
            return

        ts = pd.Timestamp(eod_row["CLOCK"])
        spot = float(eod_row["CLOSE"])

        settlement_cash = 0.0
        for leg in self.state.option_legs:
            intrinsic = 0.0
            if leg.option_type == "Put":
                intrinsic = max(float(leg.strike) - spot, 0.0)
            elif leg.option_type == "Call":
                intrinsic = max(spot - float(leg.strike), 0.0)

            settlement_cash += float(leg.qty_contracts) * intrinsic * int(leg.multiplier)

        self.state.cash_ledger += float(settlement_cash)
        self.state.option_legs = []
        self.state.state = STATE_FLAT
        self.state.close_cycle()

        self._record_position(ts, snapshot_type="event", etf_mark_override=spot)

    def _record_daily_snapshot(self, trade_date, eod_row: pd.Series) -> None:
        ts = pd.Timestamp(eod_row["CLOCK"])
        etf_mark = float(eod_row["CLOSE"])
        snap = self._record_position(ts, snapshot_type="daily", etf_mark_override=etf_mark)

        equity = float(snap["equity_ledger"])
        daily_pnl = equity - self._prev_equity
        self._prev_equity = equity

        self._equity_peak = max(self._equity_peak, equity)
        drawdown = equity - self._equity_peak

        realized = float(self.state.cash_ledger)
        unrealized = float(snap["option_mv"] + snap["etf_mv"])

        self.daily_rows.append(
            {
                "date": pd.Timestamp(trade_date),
                "cash_eod": float(self.state.cash_ledger),
                "option_mv_eod": float(snap["option_mv"]),
                "etf_mv_eod": float(snap["etf_mv"]),
                "equity_eod": equity,
                "realized_pnl_cum": realized,
                "unrealized_pnl_eod": unrealized,
                "daily_pnl": float(daily_pnl),
                "total_pnl_cum": equity,
                "drawdown": float(drawdown),
            }
        )

    def _record_position(
        self,
        ts: pd.Timestamp,
        snapshot_type: str,
        etf_mark_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        leg_map = {leg.role: leg for leg in self.state.option_legs}

        if etf_mark_override is None:
            etf_mark = self.loader.get_close_price_at_or_before(self.state.etf_symbol, ts) or 0.0
        else:
            etf_mark = float(etf_mark_override)

        option_mv = 0.0
        for leg in self.state.option_legs:
            mark = self.loader.get_close_price_at_or_before(leg.symbol, ts)
            option_mark = float(mark) if mark is not None else 0.0
            option_mv += float(leg.qty_contracts) * option_mark * int(leg.multiplier)

        short_put = leg_map.get("SHORT_PUT")
        long_put = leg_map.get("LONG_PUT")
        short_call = leg_map.get("SHORT_CALL")
        long_call = leg_map.get("LONG_CALL")

        put_wing_width = 0.0
        if short_put is not None and long_put is not None:
            put_wing_width = float(short_put.strike) - float(long_put.strike)

        call_wing_width = 0.0
        if short_call is not None and long_call is not None:
            call_wing_width = float(long_call.strike) - float(short_call.strike)

        multiplier = 0
        if short_put is not None:
            multiplier = int(short_put.multiplier)
        elif short_call is not None:
            multiplier = int(short_call.multiplier)

        max_loss_per_set = max(float(put_wing_width), float(call_wing_width)) * float(multiplier)

        contracts_per_set = 0
        if short_put is not None:
            contracts_per_set = abs(int(short_put.qty_contracts))
        elif short_call is not None:
            contracts_per_set = abs(int(short_call.qty_contracts))

        etf_mv = 0.0
        equity = float(self.state.cash_ledger) + float(option_mv) + float(etf_mv)

        snapshot = {
            "ts": pd.Timestamp(ts),
            "snapshot_type": snapshot_type,
            "state": self.state.state,
            "cycle_id": self.state.current_cycle_id,
            "expiry": pd.Timestamp(short_put.expiry) if short_put is not None else (
                pd.Timestamp(short_call.expiry) if short_call is not None else pd.NaT
            ),
            "short_put_symbol": short_put.symbol if short_put is not None else None,
            "long_put_symbol": long_put.symbol if long_put is not None else None,
            "short_call_symbol": short_call.symbol if short_call is not None else None,
            "long_call_symbol": long_call.symbol if long_call is not None else None,
            "short_put_strike": float(short_put.strike) if short_put is not None else None,
            "long_put_strike": float(long_put.strike) if long_put is not None else None,
            "short_call_strike": float(short_call.strike) if short_call is not None else None,
            "long_call_strike": float(long_call.strike) if long_call is not None else None,
            "contracts_per_set": int(contracts_per_set),
            "multiplier": int(multiplier),
            "put_wing_width": float(put_wing_width),
            "call_wing_width": float(call_wing_width),
            "max_loss_per_set": float(max_loss_per_set),
            "option_mv": float(option_mv),
            "etf_symbol": self.state.etf_symbol,
            "etf_qty": 0,
            "etf_avg_cost": 0.0,
            "etf_mark": float(etf_mark),
            "etf_mv": float(etf_mv),
            "cash_ledger": float(self.state.cash_ledger),
            "equity_ledger": float(equity),
        }
        self.positions.append(snapshot)
        return snapshot

    def _append_order(
        self,
        ts_signal,
        ts_fill,
        symbol: str,
        asset_type: str,
        option_type: Optional[str],
        leg_role: str,
        side: str,
        effect: str,
        qty: int,
        price: float,
        notional: float,
        fee: float,
        slippage: float,
        cash_flow: float,
        status: str,
        reason: str,
        cycle_id: Optional[int],
        cycle_closed: bool,
    ) -> None:
        self.orders.append(
            {
                "order_id": int(self._order_seq),
                "strategy_id": self.config["strategy_id"],
                "cycle_id": cycle_id,
                "cycle_closed": bool(cycle_closed),
                "ts_signal": pd.Timestamp(ts_signal) if ts_signal is not None else pd.NaT,
                "ts_fill": pd.Timestamp(ts_fill) if ts_fill is not None else pd.NaT,
                "symbol": symbol,
                "asset_type": asset_type,
                "option_type": option_type,
                "leg_role": leg_role,
                "side": side,
                "effect": effect,
                "qty": int(qty),
                "price": float(price),
                "notional": float(notional),
                "fee": float(fee),
                "slippage": float(slippage),
                "cash_flow": float(cash_flow),
                "status": status,
                "reason": reason,
            }
        )
        self._order_seq += 1


def _derive_option_code_from_etf_symbol(etf_symbol: str) -> str:
    text = str(etf_symbol or "").strip()
    if not text:
        return ""

    parts = text.split(".")
    if parts:
        tail = parts[-1]
        if re.fullmatch(r"\d{6,}", tail):
            return tail

    match = re.search(r"(\d{6,})", text)
    return match.group(1) if match else ""


def load_config(config_path: str) -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()

    path = Path(config_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()

    user_cfg = {}
    if path.exists():
        content = path.read_text(encoding="utf-8")
        user_cfg = yaml.safe_load(content) or {}

    cfg.update(user_cfg)
    cfg["_config_path"] = str(path)
    cfg["_config_dir"] = str(path.parent)

    cfg["fixed_contracts"] = int(cfg["fixed_contracts"])
    cfg["short_put_offset_abs"] = float(cfg["short_put_offset_abs"])
    cfg["long_put_offset_abs"] = float(cfg["long_put_offset_abs"])
    cfg["short_call_offset_abs"] = float(cfg["short_call_offset_abs"])
    cfg["long_call_offset_abs"] = float(cfg["long_call_offset_abs"])
    cfg["strike_selection_mode"] = str(cfg.get("strike_selection_mode", "abs")).strip().lower()
    cfg["short_put_target_delta"] = float(cfg.get("short_put_target_delta", 0.20))
    cfg["long_put_target_delta"] = float(cfg.get("long_put_target_delta", 0.10))
    cfg["short_call_target_delta"] = float(cfg.get("short_call_target_delta", 0.20))
    cfg["long_call_target_delta"] = float(cfg.get("long_call_target_delta", 0.10))
    cfg["min_volume_signal"] = float(cfg["min_volume_signal"])
    cfg["min_volume_fill"] = float(cfg["min_volume_fill"])
    cfg["cost_option_fee_per_contract"] = float(cfg["cost_option_fee_per_contract"])
    cfg["cost_option_slippage_ticks"] = float(cfg["cost_option_slippage_ticks"])
    cfg["cost_etf_fee_rate"] = float(cfg.get("cost_etf_fee_rate", 0.0))
    cfg["cost_etf_slippage_bps"] = float(cfg.get("cost_etf_slippage_bps", 0.0))
    cfg["dividend_yield"] = float(cfg.get("dividend_yield", 0.0))
    cfg["initial_capital"] = float(cfg.get("initial_capital", 1_000_000.0))
    cfg["contract_sizing"] = str(cfg.get("contract_sizing", "capital"))
    cfg["risk_free_rate"] = float(cfg.get("risk_free_rate", 0.0))
    cfg["use_iv_rank_filter"] = bool(cfg.get("use_iv_rank_filter", False))
    cfg["iv_rank_entry_min"] = float(cfg.get("iv_rank_entry_min", 0.50))
    cfg["iv_rank_lookback_days"] = int(cfg.get("iv_rank_lookback_days", 252))
    cfg["iv_rank_min_history"] = int(cfg.get("iv_rank_min_history", 60))
    cfg["iv_rank_min_iv"] = float(cfg.get("iv_rank_min_iv", 0.01))
    cfg["use_disk_cache"] = bool(cfg.get("use_disk_cache", True))
    cfg["entry_start_time"] = str(cfg.get("entry_start_time", "09:30:00"))
    cfg["trade_start_offset_days"] = int(cfg.get("trade_start_offset_days", 0))
    cfg["etf_symbol"] = str(cfg["etf_symbol"])

    derived_code = _derive_option_code_from_etf_symbol(cfg["etf_symbol"])
    fallback_code = str(cfg.get("option_code", "")).strip()
    cfg["option_code"] = derived_code or fallback_code
    if not cfg["option_code"]:
        raise ValueError(f"Cannot derive option_code from etf_symbol: {cfg['etf_symbol']}")

    cache_dir = cfg.get("cache_dir")
    if cache_dir:
        cache_path = Path(cache_dir)
        if not cache_path.is_absolute():
            cache_path = (Path(cfg["_config_dir"]) / cache_path).resolve()
        cfg["cache_dir"] = str(cache_path)

    return cfg


def run_backtest(config_path: str) -> Dict[str, Any]:
    cfg = load_config(config_path)

    engine = IronCondorBacktestEngine(cfg)
    frames = engine.run()

    output_dir = Path(cfg["output_dir"])
    if not output_dir.is_absolute():
        config_dir = Path(cfg.get("_config_dir", "."))
        output_dir = (config_dir / output_dir).resolve()

    save_results(output_dir, frames)
    analysis = analyze(
        output_dir,
        initial_capital=float(cfg["initial_capital"]),
        risk_free_rate=float(cfg.get("risk_free_rate", 0.0)),
        strategy_id=str(cfg.get("strategy_id", "AZYC002001")),
    )

    return {
        "orders": frames["orders"],
        "positions": frames["positions"],
        "daily_pnl": frames["daily_pnl"],
        "metrics": analysis["metrics"],
        "analysis": {k: v for k, v in analysis.items() if k != "metrics"},
        "artifacts_path": str(output_dir),
    }
