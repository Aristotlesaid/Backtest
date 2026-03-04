from __future__ import annotations

import sys
from pathlib import Path

# 统一走共享数据加载器，便于多个策略项目共用同一套缓存与接口。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from SharedData.causis_data_loader import CausisDataLoader

__all__ = ["CausisDataLoader"]
