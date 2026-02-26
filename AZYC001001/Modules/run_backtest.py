from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .backtest_engine import run_backtest
except ImportError:  # pragma: no cover
    from backtest_engine import run_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AZYC001001 wheel backtest")
    parser.add_argument(
        "--config",
        type=str,
        default=str(Path(__file__).parent / "config.yaml"),
        help="Path to YAML config file",
    )
    args = parser.parse_args()

    result = run_backtest(args.config)
    metrics = result["metrics"]

    print(f"Artifacts written to: {result['artifacts_path']}")
    if metrics is not None and not metrics.empty:
        print(metrics.to_string(index=False))


if __name__ == "__main__":
    main()
