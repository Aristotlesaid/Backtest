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


def analyze(result_dir: str | Path) -> Dict[str, Any]:
    out_dir = Path(result_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = load_results(out_dir)
    orders = frames.get("orders", pd.DataFrame())
    daily = frames.get("daily_pnl", pd.DataFrame())

    metrics = _build_metrics(orders, daily)
    metrics.to_parquet(out_dir / "metrics.parquet", index=False)
    metrics.to_csv(out_dir / "metrics.csv", index=False)

    _plot_equity_curve(daily, out_dir / "equity_curve.png")
    _plot_drawdown(daily, out_dir / "drawdown.png")

    return {
        "metrics": metrics,
        "equity_curve_path": str(out_dir / "equity_curve.png"),
        "drawdown_path": str(out_dir / "drawdown.png"),
    }


def _build_metrics(orders: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    total_pnl = float(daily["total_pnl_cum"].iloc[-1]) if not daily.empty else 0.0
    max_drawdown = float(daily["drawdown"].min()) if not daily.empty else 0.0

    if not orders.empty and "status" in orders.columns:
        filled_orders = orders[orders["status"].astype(str).str.lower() == "filled"].copy()
    else:
        filled_orders = pd.DataFrame()

    trade_count = int(len(filled_orders))
    put_sell_count = int((filled_orders.get("effect", "") == "OPEN_SHORT_PUT").sum()) if not filled_orders.empty else 0
    call_sell_count = int((filled_orders.get("effect", "") == "OPEN_SHORT_CALL").sum()) if not filled_orders.empty else 0
    assignment_count = int((filled_orders.get("effect", "") == "ASSIGN_PUT").sum()) if not filled_orders.empty else 0
    call_away_count = int((filled_orders.get("effect", "") == "CALL_AWAY").sum()) if not filled_orders.empty else 0

    win_rate_cycle, avg_cycle_days = _cycle_stats(filled_orders)

    metrics = pd.DataFrame(
        [
            {
                "total_pnl": total_pnl,
                "max_drawdown": max_drawdown,
                "trade_count": trade_count,
                "put_sell_count": put_sell_count,
                "call_sell_count": call_sell_count,
                "assignment_count": assignment_count,
                "call_away_count": call_away_count,
                "win_rate_cycle": win_rate_cycle,
                "avg_cycle_days": avg_cycle_days,
            }
        ]
    )
    return metrics


def _cycle_stats(filled_orders: pd.DataFrame) -> tuple[float, float]:
    if filled_orders.empty or "cycle_id" not in filled_orders.columns:
        return float("nan"), float("nan")

    table = filled_orders.copy()
    if "cycle_closed" in table.columns:
        table["cycle_closed"] = table["cycle_closed"].fillna(False).astype(bool)
    else:
        table["cycle_closed"] = False
    table["cycle_id"] = pd.to_numeric(table["cycle_id"], errors="coerce")
    table = table.dropna(subset=["cycle_id"]).copy()
    if table.empty:
        return float("nan"), float("nan")

    closed_ids = sorted(table.loc[table["cycle_closed"], "cycle_id"].unique().tolist())
    if not closed_ids:
        return float("nan"), float("nan")

    cycle_pnls: List[float] = []
    cycle_days: List[float] = []

    for cycle_id in closed_ids:
        cycle_rows = table[table["cycle_id"] == cycle_id].copy()
        if cycle_rows.empty:
            continue

        cash_flow = pd.to_numeric(cycle_rows.get("cash_flow", 0.0), errors="coerce").fillna(0.0).sum()
        cycle_pnls.append(float(cash_flow))

        start_rows = cycle_rows[cycle_rows.get("effect", "") == "OPEN_SHORT_PUT"]
        close_rows = cycle_rows[cycle_rows["cycle_closed"]]
        if start_rows.empty or close_rows.empty:
            continue

        start_ts = pd.to_datetime(start_rows["ts_fill"]).min()
        end_ts = pd.to_datetime(close_rows["ts_fill"]).max()
        if pd.isna(start_ts) or pd.isna(end_ts):
            continue

        cycle_days.append(float((end_ts - start_ts).total_seconds() / 86400.0))

    if not cycle_pnls:
        return float("nan"), float("nan")

    win_rate = float((pd.Series(cycle_pnls) > 0).mean())
    avg_days = float(pd.Series(cycle_days).mean()) if cycle_days else float("nan")
    return win_rate, avg_days


def _plot_equity_curve(daily: pd.DataFrame, path: Path) -> None:
    if daily.empty:
        return

    series = daily.copy()
    series["date"] = pd.to_datetime(series["date"])
    series = series.sort_values("date")

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(series["date"], series["total_pnl_cum"], linewidth=1.5)
    ax.set_title("AZYC001001 Equity Curve")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative PnL")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _plot_drawdown(daily: pd.DataFrame, path: Path) -> None:
    if daily.empty:
        return

    series = daily.copy()
    series["date"] = pd.to_datetime(series["date"])
    series = series.sort_values("date")

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.fill_between(series["date"], series["drawdown"], 0.0, alpha=0.35)
    ax.plot(series["date"], series["drawdown"], linewidth=1.2)
    ax.set_title("AZYC001001 Drawdown")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
