from __future__ import annotations

from pathlib import Path
from typing import Dict

import pandas as pd

BASE_FRAME_FILE_MAP = {
    "orders": "orders.parquet",
    "positions": "positions.parquet",
    "daily_pnl": "daily_pnl.parquet",
}


def save_results(result_dir: str | Path, frames: Dict[str, pd.DataFrame]) -> Path:
    out_dir = Path(result_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for key, filename in BASE_FRAME_FILE_MAP.items():
        frame = frames.get(key, pd.DataFrame())
        frame.to_parquet(out_dir / filename, index=False)

    return out_dir


def load_results(result_dir: str | Path) -> Dict[str, pd.DataFrame]:
    out_dir = Path(result_dir)
    result: Dict[str, pd.DataFrame] = {}

    for key, filename in BASE_FRAME_FILE_MAP.items():
        path = out_dir / filename
        if path.exists():
            result[key] = pd.read_parquet(path)
        else:
            result[key] = pd.DataFrame()

    metrics_path = out_dir / "metrics.parquet"
    if metrics_path.exists():
        result["metrics"] = pd.read_parquet(metrics_path)

    return result
