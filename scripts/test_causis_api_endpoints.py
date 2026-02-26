"""独立测试 causis_api 各接口调用方式与批量上限探测。

用法：
python scripts/test_causis_api_endpoints.py \
  --date 2025-11-10 --option-code 159915 --start 2025-01-01 --end 2025-01-10
"""

from __future__ import annotations

import argparse
import sys
from typing import List

import pandas as pd

from AZYC001001.Modules.data_loader import CausisDataLoader


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="2025-11-10")
    p.add_argument("--option-code", default="159915")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default="2025-01-10")
    p.add_argument("--etf-symbol", default="S.CN.SZSE.159915")
    p.add_argument("--probe-max", type=int, default=32)
    return p.parse_args()


def probe_batch_limit(loader: CausisDataLoader, symbols: List[str], start: str, end: str, probe_max: int) -> int:
    if not symbols:
        return 0
    cap = min(len(symbols), max(1, probe_max))
    lo, hi = 1, cap
    ok = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            loader._retry_on_timeout(
                __import__("causis_api.data", fromlist=["get_price"]).get_price,
                symbols[:mid],
                start_date=start,
                end_date=end,
                frequency="minute1",
            )
            ok = mid
            lo = mid + 1
        except Exception:
            hi = mid - 1
    return ok


def main() -> int:
    args = parse_args()
    loader = CausisDataLoader(args.start, args.end, "minute1", args.option_code)

    # 1) all_instruments + instruments
    chain = loader.load_option_chain(pd.to_datetime(args.date).date())
    print(f"[OK] option chain rows: {len(chain)}")
    if chain.empty:
        print("[WARN] option chain empty, skip following tests")
        return 0

    symbols = chain["Symbol"].dropna().astype(str).head(20).tolist()
    print(f"[OK] sample symbols: {len(symbols)}")

    # 2) get_price 单symbol
    etf = loader.load_etf_bars(args.etf_symbol)
    print(f"[OK] etf bars rows: {len(etf)}")

    # 3) get_price 多symbol（参考 data_example）
    ok_batch = probe_batch_limit(loader, symbols, args.start, args.end, args.probe_max)
    print(f"[OK] probed max stable batch size: {ok_batch}")

    # 4) 逐日逐symbol方式（当前主程序路径）
    for s in symbols[:3]:
        bars = loader.load_option_bars(s)
        print(f"[OK] option bars {s}: {len(bars)}")

    print("[DONE] causis api call format tests finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
