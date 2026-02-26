from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import pandas as pd
from causis_api.const import login

login.username = "jinqiao.xue"
login.password = "1101BX@causis"

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
    def __init__(
        self,
        start_date: str,
        end_date: str,
        frequency: str,
        option_code: str,
        use_disk_cache: bool = True,
        cache_dir: Optional[str] = None,
    ):
        self.start_date = str(start_date)
        self.end_date = str(end_date)
        self.frequency = str(frequency)
        self.option_code = str(option_code)

        self.use_disk_cache = bool(use_disk_cache)
        self.cache_dir = Path(cache_dir).expanduser().resolve() if cache_dir else (Path.cwd() / "cache").resolve()
        self.price_cache_dir = self.cache_dir / "price"
        self.chain_cache_dir = self.cache_dir / "option_chain"
        if self.use_disk_cache:
            self.price_cache_dir.mkdir(parents=True, exist_ok=True)
            self.chain_cache_dir.mkdir(parents=True, exist_ok=True)

        self.option_chain_cache: Dict[str, pd.DataFrame] = {}
        self.price_cache: Dict[str, pd.DataFrame] = {}

    def load_etf_bars(self, symbol: str) -> pd.DataFrame:
        return self._load_price_bars(symbol)

    def load_option_bars(self, symbol: str) -> pd.DataFrame:
        return self._load_price_bars(symbol)

    def load_option_chain(self, trade_date: date) -> pd.DataFrame:
        key = trade_date.isoformat()
        if key in self.option_chain_cache:
            return self.option_chain_cache[key].copy()

        cache_path = self._chain_cache_path(key)
        if self.use_disk_cache and cache_path.exists():
            chain = pd.read_parquet(cache_path)
            if not chain.empty and {"Symbol", "EndDate", "OptType", "Strike", "Multiplier"}.issubset(set(chain.columns)):
                chain["EndDate"] = pd.to_datetime(chain["EndDate"], errors="coerce").dt.date
                self.option_chain_cache[key] = chain
                return chain.copy()

        self._check_api()
        # all_instruments only accepts a single date.
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

        if self.use_disk_cache:
            chain.to_parquet(cache_path, index=False)

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

        cache_path = self._price_cache_path(symbol)
        if self.use_disk_cache and cache_path.exists():
            bars = pd.read_parquet(cache_path)
            bars = self._postprocess_bars_frame(bars, symbol)
            self.price_cache[symbol] = bars
            return bars.copy()

        self._check_api()
        bars = self._fetch_price_bars_daily(symbol)

        if self.use_disk_cache:
            bars.reset_index(drop=True).to_parquet(cache_path, index=False)

        self.price_cache[symbol] = bars
        return bars.copy()

    def _fetch_price_bars_daily(self, symbol: str) -> pd.DataFrame:
        start = pd.Timestamp(self.start_date)
        end = pd.Timestamp(self.end_date)

        frames = []
        cursor = start
        while cursor <= end:
            date_str = cursor.strftime("%Y-%m-%d")
            try:
                raw = self._retry_on_timeout(
                    get_price,
                    symbol,
                    start_date=date_str,
                    end_date=date_str,
                    frequency=self.frequency,
                )
            except Exception as exc:
                logger.warning("get_price(%s, %s) failed, skip day: %s", symbol, date_str, exc)
                cursor += pd.Timedelta(days=1)
                continue

            frame = self._normalize_price_bars(raw, default_symbol=symbol)
            if not frame.empty:
                frames.append(frame)
            cursor += pd.Timedelta(days=1)

        if not frames:
            return self._empty_bars(symbol)

        bars = pd.concat(frames, ignore_index=False)
        bars = bars.reset_index(drop=True)
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

    def _postprocess_bars_frame(self, bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if bars.empty:
            return self._empty_bars(symbol)

        frame = bars.copy()
        frame.columns = [str(c).upper() for c in frame.columns]
        if "CLOCK" not in frame.columns:
            return self._empty_bars(symbol)

        frame["CLOCK"] = pd.to_datetime(frame["CLOCK"], errors="coerce")
        frame = frame.dropna(subset=["CLOCK"]).copy().reset_index(drop=True)
        for col in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]:
            if col in frame.columns:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            else:
                frame[col] = pd.NA

        if "SYMBOL" not in frame.columns:
            frame["SYMBOL"] = symbol
        frame["SYMBOL"] = frame["SYMBOL"].fillna(symbol)

        frame = frame[["SYMBOL", "CLOCK", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]]
        frame = frame.sort_values("CLOCK").drop_duplicates(subset=["CLOCK"], keep="last")
        return frame.set_index("CLOCK", drop=False)

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
                        "Causis API timeout (%s/%s), retry in %ss... (%s)",
                        attempt,
                        max_retries,
                        wait_seconds,
                        exc,
                    )
                    time.sleep(wait_seconds)
                else:
                    logger.error("Causis API timeout after %s retries: %s", max_retries, exc)
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

    def _price_cache_path(self, symbol: str) -> Path:
        safe = self._safe_name(symbol)
        return self.price_cache_dir / f"{safe}__{self.start_date}_{self.end_date}_{self.frequency}.parquet"

    def _chain_cache_path(self, day_key: str) -> Path:
        safe_code = self._safe_name(self.option_code)
        safe_day = self._safe_name(day_key)
        return self.chain_cache_dir / f"{safe_code}__{safe_day}.parquet"

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))

    @staticmethod
    def _check_api() -> None:
        if all_instruments is None or get_price is None or instruments is None:
            raise ImportError("causis_api is not available in current environment.") from _IMPORT_ERROR
