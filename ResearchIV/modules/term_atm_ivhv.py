from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure SharedData (sibling of ResearchIV under Backtest/) is importable.
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from SharedData.causis_data_loader import CausisDataLoader

try:
    from .iv_solver import bs_greeks, implied_vol_newton
except ImportError:  # pragma: no cover
    from iv_solver import bs_greeks, implied_vol_newton


def _derive_option_code_from_etf_symbol(etf_symbol: str) -> str:
    text = str(etf_symbol or "").strip()
    parts = text.split(".")
    if parts:
        tail = parts[-1]
        if tail.isdigit():
            return tail
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _is_valid_quote(
    *,
    spot: float,
    strike: float,
    ttm_years: float,
    price: float,
    option_type: str,
    rate: float,
    dividend_yield: float,
    min_option_price: float,
) -> bool:
    if spot <= 0 or strike <= 0 or ttm_years <= 0:
        return False
    if price < min_option_price:
        return False

    disc_r = np.exp(-rate * ttm_years)
    disc_q = np.exp(-dividend_yield * ttm_years)
    kind = option_type.strip().lower()

    if kind == "call":
        intrinsic = max(spot * disc_q - strike * disc_r, 0.0)
        upper = spot * disc_q
    else:
        intrinsic = max(strike * disc_r - spot * disc_q, 0.0)
        upper = strike * disc_r
    return price >= intrinsic and price <= upper * 1.0001


def _resolve_trade_date(input_date: str, etf_symbol: str, option_code: str) -> tuple[pd.Timestamp, float]:
    start = pd.Timestamp(input_date).normalize()
    today = pd.Timestamp.now().normalize()
    if start > today:
        raise ValueError(
            f"input_date={start.date()} is in the future (today={today.date()}). "
            "Please input a date on or before today."
        )

    end = min((start + pd.Timedelta(days=20)).normalize(), today)
    loader = CausisDataLoader(
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        frequency="day",
        option_code=option_code,
        use_disk_cache=True,
    )
    bars = loader.load_etf_bars(etf_symbol).copy()
    if bars.empty:
        raise ValueError(f"No ETF daily data for {etf_symbol} from {start.date()} to {end.date()}")

    bars["trade_date"] = pd.to_datetime(bars["CLOCK"], errors="coerce").dt.normalize()
    bars = bars.dropna(subset=["trade_date", "CLOSE"]).sort_values("trade_date")
    bars = bars[bars["trade_date"] >= start]
    if bars.empty:
        raise ValueError(f"No trading day on/after {input_date} up to today={today.date()}.")

    first = bars.iloc[0]
    return pd.Timestamp(first["trade_date"]), float(first["CLOSE"])


def _select_two_terms_four_strikes(chain: pd.DataFrame, trade_date: pd.Timestamp, spot: float) -> List[Dict[str, Any]]:
    if chain.empty:
        return []

    table = chain.copy()
    table["EndDate"] = pd.to_datetime(table["EndDate"], errors="coerce").dt.normalize()
    table["Strike"] = pd.to_numeric(table["Strike"], errors="coerce")
    table["OptType"] = table["OptType"].astype(str).str.lower()
    table = table.dropna(subset=["EndDate", "Strike", "OptType", "Symbol"])
    table["ttm_days"] = (table["EndDate"] - trade_date).dt.days
    table = table[table["ttm_days"] > 0].copy()
    if table.empty:
        return []

    expiries = sorted(table["EndDate"].unique().tolist())
    chosen_expiries = expiries[:2]
    output: List[Dict[str, Any]] = []

    for idx, expiry in enumerate(chosen_expiries, start=1):
        sub = table[table["EndDate"] == expiry].copy()
        calls = sub[sub["OptType"].str.contains("call")]
        puts = sub[sub["OptType"].str.contains("put")]
        if calls.empty or puts.empty:
            continue

        call_strikes = set(calls["Strike"].tolist())
        put_strikes = set(puts["Strike"].tolist())
        common = sorted(call_strikes.intersection(put_strikes), key=lambda k: (abs(float(k) - spot), float(k)))
        if not common:
            continue

        strike_bucket = common[:4]
        term_contracts: List[Dict[str, Any]] = []
        for strike in strike_bucket:
            call_row = calls[calls["Strike"] == strike].sort_values("Symbol").iloc[0]
            put_row = puts[puts["Strike"] == strike].sort_values("Symbol").iloc[0]
            term_contracts.append(
                {
                    "strike": float(strike),
                    "call_symbol": str(call_row["Symbol"]),
                    "put_symbol": str(put_row["Symbol"]),
                }
            )

        term_name = "NearTerm" if idx == 1 else "NextTerm"
        label = f"{term_name}_Expiry_{pd.Timestamp(expiry).strftime('%Y-%m-%d')}"
        output.append(
            {
                "term": "near" if idx == 1 else "next",
                "term_label": label,
                "expiry": pd.Timestamp(expiry),
                "contracts": term_contracts,
            }
        )
    return output


