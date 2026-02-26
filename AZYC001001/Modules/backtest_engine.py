from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

try:
    from .analyze import analyze
    from .contract_selector import ContractSelector
    from .data_loader import CausisDataLoader
    from .execution import (
        apply_slippage_to_open,
        estimate_etf_fee,
        estimate_option_fee,
        option_cash_flow,
    )
    from .io_store import save_results
    from .state_machine import (
        STATE_FLAT,
        STATE_LONG_ETF_SHORT_CALL,
        STATE_SHORT_PUT,
        OptionLeg,
        StrategyState,
    )
except ImportError:  # pragma: no cover
    from analyze import analyze
    from contract_selector import ContractSelector
    from data_loader import CausisDataLoader
    from execution import apply_slippage_to_open, estimate_etf_fee, estimate_option_fee, option_cash_flow
    from io_store import save_results
    from state_machine import STATE_FLAT, STATE_LONG_ETF_SHORT_CALL, STATE_SHORT_PUT, OptionLeg, StrategyState


DEFAULT_CONFIG: Dict[str, Any] = {
    "strategy_id": "AZYC001001",
    "etf_symbol": "S.CN.SZSE.159915",
    "option_code": "159915",
    "start_date": "2025-01-01",
    "end_date": "2025-12-31",
    "frequency": "minute1",
    "fixed_contracts": 1,
    "put_offset_abs": 0.2,
    "call_offset_abs": 0.2,
    "expiry_rule": "next_month_2nd",
    "min_volume_signal": 20,
    "min_volume_fill": 20,
    "cost_option_fee_per_contract": 0.0,
    "cost_option_slippage_ticks": 0.0,
    "cost_etf_fee_rate": 0.0,
    "cost_etf_slippage_bps": 0.0,
    "option_prefetch_chunk_size": 20,
    "output_dir": "outputs/AZYC001001",
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
    "option_symbol",
    "option_type",
    "option_qty",
    "strike",
    "expiry",
    "multiplier",
    "option_mark",
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


class WheelBacktestEngine:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.loader = CausisDataLoader(
            start_date=config["start_date"],
            end_date=config["end_date"],
            frequency=config["frequency"],
            option_code=config["option_code"],
        )
        self.selector = ContractSelector(
            put_offset_abs=float(config["put_offset_abs"]),
            call_offset_abs=float(config["call_offset_abs"]),
            expiry_rule=str(config["expiry_rule"]),
        )

        self.state = StrategyState(etf_symbol=str(config["etf_symbol"]))

        self.orders: List[Dict[str, Any]] = []
        self.positions: List[Dict[str, Any]] = []
        self.daily_rows: List[Dict[str, Any]] = []
        self._order_seq = 1

        self._prev_equity = 0.0
        self._equity_peak = 0.0

    def run(self) -> Dict[str, pd.DataFrame]:
        etf_bars = self.loader.load_etf_bars(self.config["etf_symbol"])
        if etf_bars.empty:
            raise ValueError("ETF minute bars are empty for configured backtest range.")

        etf_bars = etf_bars.copy()
        etf_bars["trade_date"] = etf_bars["CLOCK"].dt.date

        for trade_date, day_bars in etf_bars.groupby("trade_date", sort=True):
            day_bars = day_bars.sort_values("CLOCK").reset_index(drop=True)
            eod_row = self.loader.pick_eod_bar(day_bars)

            if self.state.state == STATE_FLAT:
                self._scan_open_put(day_bars, trade_date)
            elif self.state.state == STATE_LONG_ETF_SHORT_CALL and self.state.option_leg is None and self.state.etf_qty > 0:
                self._scan_open_call(day_bars, trade_date)

            if self.state.option_leg is not None and self.state.option_leg.expiry == trade_date:
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

    def _scan_open_put(self, day_bars: pd.DataFrame, trade_date) -> bool:
        option_chain = self.loader.load_option_chain(trade_date)
        if option_chain.empty:
            return False

        self._prefetch_chain_side(option_chain, trade_date, opt_type="Put")
        qty_contracts = max(1, int(self.config["fixed_contracts"]))

        for row in day_bars.itertuples(index=False):
            signal_ts = pd.Timestamp(row.CLOCK)
            spot_price = float(row.CLOSE)
            contract = self.selector.select_put_contract(option_chain, trade_date, spot_price)
            if not contract:
                continue

            opened = self._try_open_short_option(
                contract=contract,
                signal_ts=signal_ts,
                qty_contracts=qty_contracts,
                effect="OPEN_SHORT_PUT",
            )
            if opened:
                return True

        return False

    def _prefetch_chain_side(self, option_chain: pd.DataFrame, trade_date, opt_type: str) -> None:
        if option_chain.empty:
            return

        side_chain = option_chain[option_chain["OptType"].astype(str).str.title() == str(opt_type).title()].copy()
        if side_chain.empty:
            return

        expiries = sorted({pd.to_datetime(x, errors="coerce").date() for x in side_chain["EndDate"].dropna().tolist() if pd.notna(pd.to_datetime(x, errors="coerce"))})
        expiries = [e for e in expiries if e is not None and e >= trade_date]
        if not expiries:
            return

        target_expiry = expiries[1] if len(expiries) >= 2 else expiries[0]
        target_symbols = side_chain[side_chain["EndDate"] == target_expiry]["Symbol"].dropna().unique().tolist()
        if not target_symbols:
            return

        self.loader.prefetch_option_bars(
            target_symbols,
            chunk_size=int(self.config.get("option_prefetch_chunk_size", 20)),
        )

    def _scan_open_call(self, day_bars: pd.DataFrame, trade_date) -> bool:
        option_chain = self.loader.load_option_chain(trade_date)
        if option_chain.empty:
            return False

        self._prefetch_chain_side(option_chain, trade_date, opt_type="Call")
        for row in day_bars.itertuples(index=False):
            signal_ts = pd.Timestamp(row.CLOCK)
            spot_price = float(row.CLOSE)
            contract = self.selector.select_call_contract(option_chain, trade_date, spot_price)
            if not contract:
                continue

            max_cover_qty = self.state.etf_qty // int(contract["Multiplier"])
            qty_contracts = min(max(1, int(self.config["fixed_contracts"])), int(max_cover_qty))
            if qty_contracts <= 0:
                continue

            opened = self._try_open_short_option(
                contract=contract,
                signal_ts=signal_ts,
                qty_contracts=qty_contracts,
                effect="OPEN_SHORT_CALL",
            )
            if opened:
                return True

        return False

    def _try_open_short_option(
        self,
        contract: Dict[str, Any],
        signal_ts: pd.Timestamp,
        qty_contracts: int,
        effect: str,
    ) -> bool:
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

        side = "SELL"
        open_price = float(fill_row["OPEN"])
        min_tick = float(contract["MinTick"])
        fill_price = apply_slippage_to_open(
            open_price=open_price,
            side=side,
            slippage_ticks=float(self.config["cost_option_slippage_ticks"]),
            min_tick=min_tick,
        )

        multiplier = int(contract["Multiplier"])
        notional = fill_price * int(qty_contracts) * multiplier
        fee = estimate_option_fee(int(qty_contracts), float(self.config["cost_option_fee_per_contract"]))
        slippage_cash = max(0.0, (open_price - fill_price) * int(qty_contracts) * multiplier)
        cash_flow = option_cash_flow(fill_price, int(qty_contracts), multiplier, side) - fee

        if self.state.current_cycle_id is None:
            self.state.start_cycle()

        self.state.cash_ledger += cash_flow
        self.state.option_leg = OptionLeg(
            symbol=str(contract["Symbol"]),
            option_type=str(contract["OptType"]).title(),
            qty_contracts=-int(qty_contracts),
            strike=float(contract["Strike"]),
            expiry=contract["EndDate"],
            multiplier=multiplier,
            min_tick=min_tick,
        )

        if self.state.option_leg.option_type == "Put":
            self.state.state = STATE_SHORT_PUT
        else:
            self.state.state = STATE_LONG_ETF_SHORT_CALL

        self._append_order(
            ts_signal=signal_ts,
            ts_fill=fill_ts,
            symbol=self.state.option_leg.symbol,
            asset_type="OPTION",
            option_type=self.state.option_leg.option_type,
            side=side,
            effect=effect,
            qty=int(qty_contracts),
            price=fill_price,
            notional=notional,
            fee=fee,
            slippage=slippage_cash,
            cash_flow=cash_flow,
            status="filled",
            reason=effect,
            cycle_id=self.state.current_cycle_id,
            cycle_closed=False,
        )

        self._record_position(fill_ts, snapshot_type="event")
        return True

    def _settle_expiry(self, eod_row: pd.Series) -> None:
        leg = self.state.option_leg
        if leg is None:
            return

        ts = pd.Timestamp(eod_row["CLOCK"])
        spot = float(eod_row["CLOSE"])
        cycle_id = self.state.current_cycle_id

        if leg.option_type == "Put":
            if spot < leg.strike:
                etf_qty = abs(leg.qty_contracts) * int(leg.multiplier)
                bps = float(self.config["cost_etf_slippage_bps"]) / 10000.0
                exec_price = float(leg.strike) * (1.0 + max(0.0, bps))
                notional = exec_price * int(etf_qty)
                fee = estimate_etf_fee(notional, float(self.config["cost_etf_fee_rate"]))
                cash_flow = -notional - fee

                prev_cost = self.state.etf_avg_cost * self.state.etf_qty
                self.state.etf_qty += int(etf_qty)
                self.state.etf_avg_cost = (prev_cost + notional + fee) / self.state.etf_qty
                self.state.cash_ledger += cash_flow

                slippage_cash = max(0.0, (exec_price - float(leg.strike)) * int(etf_qty))

                self._append_order(
                    ts_signal=ts,
                    ts_fill=ts,
                    symbol=self.state.etf_symbol,
                    asset_type="ETF",
                    option_type=None,
                    side="BUY",
                    effect="ASSIGN_PUT",
                    qty=int(etf_qty),
                    price=exec_price,
                    notional=notional,
                    fee=fee,
                    slippage=slippage_cash,
                    cash_flow=cash_flow,
                    status="filled",
                    reason="ASSIGN_AT_EXPIRY",
                    cycle_id=cycle_id,
                    cycle_closed=False,
                )

                self.state.option_leg = None
                self.state.state = STATE_LONG_ETF_SHORT_CALL
            else:
                self._append_order(
                    ts_signal=ts,
                    ts_fill=ts,
                    symbol=leg.symbol,
                    asset_type="OPTION",
                    option_type=leg.option_type,
                    side="BUY",
                    effect="EXPIRE_PUT",
                    qty=abs(leg.qty_contracts),
                    price=0.0,
                    notional=0.0,
                    fee=0.0,
                    slippage=0.0,
                    cash_flow=0.0,
                    status="filled",
                    reason="EXPIRE_WORTHLESS",
                    cycle_id=cycle_id,
                    cycle_closed=True,
                )
                self.state.option_leg = None
                self.state.state = STATE_FLAT
                self.state.close_cycle()

        elif leg.option_type == "Call":
            if spot > leg.strike and self.state.etf_qty > 0:
                deliver_qty = min(self.state.etf_qty, abs(leg.qty_contracts) * int(leg.multiplier))
                bps = float(self.config["cost_etf_slippage_bps"]) / 10000.0
                exec_price = float(leg.strike) * (1.0 - max(0.0, bps))
                notional = exec_price * int(deliver_qty)
                fee = estimate_etf_fee(notional, float(self.config["cost_etf_fee_rate"]))
                cash_flow = notional - fee

                self.state.cash_ledger += cash_flow
                self.state.etf_qty -= int(deliver_qty)
                if self.state.etf_qty == 0:
                    self.state.etf_avg_cost = 0.0

                cycle_closed = self.state.etf_qty == 0
                slippage_cash = max(0.0, (float(leg.strike) - exec_price) * int(deliver_qty))
                self._append_order(
                    ts_signal=ts,
                    ts_fill=ts,
                    symbol=self.state.etf_symbol,
                    asset_type="ETF",
                    option_type=None,
                    side="SELL",
                    effect="CALL_AWAY",
                    qty=int(deliver_qty),
                    price=exec_price,
                    notional=notional,
                    fee=fee,
                    slippage=slippage_cash,
                    cash_flow=cash_flow,
                    status="filled",
                    reason="ASSIGN_AT_EXPIRY",
                    cycle_id=cycle_id,
                    cycle_closed=cycle_closed,
                )

                self.state.option_leg = None
                if self.state.etf_qty == 0:
                    self.state.state = STATE_FLAT
                    self.state.close_cycle()
                else:
                    self.state.state = STATE_LONG_ETF_SHORT_CALL
            else:
                self._append_order(
                    ts_signal=ts,
                    ts_fill=ts,
                    symbol=leg.symbol,
                    asset_type="OPTION",
                    option_type=leg.option_type,
                    side="BUY",
                    effect="EXPIRE_CALL",
                    qty=abs(leg.qty_contracts),
                    price=0.0,
                    notional=0.0,
                    fee=0.0,
                    slippage=0.0,
                    cash_flow=0.0,
                    status="filled",
                    reason="EXPIRE_WORTHLESS",
                    cycle_id=cycle_id,
                    cycle_closed=False,
                )
                self.state.option_leg = None
                self.state.state = STATE_LONG_ETF_SHORT_CALL

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
        leg = self.state.option_leg

        if etf_mark_override is None:
            etf_mark = self.loader.get_close_price_at_or_before(self.state.etf_symbol, ts) or 0.0
        else:
            etf_mark = float(etf_mark_override)

        option_mark = 0.0
        option_mv = 0.0
        option_symbol = None
        option_type = None
        option_qty = 0
        strike = None
        expiry = None
        multiplier = None

        if leg is not None:
            option_symbol = leg.symbol
            option_type = leg.option_type
            option_qty = int(leg.qty_contracts)
            strike = float(leg.strike)
            expiry = pd.Timestamp(leg.expiry)
            multiplier = int(leg.multiplier)

            mark = self.loader.get_close_price_at_or_before(leg.symbol, ts)
            option_mark = float(mark) if mark is not None else 0.0
            option_mv = option_qty * option_mark * multiplier

        etf_mv = float(self.state.etf_qty) * float(etf_mark)
        equity = float(self.state.cash_ledger) + option_mv + etf_mv

        snapshot = {
            "ts": pd.Timestamp(ts),
            "snapshot_type": snapshot_type,
            "state": self.state.state,
            "cycle_id": self.state.current_cycle_id,
            "option_symbol": option_symbol,
            "option_type": option_type,
            "option_qty": option_qty,
            "strike": strike,
            "expiry": expiry,
            "multiplier": multiplier,
            "option_mark": float(option_mark),
            "option_mv": float(option_mv),
            "etf_symbol": self.state.etf_symbol,
            "etf_qty": int(self.state.etf_qty),
            "etf_avg_cost": float(self.state.etf_avg_cost),
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
    cfg["put_offset_abs"] = float(cfg["put_offset_abs"])
    cfg["call_offset_abs"] = float(cfg["call_offset_abs"])
    cfg["min_volume_signal"] = float(cfg["min_volume_signal"])
    cfg["min_volume_fill"] = float(cfg["min_volume_fill"])
    cfg["cost_option_fee_per_contract"] = float(cfg["cost_option_fee_per_contract"])
    cfg["cost_option_slippage_ticks"] = float(cfg["cost_option_slippage_ticks"])
    cfg["cost_etf_fee_rate"] = float(cfg["cost_etf_fee_rate"])
    cfg["cost_etf_slippage_bps"] = float(cfg["cost_etf_slippage_bps"])
    cfg["option_prefetch_chunk_size"] = int(cfg.get("option_prefetch_chunk_size", 20))

    return cfg


def run_backtest(config_path: str) -> Dict[str, Any]:
    cfg = load_config(config_path)

    engine = WheelBacktestEngine(cfg)
    frames = engine.run()

    output_dir = Path(cfg["output_dir"])
    if not output_dir.is_absolute():
        config_dir = Path(cfg.get("_config_dir", "."))
        output_dir = (config_dir / output_dir).resolve()

    save_results(output_dir, frames)
    analysis = analyze(output_dir)

    return {
        "orders": frames["orders"],
        "positions": frames["positions"],
        "daily_pnl": frames["daily_pnl"],
        "metrics": analysis["metrics"],
        "artifacts_path": str(output_dir),
    }
