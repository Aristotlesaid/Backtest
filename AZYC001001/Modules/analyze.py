from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

try:
    from .io_store import load_results
except ImportError:  # pragma: no cover
    from io_store import load_results


def analyze(
    result_dir: str | Path,
    initial_capital: float = 1_000_000.0,
    risk_free_rate: float = 0.0,
    strategy_id: str = "AZYC001001",
) -> Dict[str, Any]:
    out_dir = Path(result_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = load_results(out_dir)
    orders = frames.get("orders", pd.DataFrame())
    positions = frames.get("positions", pd.DataFrame())
    daily = frames.get("daily_pnl", pd.DataFrame())

    principal = float(initial_capital) if float(initial_capital) > 0 else 1.0
    daily_enriched = _enrich_daily(daily, positions, principal)

    metrics = _build_metrics(
        orders=orders,
        positions=positions,
        daily=daily_enriched,
        initial_capital=principal,
        risk_free_rate=float(risk_free_rate),
    )
    metrics.to_parquet(out_dir / "metrics.parquet", index=False)
    metrics.to_csv(out_dir / "metrics.csv", index=False)

    daily_enriched.to_parquet(out_dir / "daily_enriched.parquet", index=False)
    daily_enriched.to_csv(out_dir / "daily_enriched.csv", index=False)

    equity_curve_path = out_dir / "equity_curve.png"
    drawdown_path = out_dir / "drawdown.png"
    monthly_pnl_path = out_dir / "monthly_pnl.png"
    rolling_sharpe_path = out_dir / "rolling_sharpe_60d.png"
    excess_benchmark_path = out_dir / "excess_vs_benchmark.png"
    position_state_path = out_dir / "position_state.png"

    strategy_label = str(strategy_id).strip() or "Strategy"

    _plot_equity_curve(daily_enriched, equity_curve_path, strategy_label)
    _plot_drawdown(daily_enriched, drawdown_path, strategy_label)
    _plot_monthly_pnl(daily_enriched, monthly_pnl_path, strategy_label)
    _plot_rolling_sharpe(daily_enriched, rolling_sharpe_path, strategy_label)
    _plot_excess_vs_benchmark(daily_enriched, excess_benchmark_path, strategy_label)
    _plot_position_state(positions, position_state_path, strategy_label)

    return {
        "metrics": metrics,
        "daily_enriched": daily_enriched,
        "equity_curve_path": str(equity_curve_path),
        "drawdown_path": str(drawdown_path),
        "monthly_pnl_path": str(monthly_pnl_path),
        "rolling_sharpe_path": str(rolling_sharpe_path),
        "excess_benchmark_path": str(excess_benchmark_path),
        "position_state_path": str(position_state_path),
    }


def _enrich_daily(daily: pd.DataFrame, positions: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame(
            columns=[
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
                "equity_curve",
                "daily_return",
                "cum_return",
                "drawdown_pct",
                "rolling_sharpe_60d",
                "benchmark_price",
                "benchmark_shares",
                "benchmark_cash",
                "benchmark_equity",
                "benchmark_return",
                "benchmark_daily_return",
                "excess_equity",
                "excess_return",
                "excess_daily_return",
            ]
        )

    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    for col in ["daily_pnl", "total_pnl_cum", "drawdown"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)

    principal = float(initial_capital) if float(initial_capital) > 0 else 1.0
    frame["equity_curve"] = principal + frame["total_pnl_cum"]
    frame["daily_return"] = frame["daily_pnl"] / principal
    frame["cum_return"] = frame["total_pnl_cum"] / principal
    frame["drawdown_pct"] = frame["drawdown"] / principal

    roll_mean = frame["daily_return"].rolling(window=60, min_periods=20).mean()
    roll_std = frame["daily_return"].rolling(window=60, min_periods=20).std(ddof=0)
    frame["rolling_sharpe_60d"] = 0.0
    valid = roll_std > 0
    frame.loc[valid, "rolling_sharpe_60d"] = (roll_mean[valid] / roll_std[valid]) * (252 ** 0.5)

    # 基准：初始本金在首个有效日按 ETF 收盘价买入并持有（份数固定）。
    benchmark = _build_benchmark_curve(positions, principal)
    if benchmark.empty:
        frame["benchmark_price"] = pd.NA
        frame["benchmark_shares"] = 0
        frame["benchmark_cash"] = principal
        frame["benchmark_equity"] = principal
        frame["benchmark_return"] = 0.0
        frame["benchmark_daily_return"] = 0.0
    else:
        frame = frame.merge(benchmark, on="date", how="left")
        frame["benchmark_price"] = pd.to_numeric(frame["benchmark_price"], errors="coerce")
        frame["benchmark_shares"] = pd.to_numeric(frame["benchmark_shares"], errors="coerce").fillna(0).astype(int)
        frame["benchmark_cash"] = pd.to_numeric(frame["benchmark_cash"], errors="coerce").fillna(principal)
        frame["benchmark_equity"] = pd.to_numeric(frame["benchmark_equity"], errors="coerce")
        frame["benchmark_equity"] = frame["benchmark_equity"].ffill().bfill().fillna(principal)
        frame["benchmark_return"] = frame["benchmark_equity"] / principal - 1.0
        frame["benchmark_daily_return"] = frame["benchmark_equity"].pct_change().fillna(0.0)

    frame["excess_equity"] = frame["equity_curve"] - frame["benchmark_equity"]
    frame["excess_return"] = frame["cum_return"] - frame["benchmark_return"]
    frame["excess_daily_return"] = frame["daily_return"] - frame["benchmark_daily_return"]

    return frame


def _build_benchmark_curve(positions: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    if positions.empty or initial_capital <= 0:
        return pd.DataFrame(
            columns=["date", "benchmark_price", "benchmark_shares", "benchmark_cash", "benchmark_equity"]
        )

    pos = positions.copy()
    pos["ts"] = pd.to_datetime(pos.get("ts"), errors="coerce")
    pos = pos.dropna(subset=["ts"]).copy()
    if pos.empty or "etf_mark" not in pos.columns:
        return pd.DataFrame(
            columns=["date", "benchmark_price", "benchmark_shares", "benchmark_cash", "benchmark_equity"]
        )

    if "snapshot_type" in pos.columns:
        pos = pos[pos["snapshot_type"].astype(str).str.lower() == "daily"].copy()
    if pos.empty:
        return pd.DataFrame(
            columns=["date", "benchmark_price", "benchmark_shares", "benchmark_cash", "benchmark_equity"]
        )

    pos["date"] = pos["ts"].dt.normalize()
    pos["etf_mark"] = pd.to_numeric(pos["etf_mark"], errors="coerce")
    pos = pos.dropna(subset=["etf_mark"]).copy()
    pos = pos[pos["etf_mark"] > 0].copy()
    if pos.empty:
        return pd.DataFrame(
            columns=["date", "benchmark_price", "benchmark_shares", "benchmark_cash", "benchmark_equity"]
        )

    benchmark_price = pos.groupby("date", as_index=False)["etf_mark"].last().rename(columns={"etf_mark": "benchmark_price"})
    first_price = float(benchmark_price["benchmark_price"].iloc[0])
    if first_price <= 0:
        return pd.DataFrame(
            columns=["date", "benchmark_price", "benchmark_shares", "benchmark_cash", "benchmark_equity"]
        )

    # 只在起点买一次，后续不调仓；剩余现金留存。
    shares = int(float(initial_capital) // first_price)
    cash = float(initial_capital) - shares * first_price
    benchmark_price["benchmark_shares"] = int(shares)
    benchmark_price["benchmark_cash"] = float(cash)
    benchmark_price["benchmark_equity"] = benchmark_price["benchmark_price"] * shares + cash
    return benchmark_price


def _build_metrics(
    orders: pd.DataFrame,
    positions: pd.DataFrame,
    daily: pd.DataFrame,
    initial_capital: float,
    risk_free_rate: float,
) -> pd.DataFrame:
    total_pnl = float(daily["total_pnl_cum"].iloc[-1]) if not daily.empty else 0.0
    max_drawdown = float(daily["drawdown"].min()) if not daily.empty else 0.0
    total_return = total_pnl / initial_capital if initial_capital > 0 else 0.0
    max_drawdown_pct = max_drawdown / initial_capital if initial_capital > 0 else 0.0
    benchmark_return = float(daily["benchmark_return"].iloc[-1]) if ("benchmark_return" in daily.columns and not daily.empty) else 0.0
    benchmark_equity = (
        float(daily["benchmark_equity"].iloc[-1]) if ("benchmark_equity" in daily.columns and not daily.empty) else initial_capital
    )
    excess_return = total_return - benchmark_return
    excess_pnl = float(total_pnl - (benchmark_equity - initial_capital))

    if not orders.empty and "status" in orders.columns:
        filled_orders = orders[orders["status"].astype(str).str.lower() == "filled"].copy()
    else:
        filled_orders = pd.DataFrame()

    trade_count = int(len(filled_orders))
    put_sell_count = int((filled_orders.get("effect", "") == "OPEN_SHORT_PUT").sum()) if not filled_orders.empty else 0
    call_sell_count = int((filled_orders.get("effect", "") == "OPEN_SHORT_CALL").sum()) if not filled_orders.empty else 0

    assignment_by_order = int((filled_orders.get("effect", "") == "ASSIGN_PUT").sum()) if not filled_orders.empty else 0
    call_away_by_order = int((filled_orders.get("effect", "") == "CALL_AWAY").sum()) if not filled_orders.empty else 0
    assignment_by_pos, call_away_by_pos = _event_counts_from_positions(positions)

    assignment_count = max(assignment_by_order, assignment_by_pos)
    call_away_count = max(call_away_by_order, call_away_by_pos)

    cycle = _cycle_stats(positions, daily)
    win_rate_cycle = float(cycle["win_rate_cycle"])
    avg_cycle_days = float(cycle["avg_cycle_days"])

    trading_days = int(len(daily))
    years = (trading_days / 252.0) if trading_days > 0 else 0.0
    annual_return = (total_return / years) if years > 0 else 0.0

    daily_ret = daily["daily_return"].astype(float) if "daily_return" in daily.columns else pd.Series(dtype=float)
    rf_daily = float(risk_free_rate) / 252.0
    excess = daily_ret - rf_daily
    vol_daily = float(daily_ret.std(ddof=1)) if len(daily_ret) >= 2 else 0.0
    annual_volatility = vol_daily * (252 ** 0.5) if vol_daily > 0 else 0.0

    sharpe = 0.0
    if vol_daily > 0:
        sharpe = float((excess.mean() / vol_daily) * (252 ** 0.5))

    downside = excess[excess < 0]
    downside_std = float(downside.std(ddof=1)) if len(downside) >= 2 else 0.0
    sortino = float((excess.mean() / downside_std) * (252 ** 0.5)) if downside_std > 0 else 0.0

    calmar = 0.0
    if max_drawdown_pct < 0:
        calmar = float(annual_return / abs(max_drawdown_pct))

    day_win_rate = float((daily["daily_pnl"] > 0).mean()) if not daily.empty else 0.0
    gross_profit = float(daily.loc[daily["daily_pnl"] > 0, "daily_pnl"].sum()) if not daily.empty else 0.0
    gross_loss = float(daily.loc[daily["daily_pnl"] < 0, "daily_pnl"].sum()) if not daily.empty else 0.0
    profit_factor_day = float(gross_profit / abs(gross_loss)) if gross_loss < 0 else 0.0
    excess_daily = daily["excess_daily_return"].astype(float) if ("excess_daily_return" in daily.columns) else pd.Series(dtype=float)
    tracking_error_daily = float(excess_daily.std(ddof=1)) if len(excess_daily) >= 2 else 0.0
    tracking_error_annual = tracking_error_daily * (252 ** 0.5) if tracking_error_daily > 0 else 0.0
    info_ratio = float((excess_daily.mean() / tracking_error_daily) * (252 ** 0.5)) if tracking_error_daily > 0 else 0.0

    metrics = pd.DataFrame(
        [
            {
                "initial_capital": float(initial_capital),
                "total_pnl": total_pnl,
                "total_return": float(total_return),
                "benchmark_return": float(benchmark_return),
                "excess_return": float(excess_return),
                "excess_pnl": float(excess_pnl),
                "annual_return_simple": float(annual_return),
                "annual_volatility": float(annual_volatility),
                "sharpe": float(sharpe),
                "sortino": float(sortino),
                "calmar": float(calmar),
                "tracking_error_annual": float(tracking_error_annual),
                "information_ratio": float(info_ratio),
                "max_drawdown": max_drawdown,
                "max_drawdown_pct": float(max_drawdown_pct),
                "trading_days": trading_days,
                "trade_count": trade_count,
                "put_sell_count": put_sell_count,
                "call_sell_count": call_sell_count,
                "assignment_count": int(assignment_count),
                "call_away_count": int(call_away_count),
                "cycle_count_closed": int(cycle["cycle_count_closed"]),
                "cycle_count_open": int(cycle["cycle_count_open"]),
                "win_rate_cycle": win_rate_cycle,
                "avg_cycle_days": avg_cycle_days,
                "day_win_rate": float(day_win_rate),
                "profit_factor_day": float(profit_factor_day),
            }
        ]
    )
    return metrics


def _event_counts_from_positions(positions: pd.DataFrame) -> tuple[int, int]:
    if positions.empty:
        return 0, 0

    pos = positions.copy()
    pos["ts"] = pd.to_datetime(pos.get("ts"), errors="coerce")
    pos = pos.dropna(subset=["ts"]).copy()
    if "snapshot_type" in pos.columns:
        pos = pos[pos["snapshot_type"].astype(str).str.lower() == "event"].copy()
    if pos.empty:
        return 0, 0

    pos = pos.sort_values("ts").reset_index(drop=True)
    pos["state"] = pos.get("state", "").astype(str)
    pos["etf_qty"] = pd.to_numeric(pos.get("etf_qty", 0), errors="coerce").fillna(0).astype(int)

    assignment_count = 0
    call_away_count = 0
    for i in range(1, len(pos)):
        prev = pos.iloc[i - 1]
        cur = pos.iloc[i]

        if prev["state"] == "SHORT_PUT" and cur["state"] == "LONG_ETF_SHORT_CALL" and cur["etf_qty"] > prev["etf_qty"]:
            assignment_count += 1

        if prev["state"] == "LONG_ETF_SHORT_CALL" and prev["etf_qty"] > 0 and cur["state"] == "FLAT" and cur["etf_qty"] == 0:
            call_away_count += 1

    return int(assignment_count), int(call_away_count)


def _cycle_stats(positions: pd.DataFrame, daily: pd.DataFrame) -> Dict[str, float]:
    # 注意：这里不依赖平仓 order，而是从 positions 的 cycle_id 变化推断闭环。
    intervals = _extract_cycle_intervals(positions)
    if intervals.empty or daily.empty:
        return {
            "cycle_count_closed": 0,
            "cycle_count_open": int(len(intervals)),
            "win_rate_cycle": 0.0,
            "avg_cycle_days": 0.0,
        }

    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if frame.empty:
        return {
            "cycle_count_closed": 0,
            "cycle_count_open": int(len(intervals)),
            "win_rate_cycle": 0.0,
            "avg_cycle_days": 0.0,
        }

    closed = intervals[intervals["closed"]].copy()
    if closed.empty:
        return {
            "cycle_count_closed": 0,
            "cycle_count_open": int((~intervals["closed"]).sum()),
            "win_rate_cycle": 0.0,
            "avg_cycle_days": 0.0,
        }

    cycle_pnls: List[float] = []
    cycle_days: List[float] = []
    for row in closed.itertuples(index=False):
        start_date = pd.Timestamp(row.start_ts).date()
        end_date = pd.Timestamp(row.end_ts).date()

        pnl_before_start = _pnl_at_or_before(frame, start_date, inclusive=False)
        pnl_at_close = _pnl_at_or_before(frame, end_date, inclusive=True)
        cycle_pnls.append(float(pnl_at_close - pnl_before_start))

        days = float((pd.Timestamp(row.end_ts) - pd.Timestamp(row.start_ts)).total_seconds() / 86400.0)
        cycle_days.append(max(0.0, days))

    win_rate = float((pd.Series(cycle_pnls) > 0).mean()) if cycle_pnls else 0.0
    avg_days = float(pd.Series(cycle_days).mean()) if cycle_days else 0.0

    return {
        "cycle_count_closed": int(len(closed)),
        "cycle_count_open": int((~intervals["closed"]).sum()),
        "win_rate_cycle": win_rate,
        "avg_cycle_days": avg_days,
    }


def _extract_cycle_intervals(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(columns=["cycle_id", "start_ts", "end_ts", "closed"])

    pos = positions.copy()
    pos["ts"] = pd.to_datetime(pos.get("ts"), errors="coerce")
    pos = pos.dropna(subset=["ts"]).copy()
    if pos.empty:
        return pd.DataFrame(columns=["cycle_id", "start_ts", "end_ts", "closed"])

    pos["cycle_id"] = pd.to_numeric(pos.get("cycle_id"), errors="coerce")
    if "snapshot_type" in pos.columns:
        priority = {"event": 0, "daily": 1}
        pos["_pri"] = pos["snapshot_type"].astype(str).str.lower().map(priority).fillna(9)
        pos = pos.sort_values(["ts", "_pri"]).reset_index(drop=True)
    else:
        pos = pos.sort_values("ts").reset_index(drop=True)

    intervals: List[Dict[str, Any]] = []
    active_cycle_id: int | None = None
    active_start_ts = None

    for row in pos.itertuples(index=False):
        row_cycle = None if pd.isna(row.cycle_id) else int(row.cycle_id)
        ts = pd.Timestamp(row.ts)

        if active_cycle_id is None:
            if row_cycle is not None:
                active_cycle_id = row_cycle
                active_start_ts = ts
            continue

        if row_cycle == active_cycle_id:
            continue

        intervals.append(
            {
                "cycle_id": int(active_cycle_id),
                "start_ts": pd.Timestamp(active_start_ts),
                "end_ts": ts,
                "closed": True,
            }
        )

        if row_cycle is None:
            active_cycle_id = None
            active_start_ts = None
        else:
            active_cycle_id = int(row_cycle)
            active_start_ts = ts

    if active_cycle_id is not None:
        intervals.append(
            {
                "cycle_id": int(active_cycle_id),
                "start_ts": pd.Timestamp(active_start_ts),
                "end_ts": pd.Timestamp(pos["ts"].iloc[-1]),
                "closed": False,
            }
        )

    return pd.DataFrame(intervals, columns=["cycle_id", "start_ts", "end_ts", "closed"])


def _pnl_at_or_before(daily: pd.DataFrame, target_date, inclusive: bool) -> float:
    if daily.empty:
        return 0.0
    date_col = daily["date"].dt.date
    if inclusive:
        sub = daily[date_col <= target_date]
    else:
        sub = daily[date_col < target_date]
    if sub.empty:
        return 0.0
    return float(pd.to_numeric(sub["total_pnl_cum"], errors="coerce").fillna(0.0).iloc[-1])


def _plot_equity_curve(daily: pd.DataFrame, path: Path, strategy_label: str) -> None:
    if daily.empty:
        return

    series = daily.copy()
    series["date"] = pd.to_datetime(series["date"])
    series = series.sort_values("date")

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(series["date"], series["equity_curve"], linewidth=1.5)
    ax.set_title(f"{strategy_label} Equity Curve")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _plot_drawdown(daily: pd.DataFrame, path: Path, strategy_label: str) -> None:
    if daily.empty:
        return

    series = daily.copy()
    series["date"] = pd.to_datetime(series["date"])
    series = series.sort_values("date")

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.fill_between(series["date"], series["drawdown"], 0.0, alpha=0.35)
    ax.plot(series["date"], series["drawdown"], linewidth=1.2)
    ax.set_title(f"{strategy_label} Drawdown")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _plot_monthly_pnl(daily: pd.DataFrame, path: Path, strategy_label: str) -> None:
    if daily.empty:
        return

    series = daily.copy()
    series["date"] = pd.to_datetime(series["date"])
    series = series.sort_values("date")
    series["month"] = series["date"].dt.to_period("M").astype(str)
    monthly = series.groupby("month", as_index=False)["daily_pnl"].sum()
    if monthly.empty:
        return

    colors = ["#2ca02c" if x >= 0 else "#d62728" for x in monthly["daily_pnl"]]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(monthly["month"], monthly["daily_pnl"], color=colors, alpha=0.8)
    ax.set_title(f"{strategy_label} Monthly PnL")
    ax.set_xlabel("Month")
    ax.set_ylabel("PnL")
    ax.tick_params(axis="x", rotation=45)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _plot_rolling_sharpe(daily: pd.DataFrame, path: Path, strategy_label: str) -> None:
    if daily.empty or "rolling_sharpe_60d" not in daily.columns:
        return

    series = daily.copy()
    series["date"] = pd.to_datetime(series["date"])
    series = series.sort_values("date")

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(series["date"], series["rolling_sharpe_60d"], linewidth=1.2)
    ax.axhline(0.0, linewidth=1.0, color="gray", alpha=0.7)
    ax.set_title(f"{strategy_label} Rolling Sharpe (60D)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Sharpe")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _plot_excess_vs_benchmark(daily: pd.DataFrame, path: Path, strategy_label: str) -> None:
    required = {"equity_curve", "benchmark_equity", "excess_equity", "date"}
    if daily.empty or not required.issubset(set(daily.columns)):
        return

    series = daily.copy()
    series["date"] = pd.to_datetime(series["date"], errors="coerce")
    series = series.dropna(subset=["date"]).sort_values("date")
    if series.empty:
        return

    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [2, 1]})

    axes[0].plot(series["date"], series["equity_curve"], label="Strategy Equity", linewidth=1.5)
    axes[0].plot(series["date"], series["benchmark_equity"], label="Benchmark Buy&Hold ETF", linewidth=1.3)
    axes[0].set_title(f"{strategy_label} Strategy vs Benchmark")
    axes[0].set_ylabel("Equity")
    axes[0].grid(alpha=0.3)
    axes[0].legend(loc="best")

    axes[1].plot(series["date"], series["excess_equity"], color="#d62728", linewidth=1.4, label="Excess Equity")
    axes[1].axhline(0.0, color="gray", linewidth=1.0, alpha=0.8)
    axes[1].set_title("Excess Over Benchmark")
    axes[1].set_xlabel("Date")
    axes[1].set_ylabel("Excess")
    axes[1].grid(alpha=0.3)
    axes[1].legend(loc="best")

    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _plot_position_state(positions: pd.DataFrame, path: Path, strategy_label: str) -> None:
    if positions.empty:
        return

    pos = positions.copy()
    pos["ts"] = pd.to_datetime(pos.get("ts"), errors="coerce")
    pos = pos.dropna(subset=["ts"]).copy()
    if pos.empty:
        return

    if "snapshot_type" in pos.columns:
        daily = pos[pos["snapshot_type"].astype(str).str.lower() == "daily"].copy()
        if not daily.empty:
            pos = daily

    pos = pos.sort_values("ts").reset_index(drop=True)
    state_col = pos.get("state", "").astype(str)

    preferred_order = ["FLAT", "SHORT_PUT", "SHORT_CONDOR", "LONG_ETF_SHORT_CALL"]
    pretty_name = {
        "FLAT": "Flat",
        "SHORT_PUT": "Short Put",
        "SHORT_CONDOR": "Short Condor",
        "LONG_ETF_SHORT_CALL": "Long ETF + Short Call",
    }

    observed = [x for x in state_col.dropna().astype(str).unique().tolist() if x]
    ordered = [x for x in preferred_order if x in observed] + [x for x in observed if x not in preferred_order]
    if not ordered:
        ordered = ["FLAT"]

    state_to_code = {name: i for i, name in enumerate(ordered)}
    pos["state_code"] = state_col.map(state_to_code).fillna(0).astype(int)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.step(pos["ts"], pos["state_code"], where="post", linewidth=1.6, color="#1f77b4")
    ax.set_yticks(list(range(len(ordered))))
    ax.set_yticklabels([pretty_name.get(x, x) for x in ordered])
    ax.set_xlabel("Time")
    ax.set_ylabel("State")
    ax.set_title(f"{strategy_label} Position State Timeline")
    ax.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
