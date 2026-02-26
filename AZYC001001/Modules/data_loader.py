from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Dict, Iterable, Optional, Tuple

import pandas as pd

try:
    from causis_api.data import all_instruments, get_price, instruments
except Exception as exc:  # pragma: no cover - import availability depends on runtime env
    all_instruments = None
    get_price = None
    instruments = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


logger = logging.getLogger(__name__)


class CausisDataLoader:
    def __init__(self, start_date: str, end_date: str, frequency: str, option_code: str):
        self.start_date = str(start_date)
        self.end_date = str(end_date)
        self.frequency = str(frequency)
        self.option_code = str(option_code)

        self.option_chain_cache: Dict[str, pd.DataFrame] = {}
        self.price_cache: Dict[str, pd.DataFrame] = {}

    def load_etf_bars(self, symbol: str) -> pd.DataFrame:
        return self._load_price_bars(symbol)

    def load_option_bars(self, symbol: str) -> pd.DataFrame:
        return self._load_price_bars(symbol)

    def prefetch_option_bars(self, symbols: Iterable[str], chunk_size: int = 20) -> None:
        requested = [str(s) for s in symbols if s]
        if not requested:
            return

        missing = [s for s in requested if s not in self.price_cache]
        if not missing:
            return

        # 用户要求“逐日获取，不要一口气拉很多”，这里按符号逐个触发 _load_price_bars
        # _load_price_bars 内部会按日+重试拉取并缓存
        for symbol in missing:
            self._load_price_bars(symbol)

    def _prefetch_chunk_recursive(self, symbols: Iterable[str]) -> None:
        """兼容旧调用路径：退化为逐个符号预拉取。"""
        chunk = [str(s) for s in symbols if s and s not in self.price_cache]
        for symbol in chunk:
            self._load_price_bars(symbol)

    def load_option_chain(self, trade_date: date) -> pd.DataFrame:
        key = trade_date.isoformat()
        if key in self.option_chain_cache:
            return self.option_chain_cache[key].copy()

        self._check_api()
        raw_chain = self._retry_on_timeout(all_instruments, "O", key)
        if raw_chain is None:
            chain = self._empty_chain()
            self.option_chain_cache[key] = chain
            return chain.copy()

        chain = pd.DataFrame(raw_chain).copy()
        if chain.empty or "Symbol" not in chain.columns or "Code" not in chain.columns:
            chain = self._empty_chain()
            self.option_chain_cache[key] = chain
            return chain.copy()

        chain = chain[chain["Code"].astype(str) == self.option_code].copy()
        if chain.empty:
            chain = self._empty_chain()
            self.option_chain_cache[key] = chain
            return chain.copy()

        for col in ["OptType", "Strike", "MinTick", "Multiplier"]:
            if col not in chain.columns:
                chain[col] = pd.NA

        chain["OptType"] = chain["OptType"].astype(str).str.title()
        chain["Strike"] = pd.to_numeric(chain["Strike"], errors="coerce")
        chain["MinTick"] = pd.to_numeric(chain["MinTick"], errors="coerce")
        chain["Multiplier"] = pd.to_numeric(chain["Multiplier"], errors="coerce")

        missing_mask = chain[["OptType", "Strike", "Multiplier"]].isna().any(axis=1)
        missing_symbols = sorted(chain.loc[missing_mask, "Symbol"].dropna().unique().tolist())

        if missing_symbols:
            metadata = self._fetch_metadata(missing_symbols)
            if not metadata.empty:
                metadata = metadata.set_index("Symbol")
                for col in ["OptType", "Strike", "MinTick", "Multiplier"]:
                    mapped = chain["Symbol"].map(metadata[col])
                    chain[col] = chain[col].where(chain[col].notna(), mapped)

            residual_mask = chain[["OptType", "Strike", "Multiplier"]].isna().any(axis=1)
            residual_symbols = sorted(chain.loc[residual_mask, "Symbol"].dropna().unique().tolist())
            for symbol in residual_symbols:
                one = self._fetch_metadata([symbol])
                if one.empty:
                    continue
                row = one.iloc[0]
                idx = chain["Symbol"] == symbol
                for col in ["OptType", "Strike", "MinTick", "Multiplier"]:
                    if col in row:
                        chain.loc[idx & chain[col].isna(), col] = row[col]

        chain["EndDate"] = pd.to_datetime(chain.get("EndDate"), errors="coerce").dt.date
        chain["OptType"] = chain["OptType"].astype(str).str.title()
        chain["Strike"] = pd.to_numeric(chain["Strike"], errors="coerce")
        chain["MinTick"] = pd.to_numeric(chain["MinTick"], errors="coerce")
        chain["Multiplier"] = pd.to_numeric(chain["Multiplier"], errors="coerce")

        chain = chain.dropna(subset=["Symbol", "EndDate", "OptType", "Strike", "Multiplier"]).copy()
        chain = chain[chain["OptType"].isin(["Put", "Call"])].copy()
        chain["MinTick"] = chain["MinTick"].fillna(0.0001)
        chain["Multiplier"] = chain["Multiplier"].astype(int)

        keep_cols = ["Symbol", "OptType", "Strike", "EndDate", "MinTick", "Multiplier"]
        chain = chain[keep_cols].drop_duplicates(subset=["Symbol"], keep="last")
        chain = chain.sort_values(["EndDate", "OptType", "Strike", "Symbol"]).reset_index(drop=True)

        self.option_chain_cache[key] = chain
        return chain.copy()

    def get_signal_and_fill_rows(self, symbol: str, signal_ts: pd.Timestamp) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
        bars = self.load_option_bars(symbol)
        if bars.empty:
            return None, None

        ts = pd.Timestamp(signal_ts)
        loc = bars.index.searchsorted(ts)
        if loc >= len(bars) or bars.index[loc] != ts:
            return None, None

        signal_row = bars.iloc[loc]
        if loc + 1 >= len(bars):
            return signal_row, None

        fill_row = bars.iloc[loc + 1]
        return signal_row, fill_row

    def get_close_price_at_or_before(self, symbol: str, ts: pd.Timestamp) -> Optional[float]:
        bars = self.load_option_bars(symbol) if symbol.startswith("O.") else self.load_etf_bars(symbol)
        if bars.empty:
            return None

        stamp = pd.Timestamp(ts)
        loc = bars.index.searchsorted(stamp, side="right") - 1
        if loc < 0:
            return None

        value = bars.iloc[loc].get("CLOSE")
        if pd.isna(value):
            return None
        return float(value)

    @staticmethod
    def pick_eod_bar(day_bars: pd.DataFrame) -> pd.Series:
        if day_bars.empty:
            raise ValueError("day_bars is empty")

        mask = day_bars["CLOCK"].dt.strftime("%H:%M:%S") == "15:00:00"
        if mask.any():
            return day_bars.loc[mask].iloc[-1]
        return day_bars.iloc[-1]

    def _load_price_bars(self, symbol: str) -> pd.DataFrame:
        if symbol in self.price_cache:
            return self.price_cache[symbol].copy()

        self._check_api()

        bars = self._fetch_price_bars_daily(symbol)
        self.price_cache[symbol] = bars
        return bars.copy()

    def _fetch_price_bars_daily(self, symbol: str) -> pd.DataFrame:
        start = pd.Timestamp(self.start_date)
        end = pd.Timestamp(self.end_date)

        frames = []
        cursor = start
        while cursor <= end:
            date_str = cursor.strftime("%Y-%m-%d")
            raw = self._retry_on_timeout(
                get_price,
                symbol,
                start_date=date_str,
                end_date=date_str,
                frequency=self.frequency,
            )
            frame = self._normalize_price_bars(raw, default_symbol=symbol)
            if not frame.empty:
                frames.append(frame)
            cursor += pd.Timedelta(days=1)

        if not frames:
            return self._empty_bars(symbol)

        bars = pd.concat(frames, ignore_index=False)
        bars = bars.sort_values("CLOCK").drop_duplicates(subset=["CLOCK"], keep="last")
        return bars.set_index("CLOCK", drop=False)

    def _normalize_price_bars(self, raw: Any, default_symbol: Optional[str]) -> pd.DataFrame:
        if raw is None:
            return self._empty_bars(default_symbol or "UNKNOWN")

        bars = pd.DataFrame(raw).copy()
        if bars.empty:
            return self._empty_bars(default_symbol or "UNKNOWN")

        bars.columns = [str(c).upper() for c in bars.columns]
        if "CLOCK" not in bars.columns:
            return self._empty_bars(default_symbol or "UNKNOWN")

        bars["CLOCK"] = pd.to_datetime(bars["CLOCK"], errors="coerce")
        bars = bars.dropna(subset=["CLOCK"]).copy()

        for col in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]:
            if col in bars.columns:
                bars[col] = pd.to_numeric(bars[col], errors="coerce")
            else:
                bars[col] = pd.NA

        if "SYMBOL" not in bars.columns:
            bars["SYMBOL"] = default_symbol if default_symbol else pd.NA

        start_ts = pd.Timestamp(self.start_date)
        end_ts = pd.Timestamp(self.end_date) + timedelta(days=1) - pd.Timedelta(seconds=1)
        bars = bars[(bars["CLOCK"] >= start_ts) & (bars["CLOCK"] <= end_ts)].copy()

        bars = bars[["SYMBOL", "CLOCK", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]]
        bars = bars.sort_values("CLOCK").drop_duplicates(subset=["SYMBOL", "CLOCK"], keep="last")

        if default_symbol is not None:
            bars["SYMBOL"] = default_symbol
            bars = bars.set_index("CLOCK", drop=False)

        return bars

    def _store_bars_by_symbol(self, bars: pd.DataFrame, requested_symbols: Iterable[str]) -> None:
        symbols = [str(s) for s in requested_symbols if s]
        if bars.empty:
            for symbol in symbols:
                if symbol not in self.price_cache:
                    self.price_cache[symbol] = self._empty_bars(symbol)
            return

        if "SYMBOL" not in bars.columns:
            for symbol in symbols:
                if symbol not in self.price_cache:
                    self.price_cache[symbol] = self._empty_bars(symbol)
            return

        grouped = bars.groupby("SYMBOL", sort=False)
        for symbol, frame in grouped:
            frame = frame[["SYMBOL", "CLOCK", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]].copy()
            frame = frame.sort_values("CLOCK").drop_duplicates(subset=["CLOCK"], keep="last")
            frame = frame.set_index("CLOCK", drop=False)
            self.price_cache[str(symbol)] = frame

        for symbol in symbols:
            if symbol not in self.price_cache:
                self.price_cache[symbol] = self._empty_bars(symbol)

    def _fetch_metadata(self, symbols: Iterable[str]) -> pd.DataFrame:
        symbols = [str(s) for s in symbols if s]
        if not symbols:
            return self._empty_metadata()

        self._check_api()
        frames = []

        bulk = self._retry_on_timeout(instruments, symbols)
        normalized_bulk = self._normalize_metadata_frame(bulk)
        if not normalized_bulk.empty:
            frames.append(normalized_bulk)

        fetched = set(normalized_bulk["Symbol"].tolist()) if not normalized_bulk.empty else set()
        for symbol in symbols:
            if symbol in fetched:
                continue
            one = self._retry_on_timeout(instruments, symbol)
            normalized_one = self._normalize_metadata_frame(one)
            if not normalized_one.empty:
                frames.append(normalized_one)

        if not frames:
            return self._empty_metadata()

        metadata = pd.concat(frames, ignore_index=True)
        metadata = metadata.drop_duplicates(subset=["Symbol"], keep="last")
        metadata["Strike"] = pd.to_numeric(metadata["Strike"], errors="coerce")
        metadata["MinTick"] = pd.to_numeric(metadata["MinTick"], errors="coerce")
        metadata["Multiplier"] = pd.to_numeric(metadata["Multiplier"], errors="coerce")
        metadata = metadata[["Symbol", "OptType", "Strike", "MinTick", "Multiplier"]]
        return metadata

    def _retry_on_timeout(self, func, *args, max_retries: int = 3, **kwargs):
        last_exception = None

        for attempt in range(1, int(max_retries) + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                is_timeout = self._is_timeout_error(exc)
                if not is_timeout:
                    raise

                last_exception = exc
                if attempt < int(max_retries):
                    wait_seconds = attempt
                    logger.warning(
                        "Causis API请求超时（第%s/%s次尝试），%s秒后重试... (异常: %s)",
                        attempt,
                        max_retries,
                        wait_seconds,
                        exc,
                    )
                    time.sleep(wait_seconds)
                else:
                    logger.error(
                        "Causis API请求超时，已重试%s次均失败，中止操作 (异常: %s)",
                        max_retries,
                        exc,
                    )
                    raise

        if last_exception is not None:
            raise last_exception

        return None

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        text = str(exc).lower()
        name = type(exc).__name__.lower()
        return (
            isinstance(exc, TimeoutError)
            or "timeout" in text
            or "timed out" in text
            or "connectiontimeout" in name
            or "readtimeout" in name
        )

    @staticmethod
    def _normalize_metadata_frame(raw: Any) -> pd.DataFrame:
        if raw is None:
            return CausisDataLoader._empty_metadata()

        if isinstance(raw, pd.Series):
            frame = raw.to_frame().T
        elif isinstance(raw, dict):
            frame = pd.DataFrame([raw])
        else:
            frame = pd.DataFrame(raw)

        if frame.empty:
            return CausisDataLoader._empty_metadata()

        if "SYMBOL" in frame.columns and "Symbol" not in frame.columns:
            frame = frame.rename(columns={"SYMBOL": "Symbol"})

        if "Symbol" not in frame.columns:
            if frame.index.name == "Symbol":
                frame = frame.reset_index()
            elif frame.index.dtype == object:
                frame = frame.reset_index().rename(columns={"index": "Symbol"})

        if "OptType" not in frame.columns and "OPTIONTYPE" in frame.columns:
            frame = frame.rename(columns={"OPTIONTYPE": "OptType"})

        if "Strike" not in frame.columns:
            if "StrikePrice" in frame.columns:
                frame = frame.rename(columns={"StrikePrice": "Strike"})
            elif "STRIKEPRICE" in frame.columns:
                frame = frame.rename(columns={"STRIKEPRICE": "Strike"})

        if "MinTick" not in frame.columns and "MINTICK" in frame.columns:
            frame = frame.rename(columns={"MINTICK": "MinTick"})

        if "Multiplier" not in frame.columns and "MULTIPLIER" in frame.columns:
            frame = frame.rename(columns={"MULTIPLIER": "Multiplier"})

        for col in ["Symbol", "OptType", "Strike", "MinTick", "Multiplier"]:
            if col not in frame.columns:
                frame[col] = pd.NA

        frame = frame[["Symbol", "OptType", "Strike", "MinTick", "Multiplier"]]
        frame["OptType"] = frame["OptType"].astype(str).str.title()
        frame = frame.dropna(subset=["Symbol"]).copy()
        return frame

    @staticmethod
    def _empty_bars(symbol: str) -> pd.DataFrame:
        cols = ["SYMBOL", "CLOCK", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
        frame = pd.DataFrame(columns=cols)
        frame["SYMBOL"] = symbol
        return frame.set_index("CLOCK", drop=False)

    @staticmethod
    def _empty_chain() -> pd.DataFrame:
        return pd.DataFrame(columns=["Symbol", "OptType", "Strike", "EndDate", "MinTick", "Multiplier"])

    @staticmethod
    def _empty_metadata() -> pd.DataFrame:
        return pd.DataFrame(columns=["Symbol", "OptType", "Strike", "MinTick", "Multiplier"])

    @staticmethod
    def _check_api() -> None:
        if all_instruments is None or get_price is None or instruments is None:
            raise ImportError(
                "causis_api is not available in current environment."
            ) from _IMPORT_ERROR