def _build_spot_and_hv(etf_bars: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    table = etf_bars.copy()
    table["date"] = pd.to_datetime(table["CLOCK"], errors="coerce").dt.normalize()
    table["close"] = pd.to_numeric(table["CLOSE"], errors="coerce")
    table = table.dropna(subset=["date", "close"]).sort_values("date")
    table = table.drop_duplicates(subset=["date"], keep="last")
    table["log_ret"] = np.log(table["close"] / table["close"].shift(1))
    table["HV30"] = table["log_ret"].rolling(window=30, min_periods=30).std(ddof=0) * np.sqrt(252.0)
    table["HV60"] = table["log_ret"].rolling(window=60, min_periods=60).std(ddof=0) * np.sqrt(252.0)
    spot = table.set_index("date")["close"]
    hv = table.set_index("date")[["HV30", "HV60"]]
    return spot, hv


def _build_option_close_map(bars: pd.DataFrame) -> pd.Series:
    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["CLOCK"], errors="coerce").dt.normalize()
    frame["close"] = pd.to_numeric(frame["CLOSE"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    frame = frame.drop_duplicates(subset=["date"], keep="last")
    return frame.set_index("date")["close"]


def _compute_greeks_panel(
    loader: CausisDataLoader,
    etf_symbol: str,
    trade_date: pd.Timestamp,
    terms: List[Dict[str, Any]],
    risk_free_rate: float,
    dividend_yield: float,
    min_option_price: float,
) -> pd.DataFrame:
    etf = loader.load_etf_bars(etf_symbol).copy()
    spot_map, hv = _build_spot_and_hv(etf)

    rows: List[Dict[str, Any]] = []
    for term in terms:
        expiry = pd.Timestamp(term["expiry"]).normalize()
        for item in term["contracts"]:
            strike = float(item["strike"])
            symbol_map = {
                "call": str(item["call_symbol"]),
                "put": str(item["put_symbol"]),
            }
            for side, symbol in symbol_map.items():
                bars = loader.load_option_bars(symbol)
                close_map = _build_option_close_map(bars)

                for dt, option_price in close_map.items():
                    date = pd.Timestamp(dt).normalize()
                    if date > trade_date:
                        continue
                    if date not in spot_map.index:
                        continue
                    ttm_days = int((expiry - date).days)
                    if ttm_days <= 0:
                        continue

                    spot = float(spot_map.loc[date])
                    option_close = float(option_price)
                    ttm_years = ttm_days / 365.0

                    if not _is_valid_quote(
                        spot=spot,
                        strike=strike,
                        ttm_years=ttm_years,
                        price=option_close,
                        option_type=side,
                        rate=risk_free_rate,
                        dividend_yield=dividend_yield,
                        min_option_price=min_option_price,
                    ):
                        continue

                    try:
                        iv = implied_vol_newton(
                            market_price=option_close,
                            spot=spot,
                            strike=strike,
                            ttm_years=ttm_years,
                            rate=risk_free_rate,
                            dividend_yield=dividend_yield,
                            option_type=side,
                        )
                        greeks = bs_greeks(
                            spot=spot,
                            strike=strike,
                            ttm_years=ttm_years,
                            rate=risk_free_rate,
                            dividend_yield=dividend_yield,
                            vol=float(iv),
                            option_type=side,
                        )
                    except Exception:
                        continue

                    hv30 = np.nan
                    hv60 = np.nan
                    if date in hv.index:
                        hv30 = float(hv.loc[date, "HV30"]) if pd.notna(hv.loc[date, "HV30"]) else np.nan
                        hv60 = float(hv.loc[date, "HV60"]) if pd.notna(hv.loc[date, "HV60"]) else np.nan

                    rows.append(
                        {
                            "date": date,
                            "term": str(term["term"]),
                            "term_label": str(term["term_label"]),
                            "expiry": expiry,
                            "strike": float(strike),
                            "side": side,
                            "symbol": symbol,
                            "contract_key": f"{'C' if side == 'call' else 'P'}_K{strike:g}",
                            "spot": float(spot),
                            "option_close": float(option_close),
                            "ttm_days": int(ttm_days),
                            "ttm_years": float(ttm_years),
                            "IV": float(iv),
                            "Delta": float(greeks["Delta"]),
                            "Gamma": float(greeks["Gamma"]),
                            "Theta": float(greeks["Theta"]),
                            "Vega": float(greeks["Vega"]),
                            "HV30": hv30,
                            "HV60": hv60,
                        }
                    )

    panel = pd.DataFrame(rows)
    if panel.empty:
        return panel
    panel = panel.sort_values(["date", "term", "strike", "side"]).reset_index(drop=True)
    return panel


def _resolve_output_dir(output_dir: str | Path, trade_date: pd.Timestamp) -> Path:
    out_dir = Path(output_dir)
    if not out_dir.is_absolute():
        out_dir = (Path(__file__).resolve().parent / out_dir).resolve()
    out_dir = out_dir / f"term_atm_greeks_hv_{trade_date.strftime('%Y%m%d')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _resolve_calc_cache_dir(cache_dir: str | Path | None) -> Path:
    if cache_dir is None:
        root = (_project_root / "DataCache" / "research_iv").resolve()
    else:
        root = Path(cache_dir)
        if not root.is_absolute():
            root = (_project_root / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_cache_key(payload: Dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _cache_file_map(cache_root: Path, cache_key: str) -> Dict[str, Path]:
    folder = cache_root / cache_key
    return {
        "folder": folder,
        "meta": folder / "meta.json",
        "selected": folder / "selected_contracts.parquet",
        "panel": folder / "greeks_panel.parquet",
    }


def _try_load_calc_cache(cache_root: Path, cache_key: str) -> Optional[Dict[str, Any]]:
    paths = _cache_file_map(cache_root, cache_key)
    if not paths["meta"].exists() or not paths["selected"].exists() or not paths["panel"].exists():
        return None
    try:
        meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
        selected = pd.read_parquet(paths["selected"])
        panel = pd.read_parquet(paths["panel"])
        if "date" in panel.columns:
            panel["date"] = pd.to_datetime(panel["date"], errors="coerce").dt.normalize()
        if "expiry" in panel.columns:
            panel["expiry"] = pd.to_datetime(panel["expiry"], errors="coerce").dt.normalize()
        return {
            "meta": meta,
            "selected_contracts": selected,
            "greeks_panel": panel,
        }
    except Exception:
        return None


def _save_calc_cache(
    cache_root: Path,
    cache_key: str,
    meta: Dict[str, Any],
    selected_contracts: pd.DataFrame,
    greeks_panel: pd.DataFrame,
) -> None:
    paths = _cache_file_map(cache_root, cache_key)
    paths["folder"].mkdir(parents=True, exist_ok=True)
    paths["meta"].write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    selected_contracts.to_parquet(paths["selected"], index=False)
    greeks_panel.to_parquet(paths["panel"], index=False)


def _split_term_frames(greeks_panel: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if greeks_panel.empty:
        return {}
    frames: Dict[str, pd.DataFrame] = {}
    for term_label, frame in greeks_panel.groupby("term_label", sort=False):
        out = frame.copy()
        out = out.sort_values(["date", "strike", "side"]).reset_index(drop=True)
        frames[str(term_label)] = out
    return frames


def _plot_term_panel(frame: pd.DataFrame, out_path: Path, title: str) -> None:
    if frame.empty:
        return

    panel = frame.copy()
    panel["date"] = pd.to_datetime(panel["date"], errors="coerce")
    panel = panel.dropna(subset=["date"]).sort_values("date")
    if panel.empty:
        return

    fig, axes = plt.subplots(3, 2, figsize=(16, 12), sharex=False)
    axes = axes.flatten()
    metrics = ["IV", "Delta", "Gamma", "Theta", "Vega"]

    for idx, metric in enumerate(metrics):
        ax = axes[idx]
        pivot = panel.pivot_table(index="date", columns="contract_key", values=metric, aggfunc="last")
        for col in pivot.columns:
            ax.plot(pivot.index, pivot[col], linewidth=1.1, label=str(col))
        ax.set_title(metric)
        ax.grid(alpha=0.3)
        if idx == 0:
            ax.legend(loc="best", ncol=3, fontsize=9)

    hv_ax = axes[5]
    hv_frame = (
        panel[["date", "HV30", "HV60"]]
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )
    hv_ax.plot(hv_frame["date"], hv_frame["HV30"], linewidth=1.5, label="HV30")
    hv_ax.plot(hv_frame["date"], hv_frame["HV60"], linewidth=1.5, label="HV60")
    hv_ax.set_title("HV30 / HV60")
    hv_ax.grid(alpha=0.3)
    hv_ax.legend(loc="best")

    fig.suptitle(title)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def _plot_overview(term_frames: Dict[str, pd.DataFrame], out_path: Path, title: str) -> None:
    non_empty = [k for k, v in term_frames.items() if not v.empty]
    if not non_empty:
        return

    fig, axes = plt.subplots(len(non_empty), 1, figsize=(13, 4 * len(non_empty)), sharex=False)
    if len(non_empty) == 1:
        axes = [axes]

    for ax, label in zip(axes, non_empty):
        frame = term_frames[label].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).sort_values("date")
        if frame.empty:
            continue

        iv_avg = frame.groupby("date", as_index=False)["IV"].mean().rename(columns={"IV": "IV_AVG"})
        hv = frame[["date", "HV30", "HV60"]].drop_duplicates(subset=["date"], keep="last")
        merged = iv_avg.merge(hv, on="date", how="left")

        ax.plot(merged["date"], merged["IV_AVG"], linewidth=1.8, label="IV AVG")
        ax.plot(merged["date"], merged["HV30"], linewidth=1.3, linestyle="--", label="HV30")
        ax.plot(merged["date"], merged["HV60"], linewidth=1.3, linestyle=":", label="HV60")
        ax.set_title(label)
        ax.set_ylabel("Vol")
        ax.grid(alpha=0.3)
        ax.legend(loc="best")

    fig.suptitle(title)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def _materialize_outputs(
    out_dir: Path,
    selected_contracts: pd.DataFrame,
    greeks_panel: pd.DataFrame,
    input_date: str,
    trade_date: pd.Timestamp,
    trade_spot: float,
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    selected_path = out_dir / "selected_contracts.csv"
    selected_contracts.to_csv(selected_path, index=False)

    panel_csv_path = out_dir / "greeks_panel.csv"
    panel_parquet_path = out_dir / "greeks_panel.parquet"
    greeks_panel.to_csv(panel_csv_path, index=False)
    greeks_panel.to_parquet(panel_parquet_path, index=False)

    term_frames = _split_term_frames(greeks_panel)
    term_csv_paths: List[str] = []
    term_plot_paths: List[str] = []
    for label, frame in term_frames.items():
        csv_path = out_dir / f"{label}.csv"
        frame.to_csv(csv_path, index=False)
        term_csv_paths.append(str(csv_path))

        png_path = out_dir / f"{label}.png"
        _plot_term_panel(
            frame=frame,
            out_path=png_path,
            title=f"{label} | Input={input_date} | TradeDate={trade_date.strftime('%Y-%m-%d')} | Spot={trade_spot:.4f}",
        )
        term_plot_paths.append(str(png_path))

    overview_path = out_dir / "term_atm_greeks_hv_overview.png"
    _plot_overview(
        term_frames=term_frames,
        out_path=overview_path,
        title=f"Input={input_date} | TradeDate={trade_date.strftime('%Y-%m-%d')} | Spot={trade_spot:.4f}",
    )

    return {
        "selected_contracts_path": str(selected_path),
        "greeks_panel_csv_path": str(panel_csv_path),
        "greeks_panel_parquet_path": str(panel_parquet_path),
        "term_csv_paths": term_csv_paths,
        "term_plot_paths": term_plot_paths,
        "overview_plot_path": str(overview_path),
        "term_frames": term_frames,
    }


def _build_cache_payload(
    *,
    input_date: str,
    trade_date: pd.Timestamp,
    etf_symbol: str,
    option_code: str,
    lookback_days: int,
    risk_free_rate: float,
    dividend_yield: float,
    min_option_price: float,
    selected_contracts: pd.DataFrame,
) -> Dict[str, Any]:
    contract_records = (
        selected_contracts[["term", "expiry", "strike", "call_symbol", "put_symbol"]]
        .sort_values(["term", "expiry", "strike", "call_symbol", "put_symbol"])
        .to_dict(orient="records")
    )
    return {
        "engine": "term_atm_greeks_hv_v1",
        "input_date": str(input_date),
        "trade_date": trade_date.strftime("%Y-%m-%d"),
        "etf_symbol": str(etf_symbol),
        "option_code": str(option_code),
        "lookback_days": int(lookback_days),
        "risk_free_rate": float(risk_free_rate),
        "dividend_yield": float(dividend_yield),
        "min_option_price": float(min_option_price),
        "selected_contracts": contract_records,
    }


def build_term_atm_greeks_hv_snapshot(
    *,
    input_date: str,
    etf_symbol: str = "S.CN.SZSE.159915",
    option_code: Optional[str] = None,
    lookback_days: int = 240,
    hv_window: Optional[int] = None,
    risk_free_rate: float = 0.015,
    dividend_yield: float = 0.0,
    min_option_price: float = 0.001,
    output_dir: str | Path = "../outputs",
    use_calc_cache: bool = True,
    cache_dir: str | Path | None = None,
) -> Dict[str, Any]:
    option_code = str(option_code or _derive_option_code_from_etf_symbol(etf_symbol)).strip()
    if not option_code:
        raise ValueError(f"Cannot infer option_code from etf_symbol={etf_symbol}")

    trade_date, trade_spot = _resolve_trade_date(input_date=input_date, etf_symbol=etf_symbol, option_code=option_code)
    hist_start = (trade_date - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    hist_end = trade_date.strftime("%Y-%m-%d")

    loader = CausisDataLoader(
        start_date=hist_start,
        end_date=hist_end,
        frequency="day",
        option_code=option_code,
        use_disk_cache=True,
    )
    chain = loader.load_option_chain(trade_date.date())
    terms = _select_two_terms_four_strikes(chain=chain, trade_date=trade_date, spot=trade_spot)
    if len(terms) == 0:
        raise ValueError(f"No valid near/next month ATM strikes found on {trade_date.date()}")

    contract_rows: List[Dict[str, Any]] = []
    for term in terms:
        expiry = pd.Timestamp(term["expiry"])
        for c in term["contracts"]:
            contract_rows.append(
                {
                    "term": str(term["term"]),
                    "trade_date": trade_date.strftime("%Y-%m-%d"),
                    "expiry": expiry.strftime("%Y-%m-%d"),
                    "strike": float(c["strike"]),
                    "call_symbol": str(c["call_symbol"]),
                    "put_symbol": str(c["put_symbol"]),
                }
            )
    selected_contracts = pd.DataFrame(contract_rows).sort_values(["term", "expiry", "strike"]).reset_index(drop=True)

    cache_payload = _build_cache_payload(
        input_date=input_date,
        trade_date=trade_date,
        etf_symbol=etf_symbol,
        option_code=option_code,
        lookback_days=int(lookback_days),
        risk_free_rate=float(risk_free_rate),
        dividend_yield=float(dividend_yield),
        min_option_price=float(min_option_price),
        selected_contracts=selected_contracts,
    )
    cache_key = _make_cache_key(cache_payload)
    cache_root = _resolve_calc_cache_dir(cache_dir)

    from_cache = False
    cached = _try_load_calc_cache(cache_root=cache_root, cache_key=cache_key) if use_calc_cache else None
    if cached is not None:
        from_cache = True
        selected_contracts = cached["selected_contracts"]
        greeks_panel = cached["greeks_panel"]
    else:
        greeks_panel = _compute_greeks_panel(
            loader=loader,
            etf_symbol=etf_symbol,
            trade_date=trade_date,
            terms=terms,
            risk_free_rate=float(risk_free_rate),
            dividend_yield=float(dividend_yield),
            min_option_price=float(min_option_price),
        )
        if greeks_panel.empty:
            raise ValueError("No valid IV/Greek observations were computed for selected contracts.")

        cache_meta = {
            "cache_key": cache_key,
            "input_date": str(input_date),
            "trade_date": trade_date.strftime("%Y-%m-%d"),
            "trade_spot": float(trade_spot),
            "etf_symbol": str(etf_symbol),
            "option_code": str(option_code),
            "lookback_days": int(lookback_days),
            "risk_free_rate": float(risk_free_rate),
            "dividend_yield": float(dividend_yield),
            "min_option_price": float(min_option_price),
            "row_count": int(len(greeks_panel)),
            "selected_contract_count": int(len(selected_contracts) * 2),
        }
        if use_calc_cache:
            _save_calc_cache(
                cache_root=cache_root,
                cache_key=cache_key,
                meta=cache_meta,
                selected_contracts=selected_contracts,
                greeks_panel=greeks_panel,
            )

    out_dir = _resolve_output_dir(output_dir=output_dir, trade_date=trade_date)
    output_meta = _materialize_outputs(
        out_dir=out_dir,
        selected_contracts=selected_contracts,
        greeks_panel=greeks_panel,
        input_date=str(input_date),
        trade_date=trade_date,
        trade_spot=float(trade_spot),
    )

    return {
        "input_date": str(input_date),
        "trade_date": trade_date.strftime("%Y-%m-%d"),
        "trade_spot": float(trade_spot),
        "etf_symbol": str(etf_symbol),
        "option_code": str(option_code),
        "hv_window": hv_window,
        "from_cache": bool(from_cache),
        "cache_key": str(cache_key),
        "cache_dir": str(_cache_file_map(cache_root, cache_key)["folder"]),
        "output_dir": str(out_dir),
        "plot_path": str(output_meta["overview_plot_path"]),
        "selected_contracts": selected_contracts,
        "greeks_panel": greeks_panel,
        **output_meta,
    }


def notebook_show_term_atm_greeks(**kwargs) -> Dict[str, Any]:
    result = build_term_atm_greeks_hv_snapshot(**kwargs)
    try:
        from IPython.display import Image, display

        print("input_date:", result["input_date"])
        print("trade_date:", result["trade_date"])
        print("from_cache:", result["from_cache"])
        print("overview_plot:", result["overview_plot_path"])

        selected = result["selected_contracts"].copy()
        selected = selected.sort_values(["term", "expiry", "strike"]).reset_index(drop=True)
        display(selected)

        panel = result["greeks_panel"].copy()
        panel["date"] = pd.to_datetime(panel["date"], errors="coerce")
        panel = panel.sort_values(["date", "term", "strike", "side"]).reset_index(drop=True)
        display(panel.tail(20))

        for path_text in result.get("term_plot_paths", []):
            p = Path(path_text)
            if p.exists():
                display(Image(filename=str(p)))

        overview = Path(str(result.get("overview_plot_path", "")))
        if overview.exists():
            display(Image(filename=str(overview)))
    except Exception:
        pass
    return result


def build_term_atm_iv_hv_snapshot(**kwargs) -> Dict[str, Any]:
    return build_term_atm_greeks_hv_snapshot(**kwargs)
