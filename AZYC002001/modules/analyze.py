from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd

from AZYC001001.modules.analyze import analyze as base_analyze

try:
    from .io_store import load_results
except ImportError:  # pragma: no cover
    from io_store import load_results


def analyze(
    result_dir: str | Path,
    initial_capital: float = 1_000_000.0,
    risk_free_rate: float = 0.0,
    strategy_id: str = "AZYC002001",
) -> Dict[str, Any]:
    out_dir = Path(result_dir)
    base = base_analyze(
        result_dir=out_dir,
        initial_capital=float(initial_capital),
        risk_free_rate=float(risk_free_rate),
        strategy_id=strategy_id,
    )

    frames = load_results(out_dir)
    orders = frames.get("orders", pd.DataFrame())
    positions = frames.get("positions", pd.DataFrame())

    extra = _build_condor_metrics(orders, positions)
    metrics = base.get("metrics", pd.DataFrame()).copy()
    if metrics.empty:
        metrics = extra
    else:
        for col in extra.columns:
            metrics[col] = extra[col].iloc[0]

    metrics.to_parquet(out_dir / "metrics.parquet", index=False)
    metrics.to_csv(out_dir / "metrics.csv", index=False)
    base["metrics"] = metrics
    return base


def _build_condor_metrics(orders: pd.DataFrame, positions: pd.DataFrame) -> pd.DataFrame:
    defaults = {
        "condor_open_count": 0,
        "avg_open_credit_per_cycle": 0.0,
        "median_open_credit_per_cycle": 0.0,
        "positive_credit_cycle_ratio": 0.0,
        "avg_put_wing_width": 0.0,
        "avg_call_wing_width": 0.0,
        "avg_max_loss_per_set": 0.0,
    }

    if orders.empty:
        return pd.DataFrame([defaults])

    frame = orders.copy()
    if "status" in frame.columns:
        frame = frame[frame["status"].astype(str).str.lower() == "filled"].copy()

    open_effects = {"OPEN_SHORT_PUT", "OPEN_LONG_PUT", "OPEN_SHORT_CALL", "OPEN_LONG_CALL"}
    if "effect" in frame.columns:
        open_rows = frame[frame["effect"].astype(str).isin(open_effects)].copy()
    else:
        open_rows = pd.DataFrame()

    cycle_credit = pd.Series(dtype=float)
    if not open_rows.empty and "cycle_id" in open_rows.columns:
        open_rows["cycle_id"] = pd.to_numeric(open_rows["cycle_id"], errors="coerce")
        if "cash_flow" in open_rows.columns:
            open_rows["cash_flow"] = pd.to_numeric(open_rows["cash_flow"], errors="coerce").fillna(0.0)
        else:
            open_rows["cash_flow"] = 0.0
        grouped = open_rows.dropna(subset=["cycle_id"]).groupby("cycle_id", as_index=True)["cash_flow"].sum()
        cycle_credit = grouped.astype(float)

    out = defaults.copy()
    out["condor_open_count"] = int(len(cycle_credit))
    if len(cycle_credit) > 0:
        out["avg_open_credit_per_cycle"] = float(cycle_credit.mean())
        out["median_open_credit_per_cycle"] = float(cycle_credit.median())
        out["positive_credit_cycle_ratio"] = float((cycle_credit > 0).mean())

    if not positions.empty:
        pos = positions.copy()
        for col in ["put_wing_width", "call_wing_width", "max_loss_per_set"]:
            if col in pos.columns:
                pos[col] = pd.to_numeric(pos[col], errors="coerce")
            else:
                pos[col] = pd.NA

        if "snapshot_type" in pos.columns:
            pos = pos[pos["snapshot_type"].astype(str).str.lower() == "event"].copy()
        if "contracts_per_set" in pos.columns:
            pos["contracts_per_set"] = pd.to_numeric(pos["contracts_per_set"], errors="coerce").fillna(0.0)
            pos = pos[pos["contracts_per_set"] > 0].copy()

        if not pos.empty:
            out["avg_put_wing_width"] = _safe_mean(pos["put_wing_width"])
            out["avg_call_wing_width"] = _safe_mean(pos["call_wing_width"])
            out["avg_max_loss_per_set"] = _safe_mean(pos["max_loss_per_set"])

    return pd.DataFrame([out])


def _safe_mean(series: pd.Series) -> float:
    value = pd.to_numeric(series, errors="coerce").dropna().mean()
    if pd.isna(value):
        return 0.0
    return float(value)
