from __future__ import annotations

import importlib
from types import ModuleType
from typing import Tuple


def _reload_runtime_modules() -> Tuple[ModuleType, ModuleType]:
    """Load latest module code during notebook iteration without kernel restart."""
    data_loader_mod = importlib.import_module('.data_loader', __name__)
    importlib.reload(data_loader_mod)

    backtest_mod = importlib.import_module('.backtest_engine', __name__)
    importlib.reload(backtest_mod)

    analyze_mod = importlib.import_module('.analyze', __name__)
    importlib.reload(analyze_mod)
    return backtest_mod, analyze_mod


def run_backtest(config_path: str):
    backtest_mod, _ = _reload_runtime_modules()
    return backtest_mod.run_backtest(config_path)


def analyze(result_dir: str):
    _, analyze_mod = _reload_runtime_modules()
    return analyze_mod.analyze(result_dir)


__all__ = ['run_backtest', 'analyze']
